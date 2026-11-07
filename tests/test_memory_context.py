from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from errand_agent.clock import FakeClock, IST
from errand_agent.config import AppConfig
from errand_agent.intent import IntentState
from errand_agent.memory import ContextBuilder, MemoryStore
from errand_agent.responses import PROPOSAL_TOOLS
from errand_agent.redact import Redactor
from errand_agent.storage import AgentStore, json_dumps


class MemoryContextTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.config = AppConfig.from_env(self.root, {"LLM_MODE": "scripted", "CONTEXT_BUDGET_TOKENS": "3000"})
        self.clock = FakeClock(datetime(2026, 11, 7, 10, 0, tzinfo=IST))
        self.store = AgentStore(self.root / "agent.sqlite3")
        self.memories = MemoryStore(self.store, self.clock)
        self.memories.seed_demo()
        with self.store.transaction() as db:
            db.execute("INSERT INTO sessions(id,user_id,created_at,last_seen) VALUES('current','u_demo',1,1)")

    def tearDown(self) -> None:
        self.store.close()
        self.temp.cleanup()

    def test_seed_contains_52_memories_and_240_prior_messages(self) -> None:
        self.assertEqual(len(self.memories.list("u_demo")), 52)
        count = self.store.connection.execute("SELECT count(*) FROM messages WHERE is_seed=1").fetchone()[0]
        self.assertEqual(count, 240)

    def test_edited_memory_rejects_out_of_catalog_values_and_configured_secrets(self) -> None:
        guarded = MemoryStore(self.store, self.clock, Redactor("assignment-test-secret"))
        with self.assertRaisesRegex(ValueError, "supported service area"):
            guarded.create("u_demo", "pref.area", "Hidden Pending Area")
        with self.assertRaisesRegex(ValueError, "secrets"):
            guarded.create("u_demo", "pref.language", "assignment-test-secret")
        with self.assertRaisesRegex(ValueError, "HH:MM"):
            guarded.create("u_demo", "pref.earliest_time", "tomorrow morning")

    def test_pending_and_forgotten_memory_are_never_selected_or_applied(self) -> None:
        state, used = self.memories.apply_to_intent("u_demo", IntentState(action="book", service="plumber"))
        self.assertEqual(state.area, "Jaipur")
        self.assertEqual(state.earliest_time, "10:00")
        self.assertEqual(state.budget_max_inr, 1800)
        self.assertTrue(used)
        eligible, excluded = self.memories.relevant("u_demo", "plumber", "book")
        values = [json.dumps(item.value, ensure_ascii=False) for item in eligible]
        self.assertFalse(any("Zorvex Tamboli" in value or "Hidden Pending" in value or "Forgotten" in value for value in values))
        self.assertTrue(any(item["reason"] == "unconfirmed_inference" for item in excluded))

    def test_explicit_no_budget_is_not_overridden_by_saved_budget(self) -> None:
        intent = IntentState(action="book", service="plumber", no_budget=True)
        state, _ = self.memories.apply_to_intent("u_demo", intent)
        self.assertTrue(state.no_budget)
        self.assertIsNone(state.budget_max_inr)

    def test_forget_preserves_audit_history_but_excludes_referenced_content_and_blocks_inference(self) -> None:
        memory = next(item for item in self.memories.list("u_demo") if item.key == "pref.technician.plumber" and item.status == "pending")
        with self.store.transaction() as db:
            db.execute("INSERT INTO messages(id,session_id,role,content,created_at,token_est,memory_refs_json) VALUES('old','current','user','I might prefer Zorvex Tamboli, but that is unconfirmed.',2,20,?)", (json_dumps([memory.id]),))
        self.memories.forget("u_demo", memory.id)
        old = self.store.connection.execute("SELECT content FROM messages WHERE id='old'").fetchone()[0]
        self.assertIn("Zorvex Tamboli", old)
        context = ContextBuilder(self.store, self.memories, self.config).build("current", "u_demo", "Find a plumber", IntentState(action="book", service="plumber"))
        self.assertNotIn("Zorvex Tamboli", json.dumps(context.messages, ensure_ascii=False))
        self.assertTrue(any(item["id"] == "old" for item in context.manifest["excluded_history"]))
        with self.assertRaisesRegex(ValueError, "forgotten or rejected"):
            self.memories.create("u_demo", "pref.technician.plumber", "Zorvex Tamboli", "inferred", "service:plumber")
        deliberate = self.memories.create("u_demo", "pref.technician.plumber", "Zorvex Tamboli", "explicit", "service:plumber")
        self.assertEqual(next(item for item in self.memories.list("u_demo") if item.id == deliberate).status, "active")

    def test_correction_supersedes_old_value_without_mutating_conversation_or_context_audit(self) -> None:
        current = next(item for item in self.memories.list("u_demo") if item.key == "pref.area" and item.scope == "global" and item.status == "active")
        with self.store.transaction() as db:
            db.execute("INSERT INTO messages(id,session_id,role,content,created_at,token_est,memory_refs_json) VALUES('area-msg','current','user','I always prefer Jaipur for service.',3,12,?)", (json_dumps([current.id]),))
        new_id = self.memories.correct("u_demo", current.id, "Whitefield")
        self.assertEqual(self.store.connection.execute("SELECT status FROM memories WHERE id=?", (current.id,)).fetchone()[0], "superseded")
        self.assertEqual(self.store.connection.execute("SELECT content FROM messages WHERE id='area-msg'").fetchone()[0], "I always prefer Jaipur for service.")
        self.assertEqual(self.store.connection.execute("SELECT value_json FROM memories WHERE id=?", (new_id,)).fetchone()[0], '"Whitefield"')
        context = ContextBuilder(self.store, self.memories, self.config).build("current", "u_demo", "Find an electrician", IntentState(action="book", service="electrician"))
        self.assertNotIn("I always prefer Jaipur", json.dumps(context.messages, ensure_ascii=False))
        self.assertTrue(any(item["id"] == "area-msg" for item in context.manifest["excluded_history"]))

    def test_forgetting_or_correcting_memory_scrubs_live_task_and_invalidates_its_approval(self) -> None:
        old_memory = self.memories.create("u_demo", "pref.technician.plumber", "Fennel Technician", "explicit", "service:plumber")
        intent = IntentState(
            action="book", service="plumber", area="Jaipur",
            preferences={"technician": "Fennel Technician"},
            provenance={"preferences.technician": {"source": f"memory:{old_memory}", "kind": "memory_explicit"}},
            presented_options=[{"provider_id": "prov_001"}], provider_ids=["prov_001"],
        )
        with self.store.transaction() as db:
            db.execute("INSERT INTO tasks(id,session_id,user_id,state,intent_json,version,updated_at) VALUES('task-memory','current','u_demo','AWAITING_APPROVAL',?,0,?)", (intent.to_json(), 5))
            db.execute(
                "INSERT INTO pending_actions(id,task_id,session_id,user_id,kind,material_json,fingerprint,approval_code,status,idempotency_key,presented_at,expires_at,created_at) VALUES('action-memory','task-memory','current','u_demo','book','{}','fp','CODE','proposed','idem-memory',1,100,1)"
            )
        ContextBuilder(self.store, self.memories, self.config).build("current", "u_demo", "Find a plumber tomorrow", intent)
        new_memory = self.memories.correct("u_demo", old_memory, "Juniper Technician")
        task = self.store.connection.execute("SELECT state,intent_json FROM tasks WHERE id='task-memory'").fetchone()
        updated_intent = IntentState.from_json(task["intent_json"])
        self.assertEqual(task["state"], "GATHERING")
        self.assertNotIn("technician", updated_intent.preferences)
        self.assertEqual(updated_intent.presented_options, [])
        self.assertEqual(self.store.connection.execute("SELECT status FROM pending_actions WHERE id='action-memory'").fetchone()[0], "invalidated")
        logged = self.store.connection.execute("SELECT request_payload_json,payload_sha256 FROM context_logs ORDER BY id LIMIT 1").fetchone()
        self.assertIn("Fennel Technician", logged["request_payload_json"], "audit records preserve the exact context that was sent")
        self.assertEqual(hashlib.sha256(logged["request_payload_json"].encode()).hexdigest(), logged["payload_sha256"])
        updated_intent = IntentState.from_json(task["intent_json"])
        after_correction = ContextBuilder(self.store, self.memories, self.config).build("current", "u_demo", "Find a plumber tomorrow", updated_intent)
        self.assertNotIn("Fennel Technician", json.dumps(after_correction.messages, ensure_ascii=False))
        self.assertIsNotNone(new_memory)

        self.memories.forget("u_demo", new_memory)
        self.assertEqual(self.store.connection.execute("SELECT status FROM memories WHERE id=?", (new_memory,)).fetchone()[0], "forgotten")

    def test_context_budget_is_enforced_and_manifest_hash_matches_payload(self) -> None:
        builder = ContextBuilder(self.store, self.memories, self.config)
        result = builder.build("current", "u_demo", "Find a plumber this week, mornings only.", IntentState(action="book", service="plumber", area="Jaipur"))
        row = self.store.connection.execute("SELECT * FROM context_logs").fetchone()
        payload = row["request_payload_json"]
        sent = json.loads(payload)
        self.assertLessEqual(result.estimated_tokens, 3000)
        self.assertEqual(hashlib.sha256(payload.encode()).hexdigest(), result.payload_sha256)
        self.assertEqual(sent["input"], result.messages)
        self.assertEqual(sent["tools"], PROPOSAL_TOOLS)
        self.assertEqual(sent["tool_choice"], {"type": "function", "name": "propose_next_step"})
        self.assertNotIn("Zorvex Tamboli", payload)
        self.assertNotIn("Hidden Pending Area", payload)
        self.assertNotIn("Forgotten District", payload)
        self.assertTrue(result.manifest["included_memory_ids"])
        self.assertEqual(row["payload_sha256"], result.payload_sha256)
        self.assertEqual(row["rendered_dynamic_text"], result.rendered_dynamic_text)

    def test_long_seed_history_is_not_sent_to_current_session(self) -> None:
        builder = ContextBuilder(self.store, self.memories, self.config)
        result = builder.build("current", "u_demo", "Find a plumber tomorrow", IntentState(action="book", service="plumber"))
        payload = json.dumps(result.messages, ensure_ascii=False)
        self.assertNotIn("Prior conversation 1-1", payload)
        self.assertNotIn("Zorvex Tamboli", payload)

    def test_task_json_stays_valid_and_core_facts_survive_preference_truncation(self) -> None:
        builder = ContextBuilder(self.store, self.memories, self.config)
        intent = IntentState(
            action="book", service="plumber", area="Jaipur", date_start="2026-11-08",
            date_end="2026-11-09", preferences={f"preference_{index}": "x" * 100 for index in range(50)},
        )
        result = builder.build("current", "u_demo", "Find a plumber tomorrow", intent)
        dynamic = json.loads(result.messages[1]["content"])
        task = json.loads(dynamic["task"])
        self.assertEqual(task["service"], "plumber")
        self.assertEqual(task["area"], "Jaipur")
        self.assertEqual(task["date_start"], "2026-11-08")
        self.assertLessEqual(result.manifest["section_tokens"]["memories"], ContextBuilder.SECTION_CAPS["memories"])


if __name__ == "__main__":
    unittest.main()
