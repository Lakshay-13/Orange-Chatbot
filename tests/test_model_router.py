from __future__ import annotations

import unittest

from errand_agent.model_router import PrimaryFallbackModel
from errand_agent.responses import ModelError, ModelResult


class StubModel:
    def __init__(self, provider: str, result: ModelResult | None = None, error: ModelError | None = None) -> None:
        self.provider = provider
        self.result = result
        self.error = error
        self.calls = 0

    def propose(self, *_args, **_kwargs) -> ModelResult:
        self.calls += 1
        if self.error:
            raise self.error
        assert self.result is not None
        return self.result


class ModelRouterTests(unittest.TestCase):
    def result(self, provider: str) -> ModelResult:
        return ModelResult(
            {"proposal": "respond"}, f"{provider}-model", "low", 20, 8, 0,
            0.00001, 2, "request-id", provider=provider,
        )

    def test_gemini_error_uses_openai_and_returns_traced_fallback_metadata(self) -> None:
        primary = StubModel("gemini", error=ModelError("gemini_http_402_payment_required", "credits unavailable"))
        fallback = StubModel("openai", result=self.result("openai"))
        notifications = []
        router = PrimaryFallbackModel(primary, fallback, "gemini", "openai")

        result = router.propose(
            [{"role": "user", "content": "hello"}], "hello", timeout_seconds=5,
            on_fallback=lambda *event: notifications.append(event),
        )

        self.assertEqual(primary.calls, 1)
        self.assertEqual(fallback.calls, 1)
        self.assertEqual(result.provider, "openai")
        self.assertEqual(result.fallback_from, "gemini")
        self.assertEqual(result.fallback_reason, "gemini_http_402_payment_required")
        self.assertEqual(notifications, [("gemini", "openai", "gemini_http_402_payment_required")])

    def test_local_context_error_does_not_call_openai(self) -> None:
        primary = StubModel("gemini", error=ModelError("context_budget_exceeded", "too large"))
        fallback = StubModel("openai", result=self.result("openai"))
        router = PrimaryFallbackModel(primary, fallback, "gemini", "openai")

        with self.assertRaises(ModelError) as raised:
            router.propose([], "hello")

        self.assertEqual(raised.exception.code, "context_budget_exceeded")
        self.assertEqual(fallback.calls, 0)

    def test_fallback_failure_keeps_both_sanitized_error_codes(self) -> None:
        primary = StubModel("gemini", error=ModelError("gemini_http_402_payment_required", "primary details"))
        fallback = StubModel("openai", error=ModelError("http_429_insufficient_quota", "fallback details"))
        router = PrimaryFallbackModel(primary, fallback, "gemini", "openai")

        with self.assertRaises(ModelError) as raised:
            router.propose([], "hello")

        self.assertEqual(raised.exception.code, "http_429_insufficient_quota")
        self.assertEqual(raised.exception.primary_error_code, "gemini_http_402_payment_required")
        self.assertEqual(raised.exception.fallback_provider, "openai")
        self.assertNotIn("primary details", str(raised.exception))
        self.assertNotIn("fallback details", str(raised.exception))

    def test_fallback_obeys_remaining_wall_clock_time(self) -> None:
        class SlowPrimary(StubModel):
            def propose(self, *_args, **_kwargs) -> ModelResult:
                import time
                time.sleep(0.02)
                raise self.error

        primary = SlowPrimary("gemini", error=ModelError("gemini_http_402_payment_required", "no credits"))
        fallback = StubModel("openai", result=self.result("openai"))
        router = PrimaryFallbackModel(primary, fallback, "gemini", "openai")

        with self.assertRaises(ModelError) as raised:
            router.propose([], "hello", timeout_seconds=0.001)

        self.assertEqual(raised.exception.code, "limit_wall_clock")
        self.assertEqual(fallback.calls, 0)


if __name__ == "__main__":
    unittest.main()
