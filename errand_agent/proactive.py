"""Provider-cancellation detector and approval-first replacement flow."""

from __future__ import annotations

import hashlib
import threading
from datetime import date, datetime, timedelta
from typing import Any

from .agent import ErrandAgent
from .intent import IntentState
from .mock_provider import MockToolError
from .storage import json_dumps, json_loads
from .approvals import ApprovalError, approval_card_text
from .budget import estimate_tokens


class ProactiveMonitor:
    def __init__(self, agent: ErrandAgent) -> None:
        self.agent = agent
        self._lock = threading.Lock()

    def tick(self, user_id: str = "u_demo") -> list[dict[str, Any]]:
        with self._lock:
            return self._tick(user_id)

    def _tick(self, user_id: str) -> list[dict[str, Any]]:
        cancellations = self.agent.provider.list_provider_cancellations(user_id)
        if not cancellations:
            return []
        event_rows = {
            row["event_key"]: row for row in self.agent.store.connection.execute(
                "SELECT event_key,status,attempts,next_attempt_at FROM processed_provider_events"
            ).fetchall()
        }
        cancellations = [
            item for item in cancellations
            if (row := event_rows.get(f"{item['booking_id']}:cancelled_by_provider")) is None
            or row["status"] not in {"processed", "failed"}
            and float(row["next_attempt_at"] or 0) <= self.agent.clock.epoch()
        ]
        if not cancellations:
            return []
        run_id = self.agent.trace.new_run_id()
        self.agent.trace.emit(run_id, "run_start", user_id=user_id, monitor="provider_cancellation")
        self.agent.trace.emit(
            run_id, "provider_event_detected", user_id=user_id, event_type="provider_cancellation",
            booking_ids=[item["booking_id"] for item in cancellations],
        )
        session_row = self.agent.store.connection.execute(
            "SELECT id FROM sessions WHERE user_id=? ORDER BY last_seen DESC LIMIT 1", (user_id,)
        ).fetchone()
        session_id = session_row["id"] if session_row else None
        self.agent.gateway.reset_run(run_id)
        try:
            bookings = self.agent.gateway.call(run_id, session_id or "proactive", user_id, "list_bookings", {}, IntentState(), self.agent.config.max_tool_calls)
        except Exception as error:
            self.agent.trace.emit(run_id, "proactive_error", session_id, code=getattr(error, "code", "tool_error"))
            self.agent.trace.emit(run_id, "run_end", session_id, exit_reason="tool_error", recovered_events=0)
            return []
        outcomes: list[dict[str, Any]] = []
        cancellation_ids = {item["booking_id"] for item in cancellations}
        for booking in bookings:
            with self.agent.store.transaction() as db:
                db.execute(
                    "INSERT INTO bookings_cache(booking_id,user_id,provider_id,service,slot,status,updated_at) VALUES(?,?,?,?,?,?,?) "
                    "ON CONFLICT(booking_id) DO UPDATE SET provider_id=excluded.provider_id,service=excluded.service,slot=excluded.slot,status=excluded.status,updated_at=excluded.updated_at",
                    (booking["booking_id"], user_id, booking["provider_id"], booking["service"], booking["slot"], booking["status"], self.agent.clock.epoch()),
                )
            if booking["booking_id"] not in cancellation_ids or booking["status"] != "cancelled_by_provider":
                continue
            event_key = f"{booking['booking_id']}:cancelled_by_provider"
            existing = self.agent.store.connection.execute("SELECT status,attempts,next_attempt_at FROM processed_provider_events WHERE event_key=?", (event_key,)).fetchone()
            if existing and (existing["status"] in {"processed", "failed"} or float(existing["next_attempt_at"] or 0) > self.agent.clock.epoch()):
                continue
            if not existing:
                with self.agent.store.transaction() as db:
                    db.execute("INSERT OR IGNORE INTO processed_provider_events(event_key,booking_id,status,result_json,created_at) VALUES(?,?,'processing','{}',?)", (event_key, booking["booking_id"], self.agent.clock.epoch()))
                    db.execute("UPDATE processed_provider_events SET status='processing',attempts=attempts+1 WHERE event_key=?", (event_key,))
            else:
                with self.agent.store.transaction() as db:
                    db.execute("UPDATE processed_provider_events SET status='processing',attempts=attempts+1 WHERE event_key=?", (event_key,))
            try:
                result = self._process_event(run_id, session_id, user_id, booking, event_key)
                with self.agent.store.transaction() as db:
                    db.execute("UPDATE processed_provider_events SET status='processed',result_json=? WHERE event_key=?", (json_dumps(result), event_key))
                outcomes.append(result)
            except Exception as error:
                row = self.agent.store.connection.execute("SELECT attempts FROM processed_provider_events WHERE event_key=?", (event_key,)).fetchone()
                attempts = int(row["attempts"] if row else 1)
                terminal = attempts >= 3
                delay = min(60, self.agent.config.proactive_poll_seconds * (2 ** max(0, attempts - 1)))
                error_code = getattr(error, "code", "processing_error")
                with self.agent.store.transaction() as db:
                    db.execute(
                        "UPDATE processed_provider_events SET status=?,result_json=?,next_attempt_at=?,last_error=? WHERE event_key=?",
                        ("failed" if terminal else "pending", json_dumps({"error": error_code, "attempts": attempts}),
                         self.agent.clock.epoch() + delay if not terminal else 0, error_code, event_key),
                    )
                self.agent.trace.emit(
                    run_id, "proactive_error", session_id, event_key=event_key, code=error_code,
                    attempt=attempts, retry_scheduled=not terminal, delay_seconds=delay if not terminal else 0,
                )
                if terminal:
                    error_outbox_id = "out_error_" + hashlib.sha256(event_key.encode()).hexdigest()[:20]
                    failure = "I detected the provider cancellation, but replacement search failed three times. No replacement was booked. Please open the booking list and choose how to proceed."
                    with self.agent.store.transaction() as db:
                        db.execute(
                            "INSERT OR IGNORE INTO outbox(id,user_id,session_id,kind,content,status,created_at) VALUES(?,?,?,'provider_cancellation_error',?,'pending',?)",
                            (error_outbox_id, user_id, session_id, failure, self.agent.clock.epoch()),
                        )
        self.agent.trace.emit(run_id, "run_end", session_id, exit_reason="completed", detected_events=len(outcomes))
        return outcomes

    def _process_event(self, run_id: str, session_id: str | None, user_id: str, booking: dict[str, Any], event_key: str) -> dict[str, Any]:
        notification = f"Your {booking['service'].replace('_', ' ')} provider cancelled booking {booking['booking_id']}. I’m checking replacement options; I won’t book one without your approval."
        outbox_id = "out_" + hashlib.sha256(event_key.encode()).hexdigest()[:24]
        with self.agent.store.transaction() as db:
            db.execute(
                "INSERT OR IGNORE INTO outbox(id,user_id,session_id,kind,content,status,created_at) VALUES(?,?,?,'provider_cancellation',?,'pending',?)",
                (outbox_id, user_id, session_id, notification, self.agent.clock.epoch()),
            )
        try:
            original = self.agent.gateway.call(
                run_id, session_id or "proactive", user_id, "get_provider", {"provider_id": booking["provider_id"]},
                IntentState(service=booking["service"], provider_ids=[booking["provider_id"]]), self.agent.config.max_tool_calls,
            )
        except MockToolError:
            raise
        original_area = original["area"]
        original_price = int(original["price_inr"])
        slot_date = date.fromisoformat(booking["slot"][:10])
        today = self.agent.clock.now_ist().date()
        start = max(today, slot_date - timedelta(days=2))
        end = min(today + timedelta(days=14), slot_date + timedelta(days=2))
        if end < start:
            end = start
        intent = IntentState(
            action="book", service=booking["service"],
            date_start=start.isoformat(), date_end=end.isoformat(),
        )
        intent, memory_refs = self.agent.memories.apply_to_intent(user_id, intent)
        if not intent.area:
            intent.area = original_area
            intent.provenance["area"] = {
                "source": "derived:cancelled_booking.provider_area", "kind": "derived", "confirmed": True,
            }
        original_limit = int(original_price * 1.15)
        intent.budget_max_inr = min(intent.budget_max_inr, original_limit) if intent.budget_max_inr is not None else original_limit
        intent.no_budget = False
        args = {"service": intent.service, "area": intent.area, "date_range": {"start": intent.date_start, "end": intent.date_end}}
        provider_results = self.agent.gateway.call(run_id, session_id or "proactive", user_id, "search_providers", args, intent, self.agent.config.max_tool_calls)
        candidates = self.agent._filter_options(provider_results, intent)
        candidates = [candidate for candidate in candidates if candidate["provider_id"] != booking["provider_id"]]
        candidates.sort(key=lambda item: (item["area_match"] != "exact", -item["rating"], item["price_inr"], item["provider_id"]))
        if not candidates:
            message = "I couldn't find a replacement that fits your saved service preferences and the original booking's budget. Tell me which preference you want to relax."
            if session_id:
                self._deliver_in_app(outbox_id, event_key, session_id, [notification + " " + message])
            else:
                self._enqueue_followup(outbox_id, message, user_id, None)
            self.agent.trace.emit(run_id, "proactive_replacement", session_id, booking_id=booking["booking_id"], result="no_match", preferences_used=memory_refs)
            return {"event_key": event_key, "status": "no_match", "booking_id": booking["booking_id"], "notification": notification, "replacement_options": []}

        active_session = self._active_session(user_id)
        approval: dict[str, Any] | None = None
        if active_session:
            session_id = active_session
            task_id = "task_proactive_" + hashlib.sha256(event_key.encode()).hexdigest()[:24]
            existing_task = self.agent.store.connection.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
            if existing_task:
                prior_action = self.agent.store.connection.execute(
                    "SELECT * FROM pending_actions WHERE task_id=? AND status IN ('proposed','approved','executing','verifying','unverified') ORDER BY created_at DESC LIMIT 1", (task_id,)
                ).fetchone()
                if prior_action:
                    approval = {"action_id": prior_action["id"], "fingerprint": prior_action["fingerprint"], "code": prior_action["approval_code"], "material": json_loads(prior_action["material_json"], {}), "expires_at": prior_action["expires_at"]}
            else:
                intent.presented_options = candidates
                intent.provider_ids = [item["provider_id"] for item in candidates]
                with self.agent.store.transaction() as db:
                    db.execute(
                        "INSERT INTO tasks(id,session_id,user_id,state,intent_json,version,updated_at) VALUES(?,?,?,'OPTIONS_PRESENTED',?,0,?)",
                        (task_id, session_id, user_id, intent.to_json(), self.agent.clock.epoch()),
                    )
                    db.execute(
                        "INSERT INTO task_events(task_id,old_state,new_state,event,details_json,created_at) VALUES(?,'NEW','OPTIONS_PRESENTED','provider_cancellation_detected',?,?)",
                        (task_id, json_dumps({"booking_id": booking["booking_id"], "event_key": event_key}), self.agent.clock.epoch()),
                    )
                first = candidates[0]
                try:
                    action = self.agent.approvals.create_booking_approval(task_id, session_id, user_id, intent, first["provider_id"], first["available_slots"][0], run_id)
                    approval = {**action, "text": approval_card_text(action["card"])}
                except ApprovalError as error:
                    self.agent.trace.emit(run_id, "proactive_approval_unavailable", session_id, event_key=event_key, reason=error.code)
            if session_id:
                messages = [notification]
                if approval:
                    approval_text = approval.get("text") or approval_card_text(self.agent.approvals._card(approval["action_id"], "book", approval["material"], approval["code"], approval["expires_at"]))
                    messages.append(approval_text)
                else:
                    messages.append("Replacement options: " + json_dumps([
                        {key: candidate[key] for key in ("provider_id", "name", "service", "area", "price_inr", "rating", "available_slots")}
                        for candidate in candidates
                    ]))
                self._deliver_in_app(outbox_id, event_key, session_id, messages)
        else:
            options = [{key: candidate[key] for key in ("provider_id", "name", "service", "area", "price_inr", "rating", "available_slots", "attributes")} for candidate in candidates]
            self._enqueue_followup(outbox_id, notification + " Replacement options: " + json_dumps(options), user_id, None)
        self.agent.trace.emit(
            run_id, "proactive_replacement", session_id, booking_id=booking["booking_id"], result="approval_presented" if approval else "options_ready",
            provider_ids=[item["provider_id"] for item in candidates], approved=False, preferences_used=memory_refs,
        )
        return {
            "event_key": event_key, "status": "approval_presented" if approval else "options_ready",
            "booking_id": booking["booking_id"], "notification": notification,
            "replacement_options": candidates, "approval": approval,
        }

    def _active_session(self, user_id: str) -> str | None:
        row = self.agent.store.connection.execute(
            "SELECT id FROM sessions WHERE user_id=? AND last_seen>=? ORDER BY last_seen DESC LIMIT 1",
            (user_id, self.agent.clock.epoch() - 300),
        ).fetchone()
        return row["id"] if row else None

    def _enqueue_followup(self, outbox_id: str, content: str, user_id: str, session_id: str | None) -> None:
        with self.agent.store.transaction() as db:
            db.execute("UPDATE outbox SET content=?,session_id=COALESCE(session_id,?) WHERE id=?", (content, session_id, outbox_id))

    def _deliver_in_app(self, outbox_id: str, event_key: str, session_id: str, messages: list[str]) -> None:
        """Atomically persist stable-ID messages and mark the notification delivered."""
        base = "msg_proactive_" + hashlib.sha256(event_key.encode()).hexdigest()[:24]
        with self.agent.store.transaction() as db:
            row = db.execute("SELECT max(created_at) FROM messages WHERE session_id=?", (session_id,)).fetchone()
            created_at = max(self.agent.clock.epoch(), float(row[0] or 0) + 0.001)
            for index, content in enumerate(messages):
                safe = self.agent.redactor.redact(content)
                cursor = db.execute(
                    "INSERT OR IGNORE INTO messages(id,session_id,role,content,created_at,token_est,is_seed,security_labels_json,memory_refs_json) VALUES(?,?,'assistant',?,?,?,0,'[]','[]')",
                    (f"{base}_{index}", session_id, safe, created_at, estimate_tokens(safe)),
                )
                if cursor.rowcount:
                    db.execute("UPDATE sessions SET last_seen=? WHERE id=?", (created_at, session_id))
                    created_at += 0.001
            db.execute("UPDATE outbox SET status='delivered',session_id=? WHERE id=?", (session_id, outbox_id))
