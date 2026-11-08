# Errand Agent — Assignment Goal

## Objective

Complete the Founding AI Engineer assignment for a local, chat-based home-service errand agent. The supplied specification is [Orange_Engineering_Assignment.pdf](/Users/lakshaychawla/Downloads/Orange_Engineering_Assignment.pdf), supplemented by the user's task instructions. The GitHub repository is the canonical project; this local checkout is connected to `Lakshay-13/Orange-Chatbot`.

## Acceptance criteria

- [x] Deliver a successful live chat with Gemini `gemini-flash-lite-latest` first and OpenAI `gpt-6-luna` as the user-authorized fallback; centralize both models, prices, dynamic effort, and the shared global $5 request/token/time/retry limits. Both project `.env` keys generated structured responses; the fallback router is covered by offline failure-path tests. A live local HTTP chat used Gemini for provider search and comparison, then explicitly approved, verified, and replayed a mock booking without duplication.
- [x] Ensure local project `.env` credentials are not silently replaced by unrelated ambient process credentials. Regression tests cover precedence and confirm process credentials still work when the project file lacks them. Traces redact one-time approval codes.
- [x] Keep the existing `.env` unchanged and untracked. Ignore secrets, provide a placeholder-only `.env.example`, and keep the key out of outputs, traces, docs, and commits.
- [x] Implement every mock contract in the PDF with reproducible seed/scenario controls: provider search/details, 10-minute holds and price drift, booking outcomes including timeout-after-possible-success, authoritative status, transient cancellation failure, reschedule conflicts, and booking lists.
- [x] Enforce the pipeline user input → security classification → intent refinement → agent proposal → deterministic tool authorization → mock tools. Treat reviews and all tool text as untrusted data.
- [x] Collect only missing request details and preserve unrelated context during corrections. Require explicit, action-bound approval for booking, cancellation, and rescheduling. A provider, slot, or price change invalidates approval.
- [x] Make booking, retries, and approvals idempotent across process restarts. Verify ambiguous booking outcomes against authoritative status before retrying or claiming success.
- [x] Bound model turns, tool calls, retries, and wall-clock time; give a clear result on every exit path.
- [x] Persist user memories with explicit/inferred provenance, inspect/correct/forget controls, 52 seeded memories, 240 prior messages, a fixed context budget, relevance selection, and per-turn context logs. Never send forgotten or unconfirmed inferred facts.
- [x] Detect simulated provider cancellations proactively, notify the user, find preference-compatible replacements, and wait for fresh approval before rebooking.
- [x] Run at least 20 offline simulated-user evaluations covering security, reliability, memory, context, proactive follow-up, and limits; generate the table from actual results. Latest run: 45/45 passed.
- [x] Provide structured traces, a raw timeout-after-possible-success trace, and an annotated excerpt in a one-page design note.
- [x] Include a README, three verified bugs with regression tests, and a sanitized transcript of AI coding assistant use. Seven verified defects are documented; the first three satisfy the assignment minimum, and all have regression coverage.
- [x] Record and verify a 3–5 minute demo showing chat and trace side by side, or document a concrete environmental limitation and provide the closest useful alternative. The verified recording is 3:13.
- [x] Complete an independent review using Claude Opus 5.5; otherwise document the exact limitation and self-review the full assignment. The baseline Claude review and dispositions are complete. A max-effort follow-up review for the provider update stalled for about 20 minutes without output; the exact limitation and full Codex self-review are recorded in `docs/PROVIDER_UPDATE_SELF_REVIEW.md`.
- [x] Keep Git changes focused, check status and staged files before every commit, and leave a clean local Git state. No push is part of this submission.

## Completion report

The final report must state what was built, how to run it, actual test/evaluation results, the models and estimated spend, security controls, review findings, deliverable paths, local commit IDs, and any incomplete requirement with its exact reason. The latest user instruction selects Gemini first and OpenAI as the fallback.
