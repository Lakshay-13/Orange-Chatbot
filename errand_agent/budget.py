"""Persistent cost and token reservations for live model calls."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
import time
from typing import Any

from .config import AppConfig
from .ids import new_id
from .storage import AgentStore


@dataclass(frozen=True)
class Reservation:
    reservation_id: str
    input_tokens: int
    output_tokens: int
    reserved_cost_usd: float


class BudgetExceeded(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class BudgetLedger:
    def __init__(self, store: AgentStore, config: AppConfig) -> None:
        self.store = store
        self.config = config

    def _cost(self, input_tokens: int, output_tokens: int, cached_input_tokens: int = 0) -> Decimal:
        input_count = max(0, input_tokens - cached_input_tokens)
        return (
            Decimal(input_count) * Decimal(str(self.config.price_input_usd_per_mtok))
            + Decimal(max(0, cached_input_tokens)) * Decimal(str(self.config.price_cached_input_usd_per_mtok))
            + Decimal(max(0, output_tokens)) * Decimal(str(self.config.price_output_usd_per_mtok))
        ) / Decimal(1_000_000)

    def reserve(self, input_tokens: int, output_tokens: int) -> Reservation:
        if input_tokens <= 0 or output_tokens <= 0:
            raise BudgetExceeded("invalid_reservation", "Model token reservation must be positive")
        if output_tokens > self.config.max_output_tokens:
            raise BudgetExceeded("output_limit", "Requested output exceeds the configured token limit")
        cost = self._cost(input_tokens, output_tokens)
        reservation_id = new_id("usage")
        with self.store.transaction() as db:
            rows = db.execute("SELECT state,reserved_input_tokens,reserved_output_tokens,actual_input_tokens,actual_output_tokens,estimated_cost_usd FROM budget_ledger").fetchall()
            used_requests = len(rows)
            if used_requests >= self.config.max_model_requests:
                raise BudgetExceeded("request_limit", "The configured model request limit is exhausted")
            used_tokens = 0
            used_cost = Decimal("0")
            for row in rows:
                if row["state"] == "settled":
                    used_tokens += row["actual_input_tokens"] + row["actual_output_tokens"]
                    used_cost += Decimal(str(row["estimated_cost_usd"]))
                else:
                    used_tokens += row["reserved_input_tokens"] + row["reserved_output_tokens"]
                    used_cost += Decimal(str(row["estimated_cost_usd"]))
            if used_tokens + input_tokens + output_tokens > self.config.max_total_tokens:
                raise BudgetExceeded("token_limit", "The configured total token budget is exhausted")
            if used_cost + cost > Decimal(str(self.config.max_api_spend_usd)):
                raise BudgetExceeded("spend_limit", "The configured API spend limit would be exceeded")
            db.execute(
                "INSERT INTO budget_ledger(id,state,reserved_input_tokens,reserved_output_tokens,estimated_cost_usd,created_at) VALUES(?,?,?,?,?,?)",
                (reservation_id, "reserved", input_tokens, output_tokens, float(cost), time.time()),
            )
        return Reservation(reservation_id, input_tokens, output_tokens, float(cost))

    def settle(
        self,
        reservation: Reservation,
        input_tokens: int | None,
        output_tokens: int | None,
        cached_input_tokens: int = 0,
        uncertain: bool = False,
    ) -> dict[str, Any]:
        if uncertain or input_tokens is None or output_tokens is None:
            state = "uncertain"
            actual_in, actual_out = reservation.input_tokens, reservation.output_tokens
            cost = Decimal(str(reservation.reserved_cost_usd))
        else:
            state = "settled"
            actual_in, actual_out = max(0, int(input_tokens)), max(0, int(output_tokens))
            cost = self._cost(actual_in, actual_out, cached_input_tokens)
        with self.store.transaction() as db:
            db.execute(
                "UPDATE budget_ledger SET state=?,actual_input_tokens=?,actual_output_tokens=?,estimated_cost_usd=?,settled_at=? WHERE id=? AND state='reserved'",
                (state, actual_in, actual_out, float(cost), time.time(), reservation.reservation_id),
            )
        return {
            "state": state,
            "input_tokens": actual_in,
            "output_tokens": actual_out,
            "estimated_cost_usd": float(cost),
            "cached_input_tokens": cached_input_tokens,
        }

    def summary(self) -> dict[str, float | int]:
        rows = self.store.connection.execute(
            "SELECT state,reserved_input_tokens,reserved_output_tokens,actual_input_tokens,actual_output_tokens,estimated_cost_usd FROM budget_ledger"
        ).fetchall()
        tokens = 0
        cost = Decimal("0")
        for row in rows:
            if row["state"] == "settled":
                tokens += row["actual_input_tokens"] + row["actual_output_tokens"]
            else:
                tokens += row["reserved_input_tokens"] + row["reserved_output_tokens"]
            cost += Decimal(str(row["estimated_cost_usd"]))
        return {"requests": len(rows), "tokens": tokens, "estimated_cost_usd": float(cost)}


def estimate_tokens(text: str) -> int:
    """Conservative UTF-8 estimate used for preflight budget reservations."""
    return (len(text.encode("utf-8")) + 2) // 3 + 4
