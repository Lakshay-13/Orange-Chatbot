"""Redaction helpers for traces, model outputs, and diagnostic messages."""

from __future__ import annotations

import re


SECRET_ASSIGNMENTS = (
    re.compile(r"(?i)(OPENAI_API_KEY\s*[:=]\s*)([^\s,;]+)"),
    re.compile(r"(?i)(GEMINI_API_KEY\s*[:=]\s*)([^\s,;]+)"),
    re.compile(r"(?i)(GOOGLE_API_KEY\s*[:=]\s*)([^\s,;]+)"),
    re.compile(r"(?i)(GOOGLE_GENAI_API_KEY\s*[:=]\s*)([^\s,;]+)"),
    re.compile(r"(?i)(x-goog-api-key\s*[:=]\s*)([^\s,;]+)"),
    re.compile(r"(?i)(ANTHROPIC_API_KEY\s*[:=]\s*)([^\s,;]+)"),
)
TOKEN_PATTERN = re.compile(r"\bsk-[A-Za-z0-9_-]{16,}\b")
GOOGLE_KEY_PATTERN = re.compile(r"\bAIza[0-9A-Za-z_-]{30,}\b")
APPROVAL_CODE_PATTERN = re.compile(r"(?i)(approval\s+code\s+)([A-Z2-7]{6})\b")
PLACEHOLDERS = {
    "replace-with-your-openai-api-key", "replace-with-your-gemini-api-key",
    "replace-with-your-api-key", "placeholder", "your-api-key",
}
EMAIL_PATTERN = re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.I)
PHONE_PATTERN = re.compile(r"(?<!\w)(?!20\d{2}-\d{2}-\d{2}(?:[T ]|\b))(?=(?:\D*\d){10,})(?:\+?\d[\d ()-]{8,}\d)(?!\w)")


class Redactor:
    def __init__(self, *secrets: str | None) -> None:
        self._secrets = tuple(secret for secret in secrets if secret)

    def redact(self, text: str) -> str:
        result = text
        for secret in self._secrets:
            result = result.replace(secret, "[REDACTED_SECRET]")
        for pattern in SECRET_ASSIGNMENTS:
            result = pattern.sub(
                lambda match: match.group(0)
                if match.group(2).strip('"\'').casefold() in PLACEHOLDERS
                else match.group(1) + "[REDACTED_SECRET]",
                result,
            )
        result = TOKEN_PATTERN.sub("[REDACTED_SECRET]", result)
        result = GOOGLE_KEY_PATTERN.sub("[REDACTED_SECRET]", result)
        result = APPROVAL_CODE_PATTERN.sub(r"\1[REDACTED_APPROVAL_CODE]", result)
        result = EMAIL_PATTERN.sub("[REDACTED_EMAIL]", result)
        result = PHONE_PATTERN.sub("[REDACTED_PHONE]", result)
        return result


def contains_secret_pattern(text: str) -> bool:
    if TOKEN_PATTERN.search(text):
        return True
    if GOOGLE_KEY_PATTERN.search(text):
        return True
    for pattern in SECRET_ASSIGNMENTS:
        match = pattern.search(text)
        if match and match.group(2).strip('"\'').casefold() not in PLACEHOLDERS:
            return True
    return False
