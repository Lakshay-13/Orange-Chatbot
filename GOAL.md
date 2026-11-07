# Errand Agent — Assignment Goal

## Objective

Complete the Founding AI Engineer assignment for a local, chat-based home-service errand agent. The supplied specification is [Orange_Engineering_Assignment.pdf](/Users/lakshaychawla/Downloads/Orange_Engineering_Assignment.pdf), supplemented by the user's task instructions. The GitHub repository is the canonical project; this local checkout is connected to `Lakshay-13/Orange-Chatbot`.

## Acceptance criteria

- [ ] Deliver a successful live chat using verified OpenAI API model `gpt-6-luna`; centralize model settings, dynamically select reasoning effort, and enforce request, token, time, retry, and $5 budget limits. The live integration is implemented, but the configured project returned `HTTP 429 credit_balance_exhausted` before a generation response; no fallback model was used.
- [x] Keep the existing `.env` unchanged and untracked. Ignore secrets, provide a placeholder-only `.env.example`, and keep the key out of outputs, traces, docs, and commits.
- [x] Implement every mock contract in the PDF with reproducible seed/scenario controls: provider search/details, 10-minute holds and price drift, booking outcomes including timeout-after-possible-success, authoritative status, transient cancellation failure, reschedule conflicts, and booking lists.
- [x] Enforce the pipeline user input → security classification → intent refinement → agent proposal → deterministic tool authorization → mock tools. Treat reviews and all tool text as untrusted data.
- [x] Collect only missing request details and preserve unrelated context during corrections. Require explicit, action-bound approval for booking, cancellation, and rescheduling. A provider, slot, or price change invalidates approval.
- [x] Make booking, retries, and approvals idempotent across process restarts. Verify ambiguous booking outcomes against authoritative status before retrying or claiming success.
- [x] Bound model turns, tool calls, retries, and wall-clock time; give a clear result on every exit path.
- [x] Persist user memories with explicit/inferred provenance, inspect/correct/forget controls, 52 seeded memories, 240 prior messages, a fixed context budget, relevance selection, and per-turn context logs. Never send forgotten or unconfirmed inferred facts.
- [x] Detect simulated provider cancellations proactively, notify the user, find preference-compatible replacements, and wait for fresh approval before rebooking.
- [x] Run at least 20 offline simulated-user evaluations covering security, reliability, memory, context, proactive follow-up, and limits; generate the table from actual results. Latest run: 40/40 passed.
- [x] Provide structured traces, a raw timeout-after-possible-success trace, and an annotated excerpt in a one-page design note.
- [x] Include a README, three verified bugs with regression tests, and a sanitized transcript of AI coding assistant use. Five verified defects are documented; the first three satisfy the assignment minimum, and all have regression coverage.
- [x] Record and verify a 3–5 minute demo showing chat and trace side by side, or document a concrete environmental limitation and provide the closest useful alternative. The verified recording is 3:13.
- [x] Complete an independent review using Claude Opus 5.5; otherwise document the exact limitation and self-review the full assignment. Fix valid findings with regression tests. Claude review completed and every substantive finding was assessed; valid items were fixed and tested.
- [x] Keep Git changes focused, check status and staged files before every commit, and leave a clean local Git state. No push is part of this submission.

## Completion report

The final report must state what was built, how to run it, actual test/evaluation results, the model and estimated spend, security controls, review findings, deliverable paths, local commit IDs, and any incomplete requirement with its exact reason.
