# Progress

Updated: 2026-11-08 (Asia/Kolkata)

## Completed

- Read all three pages of the supplied assignment PDF and mapped requirements to the implementation and submission artifacts.
- Preserved the existing `.env`; it has not been written by this work. `.env` remains ignored. `.env.example` contains placeholder values only.
- Stage A completed with authenticated Claude Code using `claude-opus-5-5` at max effort. Architecture plan is in `DESIGN_PLAN.md`; the earlier independent review and dispositions are in `docs/CLAUDE_REVIEW.md`.
- The focused Claude Opus 5.5 follow-up for the provider update was launched at max effort but returned no response after about 20 minutes and was stopped. The exact limitation and a full current self-review are recorded in `docs/PROVIDER_UPDATE_SELF_REVIEW.md`.
- Implemented the local chat/trace app, security classification and intent refinement, typed model proposals, deterministic tool authorization, seeded persistent mock providers, action-bound approvals, durable idempotency and recovery, proactive replacement, persistent memory, bounded context, and global API budget ledger.
- Gemini `gemini-flash-lite-latest` is primary and OpenAI `gpt-6-luna` is fallback after a Gemini model error. Each provider has its own pricing; requests share one persistent $5 limit. Local context and budget errors do not trigger fallback.
- Fixed ambient credential precedence: the previous runtime environment contained API keys different from the project's `.env`, and `AppConfig` let those inherited values replace the project keys. The project file now wins for provider credentials in local runs; process credentials remain supported if the file has no provider key. Other process settings still override file settings.
- Fixed trace redaction for one-time approval codes. The existing raw timeout trace and demo JSONL artifacts were sanitized; newly written SQLite and JSONL traces are covered by a regression test.
- Completed a live local HTTP E2E using project `.env` credentials. Gemini made two structured calls, searched seeded mocks, returned two providers matching the saved preferences, and selected a provider. The simulated user selected its Saturday 08:00 IST slot at ₹887, reviewed the exact quote, and submitted explicit approval. The app verified the booking with authoritative mock status. Replaying the same confirmation returned `replayed=true` and left exactly one confirmed booking. No real provider or payment was contacted.
- OpenAI also returned a successful structured response when called using the project `.env` key. The routing fallback path is exercised by offline tests; it was not activated in the successful live Gemini E2E.
- Submission artifacts are present: setup README, one-page design note, bug table with seven verified bugs/regressions, sanitized coding-assistant transcript, generated evaluation, raw/annotated timeout trace, sanitized live Gemini E2E trace, and verified 3:13 screen recording showing chat and trace side by side.

## Verification

- Final full unit-suite rerun after credential-precedence and trace-redaction fixes: **113 passed**.
- Offline simulated-user evaluation regenerated from the actual runner: **45/45 passed** (latest run 2026-11-08 04:43 UTC).
- `compileall`, `node --check`, Node frontend test (1/1), secret scan, and `git diff --check` passed.
- Live direct client calls using project `.env`: Gemini `gemini-3.5-flash-lite` succeeded (366 total usage tokens; estimated $0.0004552); OpenAI `gpt-6-luna` succeeded (535 total usage tokens; estimated $0.0000995).
- Live local HTTP E2E: Gemini `gemini-3.5-flash-lite` used 1,824 tokens across two calls and was estimated at $0.0012622. Provider search, hold, explicit approval, booking status verification, and duplicate-confirmation replay all passed.
- Earlier HTTP 402/429 results came from the different inherited process keys. No key values were printed. The project-file keys succeeded after selecting the correct source.
- Current persistent application ledger: **17 requests, 12,514 tokens, $0.0047096 estimated/reserved**. This includes three earlier uncertain OpenAI reservations whose external billing is unknown. The global application ceiling is $5; estimated ledger headroom is $4.9952904. There is no per-test spending cap.
- The live E2E trace is `docs/traces/live_gemini_booking_e2e.jsonl`; the annotated evidence is `docs/traces/live_gemini_booking_e2e_annotated.md`.
- The existing `.env` hash and modification time match the start-of-turn snapshot. The 3:13 recording remains 1920×1080 at 60 fps. The secret scanner is clean, and submitted traces/docs contain no labeled approval codes.

## Remaining

- No remaining engineering tasks. The post-commit artifact, secret, demo, and clean-tree audit passed; no push was performed.

## Git milestones

- Final verification record: `e117b04` (`Record final assignment verification`).
- Submission milestone: `d88f8e5` (`Complete Gemini-first errand agent submission`).
- Existing milestones: `31f9342`, `7874cd5`, `438d8e8`, `39a327b`, and `0a481d2`.
- All assignment changes are committed locally. No push was performed; `.env` remains ignored and excluded.
