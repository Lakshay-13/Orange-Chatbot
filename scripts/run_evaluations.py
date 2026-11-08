#!/usr/bin/env python3
"""Run the offline simulated-user invariant matrix and write its actual results."""

from __future__ import annotations

import datetime as dt
import io
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

CASES = [
    ("Normal provider discovery", "Find matching providers and preserve price/time filters", "test_agent.AgentTests.test_full_discovery_uses_model_proposal_and_returns_matching_providers"),
    ("Only missing details are requested", "Ask for missing fields and retain supplied service/area", "test_agent.AgentTests.test_asks_only_for_missing_details_then_preserves_context"),
    ("Same provider preference preserves time window", "Resolve prior provider without dropping time constraint", "test_agent.AgentTests.test_same_provider_as_last_time_is_resolved_without_losing_time_constraint"),
    ("Correction changes only the named detail", "Keep unrelated intent and invalidate stale options", "test_intent.IntentTests.test_correction_changes_only_named_detail_and_invalidates_presented_choices"),
    ("Direct prompt and secret extraction", "Block request without calling model or exposing secrets", "test_agent.AgentTests.test_direct_secret_extraction_attack_is_blocked_before_model"),
    ("Encoded malicious payload with normal request", "Keep service intent but remove the decoded attack before model context", "test_agent.AgentTests.test_obfuscated_attack_is_removed_before_model_and_service_intent_survives"),
    ("Mixed safe request and malicious instruction", "Strip unsafe instruction and preserve legitimate request", "test_agent.AgentTests.test_mixed_prompt_injection_keeps_safe_request_and_sends_only_safe_text"),
    ("Obfuscated base64 instruction", "Detect encoded malicious instruction", "test_foundation.SecurityClassifierTests.test_obfuscated_base64_payload_is_flagged"),
    ("Multi-step attack across conversation history", "Block short continuation of a prior attack", "test_foundation.SecurityClassifierTests.test_multi_step_history_is_considered"),
    ("Provider review injection stays outside model context", "Show review as untrusted and omit it from model input", "test_agent.AgentTests.test_prompt_injection_in_provider_review_never_enters_model_context"),
    ("Model cannot invoke writes or claim success", "Reject unverified claims and write-tool proposals", "test_agent.AgentTests.test_model_cannot_claim_unverified_success_or_trigger_write_tool"),
    ("Exact approval and duplicate confirmation", "Require bound approval and create one booking", "test_agent.AgentTests.test_approval_button_is_exact_and_repeated_confirmation_does_not_duplicate"),
    ("Gemini error uses configured OpenAI fallback", "Use OpenAI only after a primary Gemini model error", "test_model_router.ModelRouterTests.test_gemini_error_uses_openai_and_returns_traced_fallback_metadata"),
    ("Local context failures do not trigger fallback", "Avoid a provider call when the local payload is invalid", "test_model_router.ModelRouterTests.test_local_context_error_does_not_call_openai"),
    ("Fallback request context is audited exactly", "Persist the exact OpenAI fallback payload and its hash", "test_agent.AgentTests.test_fallback_model_is_traced_and_exact_openai_context_is_audited"),
    ("Project credentials outrank inherited credentials", "Use project .env API credentials while retaining process settings", "test_foundation.EnvFileTests.test_process_settings_override_file_but_project_credentials_win"),
    ("Approval codes stay out of persisted traces", "Redact one-time approval codes in SQLite and JSONL", "test_foundation.RedactionTests.test_trace_redacts_one_time_approval_codes_from_all_persisted_outputs"),
    ("Injection appended to typed approval", "Do not accept an approval code when classification flags the same message", "test_agent.AgentTests.test_attack_suffix_cannot_turn_typed_approval_code_into_approval"),
    ("Mock commit persists across a timeout", "Recover one committed booking after provider restart", "test_mock_provider.MockProviderTests.test_timeout_after_possible_success_survives_process_restart"),
    ("Changed price and expired hold require new approval", "Invalidate stale approval and require current quote", "test_approvals.ApprovalTests.test_expired_hold_requote_invalidates_old_approval_and_price_change_needs_new_approval"),
    ("Timeout after possible success is status-verified", "Query authoritative status and report one verified booking", "test_approvals.ApprovalTests.test_approval_is_bound_to_provider_slot_and_quote_and_timeout_is_verified"),
    ("Provider status details mismatch", "Do not claim success when provider returns a different slot", "test_approvals.ApprovalTests.test_booking_response_with_material_status_mismatch_is_not_success"),
    ("Timeout without booking retries same key", "Retry only after not-booked status; create one booking", "test_approvals.ApprovalTests.test_timeout_not_booked_retries_same_key_after_authoritative_status"),
    ("Restart after provider commit recovers without duplicate", "Verify committed state without another booking", "test_approvals.ApprovalTests.test_restart_after_provider_commit_verifies_without_duplicate"),
    ("Slot taken is explicit and creates no booking", "Report conflict and preserve no-booking state", "test_mock_provider.MockProviderTests.test_slot_taken_is_explicit_and_does_not_create_booking"),
    ("Elapsed slots today are excluded", "Return only future time slots", "test_mock_provider.MockProviderTests.test_search_excludes_elapsed_slots_today"),
    ("Transient cancellation failure retries same key", "Retry 503 with stable key and verify cancellation", "test_approvals.ApprovalTests.test_cancel_uses_same_key_after_503_and_verifies"),
    ("Concurrent cancel replay with the same key", "Serialize writes and preserve one cancellation record", "test_mock_provider.MockProviderTests.test_concurrent_cancel_replay_with_same_key_is_serialized"),
    ("Reschedule conflict is not reported as success", "Preserve old booking slot after conflict", "test_approvals.ApprovalTests.test_reschedule_conflict_is_not_reported_as_success"),
    ("Cancel/reschedule card after booking changes", "Fail closed before write when current provider or slot differs from card", "test_approvals.ApprovalTests.test_cancel_and_reschedule_fail_closed_when_booking_changes_after_card"),
    ("Forgotten and corrected memory is scrubbed from live task", "Remove stale context and invalidate approval", "test_memory_context.MemoryContextTests.test_forgetting_or_correcting_memory_scrubs_live_task_and_invalidates_its_approval"),
    ("Forgotten memory cannot be reintroduced", "Preserve forgotten status and exclude linked history", "test_memory_context.MemoryContextTests.test_forget_preserves_audit_history_but_excludes_referenced_content_and_blocks_inference"),
    ("Ambiguous memory forget", "Ask for an exact target and leave preferences active", "test_agent.AgentTests.test_ambiguous_memory_forget_does_not_remove_any_preference"),
    ("Context token budget and payload hash", "Stay within budget and verify logged request hash", "test_memory_context.MemoryContextTests.test_context_budget_is_enforced_and_manifest_hash_matches_payload"),
    ("Seeded memory and prior conversation stay bounded", "Seed 52 memories/240 messages without sending all history", "test_memory_context.MemoryContextTests.test_seed_contains_52_memories_and_240_prior_messages"),
    ("Provider cancellation detected and replacement requires approval", "Notify and present replacement with no unapproved booking", "test_proactive.ProactiveTests.test_cancellation_is_detected_without_user_message_and_requires_fresh_approval"),
    ("Replacement respects saved service area", "Search the user's saved area when cancelled provider served nearby", "test_proactive.ProactiveTests.test_replacement_uses_saved_area_when_cancelled_provider_served_nearby_area"),
    ("Proactive replacement restores across browser refresh", "Restore the pending action and keep the proactive task separate from chat intent", "test_server.ServerTests.test_proactive_replacement_card_is_restored_without_taking_over_chat"),
    ("Inactive user receives replacement options without hold", "Queue replacement options without creating a hold", "test_proactive.ProactiveTests.test_inactive_user_gets_replacement_options_in_outbox_without_a_hold"),
    ("Execution limit fails closed", "Persist explicit unverified result; do not claim success", "test_approvals.ApprovalTests.test_execution_call_limit_is_durable_and_never_claims_unverified_success"),
    ("Model/tool/wall limits and unexpected errors", "Return clear bounded outcomes and persist run_end", "test_agent.AgentTests.test_model_tool_and_wall_limits_have_clear_exits"),
    ("Unexpected exception outcome", "Persist safe result and run_end without exception details", "test_agent.AgentTests.test_unexpected_agent_exception_is_persisted_as_clear_run_result"),
    ("Browser chat endpoint enforces CSRF", "Reject state change without valid CSRF token", "test_server.ServerTests.test_csrf_protected_chat_api_returns_security_outcome"),
    ("Server shutdown drains in-flight handlers", "Wait for HTTP handlers before closing shared SQLite runtime", "test_server.ServerTests.test_server_close_waits_for_inflight_handlers"),
    ("Budget/request limits block further model calls", "Block before exceeding configured budget/request cap", "test_model_budget.ModelBudgetTests.test_spend_and_request_limits_block_before_another_request"),
]


def main() -> int:
    timestamp = dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()
    rows: list[tuple[str, str, str, str, str, str]] = []
    failures: list[tuple[str, str]] = []
    for title, expected, dotted_name in CASES:
        suite = unittest.defaultTestLoader.loadTestsFromName(dotted_name)
        captured = io.StringIO()
        result = unittest.TextTestRunner(stream=captured, verbosity=0).run(suite)
        if result.testsRun != 1:
            status = "ERROR"
            actual = f"Selection resolved to {result.testsRun} tests"
            failures.append((title, captured.getvalue() or actual))
        elif result.failures or result.errors:
            status = "FAIL"
            failure_text = "\n".join(item[1] for item in result.failures + result.errors)
            actual = failure_text.strip().splitlines()[-1] if failure_text.strip() else "Assertion or test error"
            failures.append((title, failure_text))
        elif result.skipped:
            status = "SKIP"
            actual = "Test skipped by unittest"
        else:
            status = "PASS"
            actual = "All state and invariant assertions met"
        note = f"1 offline test: `{dotted_name}`"
        rows.append((title, expected, actual, status, note, dotted_name))
        print(f"{status:5} {title}")

    passed = sum(status == "PASS" for _, _, _, status, _, _ in rows)
    output = [
        "# Offline Simulated-User Evaluation",
        "",
        f"Generated from an actual run at **{timestamp}**. No live model/API calls are made by this runner.",
        "",
        f"**Result: {passed}/{len(rows)} passed; {len(failures)} failed or errored.**",
        "",
        "Each row runs the named deterministic `unittest` scenario and checks application invariants/state, not generated prose alone.",
        "",
        "| Scenario | Expected | Actual | Pass/Fail | Notes |",
        "| --- | --- | --- | --- | --- |",
    ]
    output.extend(f"| {title} | {expected} | {actual} | **{status}** | {note} |" for title, expected, actual, status, note, _ in rows)
    output.extend([
        "",
        "The timeout-after-possible-success case uses the durable mock provider scenario: the booking may commit before the simulated timeout, then the agent queries authoritative status and records exactly one booking.",
        "",
        "Re-run with `python3 scripts/run_evaluations.py`. The full unit suite is separate and can be run with `python3 -m unittest discover -s tests -v`.",
        "",
    ])
    report = ROOT / "docs" / "EVALUATION.md"
    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_text("\n".join(output), encoding="utf-8")
    if failures:
        for title, detail in failures:
            print(f"\n--- {title} ---\n{detail}", file=sys.stderr)
    print(f"Wrote {report.relative_to(ROOT)}")
    return 0 if not failures and passed == len(rows) else 1


if __name__ == "__main__":
    raise SystemExit(main())
