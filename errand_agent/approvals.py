"""Action-bound approvals and restart-safe execution of provider side effects."""

from __future__ import annotations

import base64
import json
import time
from datetime import datetime
from typing import Any

from .clock import Clock
from .config import AppConfig
from .ids import fingerprint, idempotency_key, new_id
from .intent import IntentState
from .mock_provider import MockProvider, MockToolError
from .storage import AgentStore, json_dumps, json_loads
from .trace import TraceRecorder


TERMINAL_ACTIONS = {"done", "failed", "invalidated", "expired", "rejected"}
TRANSITIONS: dict[str, set[str]] = {
    "NEW": {"GATHERING", "SEARCHING", "ABANDONED"},
    "GATHERING": {"GATHERING", "SEARCHING", "RESOLVING_BOOKING", "AWAITING_APPROVAL", "FAILED", "ABANDONED"},
    "SEARCHING": {"OPTIONS_PRESENTED", "GATHERING", "FAILED", "ABANDONED"},
    "OPTIONS_PRESENTED": {"HOLDING", "GATHERING", "SEARCHING", "FAILED", "ABANDONED"},
    "HOLDING": {"AWAITING_APPROVAL", "OPTIONS_PRESENTED", "FAILED"},
    "AWAITING_APPROVAL": {"EXECUTING", "GATHERING", "OPTIONS_PRESENTED", "FAILED", "ABANDONED"},
    "EXECUTING": {"VERIFYING", "FAILED", "UNVERIFIED", "OPTIONS_PRESENTED"},
    "VERIFYING": {"CONFIRMED", "DONE", "FAILED", "UNVERIFIED", "CONFLICT", "OPTIONS_PRESENTED"},
    "RESOLVING_BOOKING": {"AWAITING_APPROVAL", "GATHERING", "FAILED"},
    "CONFIRMED": set(), "DONE": set(), "FAILED": set(), "UNVERIFIED": {"VERIFYING", "CONFIRMED", "FAILED", "UNVERIFIED", "OPTIONS_PRESENTED", "DONE"},
    "CONFLICT": {"OPTIONS_PRESENTED", "FAILED"}, "ABANDONED": set(),
}


class ApprovalError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class ActionApprovalService:
    def __init__(self, store: AgentStore, provider: MockProvider, trace: TraceRecorder, config: AppConfig, clock: Clock | None = None) -> None:
        self.store = store
        self.provider = provider
        self.trace = trace
        self.config = config
        self.clock = clock or Clock()
        self._run_deadlines: dict[str, float] = {}

    def invalidate_proposed(self, task_id: str, reason: str, run_id: str | None = None, session_id: str | None = None) -> int:
        with self.store.transaction() as db:
            rows = db.execute("SELECT id,fingerprint FROM pending_actions WHERE task_id=? AND status='proposed'", (task_id,)).fetchall()
            db.execute("UPDATE pending_actions SET status='invalidated' WHERE task_id=? AND status='proposed'", (task_id,))
        for row in rows:
            if run_id:
                self.trace.emit(run_id, "approval_invalidated", session_id, action_id=row["id"], fingerprint=row["fingerprint"], reason=reason)
        return len(rows)

    def transition(self, task_id: str, new_state: str, event: str, details: dict[str, Any] | None = None, run_id: str | None = None, session_id: str | None = None) -> None:
        row = self.store.connection.execute("SELECT state,version FROM tasks WHERE id=?", (task_id,)).fetchone()
        if not row:
            raise ApprovalError("task_missing", "Task no longer exists.")
        old_state = row["state"]
        if new_state not in TRANSITIONS.get(old_state, set()):
            raise ApprovalError("illegal_transition", f"Illegal task transition {old_state} → {new_state}")
        now = self.clock.epoch()
        with self.store.transaction() as db:
            updated = db.execute(
                "UPDATE tasks SET state=?,version=version+1,updated_at=? WHERE id=? AND version=?",
                (new_state, now, task_id, row["version"]),
            )
            if updated.rowcount != 1:
                raise ApprovalError("task_race", "Task state changed during this action.")
            db.execute(
                "INSERT INTO task_events(task_id,old_state,new_state,event,details_json,created_at) VALUES(?,?,?,?,?,?)",
                (task_id, old_state, new_state, event, json_dumps(details or {}), now),
            )
        if run_id:
            self.trace.emit(run_id, "state_transition", session_id, task_id=task_id, old_state=old_state, new_state=new_state, transition_event=event, details=details or {})

    def _transition_if_possible(self, task_id: str, target: str, event: str, details: dict[str, Any], run_id: str | None, session_id: str | None) -> None:
        row = self.store.connection.execute("SELECT state FROM tasks WHERE id=?", (task_id,)).fetchone()
        if row and target in TRANSITIONS.get(row["state"], set()):
            self.transition(task_id, target, event, details, run_id, session_id)

    def create_booking_approval(
        self,
        task_id: str,
        session_id: str,
        user_id: str,
        intent: IntentState,
        provider_id: str,
        slot: str,
        run_id: str,
    ) -> dict[str, Any]:
        option = next((item for item in intent.presented_options if item.get("provider_id") == provider_id), None)
        if not option or slot not in option.get("available_slots", []):
            raise ApprovalError("option_not_presented", "Choose a provider and slot from the current options.")
        if intent.date_start and slot[:10] < intent.date_start or intent.date_end and slot[:10] > intent.date_end:
            raise ApprovalError("slot_outside_date", "That slot is outside your requested date window.")
        slot_time = slot[11:16]
        if intent.earliest_time and slot_time < intent.earliest_time:
            raise ApprovalError("slot_too_early", "That slot is earlier than your requested time.")
        if intent.latest_time and slot_time >= intent.latest_time:
            raise ApprovalError("slot_too_late", "That slot is later than your requested time.")
        if intent.budget_max_inr is not None and option["price_inr"] > intent.budget_max_inr:
            raise ApprovalError("over_budget", "That provider exceeds your budget.")
        active = self.store.connection.execute(
            "SELECT count(*) FROM pending_actions WHERE session_id=? AND status IN ('proposed','approved','executing','verifying','unverified')",
            (session_id,),
        ).fetchone()[0]
        if active >= 2:
            raise ApprovalError("active_hold_limit", "There are already two active approval holds in this session.")
        current = self.store.connection.execute("SELECT state FROM tasks WHERE id=?", (task_id,)).fetchone()
        if not current or current["state"] not in {"OPTIONS_PRESENTED", "GATHERING"}:
            raise ApprovalError("task_not_ready", "This task is not ready to hold a provider slot.")
        if current["state"] in {"OPTIONS_PRESENTED", "GATHERING"}:
            self.transition(task_id, "HOLDING", "user_selected_option", {"provider_id": provider_id, "slot": slot}, run_id, session_id)
        self.trace.emit(run_id, "tool_authorization", session_id, tool="hold_slot", allowed=True, reason="user_selected_presented_option")
        started = time.monotonic()
        self.trace.emit(run_id, "tool_call", session_id, tool="hold_slot", arguments={"provider_id": provider_id, "slot": slot})
        try:
            hold = self.provider.hold_slot(provider_id, slot, user_id)
        except MockToolError as error:
            self.trace.emit(
                run_id, "tool_error", session_id, tool="hold_slot", code=error.code,
                retryable=error.retryable, ambiguous=error.ambiguous,
                latency_ms=int((time.monotonic() - started) * 1000),
            )
            self._transition_if_possible(task_id, "OPTIONS_PRESENTED", "hold_failed", {"code": error.code}, run_id, session_id)
            raise ApprovalError(error.code, str(error)) from None
        self.trace.emit(
            run_id, "tool_result", session_id, tool="hold_slot", result=hold,
            latency_ms=int((time.monotonic() - started) * 1000),
        )
        if intent.budget_max_inr is not None and int(hold["quoted_price_inr"]) > intent.budget_max_inr:
            self._transition_if_possible(task_id, "OPTIONS_PRESENTED", "hold_quote_exceeded_budget", {"quoted_price_inr": hold["quoted_price_inr"], "budget_max_inr": intent.budget_max_inr}, run_id, session_id)
            self.trace.emit(run_id, "quote_rejected", session_id, provider_id=provider_id, quoted_price_inr=hold["quoted_price_inr"], budget_max_inr=intent.budget_max_inr)
            raise ApprovalError("quote_over_budget", "The current quote exceeds your budget. No approval was created; choose another provider or change your budget.")
        now = self.clock.epoch()
        expires_at = min(float(hold["expires_at"]) - 30, now + self.config.approval_ttl_seconds)
        if expires_at <= now:
            raise ApprovalError("hold_too_short", "The slot hold is too close to expiry; choose another slot.")
        material = {
            "provider_id": provider_id, "provider_name": option["name"], "service": option["service"],
            "area": option["area"], "slot": slot, "quoted_price_inr": hold["quoted_price_inr"],
            "search_price_inr": option["price_inr"], "hold_id": hold["hold_id"],
            "hold_expires_at": hold["expires_at"],
        }
        return self._insert_action(task_id, session_id, user_id, "book", material, expires_at, run_id)

    def create_booking_management_approval(
        self,
        task_id: str,
        session_id: str,
        user_id: str,
        kind: str,
        booking_id: str,
        new_slot: str | None,
        run_id: str,
    ) -> dict[str, Any]:
        if kind not in {"cancel", "reschedule"}:
            raise ApprovalError("invalid_action", "Unsupported booking action.")
        booking = next((item for item in self.provider.list_bookings(user_id) if item["booking_id"] == booking_id), None)
        if not booking or booking["status"] != "confirmed":
            raise ApprovalError("booking_not_active_or_owned", "That active booking was not found for this user.")
        try:
            slot_dt = datetime.fromisoformat(booking["slot"])
        except ValueError:
            raise ApprovalError("booking_invalid", "The booking slot could not be validated.") from None
        if slot_dt.timestamp() <= self.clock.epoch():
            raise ApprovalError("booking_not_upcoming", "Only an upcoming booking can be changed.")
        material: dict[str, Any] = {
            "booking_id": booking_id, "provider_id": booking["provider_id"], "service": booking["service"],
            "slot": booking["slot"],
        }
        if kind == "reschedule":
            if not new_slot:
                raise ApprovalError("missing_new_slot", "Choose an available replacement slot.")
            details = self.provider.get_provider(booking["provider_id"])
            if new_slot not in details["available_slots"]:
                raise ApprovalError("slot_not_available", "That replacement slot is not available.")
            material["new_slot"] = new_slot
        expiry = self.clock.epoch() + self.config.approval_ttl_seconds
        return self._insert_action(task_id, session_id, user_id, kind, material, expiry, run_id)

    def _insert_action(self, task_id: str, session_id: str, user_id: str, kind: str, material: dict[str, Any], expires_at: float, run_id: str) -> dict[str, Any]:
        action_id = new_id("action")
        action_fingerprint = fingerprint({"version": 1, "kind": kind, "user_id": user_id, "material": material})
        code = base64.b32encode(bytes.fromhex(action_fingerprint)).decode("ascii").rstrip("=")[:6]
        operation = {"book": "bk", "cancel": "cx", "reschedule": "rs"}[kind]
        key = idempotency_key(operation, action_id)
        now = self.clock.epoch()
        with self.store.transaction() as db:
            db.execute(
                "UPDATE pending_actions SET status='invalidated' WHERE task_id=? AND status='proposed'",
                (task_id,),
            )
            db.execute(
                "INSERT INTO pending_actions(id,task_id,session_id,user_id,kind,material_json,fingerprint,approval_code,status,idempotency_key,presented_at,expires_at,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (action_id, task_id, session_id, user_id, kind, json_dumps(material), action_fingerprint, code, "proposed", key, now, expires_at, now),
            )
        self._transition_if_possible(task_id, "AWAITING_APPROVAL", "approval_presented", {"action_id": action_id, "kind": kind}, run_id, session_id)
        card = self._card(action_id, kind, material, code, expires_at)
        self.trace.emit(run_id, "approval_presented", session_id, action_id=action_id, kind=kind, fingerprint=action_fingerprint, material=material, expires_at=expires_at)
        return {"action_id": action_id, "fingerprint": action_fingerprint, "code": code, "material": material, "expires_at": expires_at, "card": card}

    @staticmethod
    def _card(action_id: str, kind: str, material: dict[str, Any], code: str, expires_at: float) -> dict[str, Any]:
        return {
            "action_id": action_id,
            "kind": kind,
            "provider_id": material.get("provider_id"),
            "provider_name": material.get("provider_name"),
            "service": material.get("service"),
            "area": material.get("area"),
            "slot": material.get("new_slot", material.get("slot")),
            "quoted_price_inr": material.get("quoted_price_inr"),
            "search_price_inr": material.get("search_price_inr"),
            "price_delta_inr": (material.get("quoted_price_inr") - material.get("search_price_inr")) if kind == "book" else None,
            "booking_id": material.get("booking_id"),
            "current_slot": material.get("slot") if kind in {"cancel", "reschedule"} else None,
            "approval_code": code,
            "expires_at": expires_at,
        }

    def approve(self, action_id: str, user_id: str, session_id: str, action_fingerprint: str, client_request_id: str, run_id: str) -> dict[str, Any]:
        existing = self.store.connection.execute("SELECT * FROM approval_events WHERE client_request_id=?", (client_request_id,)).fetchone()
        if existing:
            if existing["action_id"] != action_id or existing["user_id"] != user_id or existing["fingerprint"] != action_fingerprint:
                raise ApprovalError("request_replay_mismatch", "This confirmation request was already used for a different action.")
            row = self._action_row(action_id)
            response = json_loads(row["response_json"], {})
            return {"status": response.get("status", row["status"]), "action_status": row["status"], "replayed": True, "result": response}
        now = self.clock.epoch()
        deferred_error: ApprovalError | None = None
        with self.store.transaction() as db:
            row = db.execute("SELECT * FROM pending_actions WHERE id=?", (action_id,)).fetchone()
            if not row or row["user_id"] != user_id or row["session_id"] != session_id:
                raise ApprovalError("approval_owner_mismatch", "This approval does not belong to this user and session.")
            if row["fingerprint"] != action_fingerprint:
                db.execute("UPDATE pending_actions SET status='invalidated' WHERE id=? AND status='proposed'", (action_id,))
                deferred_error = ApprovalError("stale_fingerprint", "These details changed. Review the current approval card.")
            elif row["status"] != "proposed":
                response = json_loads(row["response_json"], {})
                return {"status": response.get("status", row["status"]), "action_status": row["status"], "replayed": True, "result": response}
            elif now >= row["expires_at"]:
                db.execute("UPDATE pending_actions SET status='expired' WHERE id=? AND status='proposed'", (action_id,))
                deferred_error = ApprovalError("approval_expired", "This approval expired. A fresh quote is required.")
            else:
                update = db.execute(
                    "UPDATE pending_actions SET status='approved',approved_at=? WHERE id=? AND status='proposed' AND fingerprint=?",
                    (now, action_id, action_fingerprint),
                )
                if update.rowcount != 1:
                    deferred_error = ApprovalError("approval_race", "This approval was already used.")
                else:
                    db.execute(
                        "INSERT INTO approval_events(action_id,fingerprint,user_id,session_id,client_request_id,decision,created_at) VALUES(?,?,?,?,?,'approved',?)",
                        (action_id, action_fingerprint, user_id, session_id, client_request_id, now),
                    )
        if deferred_error:
            self.trace.emit(run_id, "approval_decision", session_id, action_id=action_id, decision="denied", reason=deferred_error.code, fingerprint=action_fingerprint)
            if deferred_error.code == "approval_expired" and row:
                target = "OPTIONS_PRESENTED" if row["kind"] == "book" else "GATHERING"
                self._transition_if_possible(row["task_id"], target, "approval_expired", {"action_id": action_id}, run_id, session_id)
            raise deferred_error
        self.trace.emit(run_id, "approval_decision", session_id, action_id=action_id, decision="approved", fingerprint=action_fingerprint, client_request_id=client_request_id)
        return self.execute(action_id, run_id)

    def reject(self, action_id: str, user_id: str, session_id: str, run_id: str) -> dict[str, Any]:
        with self.store.transaction() as db:
            row = db.execute("SELECT * FROM pending_actions WHERE id=?", (action_id,)).fetchone()
            if not row or row["user_id"] != user_id or row["session_id"] != session_id:
                raise ApprovalError("approval_owner_mismatch", "This approval does not belong to this user and session.")
            if row["status"] != "proposed":
                return {"status": row["status"]}
            db.execute("UPDATE pending_actions SET status='rejected' WHERE id=? AND status='proposed'", (action_id,))
            db.execute(
                "INSERT INTO approval_events(action_id,fingerprint,user_id,session_id,client_request_id,decision,created_at) VALUES(?,?,?,?,?,'rejected',?)",
                (action_id, row["fingerprint"], user_id, session_id, new_id("reject"), self.clock.epoch()),
            )
        self._transition_if_possible(row["task_id"], "GATHERING", "approval_rejected", {"action_id": action_id}, run_id, session_id)
        self.trace.emit(run_id, "approval_decision", session_id, action_id=action_id, decision="rejected", fingerprint=row["fingerprint"])
        return {"status": "rejected"}

    def execute(self, action_id: str, run_id: str, recovery: bool = False) -> dict[str, Any]:
        row = self._action_row(action_id)
        if row["status"] not in {"approved", "executing", "verifying", "unverified"}:
            return {"status": row["status"], "message": "This action is no longer executable."}
        if row["status"] == "approved":
            if row["kind"] in {"cancel", "reschedule"}:
                material = json_loads(row["material_json"], {})
                snapshot_ok, snapshot_error = self._booking_snapshot_matches(row, material, run_id)
                if not snapshot_ok:
                    outcome = "booking_changed" if snapshot_error == "booking_changed" else snapshot_error
                    message = (
                        "The booking changed after this card was shown. No provider change was made; review the current booking before approving again."
                        if outcome == "booking_changed" else "I could not verify the booking before execution, so no provider change was made. Request a fresh approval and try again."
                    )
                    return self._finish(row, "failed", {"status": outcome, "message": message}, run_id)
            with self.store.transaction() as db:
                updated = db.execute("UPDATE pending_actions SET status='executing' WHERE id=? AND status='approved'", (action_id,))
                if updated.rowcount != 1:
                    return {"status": self._action_row(action_id)["status"], "message": "This action is already being processed."}
            self._transition_if_possible(row["task_id"], "EXECUTING", "execution_started", {"action_id": action_id, "recovery": recovery}, run_id, row["session_id"])
        row = self._action_row(action_id)
        material = json_loads(row["material_json"], {})
        self._run_deadlines[run_id] = time.monotonic() + self.config.max_wall_seconds
        try:
            if row["kind"] == "book":
                return self._execute_book(row, material, run_id)
            if row["kind"] == "cancel":
                return self._execute_cancel(row, material, run_id)
            if row["kind"] == "reschedule":
                return self._execute_reschedule(row, material, run_id)
            return self._finish(row, "failed", {"status": "failed", "message": "Unsupported action."}, run_id)
        finally:
            self._run_deadlines.pop(run_id, None)

    def _booking_snapshot_matches(self, row: Any, material: dict[str, Any], run_id: str) -> tuple[bool, str]:
        if not self._claim_execution_call(row, run_id, "list_bookings", {"purpose": "pre_write_snapshot"}):
            return False, "execution_limit"
        try:
            bookings = self._provider_call(
                row, run_id, "list_bookings", {"user_id": row["user_id"]},
                lambda: self.provider.list_bookings(row["user_id"]),
            )
        except MockToolError as error:
            self.trace.emit(run_id, "booking_snapshot_unavailable", row["session_id"], action_id=row["id"], code=error.code)
            return False, error.code
        current = next((item for item in bookings if item["booking_id"] == material.get("booking_id")), None)
        matches = bool(
            current and current.get("status") == "confirmed"
            and current.get("provider_id") == material.get("provider_id")
            and current.get("service") == material.get("service")
            and current.get("slot") == material.get("slot")
        )
        self.trace.emit(
            run_id, "booking_snapshot_checked", row["session_id"], action_id=row["id"],
            matches=matches, expected={key: material.get(key) for key in ("booking_id", "provider_id", "service", "slot")},
            current={key: current.get(key) for key in ("booking_id", "provider_id", "service", "slot", "status")} if current else None,
        )
        return matches, "" if matches else "booking_changed"

    def _execute_book(self, row: Any, material: dict[str, Any], run_id: str) -> dict[str, Any]:
        if self.clock.epoch() >= float(material["hold_expires_at"]):
            return self._finish(row, "failed", {"status": "hold_expired", "message": "The slot hold expired. Choose a slot for a fresh quote."}, run_id, task_state="OPTIONS_PRESENTED")
        max_attempts = min(3, self.config.max_retries + 1)
        for attempt in range(1, max_attempts + 1):
            if not self._claim_execution_call(row, run_id, "book", {"attempt": attempt}):
                return self._finish(row, "failed", {"status": "execution_limit", "message": "The local execution-call limit was reached before booking completed."}, run_id)
            self._journal(row, run_id, "book_call", {"attempt": attempt, "idempotency_key": row["idempotency_key"]})
            try:
                result = self._provider_call(
                    row, run_id, "book", {"hold_id": material["hold_id"], "idempotency_key": row["idempotency_key"]},
                    lambda: self.provider.book(material["hold_id"], row["idempotency_key"]),
                )
            except MockToolError as error:
                if error.ambiguous:
                    self._transition_if_possible(row["task_id"], "VERIFYING", "booking_verification_started", {"action_id": row["id"], "reason": "ambiguous_result"}, run_id, row["session_id"])
                    verified = self._verify_book(row, material, run_id)
                    if verified.get("status") == "booked":
                        return self._finish(row, "done", {"status": "confirmed", "message": "Booking confirmed after checking the provider status.", "booking": verified}, run_id, verified=verified)
                    if verified.get("status") == "not_booked" and attempt < max_attempts and self.clock.epoch() < float(material["hold_expires_at"]):
                        self.trace.emit(run_id, "retry", row["session_id"], operation="book", reason="authoritative_not_booked", attempt=attempt + 1, idempotency_key=row["idempotency_key"])
                        continue
                    if verified.get("status") in {"unknown", "pending", "unavailable"}:
                        return self._finish(row, "unverified", {"status": "unverified", "message": "I could not verify the booking outcome. I will not send another booking request; check status before retrying."}, run_id)
                    return self._finish(row, "failed", {"status": "not_booked", "message": "The provider confirmed that no booking was made."}, run_id, verified=verified)
                return self._finish(row, "failed", {"status": error.code, "message": "The booking could not be completed. The approval was not reused."}, run_id)
            if result.get("status") == "booked":
                self._transition_if_possible(row["task_id"], "VERIFYING", "booking_verification_started", {"action_id": row["id"], "reason": "book_response"}, run_id, row["session_id"])
                verified = self._verify_book(row, material, run_id)
                if verified.get("status") == "booked":
                    return self._finish(row, "done", {"status": "confirmed", "message": "Booking confirmed by the provider.", "booking": verified}, run_id, verified=verified)
                if verified.get("status") in {"unknown", "pending", "unavailable"}:
                    return self._finish(row, "unverified", {"status": "unverified", "message": "The provider response was received, but status is not yet verified. I will not claim this booking succeeded."}, run_id)
                return self._finish(row, "failed", {"status": "verification_mismatch", "message": "Provider status did not match the approved details. I have not reported this as a successful booking."}, run_id, verified=verified)
            if result.get("status") == "slot_taken":
                return self._finish(row, "failed", {"status": "slot_taken", "message": "That slot was taken. Choose another available option; this approval cannot be reused."}, run_id, task_state="OPTIONS_PRESENTED")
            if result.get("status") == "hold_expired":
                return self._finish(row, "failed", {"status": "hold_expired", "message": "The hold expired. Choose a slot for a fresh quote."}, run_id, task_state="OPTIONS_PRESENTED")
            return self._finish(row, "failed", {"status": "provider_error", "message": "The provider returned an unexpected booking result."}, run_id)
        return self._finish(row, "failed", {"status": "retries_exhausted", "message": "No booking was verified after the allowed retries."}, run_id)

    def _verify_book(self, row: Any, material: dict[str, Any], run_id: str) -> dict[str, Any]:
        self._mark_action(row["id"], "verifying")
        self.trace.emit(run_id, "verification_started", row["session_id"], operation="book", action_id=row["id"], idempotency_key=row["idempotency_key"])
        result: dict[str, Any] = {"status": "unknown"}
        for attempt in range(1, min(4, self.config.max_execution_tool_calls) + 1):
            if not self._claim_execution_call(row, run_id, "get_booking_status", {"attempt": attempt}):
                return {"status": "unavailable", "error": "execution_limit"}
            self._journal(row, run_id, "status_check", {"attempt": attempt, "idempotency_key": row["idempotency_key"]})
            try:
                result = self._provider_call(
                    row, run_id, "get_booking_status", {"hold_id": material["hold_id"]},
                    lambda: self.provider.get_booking_status(hold_id=material["hold_id"]),
                )
            except MockToolError as error:
                self.trace.emit(run_id, "verification_error", row["session_id"], code=error.code, retryable=error.retryable, attempt=attempt)
                result = {"status": "unavailable", "error": error.code}
                break
            self.trace.emit(run_id, "verification_result", row["session_id"], operation="book", result=result, attempt=attempt)
            if result.get("status") != "pending":
                break
            self.clock.sleep(0.1 * attempt)
        if result.get("status") == "booked":
            expected = (material["provider_id"], material["slot"], material["quoted_price_inr"])
            actual = (result.get("provider_id"), result.get("slot"), result.get("price_inr"))
            if actual != expected:
                self.trace.emit(run_id, "verification_mismatch", row["session_id"], action_id=row["id"], expected=expected, actual=actual)
                return {**result, "status": "mismatch"}
        return result

    def _execute_cancel(self, row: Any, material: dict[str, Any], run_id: str) -> dict[str, Any]:
        max_attempts = min(self.config.max_retries + 1, self.config.max_execution_tool_calls)
        for attempt in range(1, max_attempts + 1):
            if not self._claim_execution_call(row, run_id, "cancel", {"attempt": attempt}):
                return self._finish(row, "failed", {"status": "execution_limit", "message": "The local execution-call limit was reached before cancellation completed."}, run_id)
            self._journal(row, run_id, "cancel_call", {"attempt": attempt, "idempotency_key": row["idempotency_key"]})
            try:
                result = self._provider_call(
                    row, run_id, "cancel", {"booking_id": material["booking_id"], "idempotency_key": row["idempotency_key"]},
                    lambda: self.provider.cancel(material["booking_id"], row["idempotency_key"]),
                )
            except MockToolError as error:
                if error.retryable and attempt < max_attempts:
                    self.trace.emit(run_id, "retry", row["session_id"], operation="cancel", attempt=attempt + 1, idempotency_key=row["idempotency_key"], code=error.code)
                    continue
                return self._finish(row, "failed", {"status": error.code, "message": "Cancellation could not be completed after the allowed retries."}, run_id)
            self._transition_if_possible(row["task_id"], "VERIFYING", "cancel_verification_started", {"action_id": row["id"]}, run_id, row["session_id"])
            if not self._claim_execution_call(row, run_id, "list_bookings", {"purpose": "cancel_verification"}):
                return self._finish(row, "unverified", {"status": "unverified", "message": "The cancellation response was received, but the execution-call limit prevented status verification."}, run_id)
            try:
                bookings = self._provider_call(row, run_id, "list_bookings", {"user_id": row["user_id"]}, lambda: self.provider.list_bookings(row["user_id"]))
            except MockToolError:
                return self._finish(row, "unverified", {"status": "unverified", "message": "The cancellation response was received, but the provider status could not be verified. I won't claim it succeeded."}, run_id)
            verified = next((item for item in bookings if item["booking_id"] == material["booking_id"]), None)
            if verified and verified["status"] == "cancelled_by_user":
                return self._finish(row, "done", {"status": "cancelled", "message": "Cancellation confirmed.", "booking": verified}, run_id, verified=verified)
            if result.get("status") == "cancelled":
                return self._finish(row, "unverified", {"status": "unverified", "message": "The provider returned a cancellation response, but I could not verify it."}, run_id)
        return self._finish(row, "failed", {"status": "retries_exhausted", "message": "Cancellation did not complete."}, run_id)

    def _execute_reschedule(self, row: Any, material: dict[str, Any], run_id: str) -> dict[str, Any]:
        max_attempts = min(self.config.max_retries + 1, self.config.max_execution_tool_calls)
        for attempt in range(1, max_attempts + 1):
            if not self._claim_execution_call(row, run_id, "reschedule", {"attempt": attempt}):
                return self._finish(row, "failed", {"status": "execution_limit", "message": "The local execution-call limit was reached before rescheduling completed."}, run_id)
            self._journal(row, run_id, "reschedule_call", {"attempt": attempt, "idempotency_key": row["idempotency_key"], "slot": material["new_slot"]})
            try:
                result = self._provider_call(
                    row, run_id, "reschedule", {"booking_id": material["booking_id"], "slot": material["new_slot"], "idempotency_key": row["idempotency_key"]},
                    lambda: self.provider.reschedule(material["booking_id"], material["new_slot"], row["idempotency_key"]),
                )
            except MockToolError as error:
                if error.retryable and attempt < max_attempts:
                    self.trace.emit(run_id, "retry", row["session_id"], operation="reschedule", attempt=attempt + 1, idempotency_key=row["idempotency_key"], code=error.code)
                    continue
                return self._finish(row, "failed", {"status": error.code, "message": "Rescheduling could not be completed."}, run_id)
            self._transition_if_possible(row["task_id"], "VERIFYING", "reschedule_verification_started", {"action_id": row["id"]}, run_id, row["session_id"])
            if result.get("status") == "slot_conflict":
                self._transition_if_possible(row["task_id"], "CONFLICT", "reschedule_conflict", {"booking_id": material["booking_id"], "slot": material["new_slot"]}, run_id, row["session_id"])
                return self._finish(row, "failed", {"status": "slot_conflict", "message": "That replacement slot conflicted. Choose a different slot and approve it."}, run_id)
            if not self._claim_execution_call(row, run_id, "list_bookings", {"purpose": "reschedule_verification"}):
                return self._finish(row, "unverified", {"status": "unverified", "message": "The reschedule response was received, but the execution-call limit prevented status verification."}, run_id)
            try:
                bookings = self._provider_call(row, run_id, "list_bookings", {"user_id": row["user_id"]}, lambda: self.provider.list_bookings(row["user_id"]))
            except MockToolError:
                return self._finish(row, "unverified", {"status": "unverified", "message": "The reschedule response was received, but the provider status could not be verified. I won't claim it succeeded."}, run_id)
            verified = next((item for item in bookings if item["booking_id"] == material["booking_id"]), None)
            if verified and verified["slot"] == material["new_slot"]:
                return self._finish(row, "done", {"status": "rescheduled", "message": "Reschedule confirmed.", "booking": verified}, run_id, verified=verified)
            return self._finish(row, "unverified", {"status": "unverified", "message": "I could not verify the new slot, so I have not claimed a reschedule."}, run_id)
        return self._finish(row, "failed", {"status": "retries_exhausted", "message": "Rescheduling did not complete."}, run_id)

    def _journal(self, row: Any, run_id: str, event: str, details: dict[str, Any]) -> None:
        state_row = self.store.connection.execute("SELECT state FROM tasks WHERE id=?", (row["task_id"],)).fetchone()
        state = state_row["state"] if state_row else "UNKNOWN"
        with self.store.transaction() as db:
            db.execute(
                "INSERT INTO task_events(task_id,old_state,new_state,event,details_json,created_at) VALUES(?,?,?,?,?,?)",
                (row["task_id"], state, state, event, json_dumps(details), self.clock.epoch()),
            )
        self.trace.emit(run_id, "execution_journal", row["session_id"], action_id=row["id"], journal_event=event, details=details)

    def _provider_call(self, row: Any, run_id: str, tool: str, arguments: dict[str, Any], operation: Any) -> Any:
        deadline = self._run_deadlines.get(run_id)
        if deadline is not None and time.monotonic() >= deadline:
            raise MockToolError("limit_wall_clock", "The local action time limit was reached.")
        self.trace.emit(run_id, "tool_call", row["session_id"], tool=tool, arguments=arguments, action_id=row["id"])
        started = time.monotonic()
        try:
            result = operation()
        except MockToolError as error:
            self.trace.emit(
                run_id, "tool_error", row["session_id"], tool=tool, code=error.code,
                retryable=error.retryable, ambiguous=error.ambiguous,
                latency_ms=int((time.monotonic() - started) * 1000), action_id=row["id"],
            )
            raise
        self.trace.emit(
            run_id, "tool_result", row["session_id"], tool=tool,
            result=result, latency_ms=int((time.monotonic() - started) * 1000), action_id=row["id"],
        )
        return result

    def _claim_execution_call(self, row: Any, run_id: str, operation: str, details: dict[str, Any]) -> bool:
        with self.store.transaction() as db:
            updated = db.execute(
                "UPDATE pending_actions SET execution_calls=execution_calls+1 WHERE id=? AND execution_calls<?",
                (row["id"], self.config.max_execution_tool_calls),
            )
            count_row = db.execute("SELECT execution_calls FROM pending_actions WHERE id=?", (row["id"],)).fetchone()
            count = int(count_row["execution_calls"]) if count_row else self.config.max_execution_tool_calls
        allowed = updated.rowcount == 1
        self.trace.emit(
            run_id, "execution_limit", row["session_id"], action_id=row["id"], operation=operation,
            allowed=allowed, call_count=count, limit=self.config.max_execution_tool_calls, details=details,
        )
        return allowed

    def _mark_action(self, action_id: str, status: str) -> None:
        with self.store.transaction() as db:
            db.execute("UPDATE pending_actions SET status=? WHERE id=? AND status IN ('executing','verifying','unverified')", (status, action_id))

    def _finish(
        self, row: Any, status: str, response: dict[str, Any], run_id: str,
        verified: dict[str, Any] | None = None, task_state: str | None = None,
    ) -> dict[str, Any]:
        now = self.clock.epoch()
        with self.store.transaction() as db:
            db.execute("UPDATE pending_actions SET status=?,response_json=? WHERE id=?", (status, json_dumps(response), row["id"]))
            if verified and response.get("status") in {"confirmed", "cancelled", "rescheduled"}:
                booking_id = verified.get("booking_id") or json_loads(row["material_json"], {}).get("booking_id")
                material = json_loads(row["material_json"], {})
                provider_id = verified.get("provider_id") or material.get("provider_id")
                service = verified.get("service") or material.get("service")
                slot = verified.get("slot") or material.get("new_slot") or material.get("slot")
                cache_status = "confirmed" if response["status"] == "confirmed" else "cancelled_by_user" if response["status"] == "cancelled" else "confirmed"
                if booking_id and provider_id and service and slot:
                    db.execute(
                        "INSERT INTO bookings_cache(booking_id,user_id,provider_id,service,slot,status,updated_at) VALUES(?,?,?,?,?,?,?) "
                        "ON CONFLICT(booking_id) DO UPDATE SET provider_id=excluded.provider_id,service=excluded.service,slot=excluded.slot,status=excluded.status,updated_at=excluded.updated_at",
                        (booking_id, row["user_id"], provider_id, service, slot, cache_status, now),
                    )
        target = task_state or ("CONFIRMED" if status == "done" and row["kind"] == "book" else "DONE" if status == "done" else "UNVERIFIED" if status == "unverified" else "FAILED")
        self._transition_if_possible(row["task_id"], target, f"action_{status}", {"action_id": row["id"], "status": response.get("status"), "verified": verified is not None}, run_id, row["session_id"])
        self.trace.emit(run_id, "execution_result", row["session_id"], action_id=row["id"], action_status=status, outcome=response.get("status"), verified=verified is not None, response=response)
        return {**response, "action_id": row["id"], "action_status": status}

    def _action_row(self, action_id: str) -> Any:
        row = self.store.connection.execute("SELECT * FROM pending_actions WHERE id=?", (action_id,)).fetchone()
        if not row:
            raise ApprovalError("action_missing", "Approval action was not found.")
        return row

    def recover(self, run_id: str) -> list[dict[str, Any]]:
        rows = self.store.connection.execute(
            "SELECT id FROM pending_actions WHERE status IN ('approved','executing','verifying','unverified') ORDER BY created_at"
        ).fetchall()
        recovered: list[dict[str, Any]] = []
        for item in rows:
            row = self._action_row(item["id"])
            if row["kind"] == "book" and row["status"] in {"executing", "verifying", "unverified"}:
                material = json_loads(row["material_json"], {})
                self._transition_if_possible(row["task_id"], "VERIFYING", "restart_verification_started", {"action_id": row["id"]}, run_id, row["session_id"])
                try:
                    verification = self._verify_book(row, material, run_id)
                    if verification.get("status") == "booked":
                        result = self._finish(row, "done", {"status": "confirmed", "message": "Booking verified after restart.", "booking": verification}, run_id, verified=verification)
                    elif verification.get("status") == "not_booked":
                        if self.clock.epoch() < float(material.get("hold_expires_at", 0)):
                            self._mark_action(row["id"], "approved")
                            result = self.execute(row["id"], run_id, recovery=True)
                        else:
                            result = self._finish(row, "failed", {"status": "hold_expired", "message": "The provider verified that no booking was made and the hold has expired. Choose a slot for a fresh quote."}, run_id, task_state="OPTIONS_PRESENTED")
                    elif verification.get("status") == "mismatch":
                        result = self._finish(row, "failed", {"status": "verification_mismatch", "message": "The provider status did not match the approved details. I have not reported this as a successful booking."}, run_id, verified=verification)
                    else:
                        result = self._finish(row, "unverified", {"status": "unverified", "message": "Booking status is still unavailable. I have not retried the booking or claimed success; it will be checked again."}, run_id)
                except Exception as error:
                    self.trace.emit(run_id, "recovery_error", row["session_id"], action_id=row["id"], code=getattr(error, "code", "recovery_error"))
                    result = self._finish(row, "unverified", {"status": "unverified", "message": "I could not safely recover this booking action. No duplicate request was sent; status will be checked again."}, run_id)
                recovered.append(result)
            else:
                try:
                    recovered.append(self.execute(row["id"], run_id, recovery=True))
                except Exception as error:
                    self.trace.emit(run_id, "recovery_error", row["session_id"], action_id=row["id"], code=getattr(error, "code", "recovery_error"))
                    recovered.append(self._finish(row, "unverified", {"status": "unverified", "message": "The action may have reached the provider, but its result could not be verified yet. It will be checked again before I report success."}, run_id))
        return recovered


def approval_card_text(card: dict[str, Any]) -> str:
    if card["kind"] == "book":
        return (
            f"Approval: Book {card['service']} with {card['provider_name']} at {card['slot']} for ₹{card['quoted_price_inr']} "
            f"(search price ₹{card['search_price_inr']}). Approval code {card['approval_code']}."
        )
    if card["kind"] == "cancel":
        return f"Approval: Cancel booking {card['booking_id']} with provider {card['provider_id']} at {card['slot']}. Code {card['approval_code']}."
    return f"Approval: Reschedule booking {card['booking_id']} to {card['slot']}. Code {card['approval_code']}."
