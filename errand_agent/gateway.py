"""Typed read-tool gateway. Consequential provider writes live behind approvals."""

from __future__ import annotations

import time
from typing import Any

from .clock import Clock
from .intent import IntentState
from .mock_provider import MockProvider, MockToolError
from .security import classify_untrusted_text
from .trace import TraceRecorder


READ_TOOLS = frozenset({"search_providers", "get_provider", "list_bookings"})


class ToolDenied(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class ToolGateway:
    def __init__(self, provider: MockProvider, trace: TraceRecorder, clock: Clock | None = None, max_retries: int = 2) -> None:
        self.provider = provider
        self.trace = trace
        self.clock = clock or Clock()
        self.max_retries = max_retries
        self._run_counts: dict[str, int] = {}

    def reset_run(self, run_id: str) -> None:
        self._run_counts[run_id] = 0

    def call(
        self,
        run_id: str,
        session_id: str,
        user_id: str,
        name: str,
        arguments: dict[str, Any],
        intent: IntentState,
        max_calls: int = 8,
    ) -> Any:
        count = self._run_counts.get(run_id, 0)
        if count >= max_calls:
            raise ToolDenied("tool_limit", "The run reached its tool-call limit.")
        if name not in READ_TOOLS:
            self._run_counts[run_id] = count + 1
            self.trace.emit(run_id, "tool_authorization", session_id, tool=name, allowed=False, reason="tool_not_allowlisted")
            raise ToolDenied("tool_not_allowlisted", "That tool is not available to the model.")
        self.trace.emit(run_id, "tool_authorization", session_id, tool=name, allowed=True, reason="read_only_allowlist")
        start = time.monotonic()
        last_error: MockToolError | None = None
        attempts = self.max_retries + 1 if name in {"search_providers", "get_provider", "list_bookings"} else 1
        for attempt in range(attempts):
            count = self._run_counts.get(run_id, 0)
            if count >= max_calls:
                raise ToolDenied("tool_limit", "The run reached its tool-call limit.")
            self._run_counts[run_id] = count + 1
            self.trace.emit(run_id, "tool_call", session_id, tool=name, arguments=arguments, attempt=attempt + 1)
            try:
                result = self._invoke(user_id, name, arguments, intent)
                self._validate_result(name, result)
                safe_trace = self._safe_trace_result(name, result)
                self.trace.emit(
                    run_id, "tool_result", session_id, tool=name, result=safe_trace,
                    latency_ms=int((time.monotonic() - start) * 1000), attempt=attempt + 1,
                )
                return result
            except MockToolError as error:
                last_error = error
                self.trace.emit(
                    run_id, "tool_error", session_id, tool=name, code=error.code,
                    retryable=error.retryable, ambiguous=error.ambiguous, attempt=attempt + 1,
                )
                if not error.retryable or attempt + 1 >= attempts:
                    break
        self.trace.emit(run_id, "tool_retry_exhausted", session_id, tool=name, code=last_error.code if last_error else "unknown")
        raise last_error or MockToolError("tool_error", "Tool execution failed")

    def _invoke(self, user_id: str, name: str, arguments: dict[str, Any], intent: IntentState) -> Any:
        if not isinstance(arguments, dict):
            raise ToolDenied("invalid_arguments", "Tool arguments must be an object.")
        if name == "search_providers":
            if set(arguments) != {"service", "area", "date_range"}:
                raise ToolDenied("invalid_arguments", "search_providers requires service, area, and date_range only.")
            if arguments["service"] != intent.service or arguments["area"] != intent.area:
                raise ToolDenied("intent_mismatch", "Search arguments must match validated user intent.")
            date_range = arguments["date_range"]
            if not isinstance(date_range, dict) or date_range.get("start") != intent.date_start or date_range.get("end") != intent.date_end:
                raise ToolDenied("intent_mismatch", "Search dates must match validated user intent.")
            return self.provider.search_providers(intent.service, intent.area, date_range)
        if name == "get_provider":
            if set(arguments) != {"provider_id"} or not isinstance(arguments["provider_id"], str):
                raise ToolDenied("invalid_arguments", "get_provider requires one provider_id.")
            provider_id = arguments["provider_id"]
            owned_or_discovered = provider_id in intent.provider_ids or any(
                booking["provider_id"] == provider_id for booking in self.provider.list_bookings(user_id)
            )
            if not owned_or_discovered:
                raise ToolDenied("provider_not_authorized", "Provider was not discovered in this session or a user booking.")
            raw = self.provider.get_provider(provider_id)
            review_checks = [classify_untrusted_text(str(review)) for review in raw.get("reviews", [])]
            # Raw reviews are returned only by the separate user-facing details route.
            return {
                **{key: raw[key] for key in raw if key != "reviews"},
                "review_count": len(raw.get("reviews", [])),
                "review_security_flags": [list(check.labels) for check in review_checks if check.labels],
                "untrusted_reviews_omitted": True,
            }
        if name == "list_bookings":
            if arguments:
                raise ToolDenied("invalid_arguments", "list_bookings accepts no model-supplied user identifier.")
            return self.provider.list_bookings(user_id)
        raise ToolDenied("tool_not_allowlisted", "That tool is not available to the model.")

    def provider_details_for_ui(self, provider_id: str) -> dict[str, Any]:
        """Raw review text is kept on the UI path and must be rendered as text only."""
        raw = self.provider.get_provider(provider_id)
        flags = [classify_untrusted_text(str(review)).labels for review in raw.get("reviews", [])]
        return {**raw, "review_security_flags": [list(item) for item in flags]}

    @staticmethod
    def _validate_result(name: str, result: Any) -> None:
        if name == "search_providers":
            if not isinstance(result, list) or not 3 <= len(result) <= 8:
                raise MockToolError("invalid_tool_output", "Provider search returned an invalid result count")
            required = {"provider_id", "price_inr", "rating", "available_slots", "attributes", "service", "area"}
            for item in result:
                if not isinstance(item, dict) or not required.issubset(item):
                    raise MockToolError("invalid_tool_output", "Provider search returned malformed provider data")
                if not isinstance(item["provider_id"], str) or not isinstance(item["price_inr"], int) or not isinstance(item["rating"], (int, float)):
                    raise MockToolError("invalid_tool_output", "Provider search returned malformed provider fields")
                if not isinstance(item["available_slots"], list) or not item["available_slots"] or not isinstance(item["attributes"], dict):
                    raise MockToolError("invalid_tool_output", "Provider search returned malformed availability")
        elif name == "get_provider":
            if not isinstance(result, dict) or "provider_id" not in result or not result.get("untrusted_reviews_omitted"):
                raise MockToolError("invalid_tool_output", "Provider details returned malformed data")
        elif name == "list_bookings":
            if not isinstance(result, list):
                raise MockToolError("invalid_tool_output", "Booking list returned malformed data")
            for item in result:
                if not isinstance(item, dict) or not {"booking_id", "provider_id", "service", "slot", "status"}.issubset(item):
                    raise MockToolError("invalid_tool_output", "Booking list returned malformed data")

    @staticmethod
    def _safe_trace_result(name: str, result: Any) -> Any:
        if name == "search_providers" and isinstance(result, list):
            return [
                {key: item[key] for key in ("provider_id", "service", "area", "price_inr", "rating", "available_slots", "area_match") if key in item}
                for item in result
            ]
        if name == "get_provider" and isinstance(result, dict):
            return {key: value for key, value in result.items() if key not in {"reviews", "name", "technician_name"}}
        return result
