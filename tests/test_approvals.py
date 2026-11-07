from __future__ import annotations

import tempfile
import unittest
from dataclasses import replace
from datetime import datetime
from pathlib import Path

from errand_agent.approvals import ActionApprovalService, ApprovalError
from errand_agent.clock import FakeClock, IST
from errand_agent.config import AppConfig
from errand_agent.intent import IntentState
from errand_agent.mock_provider import MockProvider
from errand_agent.storage import AgentStore, json_dumps
from errand_agent.trace import TraceRecorder


class ApprovalTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.config = AppConfig.from_env(self.root, {
            "LLM_MODE": "scripted", "DATABASE_PATH": "agent.sqlite3", "MOCK_DATABASE_PATH": "mock.sqlite3",
            "TRACE_DIR": "traces", "MAX_RETRIES": "2", "MAX_EXECUTION_TOOL_CALLS": "8",
        })
        self.clock = FakeClock(datetime(2026, 11, 7, 10, 0, tzinfo=IST))
        self.store = AgentStore(self.root / "agent.sqlite3")
        self.provider = MockProvider(self.root / "mock.sqlite3", self.clock, seed=31)
        self.trace = TraceRecorder(self.store, self.root / "traces", self.clock)
        self.approvals = ActionApprovalService(self.store, self.provider, self.trace, self.config, self.clock)
        self.run_id = self.trace.new_run_id()
        self.session_id = "session-test"
        self.user_id = "u_test"
        with self.store.transaction() as db:
            db.execute("INSERT INTO sessions(id,user_id,created_at,last_seen) VALUES(?,?,?,?)", (self.session_id, self.user_id, self.clock.epoch(), self.clock.epoch()))
        self.intent = IntentState(action="book", service="plumber", area="Jaipur", date_start="2026-11-08", date_end="2026-11-10")
        self.intent.presented_options = self.provider.search_providers("plumber", "Jaipur", {"start": "2026-11-08", "end": "2026-11-10"})
        self.intent.provider_ids = [item["provider_id"] for item in self.intent.presented_options]
        self.task_id = self._task("OPTIONS_PRESENTED", self.intent)

    def tearDown(self) -> None:
        self.provider.close()
        self.store.close()
        self.temp.cleanup()

    def _task(self, state: str, intent: IntentState) -> str:
        task_id = f"task-{len(self.store.connection.execute('SELECT id FROM tasks').fetchall()) + 1}"
        with self.store.transaction() as db:
            db.execute("INSERT INTO tasks(id,session_id,user_id,state,intent_json,version,updated_at) VALUES(?,?,?,?,?,0,?)", (task_id, self.session_id, self.user_id, state, intent.to_json(), self.clock.epoch()))
        return task_id

    def _approval(self, index: int = 0, task_id: str | None = None) -> dict:
        provider = self.intent.presented_options[index]
        return self.approvals.create_booking_approval(
            task_id or self.task_id, self.session_id, self.user_id, self.intent,
            provider["provider_id"], provider["available_slots"][0], self.run_id,
        )

    def _approve(self, action: dict, request: str = "confirm-1") -> dict:
        return self.approvals.approve(action["action_id"], self.user_id, self.session_id, action["fingerprint"], request, self.run_id)

    def test_approval_is_bound_to_provider_slot_and_quote_and_timeout_is_verified(self) -> None:
        action = self._approval()
        self.provider.force_next("book", "timeout_booked")
        outcome = self._approve(action)
        self.assertEqual(outcome["action_status"], "done")
        self.assertEqual(outcome["booking"]["status"], "booked")
        calls = [event for event in self.trace.events(self.run_id) if event["event"] == "execution_journal" and event["data"]["journal_event"] == "book_call"]
        verification = [event for event in self.trace.events(self.run_id) if event["event"] == "verification_result"]
        self.assertEqual(len(calls), 1)
        self.assertEqual(verification[-1]["data"]["result"]["status"], "booked")
        self.assertEqual(len(self.provider.list_bookings(self.user_id)), 1)

    def test_booking_response_with_material_status_mismatch_is_not_success(self) -> None:
        from unittest.mock import patch

        action = self._approval()
        self.provider.force_next("book", "success")
        original_status = self.provider.get_booking_status

        def mismatched_status(*args, **kwargs):
            result = original_status(*args, **kwargs)
            if result.get("status") == "booked":
                result["slot"] = "2026-11-08T18:00+05:30"
            return result

        with patch.object(self.provider, "get_booking_status", side_effect=mismatched_status):
            outcome = self._approve(action, "mismatched-provider-status")
        self.assertEqual(outcome["status"], "verification_mismatch")
        self.assertEqual(outcome["action_status"], "failed")
        self.assertIn("have not reported this as a successful booking", outcome["message"].casefold())
        self.assertEqual(len(self.provider.list_bookings(self.user_id)), 1)

    def test_cancel_and_reschedule_fail_closed_when_booking_changes_after_card(self) -> None:
        first = self._approval(index=0)
        self.provider.force_next("book", "success")
        first_booking = self._approve(first, "seed-booking-1")["booking"]
        second_task = self._task("OPTIONS_PRESENTED", self.intent)
        second = self._approval(index=1, task_id=second_task)
        self.provider.force_next("book", "success")
        second_booking = self._approve(second, "seed-booking-2")["booking"]

        for index, (kind, booking) in enumerate((("cancel", first_booking), ("reschedule", second_booking))):
            with self.subTest(kind=kind):
                task_id = self._task("GATHERING", IntentState(action=kind))
                new_slot = None
                if kind == "reschedule":
                    details = self.provider.get_provider(booking["provider_id"])
                    new_slot = next(slot for slot in details["available_slots"] if slot != booking["slot"])
                action = self.approvals.create_booking_management_approval(
                    task_id, self.session_id, self.user_id, kind, booking["booking_id"], new_slot, self.run_id,
                )
                changed_slot = "2026-11-10T18:00+05:30"
                with self.provider.db:
                    self.provider.db.execute("UPDATE bookings SET slot=? WHERE booking_id=?", (changed_slot, booking["booking_id"]))
                outcome = self._approve(action, f"stale-{kind}-{index}")
                self.assertEqual(outcome["status"], "booking_changed")
                self.assertEqual(outcome["action_status"], "failed")
        writes = [
            event for event in self.trace.events(self.run_id)
            if event["event"] == "tool_call" and event["data"].get("tool") in {"cancel", "reschedule"}
        ]
        self.assertEqual(writes, [])

    def test_timeout_not_booked_retries_same_key_after_authoritative_status(self) -> None:
        action = self._approval()
        self.provider.force_next("book", "timeout_not_booked")
        outcome = self._approve(action)
        self.assertEqual(outcome["action_status"], "done")
        calls = [event for event in self.trace.events(self.run_id) if event["event"] == "execution_journal" and event["data"]["journal_event"] == "book_call"]
        keys = [event["data"]["details"]["idempotency_key"] for event in calls]
        self.assertEqual(len(calls), 2)
        self.assertEqual(len(set(keys)), 1)
        self.assertEqual(len(self.provider.list_bookings(self.user_id)), 1)

    def test_repeated_confirmation_is_idempotent(self) -> None:
        action = self._approval()
        self.provider.force_next("book", "success")
        first = self._approve(action, "same-click")
        second = self._approve(action, "same-click")
        third = self._approve(action, "second-tab-click")
        self.assertEqual(first["action_status"], "done")
        self.assertTrue(second["replayed"])
        self.assertEqual(third["action_status"], "done")
        self.assertEqual(len(self.provider.list_bookings(self.user_id)), 1)
        calls = [event for event in self.trace.events(self.run_id) if event["event"] == "execution_journal" and event["data"]["journal_event"] == "book_call"]
        self.assertEqual(len(calls), 1)

    def test_no_booking_occurs_until_explicit_application_approval(self) -> None:
        self._approval()
        self.assertEqual(self.provider.list_bookings(self.user_id), [])
        self.assertEqual(self.store.connection.execute("SELECT count(*) FROM approval_events").fetchone()[0], 0)

    def test_current_quote_above_budget_never_creates_an_approval(self) -> None:
        self.intent.budget_max_inr = 1000
        self.provider.configure(price_drift="fixed", drift_value=500)
        provider = self.intent.presented_options[0]
        with self.assertRaises(ApprovalError) as raised:
            self.approvals.create_booking_approval(
                self.task_id, self.session_id, self.user_id, self.intent,
                provider["provider_id"], provider["available_slots"][0], self.run_id,
            )
        self.assertEqual(raised.exception.code, "quote_over_budget")
        self.assertEqual(self.store.connection.execute("SELECT count(*) FROM pending_actions").fetchone()[0], 0)
        self.assertEqual(self.store.connection.execute("SELECT state FROM tasks WHERE id=?", (self.task_id,)).fetchone()[0], "OPTIONS_PRESENTED")

    def test_approval_expiry_returns_booking_task_to_options_for_fresh_quote(self) -> None:
        action = self._approval()
        self.clock.advance(self.config.approval_ttl_seconds + 1)
        with self.assertRaises(ApprovalError) as raised:
            self._approve(action, "expired-approval")
        self.assertEqual(raised.exception.code, "approval_expired")
        self.assertEqual(self.store.connection.execute("SELECT state FROM tasks WHERE id=?", (self.task_id,)).fetchone()[0], "OPTIONS_PRESENTED")

    def test_expired_hold_requote_invalidates_old_approval_and_price_change_needs_new_approval(self) -> None:
        old = self._approval()
        self.clock.advance(600)
        self.approvals.transition(self.task_id, "OPTIONS_PRESENTED", "user_requested_fresh_quote", {}, self.run_id, self.session_id)
        self.provider.configure(price_drift="fixed", drift_value=350)
        refreshed = self._approval()
        self.assertGreater(refreshed["material"]["quoted_price_inr"], old["material"]["quoted_price_inr"])
        self.assertEqual(self.store.connection.execute("SELECT status FROM pending_actions WHERE id=?", (old["action_id"],)).fetchone()[0], "invalidated")
        stale = self._approve(old, "old-card-click")
        self.assertEqual(stale["status"], "invalidated")
        self.assertEqual(self.provider.list_bookings(self.user_id), [])
        self.provider.force_next("book", "success")
        result = self._approve(refreshed, "new-card-click")
        self.assertEqual(result["action_status"], "done")
        self.assertEqual(self.provider.list_bookings(self.user_id)[0]["status"], "confirmed")

    def test_wrong_user_cannot_approve_action(self) -> None:
        action = self._approval()
        with self.assertRaises(ApprovalError) as raised:
            self.approvals.approve(action["action_id"], "another-user", self.session_id, action["fingerprint"], "other", self.run_id)
        self.assertEqual(raised.exception.code, "approval_owner_mismatch")
        self.assertEqual(self.provider.list_bookings(self.user_id), [])

    def test_restart_after_provider_commit_verifies_without_duplicate(self) -> None:
        action = self._approval()
        material = action["material"]
        self.provider.force_next("book", "success")
        key = self.store.connection.execute("SELECT idempotency_key FROM pending_actions WHERE id=?", (action["action_id"],)).fetchone()[0]
        with self.store.transaction() as db:
            db.execute("UPDATE pending_actions SET status='approved',approved_at=? WHERE id=?", (self.clock.epoch(), action["action_id"]))
            db.execute("INSERT INTO approval_events(action_id,fingerprint,user_id,session_id,client_request_id,decision,created_at) VALUES(?,?,?,?,?,'approved',?)", (action["action_id"], action["fingerprint"], self.user_id, self.session_id, "restart-approved", self.clock.epoch()))
        direct = self.provider.book(material["hold_id"], key)
        self.assertEqual(direct["status"], "booked")
        with self.store.transaction() as db:
            db.execute("UPDATE pending_actions SET status='executing' WHERE id=?", (action["action_id"],))
            db.execute("UPDATE tasks SET state='EXECUTING' WHERE id=?", (self.task_id,))
        self.provider.close()
        self.store.close()
        self.store = AgentStore(self.root / "agent.sqlite3")
        self.provider = MockProvider(self.root / "mock.sqlite3", self.clock, seed=31)
        self.trace = TraceRecorder(self.store, self.root / "traces", self.clock)
        self.approvals = ActionApprovalService(self.store, self.provider, self.trace, self.config, self.clock)
        recovered = self.approvals.recover(self.run_id)
        self.assertEqual(recovered[0]["action_status"], "done")
        self.assertEqual(len(self.provider.list_bookings(self.user_id)), 1)
        self.assertEqual(self.store.connection.execute("SELECT state FROM tasks WHERE id=?", (self.task_id,)).fetchone()[0], "CONFIRMED")

    def test_recovery_of_pre_call_crash_after_expired_hold_reports_not_booked(self) -> None:
        action = self._approval()
        with self.store.transaction() as db:
            db.execute("UPDATE pending_actions SET status='executing' WHERE id=?", (action["action_id"],))
            db.execute("UPDATE tasks SET state='EXECUTING' WHERE id=?", (self.task_id,))
        self.clock.advance(601)
        result = self.approvals.recover(self.run_id)[0]
        self.assertEqual(result["status"], "hold_expired")
        self.assertIn("no booking was made", result["message"])
        self.assertEqual(self.provider.list_bookings(self.user_id), [])
        self.assertEqual(self.store.connection.execute("SELECT state FROM tasks WHERE id=?", (self.task_id,)).fetchone()[0], "OPTIONS_PRESENTED")

    def test_execution_call_limit_is_durable_and_never_claims_unverified_success(self) -> None:
        self.approvals.config = replace(self.config, max_execution_tool_calls=1)
        action = self._approval()
        self.provider.force_next("book", "success")
        result = self._approve(action, "limit-confirm")
        self.assertEqual(result["action_status"], "unverified")
        self.assertEqual(result["status"], "unverified")
        self.assertEqual(len(self.provider.list_bookings(self.user_id)), 1)
        self.assertEqual(self.store.connection.execute("SELECT execution_calls FROM pending_actions WHERE id=?", (action["action_id"],)).fetchone()[0], 1)
        recovered = self.approvals.recover(self.run_id)
        self.assertEqual(recovered[0]["status"], "unverified")
        self.assertEqual(len(self.provider.list_bookings(self.user_id)), 1)
        self.assertEqual(self.store.connection.execute("SELECT execution_calls FROM pending_actions WHERE id=?", (action["action_id"],)).fetchone()[0], 1)

    def test_cancel_uses_same_key_after_503_and_verifies(self) -> None:
        create = self._approval()
        self.provider.force_next("book", "success")
        self._approve(create)
        booking = self.provider.list_bookings(self.user_id)[0]
        task_id = self._task("GATHERING", IntentState(action="cancel"))
        action = self.approvals.create_booking_management_approval(task_id, self.session_id, self.user_id, "cancel", booking["booking_id"], None, self.run_id)
        outcome = self._approve(action, "cancel-confirm")
        self.assertEqual(outcome["action_status"], "done")
        self.assertEqual(self.provider.list_bookings(self.user_id)[0]["status"], "cancelled_by_user")
        retries = [event for event in self.trace.events(self.run_id) if event["event"] == "retry" and event["data"].get("operation") == "cancel"]
        self.assertEqual(len(retries), 1)

    def test_cancel_recovery_after_first_503_retries_idempotently(self) -> None:
        create = self._approval()
        self.provider.force_next("book", "success")
        self._approve(create)
        booking = self.provider.list_bookings(self.user_id)[0]
        task_id = self._task("GATHERING", IntentState(action="cancel"))
        action = self.approvals.create_booking_management_approval(task_id, self.session_id, self.user_id, "cancel", booking["booking_id"], None, self.run_id)
        with self.store.transaction() as db:
            db.execute("UPDATE pending_actions SET status='approved',approved_at=? WHERE id=?", (self.clock.epoch(), action["action_id"]))
            db.execute("UPDATE tasks SET state='AWAITING_APPROVAL' WHERE id=?", (task_id,))
        row = self.store.connection.execute("SELECT idempotency_key FROM pending_actions WHERE id=?", (action["action_id"],)).fetchone()
        with self.assertRaises(Exception):
            self.provider.cancel(booking["booking_id"], row["idempotency_key"])
        with self.store.transaction() as db:
            db.execute("UPDATE pending_actions SET status='executing' WHERE id=?", (action["action_id"],))
            db.execute("UPDATE tasks SET state='EXECUTING' WHERE id=?", (task_id,))
        recovered = self.approvals.recover(self.run_id)
        self.assertEqual(recovered[0]["status"], "cancelled")
        self.assertEqual(self.provider.list_bookings(self.user_id)[0]["status"], "cancelled_by_user")

    def test_reschedule_conflict_is_not_reported_as_success(self) -> None:
        create = self._approval()
        self.provider.force_next("book", "success")
        self._approve(create)
        booking = self.provider.list_bookings(self.user_id)[0]
        task_id = self._task("GATHERING", IntentState(action="reschedule"))
        details = self.provider.get_provider(booking["provider_id"])
        new_slot = next(slot for slot in details["available_slots"] if slot != booking["slot"])
        self.provider.configure(conflict_slots=[new_slot])
        action = self.approvals.create_booking_management_approval(task_id, self.session_id, self.user_id, "reschedule", booking["booking_id"], new_slot, self.run_id)
        result = self._approve(action, "reschedule-confirm")
        self.assertEqual(result["action_status"], "failed")
        self.assertEqual(result["status"], "slot_conflict")
        self.assertNotEqual(self.provider.list_bookings(self.user_id)[0]["slot"], new_slot)

    def test_reschedule_restart_uses_same_key_and_verifies_state(self) -> None:
        create = self._approval()
        self.provider.force_next("book", "success")
        self._approve(create)
        booking = self.provider.list_bookings(self.user_id)[0]
        task_id = self._task("GATHERING", IntentState(action="reschedule"))
        details = self.provider.get_provider(booking["provider_id"])
        new_slot = next(slot for slot in details["available_slots"] if slot != booking["slot"])
        action = self.approvals.create_booking_management_approval(task_id, self.session_id, self.user_id, "reschedule", booking["booking_id"], new_slot, self.run_id)
        row = self.store.connection.execute("SELECT idempotency_key FROM pending_actions WHERE id=?", (action["action_id"],)).fetchone()
        with self.store.transaction() as db:
            db.execute("UPDATE pending_actions SET status='executing' WHERE id=?", (action["action_id"],))
            db.execute("UPDATE tasks SET state='EXECUTING' WHERE id=?", (task_id,))
        self.provider.reschedule(booking["booking_id"], new_slot, row["idempotency_key"])
        recovered = self.approvals.recover(self.run_id)
        self.assertEqual(recovered[0]["status"], "rescheduled")
        self.assertEqual(self.provider.list_bookings(self.user_id)[0]["slot"], new_slot)


if __name__ == "__main__":
    unittest.main()
