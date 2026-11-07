"""Redaction helpers for traces, model outputs, and diagnostic messages."""

from __future__ import annotations

import re


SECRET_ASSIGNMENTS = (
    re.compile(r"(?i)(OPENAI_API_KEY\s*[:=]\s*)([^\s,;]+)"),
    re.compile(r"(?i)(ANTHROPIC_API_KEY\s*[:=]\s*)([^\s,;]+)"),
)
TOKEN_PATTERN = re.compile(r"\bsk-[A-Za-z0-9_-]{16,}\b")
PLACEHOLDERS = {"replace-with-your-openai-api-key", "replace-with-your-api-key", "placeholder", "your-api-key"}
EMAIL_PATTERN = re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.I)
PHONE_PATTERN = re.compile(r"(?<!\w)(?:\+?\d[\d ()-]{8,}\d)(?!\w)")


class Redactor:
    def __init__(self, secret: str | None = None) -> None:
        self._secret = secret or ""

    def redact(self, text: str) -> str:
        result = text
        if self._secret:
            result = result.replace(self._secret, "[REDACTED_SECRET]")
        for pattern in SECRET_ASSIGNMENTS:
            result = pattern.sub(
                lambda match: match.group(0)
                if match.group(2).strip('"\'').casefold() in PLACEHOLDERS
                else match.group(1) + "[REDACTED_SECRET]",
                result,
            )
        result = TOKEN_PATTERN.sub("[REDACTED_SECRET]", result)
        result = EMAIL_PATTERN.sub("[REDACTED_EMAIL]", result)
        result = PHONE_PATTERN.sub("[REDACTED_PHONE]", result)
        return result


def contains_secret_pattern(text: str) -> bool:
    if TOKEN_PATTERN.search(text):
        return True
    for pattern in SECRET_ASSIGNMENTS:
        match = pattern.search(text)
        if match and match.group(2).strip('"\'').casefold() not in PLACEHOLDERS:
            return True
    return False
