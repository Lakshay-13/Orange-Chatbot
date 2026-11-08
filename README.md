# Errand Agent

A local chat app for finding mock home-service providers and managing mock bookings. It never contacts service providers or processes payments. Consequential booking, cancellation, and rescheduling actions require an application-validated approval tied to exact details.

## Requirements

- Python 3.11 or newer
- A Gemini API key for live model mode
- A modern browser

The application and tests use only the Python standard library. No live model call is made by the automated tests or evaluation runner.

## Setup and run

1. Clone this repository and open its directory.
2. If you do not already have a `.env`, copy `.env.example` to `.env`. Preserve an existing `.env`; set `GEMINI_API_KEY` locally without committing it.
3. Run `python3 -m errand_agent.server`.
4. Open the printed local address, normally `http://127.0.0.1:8000/`.

For local runs, provider credentials set in the project `.env` take precedence over same-named credentials inherited from the process environment. This avoids accidentally using credentials from the shell or a developer tool. If the project file has no credential for a provider, process environment variables are used.

The primary application model is Google's `gemini-flash-lite-latest` alias. Its current catalog target was verified as `gemini-3.5-flash-lite`; aliases may be updated by Google. The configured OpenAI `gpt-6-luna` model is a fallback after a Gemini model error. The router records the failure and fallback in the trace; it does not fail over for local context or budget-limit errors. Each provider uses separate prices, and both share the same persistent global budget. Reasoning starts at low effort and increases only for complex requests. It keeps local SQLite state in `data/` and redacted JSONL traces in `logs/traces/`.

The live Gemini Interactions API uses `GEMINI_API_KEY` in an HTTP header and sets `store=false`; the OpenAI fallback uses `OPENAI_API_KEY` in its authorization header. The shared application budget defaults to **$5 total**, with configurable global request/token limits; known billing failures are not retried. Current standard rates are centrally configured: Gemini 3.5 Flash-Lite $0.30/1M input, $2.50/1M output (including thinking), $0.03/1M cached input; OpenAI GPT-6 Luna $0.10/1M input, $0.50/1M output, $0.01/1M cached input. Check [Gemini pricing](https://ai.google.dev/gemini-api/docs/pricing) and [OpenAI pricing](https://developers.openai.com/api/docs/pricing) before changing model identifiers or rates. Valid keys can list models while generation may still be blocked by project billing.

For an offline scripted run, set `LLM_MODE=scripted`. The deterministic model double is for local demonstrations and tests; it is not a substitute for live model behavior.

## Verification

```sh
python3 -m compileall -q errand_agent tests scripts
node --check errand_agent/web/app.js
node --test tests/test_frontend_feedback.cjs
python3 -m unittest discover -s tests -v
python3 scripts/run_evaluations.py
python3 scripts/check_secrets.py
```

The generated invariant table is [docs/EVALUATION.md](docs/EVALUATION.md). Recreate the timeout-after-possible-success trace with `python3 scripts/export_timeout_trace.py`; it uses a temporary database and mock provider. The latest live Gemini HTTP run, environment credential precedence fix, and usage ledger are recorded in [PROGRESS.md](PROGRESS.md). The sanitized live HTTP booking trace is [docs/traces/live_gemini_booking_e2e.jsonl](docs/traces/live_gemini_booking_e2e.jsonl), with an annotated summary beside it.

## Security and behavior

- Deterministic security classification runs before intent extraction. Direct secret-extraction and injection attempts are rejected or quarantined while benign service intent is retained.
- Reviews and tool output are marked as untrusted; provider-review instructions are removed from model context.
- The model can return only a strict proposal schema. Read tools use typed allowlists; write tools are never exposed to the model.
- Application code validates ownership, exact action fingerprints, approval state, expiry, idempotency, execution limits, and authoritative status.
- A timeout that may have followed a booking triggers a status query before any retry. A booking is not reported as successful until status verification.
- Persistent user memories store explicit/inferred provenance and can be inspected, corrected, or forgotten. Context selection enforces a token budget and logs the exact request payload (without the Authorization header).
- Provider cancellation events generate notifications and replacement options but never auto-book.
- `.env` is ignored by Git. `.env.example` contains placeholders only.

The trade-offs and classifier comparison are in [docs/DESIGN_NOTE.md](docs/DESIGN_NOTE.md). The independent review is [docs/CLAUDE_REVIEW.md](docs/CLAUDE_REVIEW.md). The sanitized coding-assistant transcript is [docs/AI_ASSISTANT_TRANSCRIPT.md](docs/AI_ASSISTANT_TRANSCRIPT.md). See [docs/BUGS.md](docs/BUGS.md) for verified defects and their regression tests.

## Demo

The submitted [3:13 local recording](demo/errand_agent_demo.mov) shows chat and live trace together. Deterministic controls can inject provider outcomes and a provider cancellation without using a real service. See [demo/README.md](demo/README.md) for recording details.
