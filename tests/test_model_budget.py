from __future__ import annotations

import json
import io
import tempfile
import unittest
from pathlib import Path
from urllib.error import HTTPError
from email.message import Message
from unittest.mock import patch

from errand_agent.budget import BudgetExceeded, BudgetLedger
from errand_agent.config import AppConfig
from errand_agent.responses import OpenAIResponsesClient, PROPOSAL_SCHEMA, choose_effort
from errand_agent.storage import AgentStore


class FakeResponse:
    def __init__(self, payload: dict) -> None:
        self.payload = json.dumps(payload).encode()
        self.headers = {"x-request-id": "req-offline-test"}

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self, _size: int) -> bytes:
        return self.payload


class ModelBudgetTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.store = AgentStore(self.root / "agent.sqlite3")

    def tearDown(self) -> None:
        self.store.close()
        self.temp.cleanup()

    def config(self, **overrides: str) -> AppConfig:
        values = {"LLM_MODE": "live", "OPENAI_API_KEY": "test-secret-value", "PRICE_VERIFIED_MODEL": "gpt-6-luna"}
        values.update(overrides)
        return AppConfig.from_env(self.root, values)

    def test_effort_starts_low_and_escalates_only_for_complex_requests(self) -> None:
        self.assertEqual(choose_effort("Find a plumber tomorrow."), "low")
        self.assertEqual(choose_effort("Compare providers, but if the first slot is gone reschedule to Saturday."), "medium")
        self.assertEqual(choose_effort("Compare providers, but if the first slot is gone reschedule to Saturday.", allow_high=True), "high")

    def test_budget_reserves_before_request_and_settles_actual_cost(self) -> None:
        config = self.config(MAX_API_SPEND_USD="0.001", MAX_MODEL_REQUESTS="2", MAX_TOTAL_TOKENS="10000")
        budget = BudgetLedger(self.store, config)
        reserved = budget.reserve(100, 100)
        settled = budget.settle(reserved, 50, 20)
        self.assertAlmostEqual(settled["estimated_cost_usd"], 0.000015)
        self.assertEqual(budget.summary()["requests"], 1)
        self.assertEqual(budget.summary()["tokens"], 70)

    def test_spend_and_request_limits_block_before_another_request(self) -> None:
        config = self.config(MAX_API_SPEND_USD="0.00001", MAX_MODEL_REQUESTS="1")
        budget = BudgetLedger(self.store, config)
        with self.assertRaises(BudgetExceeded) as raised:
            budget.reserve(100, 1200)
        self.assertEqual(raised.exception.code, "spend_limit")
        self.assertEqual(budget.summary()["requests"], 0)
        config2 = self.config(MAX_API_SPEND_USD="1", MAX_MODEL_REQUESTS="1")
        budget2 = BudgetLedger(self.store, config2)
        reservation = budget2.reserve(100, 100)
        budget2.settle(reservation, 50, 20)
        with self.assertRaises(BudgetExceeded) as raised2:
            budget2.reserve(100, 100)
        self.assertEqual(raised2.exception.code, "request_limit")

    def test_openai_client_uses_verified_model_and_context_without_live_network(self) -> None:
        config = self.config(MAX_API_SPEND_USD="1")
        budget = BudgetLedger(self.store, config)
        client = OpenAIResponsesClient(config, budget)
        proposal = {
            "proposal": "search_providers", "reply": "Search providers.",
            "intent_patch": {"action": None, "service": None, "area": None, "date_start": None, "date_end": None,
                             "earliest_time": None, "latest_time": None, "budget_max_inr": None, "no_budget": False},
            "provider_id": None, "slot": None,
        }
        response = FakeResponse({
            "output": [{"type": "function_call", "name": "propose_next_step", "arguments": json.dumps(proposal)}],
            "usage": {"input_tokens": 120, "output_tokens": 35, "input_tokens_details": {"cached_tokens": 5}},
        })
        captured: dict = {}

        def fake_open(request, timeout):
            captured["request"] = request
            captured["timeout"] = timeout
            return response

        with patch("urllib.request.urlopen", side_effect=fake_open) as mocked:
            result = client.propose([{"role": "system", "content": "safe instructions"}, {"role": "user", "content": "find plumber"}], "find plumber", 9)
        self.assertEqual(result.model, "gpt-6-luna")
        self.assertEqual(result.proposal, proposal)
        self.assertEqual(result.input_tokens, 120)
        self.assertEqual(result.request_id, "req-offline-test")
        self.assertEqual(captured["timeout"], 9)
        request_body = json.loads(captured["request"].data)
        self.assertEqual(request_body["model"], "gpt-6-luna")
        self.assertEqual(request_body["reasoning"]["effort"], "low")
        self.assertFalse(request_body["store"])
        self.assertEqual(mocked.call_count, 1)
        self.assertEqual(budget.summary()["requests"], 1)
        self.assertNotIn("test-secret-value", json.dumps(request_body))

    def test_insufficient_quota_is_not_retried_and_does_not_claim_token_spend(self) -> None:
        from errand_agent.responses import ModelError

        config = self.config(MAX_API_SPEND_USD="1")
        for provider_code in ("insufficient_quota", "credit_balance_exhausted", "billing_hard_limit", "account_deactivated"):
            with self.subTest(provider_code=provider_code):
                store = AgentStore(self.root / f"{provider_code}.sqlite3")
                try:
                    budget = BudgetLedger(store, config)
                    client = OpenAIResponsesClient(config, budget)
                    error_body = json.dumps({"error": {"code": provider_code, "message": "details omitted"}}).encode()
                    rejected = HTTPError(client.endpoint, 429, "rate limited", Message(), io.BytesIO(error_body))
                    try:
                        with patch("urllib.request.urlopen", side_effect=rejected) as mocked:
                            with self.assertRaises(ModelError) as raised:
                                client.propose([{"role": "system", "content": "safe"}, {"role": "user", "content": "find plumber"}], "find plumber")
                    finally:
                        rejected.close()
                    self.assertEqual(raised.exception.code, f"http_429_{provider_code}")
                    self.assertFalse(raised.exception.retryable)
                    self.assertEqual(mocked.call_count, 1)
                    self.assertEqual(budget.summary()["requests"], 1)
                    self.assertEqual(budget.summary()["tokens"], 0)
                    self.assertEqual(budget.summary()["estimated_cost_usd"], 0)
                finally:
                    store.close()

    def test_strict_proposal_schema_has_no_write_tool_surface(self) -> None:
        allowed = PROPOSAL_SCHEMA["properties"]["proposal"]["enum"]
        self.assertIn("search_providers", allowed)
        self.assertNotIn("book", allowed)
        self.assertNotIn("cancel", allowed)
        self.assertNotIn("reschedule", allowed)


if __name__ == "__main__":
    unittest.main()
