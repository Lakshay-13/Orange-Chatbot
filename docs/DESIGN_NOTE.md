# Errand Agent — design note

## Architecture and trust boundaries

The local app runs one model-driven agent behind deterministic stages: classify input, extract intent, build bounded context, request a typed proposal, authorize read tools, and display options. A single agent keeps state simple; application code owns permissions and writes. Reviews are labeled in the UI and excluded from model context. Search results are reduced to typed fields before a second proposal turn; no model tool can book, cancel, or reschedule.

Gemini `gemini-flash-lite-latest` is primary (currently `gemini-3.5-flash-lite`); OpenAI `gpt-6-luna` is the user-authorized fallback after a Gemini model error. Both use typed proposals, separate rates, and the global $5 ledger. Local `.env` credentials now take precedence over unrelated inherited keys, which caused the earlier 402/429 responses. A live HTTP run used Gemini for two model calls, then a mock provider search, exact quote hold, explicit approval, authoritative booking verification, and duplicate-confirmation replay with one booking. OpenAI generation and offline fallback paths also passed. Traces hash request payloads and redact one-time approval codes. Current rates: [Gemini](https://ai.google.dev/gemini-api/docs/pricing), [OpenAI](https://developers.openai.com/api/docs/pricing).

## Security classifier choice

The shipped classifier uses dependency-free rules, Unicode/obfuscation normalization, multi-turn checks, and safe-clause preservation. It adds no model latency, API cost, or data transfer. Its trade-off is weaker semantic coverage on novel paraphrases; performance is not a measured recall/false-positive benchmark.

Meta Prompt Guard 2 provides local 22M/86M classifiers but adds model/runtime setup and calibration. NVIDIA NeMo Guardrails covers input, retrieval, execution, output, and tool rails; NemoGuard JailbreakDetect needs an embedding model plus NIM or a hosted endpoint. Its detector rail can fail open on outage, so writes still need app enforcement. These options were reviewed, not benchmarked. [Meta model card](https://huggingface.co/meta-llama/Llama-Prompt-Guard-2-86M), [NeMo catalog](https://docs.nvidia.com/nemo/guardrails/latest/configure-guardrails/guardrail-catalog), [NemoGuard setup](https://docs.nvidia.com/nemo/guardrails/latest/get-started/tutorials/nemoguard-jailbreakdetect-deployment).

## State, approval, and recovery

Tasks move through gathering, searching, options, hold, approval, execution, and verification. Application code validates a one-time UI approval or exact `confirm CODE` against user, action fingerprint, provider, slot, and quote; any material change invalidates it. Writes use durable idempotency keys and bounded retries. Booking timeouts query authoritative status before retry or success; startup recovery reconciles pending actions. Cancel and reschedule share these safeguards.

## Memory and observability

SQLite stores preferences with explicit/inferred provenance, evidence, status, and scope. Context selection prioritizes relevant explicit facts, excludes forgotten, superseded, and pending facts, and enforces a fixed budget. Each model call logs its payload and hash. Traces record decisions, transitions, calls, sanitized results, latency, usage/cost, and recovery. Tests seed 52 memories and 240 prior messages.

**Trace excerpt:** the full raw trace is [here](traces/raw_timeout_after_possible_success.jsonl); the annotated version is [here](traces/annotated_timeout_excerpt.md).

```jsonl
{"event":"tool_call","tool":"book","attempt":1}
{"event":"tool_error","tool":"book","code":"timeout","ambiguous":true}
{"event":"tool_call","tool":"get_booking_status","hold_id":"hold_000001"}
{"event":"verification_result","status":"booked","booking_id":"book_000001"}
```

This trace records one booking call, then verifies the committed booking; no duplicate is issued.
