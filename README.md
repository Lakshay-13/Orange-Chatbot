# Errand Agent

A local chat app for finding mock home-service providers and managing mock bookings. It never contacts service providers or processes payments. Consequential booking, cancellation, and rescheduling actions require an application-validated approval tied to exact details.

## Requirements

- Python 3.11 or newer
- An OpenAI API key for live model mode
- A modern browser

The application and tests use only the Python standard library. No live model call is made by the automated tests or evaluation runner.

## Setup and run

1. Clone this repository and open its directory.
2. If you do not already have a `.env`, copy `.env.example` to `.env`. Preserve an existing `.env`; add or update `OPENAI_API_KEY` locally without committing it.
3. Run `python3 -m errand_agent.server`.
4. Open the printed local address, normally `http://127.0.0.1:8000/`.

The verified application model is `gpt-6-luna`. Settings and spending limits are centralized in `.env`; defaults start at low reasoning effort and raise effort only for complex requests. Model prices are configurable and sourced from [OpenAI API pricing](https://developers.openai.com/api/docs/pricing). The application refuses to substitute a different model silently. It keeps local SQLite state in `data/` and redacted JSONL traces in `logs/traces/`.

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

The generated invariant table is [docs/EVALUATION.md](docs/EVALUATION.md). Recreate the timeout-after-possible-success trace with `python3 scripts/export_timeout_trace.py`; it uses a temporary database and mock provider.

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
