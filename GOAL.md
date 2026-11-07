# Errand Agent — Assignment Goal

## Objective

Complete the Founding AI Engineer assignment for a local, chat-based home-service errand agent. The supplied specification is [Orange_Engineering_Assignment.pdf](/Users/lakshaychawla/Downloads/Orange_Engineering_Assignment.pdf), supplemented by the user's task instructions. The GitHub repository is the canonical project; this local checkout is connected to `Lakshay-13/Orange-Chatbot`.

## Acceptance criteria

- [ ] Deliver a working local chat application using the verified OpenAI API model `gpt-6-luna`; centralize model settings, use dynamic reasoning effort when supported, and stop calls at configurable request, token, time, retry, or $5 budget limits.
- [ ] Keep the existing `.env` unchanged and untracked. Ignore secrets, provide a placeholder-only `.env.example`, and never expose the key in outputs, traces, docs, or commits.
- [ ] Implement every mock contract in the PDF with reproducible seed/scenario controls: provider search/details, 10-minute holds and price drift, probabilistic booking outcomes including timeout-after-possible-success, authoritative status, transient cancellation failure, reschedule conflicts, and booking lists.
- [ ] Enforce the pipeline user input → security classification → intent refinement → agent proposal → deterministic tool authorization → mock tools. Treat reviews and all tool text as untrusted data.
- [ ] Collect only missing request details and preserve unrelated context during corrections. Require explicit, action-bound approval for booking, cancellation, and rescheduling. A provider, slot, or price change invalidates approval.
- [ ] Make booking, retries, and approvals idempotent across process restarts. Verify ambiguous booking outcomes against authoritative status before retrying or claiming success.
- [ ] Bound model turns, tool calls, retries, and wall-clock time; give a clear result on every exit path.
- [ ] Persist user memories with explicit/inferred provenance, inspect/correct/forget controls, at least 50 seeded memories, a long seeded conversation, a fixed context budget, relevance selection, and per-turn context logs. Never send forgotten or unconfirmed inferred facts.
- [ ] Detect simulated provider cancellations proactively, notify the user, find preference-compatible replacements, and wait for fresh approval before rebooking.
- [ ] Run at least 20 offline simulated-user evaluations covering the PDF and user-requested security, reliability, memory, context, proactive follow-up, and limit cases. Generate the evaluation table from actual results.
- [ ] Provide structured traces, a raw timeout-after-possible-success trace, and an annotated excerpt in a design note of at most one page.
- [ ] Include a README, three verified bugs with regression tests, and a sanitized transcript of AI coding assistant use.
- [ ] Record and verify a 3–5 minute demo showing chat and trace side by side, or document a concrete environmental limitation and provide the closest useful alternative.
- [ ] Complete an independent review using the requested Claude model if available; otherwise document the exact limitation and self-review the full assignment. Fix valid findings with regression tests.
- [ ] Keep Git changes focused, check status and staged files before every commit, and leave a clean local Git state. Do not push unless requested.

## Completion report

The final report must state what was built, how to run it, actual test/evaluation results, the model and estimated spend, security controls, review findings, deliverable paths, local commit IDs, and any incomplete requirement with its exact reason.
