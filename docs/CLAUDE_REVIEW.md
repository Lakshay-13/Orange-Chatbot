# Independent implementation review

- Reviewer: Claude Opus 5.5 (`claude-opus-5-5`), max effort
- Mode: read-only; inspected a source snapshot without `.env`, `data/`, `logs/`, or `.git`
- CLI exit code: 0
- Snapshot date: 2026-11-07. Initial findings below describe that pre-Gemini/pre-fallback state; the latest implementation and test status are recorded in the dispositions below and `PROVIDER_UPDATE_SELF_REVIEW.md`.

# Independent review: Errand Agent (read-only)

I read every file in the snapshot except `.env`, `data/`, `logs/` and `.git`, and I ran nothing. So I can't confirm the claimed "70 tests passed" or "29/29 evals passed"; the snapshot does contain 70 test methods.

**Bottom line:** the safety core holds up.
- The model has no route to any booking, cancel or reschedule tool.
- Each approval is single-use and tied to a fingerprint of the exact details.
- Bookings use a per-approval idempotency key and are checked against the provider's status before success is reported.

The serious problems are:
- the LLM barely does anything;
- memory can't learn new preferences;
- the design note is missing required sections;
- ambiguous or post-restart booking outcomes can get stuck;
- a forgotten preference can still reach the model.

## Verified defects

### High

**H1. The LLM doesn't really understand requests or drive tool calls (PDF §4).**
- If the keyword parser misses a field, the agent asks the user before any model call (`errand_agent/agent.py:265-279`), so the model never fills in missing details.
- From the model's output, only service, area and action survive, and only when the same keywords appear in the user's text (`agent.py:306`, `errand_agent/intent.py:225-244`). Dates, times and budget are thrown away even though they're in the schema (`responses.py:24-37`).
- Any proposal other than `search_providers` ends the run (`agent.py:316-319`).
- The model never sees search results, because `observations` is always empty (`agent.py:285-287`). The reply it wrote *before* the search is shown next to the results (`agent.py:349`).
- Keyword misreads can't be corrected. For example, "my plumber cancelled, find another" is routed to the cancel flow (`intent.py:121-129`).
- **Fix:**
  - Call the model before deciding what's missing.
  - Accept typed fields that a deterministic check can ground in the user's text.
  - Loop up to `MAX_MODEL_TURNS`, feeding back sanitized results (IDs, prices, slots, review tags).
  - Write the comparison after the results are in.
  - Leave the authorizer unchanged.

**H2. Memory can't learn, and "remember that…" goes to a failing search.**
- New memories are only ever created inside `correct()` (`memory.py:313`). No chat, UI or API path adds a new explicit or inferred preference.
- "Remember that…" is tagged as a memory request (`intent.py:125`) but then falls into a provider search with no service. With the scripted model this fails as `invalid_service` and the task ends as FAILED (`agent.py:336-340`).
- **Fix:** add explicit writes and pending inferred writes, handle memory requests before search, and add tests.

**H3. A forgotten list-valued preference still reaches the model (PDF: "must never be included").**
- The reply discloses "preferred days Saturday, Sunday" (`agent.py:544-545`), so that text is stored in the session history. The raw trace shows it at line 10.
- Forgetting scrubs `str(value)`, which is `"['Saturday', 'Sunday']"` and never matches. Values shorter than 3 characters are skipped entirely (`memory.py:289-291`).
- The history filter only checks memories that still have a stored value (`memory.py:457-461`). Forget and reject delete the value (`memory.py:328,364`), so only superseded values are ever filtered out.
- **Fix:** tag disclosure messages with `memory_refs` so the existing filter (`memory.py:471`) catches them, and add tests for list and short values.

**H4. Ambiguous and post-restart booking outcomes get stuck, and can wedge the session.**
- Nothing re-checks an unverified booking after startup. The next complete request in that session tries an illegal state transition (`agent.py:345` vs `approvals.py:30`).
  - The user gets an HTTP 400 saying "Illegal task transition UNVERIFIED → OPTIONS_PRESENTED" (`server.py:327-328`).
  - No `run_end` is written, and a model call is spent each time.
- Restart recovery (`approvals.py:456-481`) has three gaps:
  - "Not booked but hold expired" and "details mismatch" fall through with no final state and no message to the user.
  - Cancels and reschedules interrupted mid-execution are never recovered.
  - Re-verification flips an unverified action back to "verifying" (`approvals.py:325,424-426`).
- A crash after the journal entry but before `book` is called gets status `unknown` from the mock, because the key was never seen (`mock_provider.py:430-438`). The action stays stuck even though retrying with the same key would be safe.
- **Fix:**
  - Make every recovery branch end in a final state plus a user notice.
  - Treat an unseen key as "not booked", or retry with the same key while the hold is valid.
  - Add a periodic re-check.
  - Start a fresh task instead of trying to move a stuck one.

**H5. The design note is missing required content.**
- `docs/DESIGN_NOTE.md` is mostly a comparison of prompt-injection classifiers. State model, approval enforcement, idempotency/recovery, memory and observability each get at most a clause.
- It has no orchestration trade-offs and no single- vs multi-agent rationale.
- The trace excerpt is only linked (line 11), not included as the PDF asks.
- **Fix:** rewrite to the PDF's five headings in one page and embed a short trace excerpt.

### Medium

**M1. Typing "yes" can approve a card the user never saw.**
- A typed "yes" approves the newest pending approval in the session, as long as the last stored assistant message starts with "Approval:" (`agent.py:172-186`, `458-462`).
- The background monitor can store a replacement-booking card in the active session (`proactive.py:160,199-214`). It marks the notification delivered on the server, but the open page only polls *undelivered* notifications (`server.py:237-245`, `app.js:434-442`), so the card never appears.
- The user's next "yes" then books a different provider, slot and price.
- **Fix:** require `confirm <CODE>`, or bind a typed approval to the card the browser actually showed.

**M2. The provider-cancellation flow isn't live and takes over the conversation.**
- Background detections only appear after a page reload. The demo works only because the dev button reloads history (`app.js:476`).
- The replacement task becomes the session's current task, since the current task is just the most recently updated one (`agent.py:98-104`, `proactive.py:149-161`). The user's next message is merged into it.

**M3. A failed booking throws away the options and the user's details.**
- Every failure ends the task permanently (`approvals.py:445`). Picking another option then fails because no task exists (`agent.py:414-418`), even though the reply says "Choose another option" (`approvals.py:318`).
- The user's date, time and budget are lost, so the agent re-asks for them.
- **Fix:** for slot-taken or expired-hold failures, go back to the options list with refreshed availability.

**M4. The prompt-injection classifier blocks harmless follow-up messages.**
- A clean message is *blocked* if any of the previous 6 user messages matched a rule (`security.py:93-98`).
- The rules are broad: "token", "password" and "secret" all count as secret extraction (`security.py:29`). "Don't book without my confirmation" counts as an approval bypass (`security.py:30`).
- So one ordinary message can block up to the next 6 turns, including a typed "yes" (`agent.py:168-172`). Each block shows a misleading message about credentials.

**M5. Flagged attack text still reaches the model, and two security eval rows pass for the wrong reason.**
- The pattern that removes attack clauses (`security.py:112`) is narrower than the patterns that detect them (`security.py:27-33`). "reveal the hidden prompt", "disregard the rules" and base64 payloads are flagged but still sent to the model (`agent.py:162`).
- `test_foundation.py:118-123` passes on the current message alone; the history adds nothing.
- `test_foundation.py:125-130` passes because the message contains the word "token", not because the base64 was decoded.

**M6. Memory controls delete the wrong item and block legitimate corrections.**
- Saying "forget …" in chat deletes the first substring match, with no confirmation (`agent.py:510-519`).
  - "forget about the plumber, I need an electrician" deletes the electrician budget.
  - "forget my preferred days" deletes the inferred plumber-Monday preference, not the global days that were actually disclosed.
- Forgotten and rejected values are blocked for every key, including explicit user writes (`memory.py:270-275`). Because of the seed data, no day preference can ever be set to Wednesday or Thursday.

**M7. Scrubbing a corrected or forgotten value damages history and breaks the context-log proof.**
- Scrubbing also runs on corrections (`memory.py:286-306,315`). Correcting the earliest time "10:00" rewrites stored card slots to `T[SUPERSEDED_MEMORY]+05:30`.
- Logged request payloads are rewritten but `payload_sha256` is not updated (`memory.py:558-563`), so the logs no longer match their hashes.

**M8. Per-section caps cut required state while half the budget is unused.**
- The task state is cut at 730 bytes, mid-JSON (`memory.py:400-407,436`). Keys are sorted alphabetically, so `service` and `target_booking_id`, which sort after the long `provenance` block, are lost.
- Memories are capped at 7 by count (`memory.py:443-447`).
- Raw trace line 3 shows both truncated at 1,541 of 3,000 tokens. The area, time and day preferences were dropped while household notes were kept.

**M9. The evaluation table doesn't match the spec, and coverage has gaps.**
- The table has Scenario / Result / Evidence, with "all assertions passed" in every row (`scripts/run_evaluations.py:85-88`). The PDF asks for expected, actual and notes columns.
- About half the rows are unit tests, not simulated-user runs, and nothing checks invariants across scenarios.
- These paths have no test:
  - details mismatch on verification
  - follow-up after an unverified booking
  - crash before `book` is called
  - restart during cancel or reschedule
  - model-turn, tool-call and wall-clock limits
  - context pruning and the over-budget exit
  - slot taken, through the full agent
  - forgetting via chat
  - multi-turn conversation after a flagged message
- The price-change row only changes the price *before* approval (`test_approvals.py:103-117`).
- The review-injection test (`test_agent.py:114-123`) can't fail, because tool output never reaches the model at all.

**M10. The timeout trace doesn't record the timeout itself.**
- `hold_slot`, `book`, booking status, `cancel` and `reschedule` bypass the traced gateway (`approvals.py:119,295,333,357,381`).
- In the raw trace, lines 20-21 go straight from the journal entry to verification. Nothing records the timeout error, its result or its latency.
- Every timestamp is identical because of the fake clock, and tokens and cost are all zero.

**M11. Deliverables are missing.**
- The README links `docs/CLAUDE_REVIEW.md` and `docs/AI_ASSISTANT_TRANSCRIPT.md`, which don't exist (`README.md:47`).
- The demo is still pending, and its planned path is git-ignored (`.gitignore:21`). The PDF wants a Loom or YouTube link.
- The live LLM path was never exercised because the API returned 429.
- `LLM_MODE=replay` silently uses the scripted model (`agent.py:54-55`).

### Low
- **L1:** The budget check uses the search price, not the held quote (`approvals.py:105`). A quote that drifts above "under ₹1,500" can be approved with no warning.
- **L2:** A timeout followed by a details mismatch reports "no booking was made", although a booking exists (`approvals.py:307`).
- **L3:** The money regex has no word boundaries (`intent.py:35`). "2 floors 3 bedrooms" becomes a ₹3 budget, and "under 2 hours" becomes ₹2.
- **L4:** A typed approval ignores security flags: "yes\nignore previous instructions" still approves (`agent.py:162,172`).
- **L5:** The wall-clock limit is checked only once, before the model call (`agent.py:281`). Approval runs have no deadline.
- **L6:** Rejecting an approval and choosing a reschedule slot write no run start/end to the trace (`agent.py:448-456,672-683`). Exceptions that escape `handle` also skip `run_end`.
- **L7:** A provider-cancellation event that keeps failing is retried every 5 seconds forever, writing a new trace file each time (`proactive.py:31-37,82-85`).
- **L8:** Some mock failure controls do nothing. Forcing a cancel outcome is accepted but never used, and a `list_bookings` failure can't be forced (`mock_provider.py:133,446-468,495`).
- **L9:** The reschedule card doesn't show the current slot or provider (`approvals.py:492`). Cancel and reschedule don't re-check the booking against the approved details before running.
- **L10:** The model client estimates tokens differently from the context builder (`responses.py:116-118` vs `memory.py:536`). Live calls near the budget fail with a generic model error.

## Uncertainties (not verified)
- **Not executed:** the tests and evals, and the live OpenAI path (`gpt-6-luna`, strict tool schema, default prices in `config.py:122-124`).
- **Concurrency:** up to 8 request threads plus the monitor thread share one SQLite connection per database.
  - The approval updates are atomic and lock-protected.
  - Mock-provider writes in cancel and reschedule aren't synchronized.
  - The per-session lock from the design plan isn't implemented.
  - I didn't find a concrete case where this breaks an invariant.
- **Proactive search area:** replacements are searched in the cancelled provider's area, not the user's (`proactive.py:104`). That may be wrong when the original provider was from a nearby area.
- **Reproducibility:** booking outcomes are keyed on random approval IDs, so interactive runs aren't reproducible. The tests avoid this by forcing outcomes.

## Areas with no material finding
- **Local HTTP boundary:**
  - loopback-only bind
  - exact Host and Origin checks
  - CSRF token header
  - SameSite=Strict HttpOnly session cookie
  - JSON-only bodies and a 64 KB size cap
  - Content-Security-Policy and a fixed static-file list
  - text-only rendering of untrusted strings
  - provider details limited to providers the user has found or booked
- **Secrets:**
  - `.env` is read but never written
  - the API key is redacted from traces and messages
  - the Authorization header is never logged
  - `.env.example` holds placeholders only
  - a secret-scan script is included
- **Tool authorization:** the model has no route to booking, cancel or reschedule; search arguments must match the validated request; the user ID comes from the session; ownership is checked before cancel and reschedule.
- **Core booking idempotency:** one key per approval, single-use approvals, at most one booking per hold, status checked after every write, same-key retry only after the provider says "not booked", and repeated or replayed confirmations are safe.
- **Prompt injection in provider reviews:** raw reviews never reach the model, and the UI labels them as untrusted.
- **Spend ledger:** reservations persist across restarts, uncertain calls are counted, and quota errors aren't retried.

As you asked, I stayed read-only and created no files, including the plan file that plan mode would normally write.

---

# Codex finding-by-finding disposition

Codex inspected each numbered finding against the post-review source and reran offline regression tests. “Fixed” means the described unsafe or incomplete behavior is now covered by application code and/or the named test; it does not imply that the live API path succeeded.

| Finding | Disposition | Evidence |
| --- | --- | --- |
| H1 — Model orchestration and intent | Fixed. The model is called before missing-field follow-up, grounded typed patches may refine deterministic extraction, and a second bounded turn receives sanitized provider observations. The model can select only an observed provider ID; code supplies prices, availability, and action authority. | `tests/test_agent.py`: `test_asks_only_for_missing_details_then_preserves_context`, `test_full_discovery_uses_model_proposal_and_returns_matching_providers`; `tests/test_intent.py`: `test_model_patch_accepts_only_deterministically_grounded_date_time_and_budget` |
| H2 — Explicit memory writes | Fixed. Supported “remember” chat requests create explicit memories with evidence; inspect/correct/forget controls remain available. | `tests/test_agent.py`: `test_explicit_memory_write_is_provenanced_and_forget_excludes_its_messages`; `tests/test_memory_context.py`: `test_edited_memory_rejects_out_of_catalog_values_and_configured_secrets` |
| H3 — Forgotten preference in history | Fixed. Messages that disclose or establish a memory carry memory IDs; future context excludes messages tied to forgotten, rejected, or superseded records without rewriting audit text or hashes. | `tests/test_memory_context.py`: `test_forget_preserves_audit_history_but_excludes_referenced_content_and_blocks_inference`, `test_correction_supersedes_old_value_without_mutating_conversation_or_context_audit` |
| H4 — Restart/ambiguous outcomes and wedged sessions | Fixed. Startup and next-turn recovery reconcile approved/executing/verifying actions; book recovery checks the hold’s authoritative status and handles booked, not-booked, expired, mismatch, and unavailable states. Cancel/reschedule recovery reuses the persisted key. Unverified tasks are excluded from new intent selection. | `tests/test_approvals.py`: `test_restart_after_provider_commit_verifies_without_duplicate`, `test_recovery_of_pre_call_crash_after_expired_hold_reports_not_booked`, `test_cancel_recovery_after_first_503_retries_idempotently`, `test_reschedule_restart_uses_same_key_and_verifies_state`, `test_booking_response_with_material_status_mismatch_is_not_success` |
| H5 — Design note | Fixed. The current note is 442 words and includes state/approval, idempotency/recovery, memory, observability, classifier trade-offs, single-agent rationale, and an embedded timeout trace excerpt. | `docs/DESIGN_NOTE.md`; raw trace `docs/traces/raw_timeout_after_possible_success.jsonl` |
| M1 — Typed “yes” and unseen card | Fixed. Plain “yes” never approves. Typed approval requires an exact code from a current card and an unflagged message; the browser approval endpoint binds the click to action ID and fingerprint. | `tests/test_agent.py`: `test_plain_yes_cannot_approve_a_card_but_exact_code_can`, `test_attack_suffix_cannot_turn_typed_approval_code_into_approval`; `tests/test_server.py`: `test_pending_approval_cards_are_restored_after_page_reload` |
| M2 — Proactive UI refresh/task takeover | Fixed. The browser polls history, approvals, and outbox; active approval cards are restored, and proactive tasks are excluded from the ordinary current-task lookup. | `tests/test_server.py`: `test_proactive_replacement_card_is_restored_without_taking_over_chat`; `tests/test_proactive.py`: `test_cancellation_is_detected_without_user_message_and_requires_fresh_approval` |
| M3 — Failed booking loses options | Fixed. Slot-taken and expired-hold outcomes return the task to `OPTIONS_PRESENTED`; a fresh hold/quote is required before approval. | `tests/test_agent.py`: `test_slot_taken_keeps_other_options_available_for_fresh_approval`; `tests/test_approvals.py`: `test_approval_expiry_returns_booking_task_to_options_for_fresh_quote` |
| M4 — Benign requests blocked by classifier | Fixed. Benign “don’t book without confirmation” and unrelated turns after a flagged attack are allowed; only a narrow short continuation of an attack is blocked. | `tests/test_foundation.py`: `test_user_prohibition_against_booking_without_confirmation_is_benign`, `test_old_attack_does_not_poison_unrelated_followup_but_continuation_is_blocked` |
| M5 — Attack text reaches model / weak encoded-attack test | Fixed. Encoded payloads are decoded for classification, suspicious spans are stripped, and safe service intent is preserved. The regression now asserts an obfuscation-specific rule ID and checks the actual model input. | `tests/test_foundation.py`: `test_obfuscated_base64_payload_is_flagged`; `tests/test_agent.py`: `test_obfuscated_attack_is_removed_before_model_and_service_intent_survives`, `test_mixed_prompt_injection_keeps_safe_request_and_sends_only_safe_text` |
| M6 — Ambiguous memory deletion/reintroduction | Fixed. Chat forget resolves only an exact ID/key/value; an ambiguous target asks the user and deletes nothing. Explicit deliberate reintroduction is allowed, while forgotten inferred facts cannot silently return. | `tests/test_agent.py`: `test_ambiguous_memory_forget_does_not_remove_any_preference`; `tests/test_memory_context.py`: `test_forget_preserves_audit_history_but_excludes_referenced_content_and_blocks_inference` |
| M7 — Scrubbed audit records/hash drift | Fixed. Corrections/forgetting update memory references and live task state, not the original conversation or exact sent-payload log. | `tests/test_memory_context.py`: `test_correction_supersedes_old_value_without_mutating_conversation_or_context_audit`, `test_forgetting_or_correcting_memory_scrubs_live_task_and_invalidates_its_approval` |
| M8 — Truncated task JSON and weak context allocation | Fixed. Task state stays valid JSON; optional provenance/preferences are dropped as complete fields, core facts are retained, and whole memory/history/observation records are budgeted. | `tests/test_memory_context.py`: `test_task_json_stays_valid_and_core_facts_survive_preference_truncation`, `test_context_budget_is_enforced_and_manifest_hash_matches_payload` |
| M9 — Evaluation table/coverage | Fixed. The runner executes 45 selected offline invariant tests and writes Scenario / Expected / Actual / Pass/Fail / Notes from the run. It includes adversarial, approval, recovery, credential precedence, fallback, memory, proactive, context, and limit paths. | Generated results: `docs/EVALUATION.md`; rerun `python3 scripts/run_evaluations.py` |
| M10 — Timeout absent from trace | Fixed. `book` emits `tool_call`, ambiguous `tool_error` with latency, exactly one `get_booking_status` call/result, and verified completion. The exporter asserts one booking call, one timeout, one status query, and one provider booking. Hold calls are traced too. | `docs/traces/raw_timeout_after_possible_success.jsonl`; `docs/traces/annotated_timeout_excerpt.md`; `scripts/export_timeout_trace.py` |
| M11 — Missing deliverables/live run | All required file deliverables are present, including the verified 3:13 chat-and-trace recording. A subsequent live local HTTP run succeeded with Gemini after fixing project `.env` credential precedence; it completed search, exact approval, mock booking verification, and duplicate-confirmation replay. The earlier 402/429 errors came from different inherited process keys. The follow-up Claude review for this provider update stalled; its limitation and Codex self-review are recorded separately. | `demo/errand_agent_demo.mov`; `docs/traces/live_gemini_booking_e2e.jsonl`; `docs/PROVIDER_UPDATE_SELF_REVIEW.md`; `PROGRESS.md` |
| L1 — Search price vs held quote | Fixed. The held quote is checked against the user's budget before an approval record is created. | `tests/test_approvals.py`: `test_current_quote_above_budget_never_creates_an_approval` |
| L2 — Mismatch/unknown status reported as no booking | Fixed. A mismatched status is surfaced as `verification_mismatch`; unknown/unavailable remains unverified and is never claimed as success or retried blindly. | `tests/test_approvals.py`: `test_booking_response_with_material_status_mismatch_is_not_success`, `test_execution_call_limit_is_durable_and_never_claims_unverified_success` |
| L3 — Amount parser reads hours/floors as price | Fixed. Unit suffixes are skipped when extracting unqualified amounts. | `tests/test_intent.py`: `test_time_or_floor_counts_are_not_misread_as_price` |
| L4 — Security flags ignored by typed approval | Fixed. `confirm CODE` is parsed only when classification allows the complete message; a stripped attack suffix cannot authorize the action. | `tests/test_agent.py`: `test_attack_suffix_cannot_turn_typed_approval_code_into_approval` |
| L5 — Wall-clock and execution limits | Fixed. Model turns, read-tool calls, action calls, request timeouts, retries, and overall wall time are bounded. Exits return explicit safe messages; unexpected exceptions are persisted with `run_end`. | `tests/test_agent.py`: `test_model_tool_and_wall_limits_have_clear_exits`, `test_unexpected_agent_exception_is_persisted_as_clear_run_result`; `tests/test_approvals.py`: `test_execution_call_limit_is_durable_and_never_claims_unverified_success` |
| L6 — Missing run traces on direct actions/exceptions | Fixed. Approval, reject, reschedule selection, direct provider calls, and unexpected chat exceptions emit structured run outcomes. | Source paths `errand_agent/agent.py` and `errand_agent/approvals.py`; `tests/test_agent.py`: `test_unexpected_agent_exception_is_persisted_as_clear_run_result`; `tests/test_frontend_feedback.cjs` covers final UI feedback |
| L7 — Proactive retry loop | Fixed. Event processing retries use persisted exponential backoff, stop after three attempts, and queue a terminal user notice. | `tests/test_proactive.py`: `test_failed_proactive_replacement_uses_backoff_and_stops_after_three_attempts` |
| L8 — Mock failure controls incomplete | Fixed. Forced errors/outcomes are implemented for search, details, holds, bookings, status, cancellation, rescheduling, and booking lists. | `tests/test_mock_provider.py` and `tests/test_proactive.py` failure/retry cases |
| L9 — Stale cancel/reschedule card | Fixed. Cards include booking identity/current slot; the application re-reads and compares current booking owner, provider, service, status, and slot before the write. | `tests/test_approvals.py`: `test_cancel_and_reschedule_fail_closed_when_booking_changes_after_card` |
| L10 — Model/context token estimate drift | Fixed. The live client estimates the serialized request payload that it sends, and the context audit stores the exact serialized payload plus hash. | `tests/test_model_budget.py`: `test_openai_client_uses_verified_model_and_context_without_live_network`; `tests/test_memory_context.py`: `test_context_budget_is_enforced_and_manifest_hash_matches_payload` |

## Additional reviewer uncertainties

- **Nearby-area replacement:** accepted and fixed. Proactive replacement now prefers the relevant saved area, then falls back to the cancelled provider's area when the user has no saved area. `tests/test_proactive.py:test_replacement_uses_saved_area_when_cancelled_provider_served_nearby_area` verifies the actual search-tool arguments.
- **Concurrent cancel/reschedule writes:** accepted as a hardening item. The mock serializes these operations under its lock; `tests/test_mock_provider.py:test_concurrent_cancel_replay_with_same_key_is_serialized` verifies same-key cancellation replay produces one idempotency record and one final cancellation.
- **Reproducibility:** interactive random-like outcomes are deterministic for a seed and idempotency key; tests and demos can force named outcomes. No conclusion depends on a live provider.
