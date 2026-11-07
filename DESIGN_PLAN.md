# Errand Agent — Architecture and Implementation Plan

## Planning status

This plan follows the supplied three-page assignment and the user's expanded acceptance criteria. Claude Code 2.1.304 is installed and authenticated, and its `--model` option is documented. The requested “Claude Opus 5.5” was not present in Anthropic's current model documentation; the listed Opus model is `claude-opus-5`. To avoid silently substituting a model, Claude was not invoked. This is a Codex-authored fallback plan, not a Claude plan or review.

The local directory initially contained only a pre-existing `.env`; it was not a Git checkout. The connected private GitHub repository `Lakshay-13/Orange-Chatbot` exists but is empty. A local `main` checkout has been initialized with that remote, and `.env` is ignored before any staging.

The supplied key successfully listed `gpt-6-luna` from the OpenAI models endpoint. The application will use the exact ID `gpt-6-luna` with the Responses API; the API supports function calling and `reasoning.effort` values including `low`, `medium`, and `high`. No text-generation call has been made yet. API facts were checked against [OpenAI's GPT-6 Luna documentation](https://platform.openai.com/docs/models/gpt-6-luna) and [pricing](https://platform.openai.com/pricing).

## Architecture

- **Runtime:** Python standard library HTTP server, static HTML/CSS/JavaScript chat, and SQLite. This keeps local setup small and makes process-restart behavior testable.
- **Layered request path:** bounded Unicode/encoding normalization and deterministic security classification; structured intent extraction that preserves the original utterance and marks provenance; GPT-6 Luna agent proposal through typed Responses API tools; deterministic allowlist/schema/state checks; then the mock tools. Never give the model or user a direct write-capable booking tool.
- **Untrusted data:** provider reviews remain available to the detail UI and mock contract, but the model receives only sanitized provider fields and a security label. Review text cannot change intent or tool authorization. Output and trace sanitizers prevent prompt/credential disclosure.
- **Security-option assessment:** NVIDIA NeMo Guardrails offers input, retrieval, execution/tool, and output rails, but adds a framework and model-serving configuration. NVIDIA's Nemotron Content Safety Reasoning 4B is a policy-capable classifier, but its documented local deployment needs vLLM and a GPU with at least 16 GB VRAM. Meta Prompt Guard 2 targets prompt attacks; its 22M variant is smaller than 86M, but the published model is under the Llama 4 license and its Hugging Face access flow asks for personal details. These options are not required for a dependency-free local assignment. Use a fast, deterministic classifier for the known attack families, plus trust-boundary handling and application-level authorization; the classifier is not treated as the security boundary. The implementation and tests will state the detector's limits instead of claiming general prompt-injection prevention.
- **Mock tools:** SQLite-backed, seeded providers, reviews, holds, bookings, idempotency records, scenario outcomes, cancellation events, and configurable price/slot conflicts. Default booking selection follows 70% success, 15% slot-taken, and 15% ambiguous timeout; half of ambiguous outcomes create a booking.
- **Approvals:** server-generated pending-action record and fingerprint over action, provider/booking, slot, quoted price, and hold. A UI approval event is accepted only for the current fingerprint. Any changed material detail invalidates it. Database transactions and unique idempotency keys make repeated approval and restart recovery safe.
- **Timeout recovery:** after an ambiguous `book`, query authoritative status by idempotency key before retrying or returning an outcome. Return success only after status verification.
- **Memory/context:** SQLite memory records with explicit/inferred and active/forgotten states. Seed at least 50 fictional demo memories and a long conversation. Select only current, relevant, confirmed memories under a fixed estimated-token budget; store the exact user/history/memory context sent for each model request, with prompt text represented by version/hash so internal prompts are not exposed.
- **LLM and budget:** use the existing `OPENAI_API_KEY` from `.env` without editing it. Keep model, effort, rates, request limits, token ceilings, and `$5.00` cap in one config module. Price each call from returned token usage and documented rates; stop before a call if its worst-case reservation would exceed budget. Default effort is low; raise only for explicitly detected multi-step/recovery tasks. Offline tests use scripted model responses only.
- **Limits and observability:** per-turn wall-clock, model-turn, tool-call, and retry ceilings. JSONL traces record state transitions, classifications, allowlisted tool calls/results, latency, usage/cost, approvals, and idempotency; omit credentials and minimize personal data.
- **Verification:** standard-library unit tests plus 20+ named simulated-user evaluations. Generate CSV/Markdown results only from executed scenarios. Include restart, adversarial, approval invalidation, timeout, memory/context, proactive cancellation, and failure/limit cases.

## Implementation sequence

1. Secure repository setup, `GOAL.md`, this plan, progress log, `.env.example`, and README skeleton.
2. SQLite schema and deterministic mock-tool contracts; add tool-level contract and failure tests.
3. Security classifier, intent/provenance model, context and memory selection; add adversarial and forgetting tests.
4. GPT-6 Luna Responses client, typed tool proposal loop, budget accounting, execution limits, and scripted-model test adapter.
5. Deterministic workflow, action-bound approvals, idempotency/restart recovery, cancellation/reschedule, and proactive replacement.
6. Side-by-side chat/trace UI, memory controls, and demo scenario controls.
7. Run all offline tests/evals, fix failures, export actual evaluation table and timeout trace, document three actual bugs, and self-review every acceptance criterion.
8. Run the app with the verified API model, record actual usage, and attempt a 3–5 minute screen recording with the chat and trace visible.
9. Verify deliverables, secrets, diff, and clean Git status; commit each reviewable milestone locally. Do not push to GitHub unless asked.

## One-page design-note topics

State transitions, the deterministic approval boundary, idempotency and status-based recovery, memory/context rules, trade-offs, and a short excerpt from the generated timeout trace.
