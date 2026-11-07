"""Persistent preference memory, user controls, and fixed-budget context assembly."""

from __future__ import annotations

import hashlib
import json
import re
import time
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

from .budget import estimate_tokens
from .clock import Clock
from .config import AppConfig
from .ids import new_id
from .intent import IntentState
from .mock_provider import AREAS, SERVICES
from .redact import Redactor, contains_secret_pattern
from .responses import PROPOSAL_TOOLS, PROPOSAL_TOOL_CHOICE, SYSTEM_INSTRUCTIONS, choose_effort
from .storage import AgentStore, json_dumps, json_loads


ALLOWED_MEMORY_KEY = re.compile(
    r"^(pref\.(area|earliest_time|latest_time|days|min_rating|language|avoid_provider|technician\.(plumber|electrician|ac_service|cleaning|carpenter|pest_control|appliance_repair)|budget\.(plumber|electrician|ac_service|cleaning|carpenter|pest_control|appliance_repair))|household\.[a-z_]{1,32})$"
)


def _value_hash(value: Any) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True)
class MemoryRecord:
    id: str
    key: str
    scope: str
    value: Any
    kind: str
    status: str
    confidence: float | None
    created_at: float


class MemoryStore:
    def __init__(self, store: AgentStore, clock: Clock | None = None, redactor: Redactor | None = None) -> None:
        self.store = store
        self.clock = clock or Clock()
        self.redactor = redactor or Redactor()

    def seed_demo(self, user_id: str = "u_demo") -> None:
        existing = self.store.connection.execute("SELECT count(*) FROM memories WHERE user_id=?", (user_id,)).fetchone()[0]
        if existing:
            return
        now = self.clock.epoch()
        rows: list[tuple[str, str, Any, str, str, float | None]] = []
        explicit: list[tuple[str, str, Any]] = [
            ("pref.area", "global", "Jaipur"), ("pref.earliest_time", "global", "10:00"),
            ("pref.latest_time", "global", "19:00"), ("pref.language", "global", "Hindi"),
            ("pref.days", "global", ["Saturday", "Sunday"]), ("pref.min_rating", "global", 4.2),
        ]
        for service, value in zip(
            ("plumber", "electrician", "ac_service", "cleaning", "carpenter", "pest_control", "appliance_repair"),
            (1800, 1600, 2200, 2000, 1900, 2400, 2100),
        ):
            explicit.append((f"pref.budget.{service}", f"service:{service}", value))
        for service, technician in zip(
            ("electrician", "ac_service", "cleaning", "carpenter", "pest_control", "appliance_repair"),
            ("Mira Sen", "Anaya Iyer", "Rhea Nair", "Kabir Das", "Ishaan Mehta", "Mira Sen"),
        ):
            explicit.append((f"pref.technician.{service}", f"service:{service}", technician))
        explicit.append(("pref.days", "service:appliance_repair", ["Saturday"]))
        explicit.extend([
            ("pref.language", "service:plumber", "Hindi"), ("pref.language", "service:ac_service", "English"),
            ("pref.days", "service:cleaning", ["Friday"]), ("pref.min_rating", "service:plumber", 4.5),
            ("pref.area", "service:plumber", "Jaipur"), ("pref.area", "service:ac_service", "Jaipur"),
            ("pref.avoid_provider", "global", "prov_006"), ("pref.avoid_provider", "service:plumber", "prov_018"),
            ("household.home_type", "global", "apartment"), ("household.access_notes", "global", "call on arrival"),
            ("household.floor", "global", "second floor"), ("household.entry", "global", "front desk"),
        ])
        # Add the remaining explicit records as distinct service-scoped preferences.
        for index, service in enumerate(("plumber", "electrician", "ac_service", "cleaning", "carpenter", "pest_control", "appliance_repair")):
            if len(explicit) >= 30:
                break
            explicit.append(("pref.min_rating", f"service:{service}", round(4.0 + (index % 5) / 10, 1)))
        explicit = explicit[:30]
        for key, scope, value in explicit:
            rows.append((key, scope, value, "explicit", "active", None))
        inferred_confirmed = [
            ("pref.days", "service:plumber", ["Monday"]), ("pref.language", "service:electrician", "English"),
            ("pref.min_rating", "service:ac_service", 4.4), ("pref.budget.cleaning", "service:cleaning", 2300),
            ("pref.technician.carpenter", "service:carpenter", "Mira Sen"), ("household.home_type", "service:pest_control", "row house"),
        ]
        for key, scope, value in inferred_confirmed:
            rows.append((key, scope, value, "inferred", "active", 0.91))
        pending = [
            ("pref.technician.plumber", "service:plumber", "Zorvex Tamboli"),
            ("pref.area", "global", "Hidden Pending Area"),
            ("pref.language", "global", "Hidden Pending Language"),
            ("pref.days", "service:electrician", ["Tuesday"]),
            ("pref.min_rating", "service:cleaning", 4.9),
            ("pref.budget.plumber", "service:plumber", 7777),
        ]
        for key, scope, value in pending:
            rows.append((key, scope, value, "inferred", "pending", 0.62))
        forgotten = [
            ("pref.technician.plumber", "service:plumber", "Forgotten Technician"),
            ("pref.area", "service:cleaning", "Forgotten District"),
            ("pref.language", "service:cleaning", "Forgotten Language"),
            ("pref.days", "service:ac_service", ["Wednesday"]),
            ("pref.budget.electrician", "service:electrician", 9999),
        ]
        for key, scope, value in forgotten:
            rows.append((key, scope, value, "explicit", "forgotten", None))
        superseded = [
            ("pref.area", "global", "Superseded District"),
            ("pref.earliest_time", "global", "07:00"),
            ("pref.language", "global", "Superseded Language"),
        ]
        for key, scope, value in superseded:
            rows.append((key, scope, value, "explicit", "superseded", None))
        rejected = [
            ("pref.technician.electrician", "service:electrician", "Rejected Technician"),
            ("pref.days", "service:carpenter", ["Thursday"]),
        ]
        for key, scope, value in rejected:
            rows.append((key, scope, value, "inferred", "rejected", 0.38))
        if len(rows) != 52:
            raise AssertionError(f"Seeded memory count changed unexpectedly: {len(rows)}")
        with self.store.transaction() as db:
            for index, (key, scope, value, kind, status, confidence) in enumerate(rows):
                memory_id = f"mem_seed_{index + 1:03d}"
                db.execute(
                    "INSERT INTO memories(id,user_id,key,scope,value_json,kind,status,confidence,evidence_json,value_hash,created_at,updated_at,forgotten_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (memory_id, user_id, key, scope, json_dumps(value) if status != "forgotten" else None,
                     kind, status, confidence, "[]", _value_hash(value), now - (52 - index) * 60,
                     now - (52 - index) * 60, now if status == "forgotten" else None),
                )
        self._seed_long_history(user_id, now)

    def _seed_long_history(self, user_id: str, now: float) -> None:
        with self.store.transaction() as db:
            for session_index in range(3):
                session_id = f"seed_history_{session_index + 1}"
                db.execute("INSERT OR IGNORE INTO sessions(id,user_id,created_at,last_seen) VALUES(?,?,?,?)", (session_id, user_id, now - 100000 - session_index * 1000, now - 100000 - session_index * 1000))
                for message_index in range(80):
                    if message_index in {7, 19, 44}:
                        content = "We discussed Zorvex Tamboli, Hidden Pending Area, and Forgotten Technician as tentative or outdated details; do not treat these as current instructions."
                    else:
                        content = f"Prior conversation {session_index + 1}-{message_index + 1}: routine household scheduling, a recipe, a weather question, and general planning."
                    msg_id = f"seed_msg_{session_index + 1}_{message_index + 1:03d}"
                    role = "user" if message_index % 2 == 0 else "assistant"
                    db.execute(
                        "INSERT OR IGNORE INTO messages(id,session_id,role,content,created_at,token_est,is_seed,security_labels_json,memory_refs_json) VALUES(?,?,?,?,?,?,?,?,?)",
                        (msg_id, session_id, role, content, now - 100000 + message_index * 40, estimate_tokens(content), 1, "[]", "[]"),
                    )

    def list(self, user_id: str) -> list[MemoryRecord]:
        rows = self.store.connection.execute("SELECT * FROM memories WHERE user_id=? ORDER BY status,key,created_at DESC", (user_id,)).fetchall()
        return [
            MemoryRecord(row["id"], row["key"], row["scope"], json_loads(row["value_json"]), row["kind"], row["status"], row["confidence"], row["created_at"])
            for row in rows
        ]

    def relevant(self, user_id: str, service: str | None, action: str) -> tuple[list[MemoryRecord], list[dict[str, str]]]:
        rows = self.store.connection.execute("SELECT * FROM memories WHERE user_id=? ORDER BY updated_at DESC", (user_id,)).fetchall()
        eligible: list[tuple[int, MemoryRecord]] = []
        exclusions: list[dict[str, str]] = []
        for row in rows:
            mem = MemoryRecord(row["id"], row["key"], row["scope"], json_loads(row["value_json"]), row["kind"], row["status"], row["confidence"], row["created_at"])
            if mem.status in {"forgotten", "rejected", "superseded"}:
                exclusions.append({"id": mem.id, "reason": mem.status})
                continue
            if mem.status == "pending":
                exclusions.append({"id": mem.id, "reason": "unconfirmed_inference"})
                continue
            if mem.status != "active" or mem.value is None:
                exclusions.append({"id": mem.id, "reason": "not_current"})
                continue
            score = 10 if mem.kind == "explicit" else 5
            if mem.scope == "global":
                score += 3 if action in {"book", "cancel", "reschedule"} else 1
            elif service and mem.scope == f"service:{service}":
                score += 5
            elif mem.scope.startswith("provider:"):
                score += 2
            else:
                exclusions.append({"id": mem.id, "reason": "irrelevant_scope"})
                continue
            eligible.append((score, mem))
        eligible.sort(key=lambda pair: (pair[0], pair[1].created_at), reverse=True)
        return [mem for _, mem in eligible], exclusions

    def apply_to_intent(self, user_id: str, intent: IntentState) -> tuple[IntentState, list[str]]:
        memories, _ = self.relevant(user_id, intent.service, intent.action)
        result = IntentState.from_json(intent.to_json())
        refs: list[str] = []
        for mem in memories:
            value = mem.value
            if mem.key == "pref.area" and not result.area and isinstance(value, str) and value in {"Jaipur", "Indiranagar", "Koramangala", "Whitefield", "HSR Layout", "Jayanagar"}:
                result.area = value
                refs.append(mem.id)
                result.provenance["area"] = {"source": f"memory:{mem.id}", "kind": f"memory_{mem.kind}", "confirmed": True}
            elif mem.key == "pref.earliest_time" and not result.earliest_time and isinstance(value, str):
                result.earliest_time = value
                refs.append(mem.id)
                result.provenance["earliest_time"] = {"source": f"memory:{mem.id}", "kind": f"memory_{mem.kind}", "confirmed": True}
            elif mem.key == "pref.latest_time" and not result.latest_time and isinstance(value, str):
                result.latest_time = value
                refs.append(mem.id)
                result.provenance["latest_time"] = {"source": f"memory:{mem.id}", "kind": f"memory_{mem.kind}", "confirmed": True}
            elif intent.service and mem.key == f"pref.budget.{intent.service}" and result.budget_max_inr is None and not result.no_budget and isinstance(value, int):
                result.budget_max_inr = value
                refs.append(mem.id)
                result.provenance["budget_max_inr"] = {"source": f"memory:{mem.id}", "kind": f"memory_{mem.kind}", "confirmed": True}
            elif intent.service and mem.key == f"pref.technician.{intent.service}" and not result.preferences.get("technician"):
                result.preferences["technician"] = value
                refs.append(mem.id)
                result.provenance["preferences.technician"] = {"source": f"memory:{mem.id}", "kind": f"memory_{mem.kind}", "confirmed": True}
            elif mem.key == "pref.min_rating" and not result.preferences.get("min_rating") and isinstance(value, (int, float)):
                result.preferences["min_rating"] = float(value)
                refs.append(mem.id)
                result.provenance["preferences.min_rating"] = {"source": f"memory:{mem.id}", "kind": f"memory_{mem.kind}", "confirmed": True}
            elif mem.key == "pref.language" and not result.preferences.get("language") and isinstance(value, str):
                result.preferences["language"] = value
                refs.append(mem.id)
                result.provenance["preferences.language"] = {"source": f"memory:{mem.id}", "kind": f"memory_{mem.kind}", "confirmed": True}
            elif mem.key == "pref.days" and not result.preferences.get("days") and isinstance(value, list):
                result.preferences["days"] = value
                refs.append(mem.id)
                result.provenance["preferences.days"] = {"source": f"memory:{mem.id}", "kind": f"memory_{mem.kind}", "confirmed": True}
            elif mem.key == "pref.avoid_provider":
                avoided = set(result.preferences.get("avoid_providers", []))
                if isinstance(value, str):
                    avoided.add(value)
                    result.preferences["avoid_providers"] = sorted(avoided)
                    refs.append(mem.id)
                    result.provenance["preferences.avoid_providers"] = {"source": f"memory:{mem.id}", "kind": f"memory_{mem.kind}", "confirmed": True}
        return result, refs

    def create(self, user_id: str, key: str, value: Any, kind: str = "explicit", scope: str = "global", status: str | None = None, evidence: list[str] | None = None, confidence: float | None = None) -> str:
        if not ALLOWED_MEMORY_KEY.fullmatch(key):
            raise ValueError("Memory key is not in the supported preference schema")
        rendered = json_dumps(value)
        if len(rendered.encode("utf-8")) > 1024:
            raise ValueError("Preference values must be at most 1 KB")
        if self.redactor.redact(rendered) != rendered or contains_secret_pattern(rendered):
            raise ValueError("Contact details and secrets are not stored in preference memory")
        if key == "pref.area" and (not isinstance(value, str) or value not in AREAS):
            raise ValueError("Area preference must match a supported service area")
        if key in {"pref.earliest_time", "pref.latest_time"} and (not isinstance(value, str) or not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", value)):
            raise ValueError("Time preference must use HH:MM")
        if key == "pref.days" and (not isinstance(value, list) or not value or any(day not in {"Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"} for day in value)):
            raise ValueError("Day preference must be a list of weekday names")
        if key == "pref.min_rating" and (isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 <= value <= 5):
            raise ValueError("Minimum rating must be a number from 0 to 5")
        if key.startswith("pref.budget.") and (isinstance(value, bool) or not isinstance(value, int) or not 0 < value <= 1_000_000):
            raise ValueError("Service budget must be an integer from 1 to 1000000")
        if key.startswith("pref.technician.") and (not isinstance(value, str) or not value.strip() or len(value) > 80):
            raise ValueError("Technician preference must be text up to 80 characters")
        if key == "pref.language" and (not isinstance(value, str) or not value.strip() or len(value) > 40):
            raise ValueError("Language preference must be text up to 40 characters")
        if key == "pref.avoid_provider" and (not isinstance(value, str) or not re.fullmatch(r"prov_\d{3}", value)):
            raise ValueError("Avoided provider must use a provider ID from the local catalog")
        if key.startswith("household.") and (not isinstance(value, str) or len(value) > 200):
            raise ValueError("Household preference must be text up to 200 characters")
        if scope != "global" and not re.fullmatch(rf"(?:service:{'|'.join(SERVICES)}|provider:prov_\d{{3}})", scope):
            raise ValueError("Preference scope is not supported")
        value_hash = _value_hash(value)
        tombstone = self.store.connection.execute(
            "SELECT 1 FROM memories WHERE user_id=? AND value_hash=? AND status IN ('forgotten','rejected') LIMIT 1", (user_id, value_hash)
        ).fetchone()
        if tombstone and kind != "explicit":
            raise ValueError("That forgotten or rejected value cannot be silently reintroduced")
        memory_id = new_id("mem")
        now = self.clock.epoch()
        resolved_status = status or ("active" if kind == "explicit" else "pending")
        with self.store.transaction() as db:
            db.execute(
                "INSERT INTO memories(id,user_id,key,scope,value_json,kind,status,confidence,evidence_json,value_hash,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (memory_id, user_id, key, scope, json_dumps(value), kind, resolved_status, confidence, json_dumps(evidence or []), value_hash, now, now),
            )
        return memory_id

    def correct(self, user_id: str, memory_id: str, value: Any, evidence: list[str] | None = None) -> str:
        row = self.store.connection.execute("SELECT * FROM memories WHERE id=? AND user_id=? AND status IN ('active','pending')", (memory_id, user_id)).fetchone()
        if not row:
            raise ValueError("Memory not found or no longer current")
        new_id_value = self.create(user_id, row["key"], value, "explicit", row["scope"], "active", evidence or [], None)
        with self.store.transaction() as db:
            db.execute("UPDATE memories SET status='superseded',updated_at=? WHERE id=?", (self.clock.epoch(), memory_id))
            db.execute("UPDATE memories SET supersedes_id=? WHERE id=?", (memory_id, new_id_value))
        self._scrub_task_reference(user_id, memory_id)
        return new_id_value

    def forget(self, user_id: str, memory_id: str) -> None:
        row = self.store.connection.execute("SELECT * FROM memories WHERE id=? AND user_id=? AND status NOT IN ('forgotten','rejected')", (memory_id, user_id)).fetchone()
        if not row:
            raise ValueError("Memory not found or already forgotten")
        with self.store.transaction() as db:
            db.execute("UPDATE memories SET value_json=NULL,status='forgotten',forgotten_at=?,updated_at=? WHERE id=?", (self.clock.epoch(), self.clock.epoch(), memory_id))
        self._scrub_task_reference(user_id, memory_id)

    def _scrub_task_reference(self, user_id: str, memory_id: str) -> None:
        """Remove task fields sourced from a memory that was corrected or forgotten."""
        rows = self.store.connection.execute("SELECT id,state,intent_json FROM tasks WHERE user_id=?", (user_id,)).fetchall()
        source = f"memory:{memory_id}"
        for row in rows:
            intent = IntentState.from_json(row["intent_json"])
            fields = [key for key, provenance in intent.provenance.items() if provenance.get("source") == source]
            if not fields:
                continue
            for field_name in fields:
                if field_name.startswith("preferences."):
                    intent.preferences.pop(field_name.removeprefix("preferences."), None)
                elif field_name in {"area", "earliest_time", "latest_time", "budget_max_inr"}:
                    setattr(intent, field_name, None)
                intent.provenance.pop(field_name, None)
            intent.presented_options = []
            intent.provider_ids = []
            intent.selection = {}
            old_state = row["state"]
            new_state = "GATHERING" if old_state in {"AWAITING_APPROVAL", "OPTIONS_PRESENTED"} else old_state
            with self.store.transaction() as db:
                db.execute(
                    "UPDATE tasks SET state=?,intent_json=?,version=version+1,updated_at=? WHERE id=?",
                    (new_state, intent.to_json(), self.clock.epoch(), row["id"]),
                )
                db.execute("UPDATE pending_actions SET status='invalidated' WHERE task_id=? AND status='proposed'", (row["id"],))
                db.execute(
                    "INSERT INTO task_events(task_id,old_state,new_state,event,details_json,created_at) VALUES(?,?,?,?,?,?)",
                    (row["id"], old_state, new_state, "memory_value_removed", json_dumps({"memory_id": memory_id, "fields": fields}), self.clock.epoch()),
                )

    def reject(self, user_id: str, memory_id: str) -> None:
        with self.store.transaction() as db:
            updated = db.execute("UPDATE memories SET value_json=NULL,status='rejected',updated_at=? WHERE id=? AND user_id=? AND status='pending'", (self.clock.epoch(), memory_id, user_id))
            if updated.rowcount != 1:
                raise ValueError("Pending memory not found")

    def confirm(self, user_id: str, memory_id: str) -> None:
        with self.store.transaction() as db:
            updated = db.execute(
                "UPDATE memories SET status='active',updated_at=? WHERE id=? AND user_id=? AND kind='inferred' AND status='pending'",
                (self.clock.epoch(), memory_id, user_id),
            )
            if updated.rowcount != 1:
                raise ValueError("Pending inferred preference not found")


@dataclass(frozen=True)
class ContextResult:
    messages: list[dict[str, str]]
    manifest: dict[str, Any]
    rendered_dynamic_text: str
    estimated_tokens: int
    payload_sha256: str


class ContextBuilder:
    SECTION_CAPS = {
        "instructions": 550, "tool_schemas": 850, "task_state": 250,
        "memories": 300, "history": 500, "observations": 350, "current_message": 200,
    }

    def __init__(self, store: AgentStore, memories: MemoryStore, config: AppConfig, redactor: Redactor | None = None) -> None:
        self.store = store
        self.memories = memories
        self.config = config
        self.redactor = redactor or Redactor()

    @staticmethod
    def _fit(text: str, cap: int, reason: str) -> tuple[str, int, bool]:
        estimate = estimate_tokens(text)
        if estimate <= cap:
            return text, estimate, False
        maximum_bytes = max(0, cap * 3 - 20)
        raw = text.encode("utf-8")[:maximum_bytes]
        fitted = raw.decode("utf-8", errors="ignore") + " [truncated]"
        return fitted, estimate_tokens(fitted), True

    def build(
        self,
        session_id: str,
        user_id: str,
        current_text: str,
        intent: IntentState,
        observations: list[dict[str, Any]] | None = None,
        call_id: str | None = None,
        exclude_message_id: str | None = None,
    ) -> ContextResult:
        caps = dict(self.SECTION_CAPS)
        total_budget = self.config.context_budget_tokens
        cap_sum = sum(caps.values())
        if total_budget < cap_sum:
            overflow = cap_sum - total_budget
            caps["history"] = max(0, caps["history"] - overflow)
        tool_schema_text = json_dumps(PROPOSAL_TOOLS)
        schemas_text, schemas_tokens, schema_truncated = self._fit(tool_schema_text, caps["tool_schemas"], "tool_schemas")
        instructions, instructions_tokens, instructions_truncated = self._fit(SYSTEM_INSTRUCTIONS, caps["instructions"], "instructions")
        task_payload = {
            "action": intent.action, "service": intent.service, "area": intent.area,
            "date_start": intent.date_start, "date_end": intent.date_end,
            "earliest_time": intent.earliest_time, "latest_time": intent.latest_time,
            "budget_max_inr": intent.budget_max_inr, "no_budget": intent.no_budget,
            "preferences": intent.preferences, "target_booking_id": intent.target_booking_id,
            "provenance": intent.provenance,
        }
        task_truncated = False
        for optional in ("provenance", "preferences"):
            if estimate_tokens(json_dumps(task_payload)) <= caps["task_state"]:
                break
            task_payload.pop(optional, None)
            task_truncated = True
        task_text = json_dumps(task_payload)
        task_tokens = estimate_tokens(task_text)
        if task_tokens > caps["task_state"]:
            raise ValueError("context_budget_exceeded")
        selected_memories, excluded = self.memories.relevant(user_id, intent.service, intent.action)
        memory_items = [
            {"id": item.id, "key": item.key, "scope": item.scope, "value": item.value, "kind": item.kind}
            for item in selected_memories
        ]
        full_memories = list(memory_items)
        memory_items = []
        for item in full_memories:
            candidate = [*memory_items, item]
            if estimate_tokens(json_dumps(candidate)) > caps["memories"]:
                break
            memory_items = candidate
        memories_truncated = len(memory_items) < len(full_memories)
        memory_text = json_dumps(memory_items)
        memory_tokens = estimate_tokens(memory_text)
        if exclude_message_id:
            history_rows = self.store.connection.execute(
                "SELECT id,role,content,memory_refs_json,security_labels_json FROM messages WHERE session_id=? AND id!=? ORDER BY created_at DESC LIMIT 30",
                (session_id, exclude_message_id),
            ).fetchall()
        else:
            history_rows = self.store.connection.execute(
                "SELECT id,role,content,memory_refs_json,security_labels_json FROM messages WHERE session_id=? ORDER BY created_at DESC LIMIT 30", (session_id,)
            ).fetchall()
        stale_rows = self.store.connection.execute(
            "SELECT id,evidence_json FROM memories WHERE user_id=? AND status IN ('forgotten','rejected','superseded','pending')",
            (user_id,),
        ).fetchall()
        stale_memory_ids = {row["id"] for row in stale_rows}
        stale_evidence_ids = {
            evidence_id for row in stale_rows for evidence_id in json_loads(row["evidence_json"], [])
        }
        history: list[dict[str, str]] = []
        excluded_history: list[dict[str, str]] = []
        for row in reversed(history_rows):
            refs = set(json_loads(row["memory_refs_json"], []))
            labels = set(json_loads(row["security_labels_json"], []))
            content = row["content"]
            if labels:
                excluded_history.append({"id": row["id"], "reason": "security_filtered"})
                continue
            if refs.intersection(stale_memory_ids):
                excluded_history.append({"id": row["id"], "reason": "memory_reference_not_current"})
                continue
            if row["id"] in stale_evidence_ids:
                excluded_history.append({"id": row["id"], "reason": "memory_evidence_not_current"})
                continue
            safe_content = self.redactor.redact(content)
            history.append({"id": row["id"], "role": row["role"], "content": safe_content})
        full_history = list(history)
        history = []
        for item in reversed(full_history):
            candidate = [item, *history]
            if estimate_tokens(json_dumps(candidate)) > caps["history"]:
                break
            history = candidate
        history_truncated = len(history) < len(full_history)
        history_text = json_dumps(history)
        history_tokens = estimate_tokens(history_text)
        observation_items = observations or []
        full_observations = list(observation_items)
        observation_items = []
        for item in full_observations:
            safe_item = {key: item[key] for key in ("provider_id", "service", "area", "price_inr", "rating", "available_slots") if key in item}
            candidate = [*observation_items, safe_item]
            if estimate_tokens(json_dumps(candidate)) > caps["observations"]:
                break
            observation_items = candidate
        observations_truncated = len(observation_items) < len(full_observations)
        observation_text = json_dumps(observation_items)
        observation_tokens = estimate_tokens(observation_text)
        current_safe = self.redactor.redact(current_text)
        current_text_fit, current_tokens, current_truncated = self._fit(current_safe, caps["current_message"], "current_message")
        dynamic = {
            "task": task_text, "confirmed_memories": memory_text, "recent_history": history_text,
            "observations": observation_text, "current_user_message": current_text_fit,
        }
        rendered = json.dumps(dynamic, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        messages = [{"role": "system", "content": instructions}, {"role": "user", "content": rendered}]
        estimated = sum((instructions_tokens, schemas_tokens, task_tokens, memory_tokens, history_tokens, observation_tokens, current_tokens))
        # The compact JSON framing is included in the safety margin.
        estimated += estimate_tokens(rendered) - sum((task_tokens, memory_tokens, history_tokens, observation_tokens, current_tokens))
        manifest = {
            "call_id": call_id or new_id("ctx"),
            "budget_tokens": total_budget,
            "section_tokens": {
                "instructions": instructions_tokens, "tool_schemas": schemas_tokens,
                "task_state": task_tokens, "memories": memory_tokens, "history": history_tokens,
                "observations": observation_tokens, "current_message": current_tokens,
            },
            "included_memory_ids": [item["id"] for item in memory_items],
            "excluded_memories": excluded + [{"id": item.id, "reason": "over_budget_or_truncated"} for item in selected_memories[len(memory_items):]],
            "excluded_history": excluded_history,
            "truncated": {"instructions": instructions_truncated, "schemas": schema_truncated, "task": task_truncated,
                          "memories": memories_truncated, "history": history_truncated,
                          "observations": observations_truncated, "current_message": current_truncated},
            "selected_memory_kinds": {item.id: item.kind for item in selected_memories if item.id in {m["id"] for m in memory_items}},
            "instructions_sha256": hashlib.sha256(instructions.encode()).hexdigest(),
            "schemas_sha256": hashlib.sha256(schemas_text.encode()).hexdigest(),
        }
        effort = choose_effort(current_text, self.config.reasoning_effort_default, self.config.allow_high_effort)

        def exact_request_payload() -> tuple[str, str, int]:
            rendered_dynamic = json.dumps(dynamic, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            messages[:] = [{"role": "system", "content": instructions}, {"role": "user", "content": rendered_dynamic}]
            request_payload = {
                "model": self.config.openai_model,
                "input": messages,
                "reasoning": {"effort": effort},
                "max_output_tokens": self.config.max_output_tokens,
                "store": False,
                "tool_choice": PROPOSAL_TOOL_CHOICE,
                "tools": PROPOSAL_TOOLS,
            }
            serialized = json.dumps(request_payload, ensure_ascii=False, separators=(",", ":"))
            return rendered_dynamic, serialized, estimate_tokens(serialized)

        rendered, request_payload_json, estimated = exact_request_payload()
        removed_for_budget: list[str] = []
        if estimated > total_budget:
            # Required instructions and the current request win; remove optional sections first.
            for section in ("recent_history", "confirmed_memories", "observations"):
                if estimated <= total_budget:
                    break
                dynamic[section] = "[]"
                removed_for_budget.append(section)
                rendered, request_payload_json, estimated = exact_request_payload()
        if estimated > total_budget:
            raise ValueError("context_budget_exceeded")
        if "confirmed_memories" in removed_for_budget:
            manifest["excluded_memories"].extend(
                {"id": item["id"], "reason": "context_budget"} for item in memory_items
            )
            manifest["included_memory_ids"] = []
            manifest["selected_memory_kinds"] = {}
        manifest["context_sections_removed_for_budget"] = removed_for_budget
        payload_hash = hashlib.sha256(request_payload_json.encode("utf-8")).hexdigest()
        manifest["reasoning_effort"] = effort
        with self.store.transaction() as db:
            db.execute(
                "INSERT INTO context_logs(id,session_id,call_id,budget_tokens,estimated_tokens,manifest_json,rendered_dynamic_text,request_payload_json,payload_sha256,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (manifest["call_id"], session_id, manifest["call_id"], total_budget, estimated, json_dumps(manifest), rendered, request_payload_json, payload_hash, time.time()),
            )
        manifest["payload_sha256"] = payload_hash
        manifest["estimated_tokens"] = estimated
        return ContextResult(messages, manifest, rendered, estimated, payload_hash)
