# Progress

Updated: 2026-11-07 (Asia/Kolkata)

## Completed

- Read and checked all three pages of the supplied assignment PDF; mapped its requirements into `GOAL.md`, design, implementation, tests, and submission artifacts.
- Inspected the canonical repository and preserved its initial state. The existing `.env` is unchanged, ignored, untracked, and never included in assistant inputs or generated files. `.env.example` contains placeholder values only.
- Stage A: verified Claude Code CLI authentication and `claude-opus-5-5`; used max effort for the architecture-only plan. `DESIGN_PLAN.md` is the implementation starting point.
- Stage B: implemented the local chat/trace app, typed model proposals, deterministic tool authorization, seeded persistent mocks, explicit approvals, durable idempotency, booking recovery/status verification, proactive replacement, persistent memory, bounded context, and conservative API budget ledger.
- Verified the OpenAI API catalog model ID `gpt-6-luna` and configured it centrally. No alternate model is substituted.
- Completed the independent Claude Opus 5.5 max-effort review using a credential-free, read-only snapshot. Findings H1–H5, M1–M11, L1–L10 and reviewer uncertainties have reasoned dispositions in `docs/CLAUDE_REVIEW.md`; valid findings were fixed and have regression coverage.
- Added the setup README, one-page design note (383 words), verified bug report (three required defects plus two further regressions), sanitized assistant transcript, generated evaluation table, and raw/annotated timeout trace.
- Recorded and inspected `demo/errand_agent_demo.mov`: 193 seconds (3:13), 1920×1080 at 60 fps. It shows the local chat and trace, review injection boundary, exact approval, timeout/status recovery, direct prompt-injection filtering, memory provenance, and replacement options left unapproved.

## Verification

- `python3 -m unittest discover -s tests -q` — **98 passed**.
- `python3 scripts/run_evaluations.py` — **40/40 passed**; `docs/EVALUATION.md` was generated from the actual run.
- `python3 -m compileall -q errand_agent tests scripts` — passed.
- `node --check errand_agent/web/app.js` and `node --test tests/test_frontend_feedback.cjs` — passed.
- Scripted local smoke test — `/` and `/api/session` both returned HTTP 200; session initialized with `LLM_MODE=scripted`; server shut down cleanly after requests.
- `python3 scripts/export_timeout_trace.py` — verified 1 booking call, 1 timeout error, 1 authoritative status query/check, 1 stored booking, and `booked` result.
- `python3 scripts/check_secrets.py` — clean. `git check-ignore .env` confirms it is ignored; `git ls-files --error-unmatch .env` confirms it is not tracked.
- `git diff --check` — passed in the final audit after the documentation and recording updates.

## Application API usage and limitation

- Application model configured: **`gpt-6-luna`**. The catalog was verified, but the configured OpenAI project returned `HTTP 429 credit_balance_exhausted`; no generation response or successful live chat was obtained. No fallback model was used, and no further live calls will be made.
- Read-only usage-ledger summary: **5 attempts** — 3 uncertain (4,578 estimated input + 3,600 output tokens; **$0.0022578 estimated/reserved**) and 2 rejected requests settled at zero tokens and zero cost. Actual provider billing for the 3 uncertain attempts is not observable from this client ledger; $0.0022578 is a conservative local estimate, not confirmed spend.
- Automated tests, evaluation, trace export, and the recorded demo use offline mocks/scripted outputs and make no live API calls.
- The application budget is capped at $5.00. Claude CLI’s reported plan-stage list-rate usage was $5.2401078 and is separate from application API usage.

## Remaining

- The only requirement blocked by external state is successful live inference: the OpenAI project needs usable credits to verify an end-to-end live response. The app, tests, offline evaluation, and demo are complete.

## Git milestones

- `31f9342` — complete secured errand agent implementation, tests, review, traces, documentation, and recorded demo.
- Earlier milestones: `7874cd5`, `438d8e8`, `39a327b`, and `0a481d2`.
- No push was performed. `.env` was excluded from every commit.
