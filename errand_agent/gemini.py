"""Gemini Interactions API client for typed, non-authoritative agent proposals."""

from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.request
from typing import Any

from .budget import BudgetExceeded, BudgetLedger, estimate_tokens
from .config import AppConfig
from .responses import ModelError, ModelResult, PROPOSAL_SCHEMA, SYSTEM_INSTRUCTIONS, choose_effort


INTERACTIONS_ENDPOINT = "https://generativelanguage.googleapis.com/v1beta/interactions"
_SAFE_CODE = re.compile(r"[A-Za-z0-9_.-]{1,64}")


def gemini_thinking_level(effort: str) -> str:
    """Map the app's effort levels to Gemini 3 Flash-Lite's supported values."""
    if effort in {"none", "minimal"}:
        return "minimal"
    if effort in {"xhigh", "max"}:
        return "high"
    if effort in {"low", "medium", "high"}:
        return effort
    raise ValueError("Unsupported Gemini thinking level")


def build_gemini_payload(
    config: AppConfig,
    input_messages: list[dict[str, str]],
    effort: str,
) -> dict[str, Any]:
    system_instruction = SYSTEM_INSTRUCTIONS
    prompt_parts: list[str] = []
    for item in input_messages:
        role = item.get("role", "user")
        content = item.get("content", "")
        if not isinstance(content, str):
            raise ModelError("invalid_context", "The prepared model context is invalid.")
        if role == "system":
            system_instruction = content
        elif role in {"user", "assistant", "model"}:
            if role == "user" and not prompt_parts:
                prompt_parts.append(content)
            else:
                label = "MODEL" if role in {"assistant", "model"} else "USER"
                prompt_parts.append(f"[{label} MESSAGE]\n{content}")
        else:
            raise ModelError("invalid_context", "The prepared model context has an unsupported role.")
    prompt = "\n\n".join(prompt_parts)
    if not prompt.strip():
        raise ModelError("invalid_context", "The prepared model context is empty.")
    return {
        "model": config.gemini_model,
        "input": prompt,
        "system_instruction": system_instruction,
        "response_format": {
            "type": "text",
            "mime_type": "application/json",
            "schema": PROPOSAL_SCHEMA,
        },
        "generation_config": {
            "max_output_tokens": config.max_output_tokens,
            "thinking_level": gemini_thinking_level(effort),
        },
        "store": False,
    }


class GeminiInteractionsClient:
    """Small stdlib-only Gemini client; API credentials never enter request data or logs."""

    endpoint = INTERACTIONS_ENDPOINT

    def __init__(self, config: AppConfig, budget: BudgetLedger) -> None:
        self.config = config
        self.budget = budget

    def propose(
        self,
        input_messages: list[dict[str, str]],
        current_user_text: str,
        timeout_seconds: float | None = None,
        on_retry: Any = None,
        on_fallback: Any = None,
    ) -> ModelResult:
        if not self.config.gemini_model_ready:
            raise ModelError("model_unavailable", "The configured Gemini model is not ready or verified.")
        effort = gemini_thinking_level(
            choose_effort(current_user_text, self.config.reasoning_effort_default, self.config.allow_high_effort)
        )
        payload = build_gemini_payload(self.config, input_messages, effort)
        serialized = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        input_est = estimate_tokens(serialized)
        if input_est > self.config.context_budget_tokens:
            raise ModelError("context_budget_exceeded", "The prepared context exceeds the configured input budget.")

        last_error: ModelError | None = None
        started_all = time.monotonic()
        for attempt in range(self.config.max_retries + 1):
            reservation = self.budget.reserve(input_est, self.config.max_output_tokens)
            request = urllib.request.Request(
                self.endpoint,
                data=serialized.encode("utf-8"),
                headers={
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                    "x-goog-api-key": self.config.gemini_api_key or "",
                },
                method="POST",
            )
            started = time.monotonic()
            configured_timeout = self.config.model_http_timeout_seconds
            timeout = min(configured_timeout, timeout_seconds) if timeout_seconds else configured_timeout
            try:
                with urllib.request.urlopen(request, timeout=timeout) as response:
                    body = response.read(2_000_000)
                    request_id = response.headers.get("x-goog-request-id") or response.headers.get("x-request-id")
            except urllib.error.HTTPError as error:
                try:
                    provider_code = self._provider_error_code(error)
                    retryable = error.code in {408, 409, 500, 502, 503, 504}
                    if error.code in {408, 409, 500, 502, 503, 504}:
                        self.budget.settle(reservation, None, None, uncertain=True)
                    else:
                        self.budget.settle(reservation, 0, 0)
                    if error.code == 402:
                        code = "gemini_http_402_payment_required"
                        message = "The Gemini project needs prepaid API credits before another model request."
                    else:
                        code = f"gemini_http_{error.code}" + (f"_{provider_code}" if provider_code else "")
                        message = "The Gemini request failed; provider details are redacted."
                    last_error = ModelError(code, message, retryable=retryable)
                    if retryable and attempt < self.config.max_retries:
                        delay = self._retry_delay(error, attempt)
                        remaining = (timeout_seconds if timeout_seconds is not None else configured_timeout) - (time.monotonic() - started_all)
                        if delay >= remaining:
                            raise last_error from None
                        if on_retry:
                            on_retry(attempt + 1, code, delay)
                        time.sleep(delay)
                        continue
                    raise last_error from None
                finally:
                    error.close()
            except (TimeoutError, urllib.error.URLError, OSError):
                self.budget.settle(reservation, None, None, uncertain=True)
                last_error = ModelError(
                    "transport_timeout", "The Gemini request timed out; its cost is conservatively reserved.",
                    retryable=True, uncertain=True,
                )
                if attempt < self.config.max_retries:
                    delay = min(1.0 * (2 ** attempt), 4.0)
                    remaining = (timeout_seconds if timeout_seconds is not None else configured_timeout) - (time.monotonic() - started_all)
                    if delay >= remaining:
                        raise last_error from None
                    if on_retry:
                        on_retry(attempt + 1, last_error.code, delay)
                    time.sleep(delay)
                    continue
                raise last_error from None

            latency_ms = int((time.monotonic() - started) * 1000)
            try:
                response_payload = json.loads(body.decode("utf-8"))
                if not isinstance(response_payload, dict):
                    raise ValueError
                usage = response_payload.get("usage")
                if not isinstance(usage, dict):
                    self.budget.settle(reservation, None, None, uncertain=True)
                    raise ModelError("usage_unavailable", "Gemini returned no usage data; the request cost remains reserved.", uncertain=True)
                input_tokens = self._nonnegative_int(usage.get("total_input_tokens"))
                output_tokens = self._nonnegative_int(usage.get("total_output_tokens"))
                thought_tokens = self._nonnegative_int(usage.get("total_thought_tokens", 0))
                if input_tokens is None or output_tokens is None:
                    self.budget.settle(reservation, None, None, uncertain=True)
                    raise ModelError("usage_unavailable", "Gemini returned incomplete usage data; the request cost remains reserved.", uncertain=True)
                cached_tokens = self._nonnegative_int(usage.get("total_cached_tokens", 0)) or 0
                billable_output_tokens = output_tokens + (thought_tokens or 0)
                settled = self.budget.settle(reservation, input_tokens, billable_output_tokens, cached_tokens)
                proposal = self._extract_proposal(response_payload)
                status = response_payload.get("status")
                if status != "completed":
                    raise ModelError("incomplete_response", "Gemini returned an incomplete model response.")
                response_model = response_payload.get("model")
                model = response_model if isinstance(response_model, str) and response_model else self.config.gemini_model
            except ModelError:
                raise
            except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError, KeyError):
                self.budget.settle(reservation, None, None, uncertain=True)
                raise ModelError("invalid_response", "Gemini returned an incomplete or invalid structured response.", uncertain=True) from None

            return ModelResult(
                proposal=proposal,
                model=model,
                effort=effort,
                input_tokens=input_tokens,
                output_tokens=billable_output_tokens,
                cached_input_tokens=cached_tokens,
                estimated_cost_usd=settled["estimated_cost_usd"],
                latency_ms=latency_ms,
                request_id=request_id or response_payload.get("id"),
                thought_tokens=thought_tokens or 0,
                provider="gemini",
            )

        raise last_error or ModelError("model_error", "The Gemini request did not complete.")

    @staticmethod
    def _provider_error_code(error: urllib.error.HTTPError) -> str:
        try:
            payload = json.loads(error.read(8192).decode("utf-8"))
            detail = payload.get("error", {}) if isinstance(payload, dict) else {}
            candidate = detail.get("status") or detail.get("reason") if isinstance(detail, dict) else None
            if not isinstance(candidate, str) and isinstance(detail, dict):
                details = detail.get("details", [])
                if isinstance(details, list):
                    candidate = next((item.get("reason") for item in details if isinstance(item, dict) and isinstance(item.get("reason"), str)), None)
            return candidate if isinstance(candidate, str) and _SAFE_CODE.fullmatch(candidate) else ""
        except (OSError, UnicodeDecodeError, json.JSONDecodeError, TypeError):
            return ""

    @staticmethod
    def _retry_delay(error: urllib.error.HTTPError, attempt: int) -> float:
        header = error.headers.get("Retry-After") if error.headers else None
        if header:
            try:
                return min(10.0, max(0.0, float(header)))
            except ValueError:
                pass
        return min(1.0 * (2 ** attempt), 4.0)

    @staticmethod
    def _nonnegative_int(value: Any) -> int | None:
        try:
            result = int(value)
        except (TypeError, ValueError, OverflowError):
            return None
        return result if result >= 0 else None

    @staticmethod
    def _extract_proposal(payload: dict[str, Any]) -> dict[str, Any]:
        if payload.get("status") != "completed":
            raise ModelError("incomplete_response", "Gemini returned an incomplete model response.")
        text_parts: list[str] = []
        for step in payload.get("steps", []):
            if not isinstance(step, dict) or step.get("type") != "model_output":
                continue
            for content in step.get("content", []):
                if isinstance(content, dict) and content.get("type") == "text" and isinstance(content.get("text"), str):
                    text_parts.append(content["text"])
        if not text_parts:
            raise ModelError("invalid_response", "Gemini returned no structured proposal.")
        try:
            proposal = json.loads("\n".join(text_parts))
        except json.JSONDecodeError:
            raise ModelError("invalid_response", "Gemini returned an invalid structured proposal.") from None
        if not isinstance(proposal, dict) or set(proposal) != set(PROPOSAL_SCHEMA["required"]):
            raise ModelError("invalid_response", "Gemini returned a proposal with an invalid shape.")
        return proposal
