"""Deterministic request and untrusted-text classification.

This classifier is a signal only. Tool allowlists, ownership checks, approval
fingerprints, and state transitions remain the security boundary.
"""

from __future__ import annotations

import base64
import binascii
import re
import unicodedata
from dataclasses import dataclass


_ZERO_WIDTH_AND_BIDI = {
    "\u200b", "\u200c", "\u200d", "\u200e", "\u200f", "\u202a", "\u202b",
    "\u202c", "\u202d", "\u202e", "\u2060", "\u2066", "\u2067", "\u2068",
    "\u2069", "\ufeff",
}
_CONFUSABLES = str.maketrans({
    "а": "a", "с": "c", "е": "e", "і": "i", "о": "o", "р": "p", "ѕ": "s",
    "х": "x", "у": "y", "Α": "A", "Β": "B", "Ε": "E", "Ι": "I", "Κ": "K",
    "Μ": "M", "Ν": "N", "Ο": "O", "Ρ": "P", "Τ": "T", "Χ": "X",
})

_RULES: tuple[tuple[str, str, re.Pattern[str]], ...] = (
    ("RF4_SYSTEM_PROMPT", "secret_exfiltration", re.compile(r"\b(system|hidden|developer)\s+(prompt|instructions?)\b|\b(reveal|show|print|repeat|dump)\b.{0,32}\b(prompt|instructions?)\b", re.I)),
    ("RF4_API_SECRET", "secret_exfiltration", re.compile(r"\b(api[_ -]?key|secret|credential|\.env|password|token)\b", re.I)),
    ("RF3_APPROVAL_BYPASS", "approval_bypass", re.compile(r"\b(pre[- ]?approve|skip|bypass|without|don't|do not)\b.{0,48}\b(approval|confirmation|authorization|permission)\b|\b(already approved|approval is granted|bina pooche book karo)\b", re.I)),
    ("RF2_INSTRUCTION_OVERRIDE", "instruction_override", re.compile(r"\b(ignore|disregard|override|forget)\b.{0,36}\b(all |the |your |previous |prior |above |system |developer )?(instructions?|rules?|prompts?)\b|\b(you are now|act as (an? )?(admin|developer|system))\b", re.I)),
    ("RF1_ROLE_SPOOF", "role_spoof", re.compile(r"(?:^|\n)\s*(system|developer|assistant)\s*:|\[/?INST\]|<\|(?:system|developer|assistant|tool)\|>|\bfunction_call\b", re.I)),
)
_APPROVAL_WORDS = re.compile(r"^(yes|yeah|yep|confirm|approve|go ahead|book it|haan|theek hai)[.! ]*$", re.I)
_SENTENCE_SPLIT = re.compile(r"(?<=[.!?;])\s+|\n+")
_BASE64_RUN = re.compile(r"(?<![A-Za-z0-9+/=])[A-Za-z0-9+/]{48,}={0,2}(?![A-Za-z0-9+/=])")


@dataclass(frozen=True)
class Classification:
    action: str
    labels: tuple[str, ...]
    rule_ids: tuple[str, ...]
    normalized_text: str
    safe_text: str
    transformed: bool = False


def normalize_text(text: str) -> tuple[str, bool]:
    normalized = unicodedata.normalize("NFKC", text)
    without_controls = "".join(
        character for character in normalized
        if character not in _ZERO_WIDTH_AND_BIDI
        and (unicodedata.category(character) not in {"Cc", "Cf"} or character in "\n\t")
    )
    return without_controls, without_controls != text


def _detection_view(text: str) -> str:
    return unicodedata.normalize("NFKC", text).translate(_CONFUSABLES)


def _decoded_payloads(text: str) -> list[str]:
    decoded: list[str] = []
    for match in _BASE64_RUN.finditer(text):
        try:
            candidate = base64.b64decode(match.group(0) + "==", validate=False).decode("utf-8", errors="ignore")
        except (binascii.Error, ValueError):
            continue
        if candidate and len(candidate) >= 8:
            decoded.append(candidate)
    return decoded


def _rule_hits(text: str) -> set[tuple[str, str]]:
    view = _detection_view(text)
    hits = {(rule_id, label) for rule_id, label, pattern in _RULES if pattern.search(view)}
    for decoded in _decoded_payloads(view):
        decoded_view = _detection_view(decoded)
        for rule_id, label, pattern in _RULES:
            if pattern.search(decoded_view):
                hits.add(("RF6_OBFUSCATED_" + rule_id, label))
    return hits


def classify_user(text: str, history: tuple[str, ...] = (), max_chars: int = 1000) -> Classification:
    normalized, transformed = normalize_text(text)
    if len(normalized) > max_chars:
        return Classification("block", ("resource_abuse",), ("RF7_LENGTH",), normalized, "", transformed)

    hits = _rule_hits(normalized)
    combined_history = "\n".join((*history[-6:], normalized))
    history_hits = _rule_hits(combined_history) - hits
    hits |= history_hits

    if transformed and _rule_hits(_detection_view(normalized)):
        hits.add(("RF6_NORMALIZED", "obfuscation"))

    if not hits:
        if _APPROVAL_WORDS.fullmatch(normalized.strip()):
            return Classification("allow", (), (), normalized, normalized, transformed)
        return Classification("allow", (), (), normalized, normalized, transformed)

    labels = tuple(sorted({label for _, label in hits}))
    rule_ids = tuple(sorted({rule_id for rule_id, _ in hits}))
    clauses = [clause.strip() for clause in _SENTENCE_SPLIT.split(normalized) if clause.strip()]
    attack_pattern = re.compile(r"(?i)(system\s+prompt|api[_ -]?key|\.env|ignore.{0,36}(instructions?|rules?|prompts?)|bypass.{0,48}(approval|confirmation|authorization)|pre[- ]?approve|developer\s*:|system\s*:)")
    safe_clauses = [clause for clause in clauses if not attack_pattern.search(_detection_view(clause))]
    safe_text = " ".join(safe_clauses)

    # A standalone request to expose secrets/prompts is blocked. Mixed requests
    # keep their ordinary service intent, stripped of the attack clause.
    secret_request = "secret_exfiltration" in labels
    if secret_request and not safe_text:
        action = "block"
    else:
        action = "flag"
    if not safe_text and action == "flag":
        action = "block"
    return Classification(action, labels, rule_ids, normalized, safe_text, transformed)


def classify_untrusted_text(text: str) -> Classification:
    normalized, transformed = normalize_text(text)
    hits = _rule_hits(normalized)
    if transformed and hits:
        hits.add(("RF6_NORMALIZED", "obfuscation"))
    return Classification(
        "flag" if hits else "allow",
        tuple(sorted({label for _, label in hits})),
        tuple(sorted({rule_id for rule_id, _ in hits})),
        normalized,
        "" if hits else normalized,
        transformed,
    )


def suspicious_mixed_script(text: str) -> bool:
    scripts: set[str] = set()
    for char in text:
        if not char.isalpha():
            continue
        name = unicodedata.name(char, "")
        if "LATIN" in name:
            scripts.add("latin")
        elif "CYRILLIC" in name:
            scripts.add("cyrillic")
        elif "GREEK" in name:
            scripts.add("greek")
    return len(scripts) > 1
