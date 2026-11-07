"""Grounded request state and conservative deterministic extraction helpers."""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta
from typing import Any

from .clock import Clock
from .ids import fingerprint
from .mock_provider import AREAS, SERVICES


SERVICE_TERMS = {
    "plumber": ("plumber", "plumbing", "tap", "faucet", "sink"),
    "electrician": ("electrician", "electrical", "wiring", "socket", "switch"),
    "ac_service": ("ac service", "ac servicing", "air conditioner", "air conditioning", "a/c", " ac "),
    "cleaning": ("cleaning", "cleaner", "deep clean", "house clean"),
    "carpenter": ("carpenter", "carpentry", "woodwork", "cabinet"),
    "pest_control": ("pest control", "pest", "termite", "cockroach"),
    "appliance_repair": ("appliance repair", "washing machine", "refrigerator", "fridge", "oven repair"),
}
SERVICE_LABELS = {
    "plumber": "plumber", "electrician": "electrician", "ac_service": "AC service",
    "cleaning": "cleaning", "carpenter": "carpenter", "pest_control": "pest control",
    "appliance_repair": "appliance repair",
}
TIME_PHRASES = {
    "morning": ("06:00", "12:00"), "mornings": ("06:00", "12:00"),
    "afternoon": ("12:00", "17:00"), "afternoons": ("12:00", "17:00"),
    "evening": ("17:00", "21:00"), "evenings": ("17:00", "21:00"),
}
_MONEY = re.compile(r"(?i)(?:₹\s*|rs\.?\s*|inr\s*)(\d[\d,]*(?:\.\d+)?)\s*([kK]?)|\bunder\s+(\d[\d,]*(?:\.\d+)?)\s*([kK]?)")
_TIME = re.compile(r"(?i)(?:not\s+before|after|from|earliest\s+at)\s+(\d{1,2})(?::(\d{2}))?\s*(a\.?m\.?|p\.?m\.?)")
_ISO_DATE = re.compile(r"\b(20\d\d-\d\d-\d\d)\b")
_MIN_RATING = re.compile(r"(?i)(?:at least|minimum|min\.?|above)\s*(\d(?:\.\d)?)\s*(?:stars?)?")


@dataclass
class IntentState:
    action: str = "inquire"
    service: str | None = None
    problem: str | None = None
    area: str | None = None
    date_start: str | None = None
    date_end: str | None = None
    earliest_time: str | None = None
    latest_time: str | None = None
    budget_max_inr: int | None = None
    no_budget: bool = False
    preferences: dict[str, Any] = field(default_factory=dict)
    target_booking_id: str | None = None
    selection: dict[str, str] = field(default_factory=dict)
    provider_ids: list[str] = field(default_factory=list)
    presented_options: list[dict[str, Any]] = field(default_factory=list)
    asked_fields: list[str] = field(default_factory=list)
    provenance: dict[str, dict[str, Any]] = field(default_factory=dict)
    revision: int = 0

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False, sort_keys=True, separators=(",", ":"))

    @classmethod
    def from_json(cls, value: str | dict[str, Any] | None) -> "IntentState":
        if not value:
            return cls()
        raw = json.loads(value) if isinstance(value, str) else dict(value)
        allowed = set(cls.__dataclass_fields__)
        return cls(**{key: val for key, val in raw.items() if key in allowed})

    def missing_required(self) -> list[str]:
        if self.action == "book":
            required = (("service", self.service), ("area", self.area), ("date_range", self.date_start and self.date_end))
        elif self.action == "reschedule":
            required = (("target_booking", self.target_booking_id), ("date_range", self.date_start and self.date_end))
        elif self.action == "cancel":
            required = (("target_booking", self.target_booking_id),)
        else:
            required = ()
        return [key for key, value in required if not value]

    def fingerprint_material(self) -> str:
        return fingerprint(asdict(self))


def _parse_date_window(text: str, clock: Clock) -> tuple[str | None, str | None]:
    lower = text.casefold()
    today = clock.now_ist().date()
    explicit = _ISO_DATE.search(text)
    if explicit:
        try:
            parsed = date.fromisoformat(explicit.group(1))
            return parsed.isoformat(), parsed.isoformat()
        except ValueError:
            pass
    if "today" in lower:
        return today.isoformat(), today.isoformat()
    if "tomorrow" in lower:
        parsed = today + timedelta(days=1)
        return parsed.isoformat(), parsed.isoformat()
    if "this week" in lower or "this weekend" in lower:
        sunday = today + timedelta(days=(6 - today.weekday()))
        return today.isoformat(), sunday.isoformat()
    weekdays = {"monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3, "friday": 4, "saturday": 5, "sunday": 6}
    for name, weekday in weekdays.items():
        if re.search(rf"\b{ name }\b", lower):
            days = (weekday - today.weekday()) % 7
            if days == 0:
                days = 7
            parsed = today + timedelta(days=days)
            return parsed.isoformat(), parsed.isoformat()
    return None, None


def parse_user_message(text: str, clock: Clock, source_id: str) -> dict[str, Any]:
    """Return only fields grounded in the current user message."""
    lower = f" {text.casefold()} "
    patch: dict[str, Any] = {}
    normalized = re.sub(r"\s+", " ", lower)
    if any(term in normalized for term in ("what do you remember", "forget ", "remember that", "my preferences")):
        patch["action"] = "memory"
    elif re.search(r"\b(?:reschedule|move|shift|change)\b.{0,40}\b(?:my )?(?:booking|appointment)\b|\b(?:reschedule|move|shift) my\b", normalized):
        patch["action"] = "reschedule"
    elif re.search(r"\b(?:cancel|call off)\s+(?:my\s+)?(?:booking|appointment|service)\b", normalized):
        patch["action"] = "cancel"
    elif re.search(r"\b(?:provider|plumber|electrician|technician)\s+(?:has )?cancelled\b|\b(?:cancelled|canceled)\s+(?:by (?:the )?provider|my provider)\b", normalized) and re.search(r"\b(?:find|look for|replace|another|replacement)\b", normalized):
        patch["action"] = "book"
    elif any(term in normalized for term in ("book", "schedule", "arrange", "find", "look for", "need a", "need an", "i need", "replacement", "another provider")):
        patch["action"] = "book"
    for service in SERVICES:
        if any(term in lower for term in SERVICE_TERMS[service]):
            patch["service"] = service
            break
    for area in sorted(AREAS, key=len, reverse=True):
        if area.casefold() in lower:
            patch["area"] = area
            break
    start, end = _parse_date_window(text, clock)
    if start:
        patch["date_start"], patch["date_end"] = start, end
    for phrase, (earliest, latest) in TIME_PHRASES.items():
        if re.search(rf"\b{phrase}\b", lower):
            patch["earliest_time"], patch["latest_time"] = earliest, latest
            break
    timed = _TIME.search(text)
    if timed:
        hour = int(timed.group(1)) % 12
        if timed.group(3).lower().startswith("p"):
            hour += 12
        value = f"{hour:02d}:{timed.group(2) or '00'}"
        patch["earliest_time"] = value
    if "no budget" in lower or "no limit" in lower or "any price" in lower:
        patch["no_budget"] = True
        patch["budget_max_inr"] = None
    else:
        money = None
        for candidate in _MONEY.finditer(text):
            has_currency = any(candidate.group(index) is not None for index in (1, 2))
            trailing = text[candidate.end():candidate.end() + 24]
            if not has_currency and re.match(r"\s*(?:hours?|hrs?|minutes?|mins?|bedrooms?|floors?|stories?|people|persons?)\b", trailing, re.I):
                continue
            money = candidate
            break
        if money:
            digits = money.group(1) or money.group(3)
            thousands = money.group(2) or money.group(4) or ""
            amount = float(digits.replace(",", "")) * (1000 if thousands else 1)
            patch["budget_max_inr"] = int(amount)
            patch["no_budget"] = False
    rating = _MIN_RATING.search(text)
    if rating:
        patch["preferences"] = {"min_rating": float(rating.group(1))}
    if re.search(r"\bsame\b.{0,50}\b(last time|previous(?:ly)?|before)\b", lower):
        patch["action"] = "book"
        patch.setdefault("preferences", {})["same_provider_as_last"] = True
    booking = re.search(r"\b(book_\d{6}|B-?\d{4,})\b", text, re.I)
    if booking:
        patch["target_booking_id"] = booking.group(1)
    if len(text.strip()) >= 3:
        patch["problem"] = text.strip()[:500]
    patch["_source_id"] = source_id
    return patch


def merge_intent(current: IntentState, patch: dict[str, Any], clock: Clock) -> IntentState:
    """Merge a user-grounded patch; unrelated facts and preferences remain intact."""
    result = IntentState.from_json(current.to_json())
    source_id = patch.get("_source_id", "unknown")
    material_before = (
        result.action, result.service, result.area, result.date_start, result.date_end,
        result.earliest_time, result.latest_time, result.budget_max_inr, result.no_budget,
        json.dumps(result.preferences, sort_keys=True), result.target_booking_id,
    )
    for key, value in patch.items():
        if key.startswith("_") or key not in result.__dataclass_fields__:
            continue
        if key == "preferences":
            result.preferences.update(value)
        elif value is not None and value != "":
            setattr(result, key, value)
        else:
            setattr(result, key, value)
        result.provenance[key] = {"source": source_id, "kind": "explicit", "set_at": clock.epoch()}
    if result.service not in SERVICES:
        result.service = None
    if result.area not in AREAS:
        result.area = None
    if result.date_start and result.date_end:
        today = clock.now_ist().date()
        try:
            first, last = date.fromisoformat(result.date_start), date.fromisoformat(result.date_end)
            if first < today or last < first or (last - today).days > 30:
                result.date_start = result.date_end = None
        except ValueError:
            result.date_start = result.date_end = None
    if result.earliest_time and result.latest_time and result.earliest_time > result.latest_time:
        result.earliest_time, result.latest_time = result.latest_time, result.earliest_time
    material_after = (
        result.action, result.service, result.area, result.date_start, result.date_end,
        result.earliest_time, result.latest_time, result.budget_max_inr, result.no_budget,
        json.dumps(result.preferences, sort_keys=True), result.target_booking_id,
    )
    if result.to_json() != current.to_json():
        result.revision += 1
    if material_after != material_before:
        result.presented_options = []
        result.provider_ids = []
        result.selection = {}
    return result


def sanitize_model_patch(
    patch: dict[str, Any], current: IntentState, user_message: str,
    grounded_patch: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Allow semantic suggestions only when their source words occur in user text."""
    lower = user_message.casefold()
    validated: dict[str, Any] = {}
    service = patch.get("service")
    if service in SERVICES and any(term in lower for term in SERVICE_TERMS[service]):
        validated["service"] = service
    area = patch.get("area")
    if area in AREAS and area.casefold() in lower:
        validated["area"] = area
    action = patch.get("action")
    if action in {"book", "reschedule", "cancel", "inquire", "memory"}:
        # The model cannot elevate a read-only request into a consequential action.
        if action not in {"book", "reschedule", "cancel"} or action in current.action or action in lower:
            validated["action"] = action
    if isinstance(patch.get("preferences"), dict):
        grounded = {key: value for key, value in patch["preferences"].items() if key == "min_rating" and str(value) in lower}
        if grounded:
            validated["preferences"] = grounded
    grounded = grounded_patch or {}
    for field in ("date_start", "date_end", "earliest_time", "latest_time", "budget_max_inr", "no_budget"):
        if field in grounded and patch.get(field) == grounded[field]:
            validated[field] = grounded[field]
    return validated
