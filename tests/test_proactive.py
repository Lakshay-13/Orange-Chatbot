from __future__ import annotations

import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from errand_agent.agent import ErrandAgent
from errand_agent.clock import FakeClock, IST
from errand_agent.config import AppConfig
from errand_agent.mock_provider import MockProvider
from errand_agent.proactive import ProactiveMonitor
from errand_agent.responses import ScriptedModel
from errand_agent.storage import AgentStore


class ProactiveTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.config = AppConfig.from_env(self.root, {
            "LLM_MODE": "scripted", "DATABASE_PATH": "agent.sqlite3", "MOCK_DATABASE_PATH": "mock.sqlite3", "TRACE_DIR": "traces",
        })
        self.clock = FakeClock(datetime(2026, 11, 7, 10, 0, tzinfo=IST))
        self.store = AgentStore(self.root / "agent.sqlite3")
        self.provider = MockProvider(self.root / "mock.sqlite3", self.clock, seed=51)
        self.agent = ErrandAgent(self.config, self.store, self.provider, self.clock, ScriptedModel())
        self.agent.memories.seed_demo()
        self.provider.seed_demo_bookings()
        self.monitor = ProactiveMonitor(self.agent)

    def tearDown(self) -> None:
        self.agent.close()
        self.temp.cleanup()

    def test_cancellation_is_detected_without_user_message_and_requires_fresh_approval(self) -> None:
        session = self.agent.create_session()
        old = next(item for item in self.provider.list_bookings("u_demo") if item["booking_id"] == "book_000002")
        self.provider.simulate_provider_cancellation(old["booking_id"])
        result = self.monitor.tick()
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["status"], "approval_presented")
        self.assertTrue(result[0]["replacement_options"])
        self.assertTrue(result[0]["approval"])
        action = result[0]["approval"]
        self.assertNotEqual(action["material"]["provider_id"], old["provider_id"])
        self.assertEqual(self.provider.list_bookings("u_demo")[0]["status"], "completed")
        active = self.store.connection.execute("SELECT status FROM pending_actions WHERE id=?", (action["action_id"],)).fetchone()[0]
        self.assertEqual(active, "proposed")
        self.assertEqual(self.store.connection.execute("SELECT status FROM processed_provider_events").fetchone()[0], "processed")
        self.assertEqual(self.store.connection.execute("SELECT status FROM outbox WHERE kind='provider_cancellation'").fetchone()[0], "delivered")
        message_count = self.store.connection.execute("SELECT count(*) FROM messages WHERE id LIKE 'msg_proactive_%'").fetchone()[0]
        trace_count = self.store.connection.execute("SELECT count(*) FROM trace_events").fetchone()[0]
        repeated = self.monitor.tick()
        self.assertEqual(repeated, [])
        self.assertEqual(self.store.connection.execute("SELECT count(*) FROM messages WHERE id LIKE 'msg_proactive_%'").fetchone()[0], message_count)
        self.assertEqual(self.store.connection.execute("SELECT count(*) FROM trace_events").fetchone()[0], trace_count)
        self.provider.force_next("book", "success")
        approved = self.agent.approve_action(session, action["action_id"], action["fingerprint"], "fresh-replacement-approval")
        self.assertEqual(approved["action_status"], "done")
        self.assertEqual(len(self.provider.list_bookings("u_demo")), 3)

    def test_inactive_user_gets_replacement_options_in_outbox_without_a_hold(self) -> None:
        session = self.agent.create_session()
        with self.store.transaction() as db:
            db.execute("UPDATE sessions SET last_seen=? WHERE id=?", (self.clock.epoch() - 1000, session))
        old = next(item for item in self.provider.list_bookings("u_demo") if item["booking_id"] == "book_000002")
        self.provider.simulate_provider_cancellation(old["booking_id"])
        result = self.monitor.tick()
        self.assertEqual(result[0]["status"], "options_ready")
        self.assertIsNone(result[0]["approval"])
        self.assertEqual(self.store.connection.execute("SELECT count(*) FROM pending_actions").fetchone()[0], 0)
        outbox = self.store.connection.execute("SELECT content,status FROM outbox WHERE kind='provider_cancellation'").fetchone()
        self.assertEqual(outbox["status"], "pending")
        self.assertIn("Replacement options", outbox["content"])

    def test_no_match_notifies_without_silently_relaxing_preferences(self) -> None:
        session = self.agent.create_session()
        with self.provider.db:
            self.provider.db.execute("UPDATE providers SET price_inr=99999 WHERE service='ac_service' AND provider_id!='prov_013'")
        old = next(item for item in self.provider.list_bookings("u_demo") if item["booking_id"] == "book_000002")
        self.provider.simulate_provider_cancellation(old["booking_id"])
        result = self.monitor.tick()
        self.assertEqual(result[0]["status"], "no_match")
        self.assertEqual(result[0]["replacement_options"], [])
        message = self.store.connection.execute("SELECT content FROM messages WHERE id LIKE 'msg_proactive_%'").fetchone()[0]
        self.assertIn("preference", message)
        self.assertEqual(self.store.connection.execute("SELECT count(*) FROM pending_actions").fetchone()[0], 0)
        self.assertEqual(self.store.connection.execute("SELECT status FROM outbox WHERE kind='provider_cancellation'").fetchone()[0], "delivered")
        self.assertEqual(self.provider.list_bookings("u_demo")[-1]["status"], "cancelled_by_provider")

    def test_replacement_uses_saved_area_when_cancelled_provider_served_nearby_area(self) -> None:
        self.agent.create_session()
        with self.provider.db:
            self.provider.db.execute("UPDATE providers SET area='Indiranagar' WHERE provider_id='prov_013'")
        self.provider.simulate_provider_cancellation("book_000002")
        result = self.monitor.tick()[0]
        self.assertEqual(result["status"], "approval_presented")
        self.assertTrue(result["replacement_options"])
        searches = self.agent.store.connection.execute(
            "SELECT data_json FROM trace_events WHERE event='tool_call' ORDER BY id DESC"
        ).fetchall()
        import json
        args = next(json.loads(row[0])["arguments"] for row in searches if json.loads(row[0]).get("tool") == "search_providers")
        self.assertEqual(args["area"], "Jaipur")
        self.assertNotEqual(result["replacement_options"][0]["provider_id"], "prov_013")

    def test_failed_proactive_replacement_uses_backoff_and_stops_after_three_attempts(self) -> None:
        session = self.agent.create_session()
        old = next(item for item in self.provider.list_bookings("u_demo") if item["booking_id"] == "book_000002")
        self.provider.simulate_provider_cancellation(old["booking_id"])
        for expected_attempt, delay in ((1, 5), (2, 10), (3, 0)):
            # The read gateway allows three bounded attempts per tick; force a
            # complete outage so this test exercises the durable monitor retry.
            for _ in range(self.agent.gateway.max_retries + 1):
                self.provider.force_next("get_provider", "raise")
            self.monitor.tick()
            row = self.store.connection.execute("SELECT status,attempts,next_attempt_at FROM processed_provider_events").fetchone()
            self.assertEqual(row["attempts"], expected_attempt)
            if expected_attempt < 3:
                self.assertEqual(row["status"], "pending")
                self.assertEqual(row["next_attempt_at"], self.clock.epoch() + delay)
                self.assertEqual(self.monitor.tick(), [])
                self.clock.advance(delay)
            else:
                self.assertEqual(row["status"], "failed")
        self.assertEqual(self.monitor.tick(), [])
        self.assertEqual(self.store.connection.execute("SELECT attempts FROM processed_provider_events").fetchone()[0], 3)
        failure = self.store.connection.execute("SELECT content FROM outbox WHERE kind='provider_cancellation_error'").fetchone()[0]
        self.assertIn("No replacement was booked", failure)


if __name__ == "__main__":
    unittest.main()
