#!/usr/bin/env python3
"""Exercise the real app path with the deterministic mock's ambiguous-booking case."""

from __future__ import annotations

import json
import sys
import tempfile
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from errand_agent.agent import ErrandAgent
from errand_agent.clock import FakeClock, IST
from errand_agent.config import AppConfig
from errand_agent.mock_provider import MockProvider
from errand_agent.responses import ScriptedModel
from errand_agent.storage import AgentStore


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="errand-trace-") as temporary:
        root = Path(temporary)
        config = AppConfig.from_env(root, {
            "LLM_MODE": "scripted", "DATABASE_PATH": "agent.sqlite3",
            "MOCK_DATABASE_PATH": "mock.sqlite3", "TRACE_DIR": "traces",
            "SCENARIO_SEED": "17", "MAX_RETRIES": "2",
        })
        clock = FakeClock(datetime(2026, 11, 7, 10, 0, tzinfo=IST))
        store = AgentStore(config.database_path)
        provider = MockProvider(config.mock_database_path, clock, seed=config.scenario_seed)
        agent = ErrandAgent(config, store, provider, clock=clock, model=ScriptedModel())
        try:
            agent.memories.seed_demo()
            session_id = agent.create_session()
            discovery = agent.handle(
                session_id,
                "Find a plumber in Jaipur this week, mornings only, under ₹1,500.",
                "trace-discovery",
            )
            if discovery.get("exit_reason") != "completed" or not discovery.get("providers"):
                raise RuntimeError("The deterministic discovery scenario did not produce provider options.")
            option = discovery["providers"][0]
            slot = option["available_slots"][0]
            selection = agent.select_option(session_id, option["provider_id"], slot)
            action = selection["approval"]
            agent.provider.force_next("book", "timeout_booked")
            outcome = agent.approve_action(
                session_id, action["action_id"], action["fingerprint"], "trace-explicit-approval",
            )
            if outcome.get("status") != "confirmed" or outcome.get("action_status") != "done":
                raise RuntimeError("The scripted timeout scenario did not reach a verified booking.")

            booking_rows = agent.provider.list_bookings("u_demo")
            if len(booking_rows) != 1:
                raise RuntimeError(f"Expected one durable booking after verification; observed {len(booking_rows)}.")
            booking_id = booking_rows[0]["booking_id"]
            events: list[dict] = []
            for run_id in (discovery["run_id"], selection["run_id"], outcome["run_id"]):
                path = root / "traces" / f"{run_id}.jsonl"
                events.extend(json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip())
            approval_events = [event for event in events if event["event"] == "approval_decision"]
            book_calls = [event for event in events if event["event"] == "tool_call" and event["data"].get("tool") == "book"]
            status_checks = [event for event in events if event["event"] == "execution_journal" and event["data"].get("journal_event") == "status_check"]
            status_calls = [event for event in events if event["event"] == "tool_call" and event["data"].get("tool") == "get_booking_status"]
            timeout_errors = [event for event in events if event["event"] == "tool_error" and event["data"].get("tool") == "book" and event["data"].get("code") == "timeout" and event["data"].get("ambiguous") is True]
            verification = [event for event in events if event["event"] == "verification_result" and event["data"].get("operation") == "book"]
            if len(approval_events) != 1 or approval_events[0]["data"].get("decision") != "approved":
                raise RuntimeError("The generated trace is missing the explicit application approval event.")
            if len(book_calls) != 1 or len(status_calls) != 1 or len(status_checks) != 1 or len(timeout_errors) != 1 or not verification or verification[-1]["data"].get("result", {}).get("status") != "booked":
                raise RuntimeError("The generated trace does not prove one booking call and authoritative verification.")

            output_dir = ROOT / "docs" / "traces"
            output_dir.mkdir(parents=True, exist_ok=True)
            raw_path = output_dir / "raw_timeout_after_possible_success.jsonl"
            raw_path.write_text("".join(json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n" for event in events), encoding="utf-8")

            excerpt_events = [
                next(event for event in events if event["event"] == "approval_decision"),
                book_calls[0],
                timeout_errors[0],
                next(event for event in events if event["event"] == "state_transition" and event["data"].get("transition_event") == "booking_verification_started"),
                status_calls[0],
                verification[-1],
                next(event for event in events if event["event"] == "execution_result" and event["data"].get("action_status") == "done"),
            ]
            excerpt_lines = [
                "# Annotated timeout-after-possible-success trace",
                "",
                "Generated by `python3 scripts/export_timeout_trace.py` using the application approval service, a temporary SQLite database, and the seeded mock provider. The model proposal is offline scripted; the booking, timeout, status query, and verification run through production application code. No real provider or payment system is called.",
                "",
                f"**Outcome:** the user explicitly approved the exact quote; the mock provider committed booking `{booking_id}` and then returned an ambiguous timeout; the application queried authoritative status and verified `booked`. The application issued one booking call and the database contains exactly one booking.",
                "",
                "| Step | Event | What it proves |",
                "| --- | --- | --- |",
                "| 1 | `approval_decision: approved` | Approval was explicit and bound to the action fingerprint. |",
                "| 2 | `tool_call: book` | Exactly one booking call was issued with the persisted idempotency key. |",
                "| 3 | `tool_error: timeout, ambiguous=true` | The provider committed, but the response was lost; the trace records that ambiguous error and its latency. |",
                "| 4 | `state_transition: booking_verification_started` | The agent starts verification before any retry. |",
                "| 5 | `tool_call: get_booking_status` | The agent queries the provider's authoritative status. |",
                "| 6 | `verification_result: booked` | The authoritative state matches the approved hold. |",
                "| 7 | `execution_result: done` | Only after verification does the task become confirmed. |",
                "",
                "```jsonl",
            ]
            excerpt_lines.extend(json.dumps(event, ensure_ascii=False, sort_keys=True) for event in excerpt_events)
            excerpt_lines.extend(["```", ""])
            (output_dir / "annotated_timeout_excerpt.md").write_text("\n".join(excerpt_lines), encoding="utf-8")
            print(json.dumps({"trace": str(raw_path.relative_to(ROOT)), "events": len(events), "booking_calls": len(book_calls), "timeout_errors": len(timeout_errors), "status_queries": len(status_calls), "status_checks": len(status_checks), "bookings": len(booking_rows), "verified_status": verification[-1]["data"]["result"]["status"]}, sort_keys=True))
        finally:
            agent.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
