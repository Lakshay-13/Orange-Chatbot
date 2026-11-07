"""Structured, redacted traces persisted in SQLite and per-run JSONL files."""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any

from .clock import Clock
from .ids import new_id
from .redact import Redactor
from .storage import AgentStore, json_dumps


class TraceRecorder:
    def __init__(self, store: AgentStore, directory: Path, clock: Clock | None = None, redactor: Redactor | None = None) -> None:
        self.store = store
        self.directory = directory
        self.clock = clock or Clock()
        self.redactor = redactor or Redactor()
        self._lock = threading.Lock()
        self.directory.mkdir(parents=True, exist_ok=True)

    def new_run_id(self) -> str:
        return new_id("run")

    def emit(self, run_id: str, event: str, session_id: str | None = None, **data: Any) -> dict[str, Any]:
        safe = self._sanitize(data)
        record = {
            "run_id": run_id,
            "event": event,
            "session_id": session_id,
            "timestamp": self.clock.now_utc().isoformat(timespec="milliseconds"),
            "data": safe,
        }
        with self._lock, self.store.transaction() as db:
            db.execute(
                "INSERT INTO trace_events(run_id,session_id,event,data_json,created_at) VALUES(?,?,?,?,?)",
                (run_id, session_id, event, json_dumps(safe), self.clock.epoch()),
            )
            path = self.directory / f"{run_id}.jsonl"
            with path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
        return record

    def _sanitize(self, value: Any, key: str = "") -> Any:
        if isinstance(value, dict):
            return {str(k): self._sanitize(v, str(k)) for k, v in value.items()}
        if isinstance(value, (list, tuple)):
            return [self._sanitize(item, key) for item in value]
        if isinstance(value, str):
            if key.casefold() in {"api_key", "openai_api_key", "authorization", "secret", "credential"}:
                return "[REDACTED_SECRET]"
            return self.redactor.redact(value)
        if value is None or isinstance(value, (bool, int, float)):
            return value
        return self.redactor.redact(str(value))

    def events(self, run_id: str) -> list[dict[str, Any]]:
        rows = self.store.connection.execute(
            "SELECT event,data_json,created_at,session_id FROM trace_events WHERE run_id=? ORDER BY id", (run_id,)
        ).fetchall()
        return [
            {"event": row["event"], "data": json.loads(row["data_json"]), "created_at": row["created_at"], "session_id": row["session_id"]}
            for row in rows
        ]
