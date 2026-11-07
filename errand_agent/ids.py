"""Stable action fingerprints and idempotency keys."""

from __future__ import annotations

import hashlib
import json
import uuid
from typing import Any


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def fingerprint(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def idempotency_key(operation: str, action_id: str) -> str:
    digest = hashlib.sha256(f"{operation}:{action_id}".encode("utf-8")).hexdigest()[:32]
    return f"{operation}_{digest}"
