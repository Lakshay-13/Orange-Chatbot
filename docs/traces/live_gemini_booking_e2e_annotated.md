# Live Gemini booking trace — annotated

**Run:** `run_3ea0868f6fe349e09e0d78d63e91d491` on 2026-11-08. The full sanitized JSONL is [live_gemini_booking_e2e.jsonl](live_gemini_booking_e2e.jsonl). This was a local HTTP test against the seeded mock provider database; no real service provider or payment was involved.

1. **Bounded context:** The first `context_manifest` estimated 1,431 tokens against the 3,000-token budget. It selected relevant explicit memories and excluded forgotten, superseded, rejected, inferred, or irrelevant entries. The second model payload was also within budget at 1,584 estimated tokens.
2. **Live model and read authorization:** Gemini `gemini-3.5-flash-lite` returned `search_providers`; the app recorded 658 input and 120 output tokens at an estimated $0.0004974. Deterministic policy authorized the read-only search. The mock returned six providers, and application filters retained two that met saved rating, language, date, and budget preferences.
3. **Comparison:** The second Gemini call used 841 input and 205 output tokens at an estimated $0.0007648. It selected `prov_001`, one of the observed options. The application did not let the model hold or book a slot.
4. **Exact approval:** The user-selected option was held for `2026-11-14T08:00+05:30`; the current quote was ₹887, matching the search price. The approval card bound the provider, slot, and quote. The approval code is redacted from this trace.
5. **Verified write:** After explicit approval, application code called `book` with a durable idempotency key, then queried `get_booking_status`. The status check returned the same provider, slot, and ₹887 price. The task transitioned through `EXECUTING` and `VERIFYING` to `CONFIRMED`; the user-facing result reported confirmation only after verification.
6. **Duplicate confirmation:** The exact same approval request was replayed. The endpoint returned `replayed=true`; a fresh booking-list query contained exactly one matching confirmed booking.

The two model calls used 1,824 combined tokens and cost an estimated **$0.0012622** at the configured rates. The persistent application ledger after this run recorded 17 requests, 12,514 tokens, and $0.0047096 estimated/reserved, including earlier uncertain reservations.
