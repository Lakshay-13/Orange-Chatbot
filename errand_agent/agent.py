"""Conversation orchestration and deterministic authorization around model proposals."""

from __future__ import annotations

import json
import re
import time
from datetime import date, datetime
from typing import Any

from .approvals import TRANSITIONS, ActionApprovalService, ApprovalError, approval_card_text
from .budget import BudgetExceeded, BudgetLedger
from .clock import Clock
from .config import AppConfig
from .gateway import ToolDenied, ToolGateway
from .gemini import GeminiInteractionsClient
from .ids import new_id
from .intent import SERVICE_TERMS, IntentState, merge_intent, parse_user_message, sanitize_model_patch
from .memory import ContextBuilder, MemoryStore
from .mock_provider import MockProvider, MockToolError
from .model_router import PrimaryFallbackModel
from .redact import Redactor, contains_secret_pattern
from .responses import ModelError, OpenAIResponsesClient, ScriptedModel, SYSTEM_INSTRUCTIONS, build_openai_payload
from .security import Classification, classify_user
from .storage import AgentStore, json_dumps, json_loads
from .trace import TraceRecorder


TERMINAL_TASK_STATES = {"CONFIRMED", "DONE", "FAILED", "ABANDONED"}
AFFIRMATIONS = {"yes", "yeah", "yep", "confirm", "approve", "go ahead", "book it", "haan", "theek hai"}


class ErrandAgent:
    def __init__(
        self,
        config: AppConfig,
        store: AgentStore,
        provider: MockProvider,
        clock: Clock | None = None,
        model: Any | None = None,
        trace: TraceRecorder | None = None,
    ) -> None:
        self.config = config
        self.store = store
        self.provider = provider
        self.clock = clock or Clock()
        self.redactor = Redactor(config.openai_api_key, config.gemini_api_key)
        self.trace = trace or TraceRecorder(store, config.trace_dir, self.clock, self.redactor)
        self.budget = BudgetLedger(store, config)
        self.memories = MemoryStore(store, self.clock, self.redactor)
        self.context = ContextBuilder(store, self.memories, config, self.redactor)
        self.gateway = ToolGateway(provider, self.trace, self.clock, config.max_retries)
        self.approvals = ActionApprovalService(store, provider, self.trace, config, self.clock)
        if model is not None:
            self.model = model
        elif config.llm_mode == "scripted":
            self.model = ScriptedModel()
        elif config.llm_provider == "gemini":
            primary = GeminiInteractionsClient(config, self.budget)
            fallback = None
            if config.fallback_model_ready:
                fallback_config = config.for_provider("openai")
                fallback = OpenAIResponsesClient(fallback_config, BudgetLedger(store, fallback_config))
            self.model = PrimaryFallbackModel(primary, fallback, "gemini", "openai" if fallback else None)
        else:
            self.model = OpenAIResponsesClient(config, self.budget)
        self.memories.seed_demo("u_demo")

    def create_session(self, user_id: str = "u_demo") -> str:
        session_id = new_id("session")
        now = self.clock.epoch()
        with self.store.transaction() as db:
            db.execute("INSERT INTO sessions(id,user_id,created_at,last_seen) VALUES(?,?,?,?)", (session_id, user_id, now, now))
        return session_id

    def ensure_session(self, session_id: str | None, user_id: str = "u_demo") -> str:
        if session_id:
            row = self.store.connection.execute("SELECT user_id FROM sessions WHERE id=?", (session_id,)).fetchone()
            if row and row["user_id"] == user_id:
                return session_id
        return self.create_session(user_id)

    def _next_timestamp(self, session_id: str) -> float:
        row = self.store.connection.execute("SELECT max(created_at) FROM messages WHERE session_id=?", (session_id,)).fetchone()
        previous = float(row[0]) if row and row[0] is not None else 0.0
        return max(self.clock.epoch(), previous + 0.001)

    def _insert_message(
        self,
        session_id: str,
        role: str,
        content: str,
        labels: tuple[str, ...] = (),
        memory_refs: list[str] | None = None,
    ) -> str:
        msg_id = new_id("msg")
        safe_content = self.redactor.redact(content)
        created = self._next_timestamp(session_id)
        with self.store.transaction() as db:
            db.execute(
                "INSERT INTO messages(id,session_id,role,content,created_at,token_est,is_seed,security_labels_json,memory_refs_json) VALUES(?,?,?,?,?,?,0,?,?)",
                (msg_id, session_id, role, safe_content, created, max(1, (len(safe_content.encode()) + 2) // 3), json_dumps(list(labels)), json_dumps(memory_refs or [])),
            )
            db.execute("UPDATE sessions SET last_seen=? WHERE id=?", (created, session_id))
        return msg_id

    def _current_task(self, session_id: str, user_id: str) -> Any:
        row = self.store.connection.execute(
            "SELECT * FROM tasks WHERE session_id=? AND id NOT LIKE 'task_proactive_%' AND state NOT IN ('CONFIRMED','DONE','FAILED','ABANDONED','UNVERIFIED') ORDER BY updated_at DESC LIMIT 1",
            (session_id,),
        ).fetchone()
        if row:
            return row
        task_id = new_id("task")
        intent = IntentState()
        now = self.clock.epoch()
        with self.store.transaction() as db:
            db.execute(
                "INSERT INTO tasks(id,session_id,user_id,state,intent_json,version,updated_at) VALUES(?,?,?,'NEW',?,0,?)",
                (task_id, session_id, user_id, intent.to_json(), now),
            )
        self.approvals.transition(task_id, "GATHERING", "task_started", {}, session_id=session_id)
        return self.store.connection.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()

    def _save_intent(self, task_id: str, intent: IntentState) -> None:
        with self.store.transaction() as db:
            db.execute("UPDATE tasks SET intent_json=?,version=version+1,updated_at=? WHERE id=?", (intent.to_json(), self.clock.epoch(), task_id))

    def _previous_user_texts(self, session_id: str, limit: int = 6) -> tuple[str, ...]:
        rows = self.store.connection.execute(
            "SELECT content FROM messages WHERE session_id=? AND role='user' ORDER BY created_at DESC LIMIT ?", (session_id, limit)
        ).fetchall()
        return tuple(row["content"] for row in reversed(rows))

    def _finish_response(self, run_id: str, session_id: str, user_id: str, client_message_id: str, response: dict[str, Any], reply: str, labels: tuple[str, ...] = (), memory_refs: list[str] | None = None) -> dict[str, Any]:
        response["message"] = reply
        response["run_id"] = run_id
        response.setdefault("model_calls", 0)
        response.setdefault("tool_calls", self.gateway._run_counts.get(run_id, 0))
        response["budget"] = self.budget.summary()
        self._insert_message(session_id, "assistant", reply, labels, memory_refs)
        self.trace.emit(
            run_id, "run_end", session_id, exit_reason=response.get("exit_reason", "completed"),
            user_facing_outcome=reply, model_calls=response.get("model_calls", 0),
            tool_calls=response.get("tool_calls", 0), budget=response["budget"],
        )
        with self.store.transaction() as db:
            db.execute(
                "INSERT OR REPLACE INTO processed_client_messages(client_message_id,session_id,response_json,created_at) VALUES(?,?,?,?)",
                (client_message_id, session_id, json_dumps(response), self.clock.epoch()),
            )
        return response

    def handle(self, session_id: str, text: str, client_message_id: str | None = None) -> dict[str, Any]:
        message_id = client_message_id or new_id("client")
        try:
            return self._handle(session_id, text, message_id)
        except Exception as error:
            session = self.store.connection.execute("SELECT user_id FROM sessions WHERE id=?", (session_id,)).fetchone()
            if not session:
                raise
            prior = self.store.connection.execute(
                "SELECT response_json FROM processed_client_messages WHERE client_message_id=? AND session_id=?",
                (message_id, session_id),
            ).fetchone()
            if prior:
                return json_loads(prior["response_json"], {})
            runs = self.store.connection.execute(
                "SELECT run_id,data_json FROM trace_events WHERE session_id=? AND event='run_start' ORDER BY id DESC LIMIT 20",
                (session_id,),
            ).fetchall()
            run_id = None
            for candidate in runs:
                if json_loads(candidate["data_json"], {}).get("client_message_id") == message_id:
                    run_id = candidate["run_id"]
                    break
            reply = "I couldn't complete this step safely. No success is being claimed. Check the current booking status before repeating a booking request."
            response = {
                "exit_reason": "internal_error", "message": reply, "run_id": run_id,
                "model_calls": 0, "tool_calls": 0, "budget": self.budget.summary(),
            }
            self._insert_message(session_id, "assistant", reply)
            with self.store.transaction() as db:
                db.execute(
                    "INSERT OR REPLACE INTO processed_client_messages(client_message_id,session_id,response_json,created_at) VALUES(?,?,?,?)",
                    (message_id, session_id, json_dumps(response), self.clock.epoch()),
                )
            if run_id:
                self.trace.emit(run_id, "run_error", session_id, error_type=type(error).__name__)
                self.trace.emit(run_id, "run_end", session_id, exit_reason="internal_error", user_facing_outcome=reply)
            return response

    def _handle(self, session_id: str, text: str, client_message_id: str) -> dict[str, Any]:
        session_row = self.store.connection.execute("SELECT user_id FROM sessions WHERE id=?", (session_id,)).fetchone()
        if not session_row:
            raise ValueError("Session is invalid; reload the local app to start a new one.")
        user_id = session_row["user_id"]
        message_id = client_message_id
        prior = self.store.connection.execute("SELECT response_json FROM processed_client_messages WHERE client_message_id=? AND session_id=?", (message_id, session_id)).fetchone()
        if prior:
            return json_loads(prior["response_json"], {})
        run_id = self.trace.new_run_id()
        started = time.monotonic()
        deadline = started + self.config.max_wall_seconds
        self.gateway.reset_run(run_id)
        self.trace.emit(run_id, "run_start", session_id, user_id=user_id, client_message_id=message_id, input_chars=len(text), limits={"wall_seconds": self.config.max_wall_seconds, "model_turns": self.config.max_model_turns, "tool_calls": self.config.max_tool_calls})
        pending_recovery = self.store.connection.execute(
            "SELECT 1 FROM pending_actions WHERE user_id=? AND status IN ('approved','executing','verifying','unverified') LIMIT 1",
            (user_id,),
        ).fetchone()
        if pending_recovery:
            recovered = self.approvals.recover(run_id)
            self._publish_recovery_results(recovered)
        history = self._previous_user_texts(session_id)
        classification = classify_user(text, history=history, max_chars=self.config.max_user_chars)
        self.trace.emit(run_id, "security_classification", session_id, action=classification.action, labels=classification.labels, rule_ids=classification.rule_ids, transformed=classification.transformed)
        safe_text = classification.safe_text if classification.action == "flag" else classification.normalized_text
        safe_text = self.redactor.redact(safe_text)
        message_id_db = self._insert_message(session_id, "user", text, classification.labels)
        task_row = self._current_task(session_id, user_id)
        intent = IntentState.from_json(task_row["intent_json"])

        if classification.action == "block" or not safe_text.strip():
            reply = "I can help with home-service requests, but I can't reveal private instructions, credentials, or internal data."
            return self._finish_response(run_id, session_id, user_id, message_id, {"exit_reason": "blocked_by_security", "security_labels": list(classification.labels)}, reply, classification.labels)

        explicit_code = (
            re.fullmatch(r"(?i)confirm\s+([A-Z2-7]{6})[.! ]*", safe_text.strip())
            if classification.action == "allow" and not classification.labels else None
        )
        if explicit_code:
            pending = self._latest_proposed_action(session_id, user_id)
            if not pending or pending["approval_code"].casefold() != explicit_code.group(1).casefold():
                return self._finish_response(run_id, session_id, user_id, message_id, {"exit_reason": "approval_code_mismatch"}, "That confirmation code does not match a current approval card. No action was taken.")
            try:
                outcome = self.approvals.approve(pending["id"], user_id, session_id, pending["fingerprint"], message_id, run_id)
                reply = outcome.get("message", f"Action status: {outcome.get('status', 'unknown')}.")
                return self._finish_response(run_id, session_id, user_id, message_id, {"exit_reason": "completed", "action": outcome}, reply)
            except ApprovalError as error:
                return self._finish_response(run_id, session_id, user_id, message_id, {"exit_reason": error.code}, str(error))
        if safe_text.strip().casefold() in AFFIRMATIONS:
            return self._finish_response(run_id, session_id, user_id, message_id, {"exit_reason": "approval_requires_exact_confirmation"}, "A plain yes does not identify an action. Use the approve button on the exact card or `confirm CODE` from that card.")

        if classification.action == "flag" and not safe_text.strip():
            return self._finish_response(run_id, session_id, user_id, message_id, {"exit_reason": "blocked_by_security", "security_labels": list(classification.labels)}, "I can help with the home-service part of your request. Please restate that part without the unsafe instructions.", classification.labels)

        task_row = self.store.connection.execute("SELECT * FROM tasks WHERE id=?", (task_row["id"],)).fetchone()
        intent_before = IntentState.from_json(task_row["intent_json"])
        patch = parse_user_message(safe_text, self.clock, message_id_db)
        intent = merge_intent(intent_before, patch, self.clock)
        if intent.action == "book" or intent.action == "inquire":
            intent, memory_refs = self.memories.apply_to_intent(user_id, intent)
        else:
            memory_refs = []
        if memory_refs:
            with self.store.transaction() as db:
                db.execute("UPDATE messages SET memory_refs_json=? WHERE id=?", (json_dumps(memory_refs), message_id_db))
        if intent.service and intent.preferences.get("same_provider_as_last") and not intent.preferences.get("preferred_provider_id"):
            try:
                prior_bookings = self.gateway.call(
                    run_id, session_id, user_id, "list_bookings", {}, intent, self.config.max_tool_calls,
                )
            except (MockToolError, ToolDenied) as error:
                return self._finish_response(
                    run_id, session_id, user_id, message_id,
                    {"exit_reason": getattr(error, "code", "tool_error"), "tool_calls": self.gateway._run_counts.get(run_id, 0)},
                    "I couldn't verify which provider you used last time, so I haven't guessed. Please choose a provider or try again.",
                    classification.labels,
                )
            prior = [
                item for item in prior_bookings
                if item["service"] == intent.service and item["status"] in {"completed", "confirmed"}
                and datetime.fromisoformat(item["slot"]).timestamp() < self.clock.epoch()
            ]
            prior.sort(key=lambda item: item["slot"], reverse=True)
            if not prior:
                return self._finish_response(
                    run_id, session_id, user_id, message_id,
                    {"exit_reason": "prior_provider_not_found", "tool_calls": self.gateway._run_counts.get(run_id, 0)},
                    "I couldn't find a completed booking for that service to identify the previous provider. Which provider would you like to use?",
                    classification.labels,
                )
            intent.preferences["preferred_provider_id"] = prior[0]["provider_id"]
            intent.provenance["preferences.preferred_provider_id"] = {
                "source": "derived:list_bookings", "kind": "derived", "confirmed": True,
            }
            self._save_intent(task_row["id"], intent)
            self.trace.emit(
                run_id, "intent_refinement", session_id, field="preferred_provider_id",
                source="list_bookings", booking_id=prior[0]["booking_id"], provider_id=prior[0]["provider_id"],
            )
        material_changed = self._material_changed(intent_before, intent)
        if material_changed:
            self.approvals.invalidate_proposed(task_row["id"], "user_changed_material_details", run_id, session_id)
            if task_row["state"] != "GATHERING" and "GATHERING" in TRANSITIONS.get(task_row["state"], set()):
                self.approvals.transition(task_row["id"], "GATHERING", "user_changed_material_details", {}, run_id, session_id)
        self._save_intent(task_row["id"], intent)

        if self._is_memory_inspect(safe_text):
            reply, memory_payload = self._memory_listing(user_id)
            return self._finish_response(run_id, session_id, user_id, message_id, {"exit_reason": "completed", "memories": memory_payload}, reply)
        forget_target = self._memory_forget_target(user_id, safe_text)
        if safe_text.casefold().startswith("forget ") and not forget_target:
            return self._finish_response(
                run_id, session_id, user_id, message_id,
                {"exit_reason": "memory_target_ambiguous_or_missing"},
                "I couldn't identify one exact preference to forget. Open the Memory panel and use its item ID, or name the preference and scope clearly.",
            )
        if forget_target:
            try:
                self.memories.forget(user_id, forget_target["id"])
                return self._finish_response(run_id, session_id, user_id, message_id, {"exit_reason": "completed", "forgotten_memory_id": forget_target["id"]}, f"Forgot {forget_target['key']}. Future model context excludes messages tied to that preference.")
            except ValueError as error:
                return self._finish_response(run_id, session_id, user_id, message_id, {"exit_reason": "memory_error"}, str(error))

        memories_to_remember = self._memory_remember_target(safe_text)
        if safe_text.casefold().startswith("remember ") and not memories_to_remember:
            return self._finish_response(
                run_id, session_id, user_id, message_id,
                {"exit_reason": "memory_fact_unsupported"},
                "I couldn't map that preference to a supported memory field. Use the Memory panel to add or correct a listed preference.",
            )
        if memories_to_remember:
            try:
                memory_ids = [
                    self.memories.create(
                        user_id, item["key"], item["value"], kind="explicit",
                        scope=item.get("scope", "global"), evidence=[message_id_db],
                    )
                    for item in memories_to_remember
                ]
                with self.store.transaction() as db:
                    db.execute("UPDATE messages SET memory_refs_json=? WHERE id=?", (json_dumps(memory_ids), message_id_db))
                return self._finish_response(
                    run_id, session_id, user_id, message_id,
                    {"exit_reason": "completed", "saved_memory_ids": memory_ids},
                    "Saved that as an explicit preference. You can inspect, correct, or forget it in the Memory panel.",
                    memory_refs=memory_ids,
                )
            except ValueError as error:
                return self._finish_response(run_id, session_id, user_id, message_id, {"exit_reason": "memory_error"}, str(error))

        if self._is_bookings_request(safe_text):
            try:
                bookings = self.gateway.call(run_id, session_id, user_id, "list_bookings", {}, intent, self.config.max_tool_calls)
                reply = "You have no saved bookings." if not bookings else f"I found {len(bookings)} booking(s). Use the booking list to cancel or reschedule one with a fresh approval."
                return self._finish_response(run_id, session_id, user_id, message_id, {"exit_reason": "completed", "bookings": bookings, "tool_calls": 1}, reply)
            except (MockToolError, ToolDenied) as error:
                return self._finish_response(run_id, session_id, user_id, message_id, {"exit_reason": getattr(error, "code", "tool_error")}, "I couldn't retrieve bookings right now. Please try again.")

        if intent.action in {"cancel", "reschedule"}:
            return self._handle_booking_management(run_id, session_id, user_id, message_id, safe_text, intent, task_row["id"], deadline)

        if task_row["state"] == "AWAITING_APPROVAL" and not material_changed:
            return self._finish_response(run_id, session_id, user_id, message_id, {"exit_reason": "awaiting_approval"}, "Your current approval card is still active. Use its approve or reject button, or tell me what detail you want to change.")

        if self._wall_exhausted(deadline):
            return self._finish_response(run_id, session_id, user_id, message_id, {"exit_reason": "limit_wall_clock"}, "This request reached the local time limit before I could search. Please try again.")

        model_calls = 0
        observations: list[dict[str, Any]] = []
        labels = {"service": "which home service you need", "area": "which area to search", "date_range": "what day or date range works"}

        def fallback_callback_for(context: Any):
            def record_fallback(primary: str, fallback: str, code: str) -> None:
                fallback_config = self.config.for_provider(fallback)
                payload, _ = build_openai_payload(fallback_config, context.messages, safe_text)
                payload_hash, estimated = self.context.record_fallback_payload(
                    session_id, context.manifest["call_id"], fallback, payload,
                )
                self.trace.emit(
                    run_id, "model_fallback", session_id, primary_provider=primary,
                    fallback_provider=fallback, primary_error_code=code,
                    payload_sha256=payload_hash, estimated_input_tokens=estimated,
                )
            return record_fallback

        try:
            context = self.context.build(session_id, user_id, safe_text, intent, observations, exclude_message_id=message_id_db)
            self.trace.emit(run_id, "context_manifest", session_id, **context.manifest)
            if self.config.max_model_turns < 1:
                raise ModelError("limit_model_calls", "This run reached its model-turn limit.")
            model_calls += 1
            result = self.model.propose(
                context.messages, safe_text, max(0.1, deadline - time.monotonic()),
                on_retry=lambda attempt, code, delay: self.trace.emit(
                    run_id, "retry", session_id, operation="model", attempt=attempt,
                    provider_code=code, delay_seconds=delay,
                ),
                on_fallback=fallback_callback_for(context),
            )
            self.trace.emit(
                run_id, "model_call", session_id, model=result.model,
                provider=result.provider or self.config.llm_provider,
                fallback_from=result.fallback_from, fallback_reason=result.fallback_reason,
                effort=result.effort,
                input_tokens=result.input_tokens, output_tokens=result.output_tokens,
                thought_tokens=result.thought_tokens,
                cached_input_tokens=result.cached_input_tokens, estimated_cost_usd=result.estimated_cost_usd,
                latency_ms=result.latency_ms, request_id=result.request_id,
                proposal=result.proposal.get("proposal"), payload_sha256=context.payload_sha256,
            )
            if self._wall_exhausted(deadline):
                raise ModelError("limit_wall_clock", "The model turn exceeded the local execution time limit.")
            model_patch = sanitize_model_patch(result.proposal.get("intent_patch", {}), intent, safe_text, patch)
            if model_patch:
                model_patch["_source_id"] = message_id_db
                intent = merge_intent(intent, model_patch, self.clock)
                self._save_intent(task_row["id"], intent)
            next_step = result.proposal.get("proposal")
            missing = ["service"] if intent.action in {"inquire", "book"} and not intent.service else intent.missing_required()
            if missing:
                prompt = "I still need " + " and ".join(labels.get(field, field) for field in missing) + "."
                if memory_refs:
                    used = [self._memory_disclosure(memory_id) for memory_id in memory_refs]
                    used = [item for item in used if item]
                    if used:
                        prompt += " I used your saved preferences: " + ", ".join(used) + "."
                self._record_assistant_transition(session_id, task_row["id"], "GATHERING", "missing_information", run_id)
                return self._finish_response(run_id, session_id, user_id, message_id, {"exit_reason": "asked_user", "missing_fields": missing, "model_calls": model_calls}, prompt, classification.labels, memory_refs)
            if next_step in {"ask_user", "respond"}:
                reply = self._model_safe_reply(result.proposal.get("reply", ""), "What else would you like help with?")
                return self._finish_response(run_id, session_id, user_id, message_id, {"exit_reason": "model_responded", "model_calls": model_calls}, reply, classification.labels, memory_refs)
            if next_step != "search_providers":
                self.trace.emit(run_id, "tool_authorization", session_id, tool=next_step, allowed=False, reason="proposal_does_not_match_search_intent")
                reply = "I have your service details. Tell me if you want me to search providers now."
                return self._finish_response(run_id, session_id, user_id, message_id, {"exit_reason": "proposal_denied", "model_calls": model_calls}, reply, classification.labels, memory_refs)
        except BudgetExceeded as error:
            reason = {"spend_limit": "limit_budget_usd", "request_limit": "limit_requests", "token_limit": "limit_total_tokens"}.get(error.code, error.code)
            self.trace.emit(run_id, "model_budget_blocked", session_id, code=error.code, requests=self.budget.summary()["requests"])
            return self._finish_response(run_id, session_id, user_id, message_id, {"exit_reason": reason, "model_calls": model_calls}, "The model budget is exhausted or cannot safely cover another request. Your details are saved; no provider search was made.")
        except ModelError as error:
            self.trace.emit(
                run_id, "model_error", session_id, code=error.code,
                retryable=error.retryable, uncertain=error.uncertain,
                primary_error_code=error.primary_error_code,
                fallback_provider=error.fallback_provider,
                requests=self.budget.summary()["requests"],
            )
            return self._finish_response(run_id, session_id, user_id, message_id, {"exit_reason": error.code, "model_calls": model_calls}, "I couldn't get a reliable model response, so I did not search or take any action. Your request is saved; please try again.")
        except ValueError as error:
            if str(error) == "context_budget_exceeded":
                return self._finish_response(run_id, session_id, user_id, message_id, {"exit_reason": "context_budget_exceeded", "model_calls": model_calls}, "The request context exceeded the configured limit. Please shorten the request and try again.")
            raise

        state_row = self.store.connection.execute("SELECT state FROM tasks WHERE id=?", (task_row["id"],)).fetchone()
        if state_row["state"] in {"GATHERING", "OPTIONS_PRESENTED"}:
            self.approvals.transition(task_row["id"], "SEARCHING", "model_proposed_search", {"service": intent.service, "area": intent.area, "date_start": intent.date_start, "date_end": intent.date_end}, run_id, session_id)
        arguments = {"service": intent.service, "area": intent.area, "date_range": {"start": intent.date_start, "end": intent.date_end}}
        try:
            provider_results = self.gateway.call(run_id, session_id, user_id, "search_providers", arguments, intent, self.config.max_tool_calls)
        except (MockToolError, ToolDenied) as error:
            self._record_assistant_transition(session_id, task_row["id"], "FAILED", "search_failed", run_id)
            return self._finish_response(run_id, session_id, user_id, message_id, {"exit_reason": getattr(error, "code", "tool_error"), "model_calls": model_calls, "tool_calls": self.gateway._run_counts.get(run_id, 0)}, "The provider search failed safely. Your request is saved, and no booking was made. Please try again.")
        filtered = self._filter_options(provider_results, intent)
        intent.presented_options = filtered
        intent.provider_ids = [item["provider_id"] for item in filtered]
        self._save_intent(task_row["id"], intent)
        self.approvals.transition(task_row["id"], "OPTIONS_PRESENTED", "provider_options_presented", {"provider_ids": intent.provider_ids}, run_id, session_id)
        if not filtered:
            reply = "I couldn't find an option that fits all of your date, time, rating, and budget preferences. Tell me which constraint you want to change."
            return self._finish_response(run_id, session_id, user_id, message_id, {"exit_reason": "no_matching_options", "providers": [], "model_calls": model_calls, "tool_calls": self.gateway._run_counts.get(run_id, 0)}, reply)
        reply = "I found providers that match your request. Choose a provider and available slot to review a quote."
        if self.config.max_model_turns > model_calls and not self._wall_exhausted(deadline):
            observations = [{
                "provider_id": item["provider_id"], "service": item["service"], "area": item["area"],
                "price_inr": item["price_inr"], "rating": item["rating"], "available_slots": item["available_slots"],
            } for item in filtered]
            try:
                context = self.context.build(session_id, user_id, safe_text, intent, observations, exclude_message_id=message_id_db)
                self.trace.emit(run_id, "context_manifest", session_id, **context.manifest)
                model_calls += 1
                comparison = self.model.propose(
                    context.messages, safe_text, max(0.1, deadline - time.monotonic()),
                    on_retry=lambda attempt, code, delay: self.trace.emit(
                        run_id, "retry", session_id, operation="model", attempt=attempt,
                        provider_code=code, delay_seconds=delay,
                    ),
                    on_fallback=fallback_callback_for(context),
                )
                self.trace.emit(
                    run_id, "model_call", session_id, model=comparison.model,
                    provider=comparison.provider or self.config.llm_provider,
                    fallback_from=comparison.fallback_from, fallback_reason=comparison.fallback_reason,
                    effort=comparison.effort,
                    input_tokens=comparison.input_tokens, output_tokens=comparison.output_tokens,
                    thought_tokens=comparison.thought_tokens,
                    cached_input_tokens=comparison.cached_input_tokens, estimated_cost_usd=comparison.estimated_cost_usd,
                    latency_ms=comparison.latency_ms, request_id=comparison.request_id,
                    proposal=comparison.proposal.get("proposal"), selected_provider_id=comparison.proposal.get("provider_id"),
                    payload_sha256=context.payload_sha256,
                )
                if not self._wall_exhausted(deadline):
                    selected = next((item for item in filtered if item["provider_id"] == comparison.proposal.get("provider_id")), None)
                    if selected:
                        reply = (
                            f"I found {len(filtered)} matching providers. A model-selected option is "
                            f"{selected['name']} ({selected['provider_id']}): ₹{selected['price_inr']}, "
                            f"rated {selected['rating']:.1f}. Choose an available slot to review its current quote."
                        )
                    else:
                        self.trace.emit(run_id, "model_selection_rejected", session_id, reason="provider_not_in_sanitized_observations")
                else:
                    self.trace.emit(run_id, "model_turn_exceeded_deadline", session_id, turn=model_calls)
            except (BudgetExceeded, ModelError, ValueError) as error:
                code = getattr(error, "code", "context_budget_exceeded" if str(error) == "context_budget_exceeded" else "model_error")
                self.trace.emit(
                    run_id, "comparison_model_unavailable", session_id, code=code,
                    primary_error_code=getattr(error, "primary_error_code", None),
                    fallback_provider=getattr(error, "fallback_provider", None),
                )
        if memory_refs:
            used = [self._memory_disclosure(memory_id) for memory_id in memory_refs]
            used = [item for item in used if item]
            if used:
                reply += " Using saved preferences: " + ", ".join(used) + "."
        return self._finish_response(
            run_id, session_id, user_id, message_id,
            {"exit_reason": "completed", "providers": filtered, "model_calls": model_calls, "tool_calls": self.gateway._run_counts.get(run_id, 0)},
            reply,
            memory_refs=memory_refs,
        )

    def _material_changed(self, before: IntentState, after: IntentState) -> bool:
        return any((
            before.action != after.action, before.service != after.service, before.area != after.area,
            before.date_start != after.date_start, before.date_end != after.date_end,
            before.earliest_time != after.earliest_time, before.latest_time != after.latest_time,
            before.budget_max_inr != after.budget_max_inr, before.no_budget != after.no_budget,
            before.preferences != after.preferences, before.target_booking_id != after.target_booking_id,
        ))

    def _filter_options(self, providers: list[dict[str, Any]], intent: IntentState) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        excluded = set(intent.preferences.get("avoid_providers", []))
        min_rating = float(intent.preferences.get("min_rating", 0))
        preferred_technician = intent.preferences.get("technician")
        preferred_language = intent.preferences.get("language")
        preferred_days = set(intent.preferences.get("days", []))
        preferred_provider_id = intent.preferences.get("preferred_provider_id")
        for provider in providers:
            if preferred_provider_id and provider["provider_id"] != preferred_provider_id:
                continue
            if intent.budget_max_inr is not None and provider["price_inr"] > intent.budget_max_inr:
                continue
            if provider["rating"] < min_rating or provider["provider_id"] in excluded:
                continue
            if preferred_technician and provider["attributes"].get("technician_name") != preferred_technician:
                continue
            if preferred_language and preferred_language not in provider["attributes"].get("languages", []):
                continue
            slots = []
            for slot in provider["available_slots"]:
                slot_date, slot_time = slot[:10], slot[11:16]
                try:
                    weekday = datetime.fromisoformat(slot).strftime("%A")
                except ValueError:
                    continue
                if preferred_days and weekday not in preferred_days:
                    continue
                if intent.date_start and slot_date < intent.date_start or intent.date_end and slot_date > intent.date_end:
                    continue
                if intent.earliest_time and slot_time < intent.earliest_time:
                    continue
                if intent.latest_time and slot_time >= intent.latest_time:
                    continue
                slots.append(slot)
            if not slots:
                continue
            result.append({**provider, "available_slots": slots})
        return result

    def select_option(self, session_id: str, provider_id: str, slot: str) -> dict[str, Any]:
        session = self.store.connection.execute("SELECT user_id FROM sessions WHERE id=?", (session_id,)).fetchone()
        if not session:
            raise ApprovalError("session_missing", "Session expired; reload the local app.")
        task = self.store.connection.execute(
            "SELECT * FROM tasks WHERE session_id=? AND state NOT IN ('CONFIRMED','DONE','FAILED','ABANDONED') ORDER BY updated_at DESC LIMIT 1", (session_id,)
        ).fetchone()
        if not task:
            raise ApprovalError("task_missing", "There are no current provider options.")
        intent = IntentState.from_json(task["intent_json"])
        run_id = self.trace.new_run_id()
        self.trace.emit(run_id, "run_start", session_id, user_id=session["user_id"], action="select_option")
        try:
            approval = self.approvals.create_booking_approval(task["id"], session_id, session["user_id"], intent, provider_id, slot, run_id)
            text = approval_card_text(approval["card"])
            self._insert_message(session_id, "assistant", text)
            self.trace.emit(run_id, "run_end", session_id, exit_reason="awaiting_approval", user_facing_outcome=text)
            return {"approval": approval, "message": text, "run_id": run_id}
        except ApprovalError as error:
            self.trace.emit(run_id, "run_end", session_id, exit_reason=error.code, user_facing_outcome=str(error))
            raise
        except Exception:
            self.trace.emit(run_id, "run_end", session_id, exit_reason="internal_error", user_facing_outcome="The slot could not be prepared. No approval was created.")
            raise

    def approve_action(self, session_id: str, action_id: str, action_fingerprint: str, client_request_id: str) -> dict[str, Any]:
        session = self.store.connection.execute("SELECT user_id FROM sessions WHERE id=?", (session_id,)).fetchone()
        if not session:
            raise ApprovalError("session_missing", "Session expired; reload the local app.")
        run_id = self.trace.new_run_id()
        self.trace.emit(run_id, "run_start", session_id, user_id=session["user_id"], action="approval")
        try:
            outcome = self.approvals.approve(action_id, session["user_id"], session_id, action_fingerprint, client_request_id, run_id)
            message = outcome.get("message", f"This action is {outcome.get('status', 'still processing')}.")
            self._insert_message(session_id, "assistant", message)
            self.trace.emit(run_id, "run_end", session_id, exit_reason="completed", user_facing_outcome=message)
            return {**outcome, "message": message, "run_id": run_id, "budget": self.budget.summary()}
        except ApprovalError as error:
            self.trace.emit(run_id, "run_end", session_id, exit_reason=error.code, user_facing_outcome=str(error))
            raise
        except Exception:
            message = "The action result could not be confirmed. Check the booking list before trying again."
            self._insert_message(session_id, "assistant", message)
            self.trace.emit(run_id, "run_end", session_id, exit_reason="internal_error", user_facing_outcome=message)
            return {"status": "unverified", "action_status": "unverified", "message": message, "run_id": run_id, "budget": self.budget.summary()}

    def reject_action(self, session_id: str, action_id: str) -> dict[str, Any]:
        session = self.store.connection.execute("SELECT user_id FROM sessions WHERE id=?", (session_id,)).fetchone()
        if not session:
            raise ApprovalError("session_missing", "Session expired; reload the local app.")
        run_id = self.trace.new_run_id()
        self.trace.emit(run_id, "run_start", session_id, user_id=session["user_id"], action="reject")
        try:
            outcome = self.approvals.reject(action_id, session["user_id"], session_id, run_id)
            message = "Approval rejected. No provider change was made."
            self._insert_message(session_id, "assistant", message)
            self.trace.emit(run_id, "run_end", session_id, exit_reason="rejected", user_facing_outcome=message)
            return {**outcome, "message": message, "run_id": run_id}
        except ApprovalError as error:
            self.trace.emit(run_id, "run_end", session_id, exit_reason=error.code, user_facing_outcome=str(error))
            raise
        except Exception:
            self.trace.emit(run_id, "run_end", session_id, exit_reason="internal_error", user_facing_outcome="The rejection could not be recorded. No provider change was made.")
            raise

    def _latest_proposed_action(self, session_id: str, user_id: str) -> Any:
        return self.store.connection.execute(
            "SELECT * FROM pending_actions WHERE session_id=? AND user_id=? AND status='proposed' ORDER BY presented_at DESC LIMIT 1",
            (session_id, user_id),
        ).fetchone()

    def _model_safe_reply(self, reply: str, fallback: str) -> str:
        if not isinstance(reply, str) or not reply.strip():
            return fallback
        safe = self.redactor.redact(reply.strip())
        if contains_secret_pattern(safe) or re.search(r"(?i)\b(booked|booking confirmed|cancelled|canceled|rescheduled successfully)\b", safe):
            return fallback
        return safe[:1000]

    def _record_assistant_transition(self, session_id: str, task_id: str, target: str, event: str, run_id: str) -> None:
        state = self.store.connection.execute("SELECT state FROM tasks WHERE id=?", (task_id,)).fetchone()
        if state and state["state"] != target:
            try:
                self.approvals.transition(task_id, target, event, {}, run_id, session_id)
            except ApprovalError:
                # Keep a clear user-facing answer even if a stale task transition was rejected.
                self.trace.emit(run_id, "state_transition_denied", session_id, task_id=task_id, current_state=state["state"], requested_state=target, reason="illegal_transition")

    def _wall_exhausted(self, deadline: float) -> bool:
        return time.monotonic() >= deadline

    def _is_memory_inspect(self, text: str) -> bool:
        lower = text.casefold()
        return "what do you remember" in lower or "show my preferences" in lower or lower.strip() in {"/memory", "my preferences"}

    def _is_bookings_request(self, text: str) -> bool:
        lower = text.casefold()
        return "my bookings" in lower or "list bookings" in lower or lower.strip() == "/bookings"

    def _memory_listing(self, user_id: str) -> tuple[str, list[dict[str, Any]]]:
        memories = self.memories.list(user_id)
        payload = [
            {"id": item.id, "key": item.key, "scope": item.scope, "value": item.value,
             "kind": item.kind, "status": item.status, "confidence": item.confidence}
            for item in memories
        ]
        groups = {"explicit": [], "inferred": [], "pending": []}
        for item in payload:
            if item["status"] == "active" and item["kind"] == "explicit":
                groups["explicit"].append(item)
            elif item["status"] == "active" and item["kind"] == "inferred":
                groups["inferred"].append(item)
            elif item["status"] == "pending":
                groups["pending"].append(item)
        reply = f"I have {len(groups['explicit'])} explicit preferences, {len(groups['inferred'])} confirmed inferred preferences, and {len(groups['pending'])} unconfirmed suggestions. You can edit or forget any item in the Memory panel."
        return reply, payload

    def _memory_forget_target(self, user_id: str, text: str) -> dict[str, Any] | None:
        lower = text.casefold()
        if not lower.startswith("forget "):
            return None
        candidates = [item for item in self.memories.list(user_id) if item.status in {"active", "pending"}]
        tail = lower.removeprefix("forget ").strip().rstrip(".!?").strip()
        by_id = [item for item in candidates if item.id.casefold() == tail]
        if len(by_id) == 1:
            return {"id": by_id[0].id, "key": by_id[0].key}

        def label(key: str) -> str:
            return key.removeprefix("pref.").replace(".", " ").replace("_", " ").casefold()

        matches = [
            item for item in candidates
            if tail in {item.key.casefold(), label(item.key)}
            or item.value is not None and tail in {str(item.value).casefold(), json_dumps(item.value).casefold()}
        ]
        if len(matches) == 1:
            return {"id": matches[0].id, "key": matches[0].key}
        return None

    def _memory_remember_target(self, text: str) -> list[dict[str, Any]]:
        match = re.fullmatch(r"\s*remember(?: that)?\s+(.+?)\s*[.!]?\s*", text, re.I)
        if not match:
            return []
        fact = match.group(1).strip()
        lower = fact.casefold()
        area = re.search(r"\b(?:my )?(?:home )?area (?:is|=)\s+([a-z][a-z ]{1,40})\b", fact, re.I)
        if area:
            from .mock_provider import AREAS
            canonical = next((value for value in AREAS if value.casefold() == area.group(1).strip().casefold()), None)
            return [{"key": "pref.area", "value": canonical}] if canonical else []
        if re.search(r"\b(?:i )?(?:prefer|like)\s+(?:the )?mornings?\b|\bmorning appointments?\b", lower):
            return [
                {"key": "pref.earliest_time", "value": "06:00"},
                {"key": "pref.latest_time", "value": "12:00"},
            ]
        days = [name.title() for name in ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday") if re.search(rf"\b{name}\b", lower)]
        if days and re.search(r"\b(?:prefer|available|best|works|like)\b", lower):
            return [{"key": "pref.days", "value": days}]
        rating = re.search(r"\b(?:at least|minimum|above|over)\s*(\d(?:\.\d)?)\s*(?:stars?)?\b", lower)
        if rating and re.search(r"\b(?:rating|rated|stars?)\b", lower):
            return [{"key": "pref.min_rating", "value": float(rating.group(1))}]
        service = next((name for name, terms in SERVICE_TERMS.items() if any(term.strip() in lower for term in terms)), None)
        budget = re.search(r"(?:₹\s*|rs\.?\s*|inr\s*)(\d[\d,]*)", fact, re.I)
        if service and budget and re.search(r"\b(?:budget|under|below|maximum|max)\b", lower):
            return [{"key": f"pref.budget.{service}", "scope": f"service:{service}", "value": int(budget.group(1).replace(",", ""))}]
        return []

    def _memory_short_value(self, memory_id: str) -> str | None:
        row = self.store.connection.execute("SELECT key,value_json FROM memories WHERE id=? AND status='active'", (memory_id,)).fetchone()
        if not row:
            return None
        return f"{row['key']}={json_loads(row['value_json'])}"

    def _memory_disclosure(self, memory_id: str) -> str | None:
        row = self.store.connection.execute("SELECT key,value_json FROM memories WHERE id=? AND status='active'", (memory_id,)).fetchone()
        if not row:
            return None
        key, value = row["key"], json_loads(row["value_json"])
        if key == "pref.area":
            return f"saved area {value}"
        if key == "pref.earliest_time":
            return f"earliest time {value}"
        if key == "pref.latest_time":
            return f"latest time {value}"
        if key.startswith("pref.budget."):
            return f"budget ₹{value}"
        if key == "pref.min_rating":
            return f"minimum rating {value}"
        if key == "pref.language":
            return f"language {value}"
        if key == "pref.days":
            return "preferred days " + ", ".join(value)
        if key.startswith("pref.technician."):
            return f"technician {value}"
        return None

    def memory_list(self, session_id: str) -> list[dict[str, Any]]:
        session = self.store.connection.execute("SELECT user_id FROM sessions WHERE id=?", (session_id,)).fetchone()
        if not session:
            raise ValueError("Session expired")
        return self._memory_listing(session["user_id"])[1]

    def correct_memory(self, session_id: str, memory_id: str, value: Any) -> dict[str, Any]:
        session = self.store.connection.execute("SELECT user_id FROM sessions WHERE id=?", (session_id,)).fetchone()
        if not session:
            raise ValueError("Session expired")
        memory_id_new = self.memories.correct(session["user_id"], memory_id, value, [])
        return {"memory_id": memory_id_new, "memories": self.memory_list(session_id)}

    def forget_memory(self, session_id: str, memory_id: str) -> dict[str, Any]:
        session = self.store.connection.execute("SELECT user_id FROM sessions WHERE id=?", (session_id,)).fetchone()
        if not session:
            raise ValueError("Session expired")
        self.memories.forget(session["user_id"], memory_id)
        return {"forgotten": memory_id, "memories": self.memory_list(session_id)}

    def confirm_memory(self, session_id: str, memory_id: str) -> dict[str, Any]:
        session = self.store.connection.execute("SELECT user_id FROM sessions WHERE id=?", (session_id,)).fetchone()
        if not session:
            raise ValueError("Session expired")
        self.memories.confirm(session["user_id"], memory_id)
        return {"confirmed": memory_id, "memories": self.memory_list(session_id)}

    def reject_memory(self, session_id: str, memory_id: str) -> dict[str, Any]:
        session = self.store.connection.execute("SELECT user_id FROM sessions WHERE id=?", (session_id,)).fetchone()
        if not session:
            raise ValueError("Session expired")
        self.memories.reject(session["user_id"], memory_id)
        return {"rejected": memory_id, "memories": self.memory_list(session_id)}

    def provider_details(self, session_id: str, provider_id: str) -> dict[str, Any]:
        session = self.store.connection.execute("SELECT user_id FROM sessions WHERE id=?", (session_id,)).fetchone()
        if not session:
            raise ValueError("Session expired")
        rows = self.store.connection.execute("SELECT intent_json FROM tasks WHERE session_id=?", (session_id,)).fetchall()
        discovered = any(provider_id in IntentState.from_json(row["intent_json"]).provider_ids for row in rows)
        owned = any(booking["provider_id"] == provider_id for booking in self.provider.list_bookings(session["user_id"]))
        if not discovered and not owned:
            raise ApprovalError("provider_not_authorized", "Provider details are available only for options you found or your own bookings.")
        return self.gateway.provider_details_for_ui(provider_id)

    def list_bookings(self, session_id: str) -> list[dict[str, Any]]:
        session = self.store.connection.execute("SELECT user_id FROM sessions WHERE id=?", (session_id,)).fetchone()
        if not session:
            raise ValueError("Session expired")
        return self.provider.list_bookings(session["user_id"])

    def pending_approvals(self, session_id: str) -> list[dict[str, Any]]:
        session = self.store.connection.execute("SELECT user_id FROM sessions WHERE id=?", (session_id,)).fetchone()
        if not session:
            raise ValueError("Session expired")
        rows = self.store.connection.execute(
            "SELECT * FROM pending_actions WHERE session_id=? AND user_id=? AND status='proposed' AND expires_at>? ORDER BY presented_at",
            (session_id, session["user_id"], self.clock.epoch()),
        ).fetchall()
        actions = []
        for row in rows:
            material = json_loads(row["material_json"], {})
            card = self.approvals._card(row["id"], row["kind"], material, row["approval_code"], row["expires_at"])
            actions.append({
                "action_id": row["id"], "fingerprint": row["fingerprint"], "code": row["approval_code"],
                "material": material, "expires_at": row["expires_at"], "card": card,
                "text": approval_card_text(card),
            })
        return actions

    def current_options(self, session_id: str) -> list[dict[str, Any]]:
        task = self.store.connection.execute(
            "SELECT intent_json FROM tasks WHERE session_id=? AND id NOT LIKE 'task_proactive_%' AND state='OPTIONS_PRESENTED' ORDER BY updated_at DESC LIMIT 1",
            (session_id,),
        ).fetchone()
        if not task:
            return []
        return IntentState.from_json(task["intent_json"]).presented_options

    def _handle_booking_management(
        self, run_id: str, session_id: str, user_id: str, message_id: str, text: str,
        intent: IntentState, task_id: str, deadline: float,
    ) -> dict[str, Any]:
        try:
            bookings = self.gateway.call(run_id, session_id, user_id, "list_bookings", {}, intent, self.config.max_tool_calls)
        except (MockToolError, ToolDenied):
            return self._finish_response(run_id, session_id, user_id, message_id, {"exit_reason": "tool_error"}, "I couldn't retrieve your booking right now. Please try again.")
        upcoming = []
        for booking in bookings:
            if booking["status"] != "confirmed":
                continue
            try:
                if datetime.fromisoformat(booking["slot"]).timestamp() > self.clock.epoch():
                    upcoming.append(booking)
            except ValueError:
                continue
        target = intent.target_booking_id
        if not target:
            matching = [booking for booking in upcoming if not intent.service or booking["service"] == intent.service]
            if len(matching) == 1:
                target = matching[0]["booking_id"]
                intent.target_booking_id = target
                intent.provenance["target_booking_id"] = {"source": "derived:list_bookings", "kind": "derived", "confirmed": True}
                self._save_intent(task_id, intent)
            else:
                return self._finish_response(run_id, session_id, user_id, message_id, {"exit_reason": "asked_user", "bookings": upcoming}, "Which upcoming booking do you mean? Select one from your booking list.")
        booking = next((item for item in upcoming if item["booking_id"] == target), None)
        if not booking:
            return self._finish_response(run_id, session_id, user_id, message_id, {"exit_reason": "booking_not_found", "bookings": upcoming}, "I couldn't find that as one of your upcoming active bookings.")
        if intent.action == "cancel":
            try:
                approval = self.approvals.create_booking_management_approval(task_id, session_id, user_id, "cancel", target, None, run_id)
            except ApprovalError as error:
                return self._finish_response(run_id, session_id, user_id, message_id, {"exit_reason": error.code}, str(error))
            reply = approval_card_text(approval["card"])
            return self._finish_response(run_id, session_id, user_id, message_id, {"exit_reason": "awaiting_approval", "approval": approval, "bookings": upcoming}, reply)
        if not intent.date_start:
            return self._finish_response(run_id, session_id, user_id, message_id, {"exit_reason": "asked_user", "missing_fields": ["new_date"]}, "What date should I check for the reschedule?")
        try:
            details = self.gateway.call(run_id, session_id, user_id, "get_provider", {"provider_id": booking["provider_id"]}, intent, self.config.max_tool_calls)
            slots = [slot for slot in details["available_slots"] if intent.date_start <= slot[:10] <= (intent.date_end or intent.date_start)]
            if intent.earliest_time:
                slots = [slot for slot in slots if slot[11:16] >= intent.earliest_time]
            if intent.latest_time:
                slots = [slot for slot in slots if slot[11:16] < intent.latest_time]
        except MockToolError:
            slots = []
        if not slots:
            return self._finish_response(run_id, session_id, user_id, message_id, {"exit_reason": "no_matching_options"}, "There is no available replacement slot that matches that date and time window.")
        return self._finish_response(run_id, session_id, user_id, message_id, {"exit_reason": "awaiting_slot_selection", "reschedule_slots": slots, "booking": booking}, "Choose a replacement slot. I will show a separate approval card before rescheduling.")

    def select_reschedule_slot(self, session_id: str, booking_id: str, slot: str) -> dict[str, Any]:
        session = self.store.connection.execute("SELECT user_id FROM sessions WHERE id=?", (session_id,)).fetchone()
        if not session:
            raise ApprovalError("session_missing", "Session expired")
        task = self.store.connection.execute("SELECT * FROM tasks WHERE session_id=? AND state NOT IN ('CONFIRMED','DONE','FAILED','ABANDONED') ORDER BY updated_at DESC LIMIT 1", (session_id,)).fetchone()
        if not task:
            raise ApprovalError("task_missing", "Task no longer exists")
        run_id = self.trace.new_run_id()
        self.trace.emit(run_id, "run_start", session_id, user_id=session["user_id"], action="reschedule_select")
        try:
            approval = self.approvals.create_booking_management_approval(task["id"], session_id, session["user_id"], "reschedule", booking_id, slot, run_id)
            text = approval_card_text(approval["card"])
            self._insert_message(session_id, "assistant", text)
            self.trace.emit(run_id, "run_end", session_id, exit_reason="awaiting_approval", user_facing_outcome=text)
            return {"approval": approval, "message": text, "run_id": run_id}
        except ApprovalError as error:
            self.trace.emit(run_id, "run_end", session_id, exit_reason=error.code, user_facing_outcome=str(error))
            raise
        except Exception:
            self.trace.emit(run_id, "run_end", session_id, exit_reason="internal_error", user_facing_outcome="The reschedule approval could not be prepared. No provider change was made.")
            raise

    def recover(self) -> list[dict[str, Any]]:
        run_id = self.trace.new_run_id()
        self.trace.emit(run_id, "run_start", event_type="startup_recovery")
        results = self.approvals.recover(run_id)
        self._publish_recovery_results(results)
        self.trace.emit(run_id, "run_end", exit_reason="recovery_complete", recovered_actions=len(results))
        return results

    def _publish_recovery_results(self, results: list[dict[str, Any]]) -> None:
        for result in results:
            action_id = result.get("action_id")
            if not action_id:
                continue
            row = self.store.connection.execute("SELECT session_id FROM pending_actions WHERE id=?", (action_id,)).fetchone()
            if not row:
                continue
            message = result.get("message") or f"Action status: {result.get('status', 'unverified')}."
            exists = self.store.connection.execute(
                "SELECT 1 FROM messages WHERE session_id=? AND role='assistant' AND content=? LIMIT 1",
                (row["session_id"], message),
            ).fetchone()
            if not exists:
                self._insert_message(row["session_id"], "assistant", message)

    def close(self) -> None:
        self.provider.close()
        self.store.close()
