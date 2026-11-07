# Progress

Updated: 2026-11-07 (Asia/Kolkata)

## Completed

- Read and visually checked all 3 pages of `Orange_Engineering_Assignment.pdf`.
- Inspected the workspace: only the pre-existing `.env` was present; its contents were not displayed or changed.
- Confirmed through the connected GitHub account that `Lakshay-13/Orange-Chatbot` is a private, empty repository. Initialized local `main` and attached `origin`.
- Added `.gitignore` before staging; verified `.env` is ignored.
- Verified Claude Code v2.1.319 is installed and authenticated. Session metadata resolved the `opus` alias to the first-party canonical model `claude-opus-5-5`.
- Used the existing OpenAI key to list models. HTTP 200 returned `gpt-6-luna`; the key was not printed. This catalog request generated no model tokens.
- Read official model and classifier/guardrail documentation.
- Claude Opus 5.5 produced a 691-line, 36-scenario architecture and work plan at max effort. Read the full plan, compared it with the assignment, added verification notes, and copied it into `DESIGN_PLAN.md`.

## In progress

- Stage B: implementing the SQLite-backed mock tools, guarded agent workflow, persistent memory/context, and local chat/trace UI. M0 configuration/security foundation is complete; M1 durable state and mock tools are next.

## Validation results

- `python3 -m unittest discover -s tests -p 'test_foundation.py' -v`: 15 passed.
- `python3 scripts/check_secrets.py`: passed; `.env` is ignored and excluded.
- OpenAI application generation calls: none; estimated application spend is `$0.000000`.
- Claude plan session reported `$5.2401078` list-rate usage. This is coding-assistant usage, separate from the OpenAI application budget; earlier timed-out calls did not return usage metadata.
- Git commits: `7874cd5` (`docs: define errand agent goal and architecture`), `438d8e8` (`docs: add Claude Opus 5.5 architecture plan`).

## Outstanding

- Complete every acceptance item in `GOAL.md` and the assignment PDF.
- Run offline tests and at least 20 evaluations; generate results from actual execution.
- Produce the one-page design note, three verified bug records, raw timeout trace, and sanitized assistant transcript.
- Run the app with the verified API model and record actual usage within the `$5` cap.
- Record and verify a 3–5 minute local side-by-side demo or document a concrete recording limitation. Do not publish externally without authorization.
- Run the requested Claude Opus 5.5 independent review after implementation, triage findings, and fix valid issues.
- Check secrets and Git diff; commit each milestone and leave a clean local Git state.

## Blockers and limitations

- No implementation blocker is known.
- Exact total Claude CLI billing is unknown because the earlier timed-out attempts did not return usage metadata.
