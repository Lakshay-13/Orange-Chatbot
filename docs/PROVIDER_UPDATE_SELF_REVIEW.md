# Provider, credential, and trace follow-up review

## Independent review attempt

The earlier Claude Opus 5.5 review and its finding-by-finding dispositions are recorded in [CLAUDE_REVIEW.md](CLAUDE_REVIEW.md). For these provider and security changes, Claude Code 2.1.319 was invoked with the verified `claude-opus-5-5` model ID, `--effort max`, `--tools ""`, plan permission mode, and session persistence disabled. The curated snapshot included the full assignment PDF text and relevant implementation, tests, and docs. `.env`, all provider credentials, databases, and runtime logs were excluded; the Claude process environment did not contain the OpenAI or Google API keys. The process returned no stdout or stderr for about 20 minutes while its network connection remained active. It was stopped with SIGTERM (exit 143), so this follow-up did not produce Claude findings. No failure output was treated as a code-review result.

## Codex self-review of the provider update

1. **Credential source was the cause of the failed live requests.** Before the fix, `AppConfig.from_env` loaded project `.env` values, then overlaid process variables. Both provider values in the process environment differed from the project file. A direct request through the then-current config reproduced Gemini HTTP 402 and OpenAI HTTP 429. Selecting the project-file values made each provider return a structured response. After the fix, the actual local HTTP app used Gemini for search and comparison, then completed a mock booking. The project keys themselves were not displayed.

2. **The source-precedence fix is narrowly scoped.** Project-file provider credentials now win for this local checkout. Other settings still follow normal process-over-file precedence. If a project file has no key, the process key remains supported. Regression coverage checks both cases, and the local HTTP server reports Gemini primary with OpenAI fallback ready.

3. **Fallback remains bounded and does not override local enforcement.** `PrimaryFallbackModel` tries OpenAI after a Gemini `ModelError`, preserves both sanitized provider codes on failure, and passes only the remaining wall-clock allowance. Local context and budget errors do not trigger fallback. Both providers share the persistent ledger while each uses its own configured rates. Offline tests cover routing, blocked local errors, wall limits, and exact fallback-payload audit. OpenAI returned a successful direct structured response using the project key. A live fallback was not exercised because Gemini succeeded in the full HTTP run.

4. **Trace authorization data is now redacted.** The live run exposed a short-lived confirmation value in a user-facing trace string. `Redactor` now removes that exact labeled value before either SQLite or JSONL persistence. Regression coverage checks both persisted destinations. The previously generated raw timeout trace and demo JSONL artifacts were redacted; a scan found no remaining labeled confirmation values in `docs/` or `logs/`.

5. **The live write remained under application authorization.** The HTTP run used the seeded mock provider only. The model proposed a search, deterministic code authorized the read tool, and the test user selected a provider/slot. The approval card bound the provider, slot, and ₹887 quote. After explicit approval, the app called `book`, queried authoritative status, and recorded one confirmed booking. Replaying the same request ID returned `replayed=true` and the booking list still contained one record. The raw sanitized trace and annotated sequence are in `traces/live_gemini_booking_e2e.jsonl` and `traces/live_gemini_booking_e2e_annotated.md`.

6. **Validation supports the update.** The complete offline suite passed 113 tests, the actual generated evaluation passed 45/45 scenarios, the frontend test passed 1/1, compile and secret checks passed, `.env` remained unchanged, and the existing 193-second demo recording remained present at 1920×1080 and 60 fps.

## Assignment-wide coverage audit

| Requirement area | Current evidence checked |
| --- | --- |
| LLM and provider configuration | Gemini `gemini-flash-lite-latest` is primary, OpenAI `gpt-6-luna` is fallback, provider rates are separate, model effort starts low, and both project keys generated structured output. Live HTTP traces show Gemini as the actual model for both turns. |
| Security and trust boundaries | Rule-based direct/obfuscated/multi-turn classification and safe-intent retention: `tests/test_foundation.py`, `tests/test_agent.py`. Provider-review injection exclusion: `test_prompt_injection_in_provider_review_never_enters_model_context`. Deterministic tool authorization and exact approval remain in application code. The design note records why NVIDIA NeMo and Meta Prompt Guard were evaluated but not deployed. |
| Mock contracts and failures | `tests/test_mock_provider.py` covers catalog shape, injection review, holds, expiry, quote drift, slot conflict, cancel 503, cancellation events, and timeout outcomes. `tests/test_approvals.py` covers authoritative status, retry, and restart recovery. |
| Conversation and approvals | `tests/test_agent.py` covers missing fields, corrections, and model claims; `tests/test_approvals.py` binds approval to current provider, slot, and quote. The live HTTP trace proves one approved mock booking and one replay only. |
| Bounded execution and idempotency | `tests/test_model_budget.py`, `tests/test_model_router.py`, `tests/test_approvals.py`, and `tests/test_server.py` cover global budget limits, provider fallback deadlines, call limits, retries, restart recovery, and safe exits. |
| Persistent memory and context | `tests/test_memory_context.py` covers seeded memories/history, provenance, correction/forgetting, selection, fixed budget, exact payload hashes, and exclusion rules. Live manifests stayed below the 3,000-token limit. |
| Proactive follow-up | `tests/test_proactive.py` and `tests/test_server.py` cover cancellation detection, replacement selection, notification/restoration, and fresh approval without an automatic hold. |
| Evaluation | `scripts/run_evaluations.py` executed 45 named offline invariant scenarios and wrote the actual 45/45 table in `docs/EVALUATION.md`; no live API calls occur in this runner. |
| Traces and budget accounting | The timeout trace shows ambiguous booking followed by authoritative status verification. The live trace shows model calls, search, approval, booking verification, and replay. One-time approval values are redacted. The persistent ledger tracks the $5 global limit and provider usage estimates. |
| Deliverables and demo | README, `.env.example`, one-page design note, seven bug reports with regressions, coding-assistant transcript, evaluation, trace artifacts, and the verified 193-second side-by-side demo are present. |

No additional implementation defect was identified in this assignment-wide pass. The two issues found during the current update (ambient credential precedence and approval-code trace leakage) have regression tests and successful verification. The live fallback branch remains the only model-path behavior not observed under an actual provider failure; its routing and audit behavior are covered offline, and OpenAI generation itself was verified live.

## Remaining verification boundary

The Gemini-primary live path and direct OpenAI generation both succeeded with the project-file credentials. OpenAI fallback execution is validated with deterministic offline failures, but no live Gemini outage was induced merely to exercise fallback. Application live spend remains accounted in the shared ledger; the exact current summary is in [PROGRESS.md](../PROGRESS.md). The fallback review stall is the only unavailable independent check in this follow-up.
