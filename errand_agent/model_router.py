"""Primary model routing with an explicit, observable OpenAI fallback."""

from __future__ import annotations

from dataclasses import replace
import time
from typing import Any, Callable

from .responses import ModelError, ModelResult


FallbackCallback = Callable[[str, str, str], None]


class PrimaryFallbackModel:
    """Try the configured primary first; use the fallback only after a model error."""

    _LOCAL_ERRORS = {"invalid_context", "context_budget_exceeded", "limit_model_calls", "limit_wall_clock"}

    def __init__(
        self,
        primary: Any,
        fallback: Any | None,
        primary_provider: str,
        fallback_provider: str | None,
    ) -> None:
        self.primary = primary
        self.fallback = fallback
        self.primary_provider = primary_provider
        self.fallback_provider = fallback_provider

    def propose(
        self,
        input_messages: list[dict[str, str]],
        current_user_text: str,
        timeout_seconds: float | None = None,
        on_retry: Any = None,
        on_fallback: FallbackCallback | None = None,
    ) -> ModelResult:
        started = time.monotonic()
        try:
            return self.primary.propose(
                input_messages, current_user_text, timeout_seconds, on_retry=on_retry,
            )
        except ModelError as primary_error:
            if (
                self.fallback is None
                or not self.fallback_provider
                or primary_error.code in self._LOCAL_ERRORS
            ):
                raise
            if on_fallback:
                on_fallback(self.primary_provider, self.fallback_provider, primary_error.code)
            remaining = None
            if timeout_seconds is not None:
                remaining = timeout_seconds - (time.monotonic() - started)
                if remaining <= 0:
                    raise ModelError(
                        "limit_wall_clock", "The request time limit expired before fallback could run.",
                        uncertain=primary_error.uncertain,
                    ) from None
            try:
                result = self.fallback.propose(
                    input_messages, current_user_text, remaining, on_retry=on_retry,
                )
            except ModelError as fallback_error:
                raise ModelError(
                    fallback_error.code,
                    "The primary and fallback model requests failed; provider details are redacted.",
                    retryable=fallback_error.retryable,
                    uncertain=primary_error.uncertain or fallback_error.uncertain,
                    primary_error_code=primary_error.code,
                    fallback_provider=self.fallback_provider,
                ) from None
            return replace(
                result,
                fallback_from=self.primary_provider,
                fallback_reason=primary_error.code,
            )
