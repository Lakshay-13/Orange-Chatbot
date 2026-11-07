# Recorded demo

[errand_agent_demo.mov](errand_agent_demo.mov) is a verified 3:13 local screen recording (1920×1080, 60 fps). It shows the chat and live trace side by side and uses the scripted offline model with deterministic mock providers; it makes no live API calls and contacts no real provider.

The walkthrough demonstrates provider search and an untrusted review, exact quote approval, a booking timeout followed by authoritative status verification, a direct prompt injection that is filtered, persistent preference provenance, and proactive cancellation replacement options that remain unapproved.

To recreate the UI flow locally, follow the setup in the root [README](../README.md), set `LLM_MODE=scripted` and `DEV_CONTROLS=1`, and open the local app in a browser.
