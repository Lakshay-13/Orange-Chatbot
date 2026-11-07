"""Central configuration for runtime limits, model, pricing, and local storage."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping

from .envfile import read_env_file


def _as_bool(value: str, default: bool = False) -> bool:
    if not value:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _as_int(values: Mapping[str, str], key: str, default: int) -> int:
    try:
        value = int(values.get(key, str(default)))
    except (TypeError, ValueError):
        raise ValueError(f"{key} must be an integer") from None
    if value <= 0:
        raise ValueError(f"{key} must be greater than zero")
    return value


def _as_float(values: Mapping[str, str], key: str, default: float) -> float:
    try:
        value = float(values.get(key, str(default)))
    except (TypeError, ValueError):
        raise ValueError(f"{key} must be a number") from None
    if value <= 0:
        raise ValueError(f"{key} must be greater than zero")
    return value


def _path(root: Path, values: Mapping[str, str], key: str, default: str) -> Path:
    candidate = Path(values.get(key, default)).expanduser()
    return candidate if candidate.is_absolute() else (root / candidate).resolve()


@dataclass(frozen=True)
class AppConfig:
    root: Path
    openai_api_key: str | None = field(repr=False)
    openai_model: str
    llm_mode: str
    price_verified_model: str
    price_input_usd_per_mtok: float
    price_cached_input_usd_per_mtok: float
    price_output_usd_per_mtok: float
    max_api_spend_usd: float
    max_model_requests: int
    max_total_tokens: int
    context_budget_tokens: int
    max_output_tokens: int
    max_model_turns: int
    max_tool_calls: int
    max_execution_tool_calls: int
    max_wall_seconds: float
    model_http_timeout_seconds: float
    max_retries: int
    reasoning_effort_default: str
    allow_high_effort: bool
    approval_ttl_seconds: int
    proactive_poll_seconds: int
    max_user_chars: int
    app_host: str
    app_port: int
    database_path: Path
    mock_database_path: Path
    trace_dir: Path
    scenario_seed: int
    dev_controls: bool

    @property
    def live_model_ready(self) -> bool:
        return bool(
            self.openai_api_key
            and self.llm_mode == "live"
            and self.openai_model == self.price_verified_model
            and self.openai_model == "gpt-6-luna"
            and self.price_input_usd_per_mtok > 0
            and self.price_output_usd_per_mtok > 0
        )

    @classmethod
    def from_env(
        cls,
        root: Path | None = None,
        environ: Mapping[str, str] | None = None,
    ) -> "AppConfig":
        project_root = (root or Path(__file__).resolve().parents[1]).resolve()
        file_values = read_env_file(project_root / ".env")
        source = dict(file_values)
        source.update(dict(os.environ if environ is None else environ))

        model = source.get("OPENAI_MODEL", "gpt-6-luna").strip()
        mode = source.get("LLM_MODE", "live").strip().lower()
        if mode not in {"live", "scripted", "replay"}:
            raise ValueError("LLM_MODE must be live, scripted, or replay")
        effort = source.get("REASONING_EFFORT_DEFAULT", "low").strip().lower()
        if effort not in {"none", "low", "medium", "high", "xhigh", "max"}:
            raise ValueError("REASONING_EFFORT_DEFAULT is unsupported")
        port = _as_int(source, "APP_PORT", 8000)
        if port > 65535:
            raise ValueError("APP_PORT must be at most 65535")
        seed_raw = source.get("SCENARIO_SEED", "17")
        try:
            seed = int(seed_raw)
        except ValueError:
            raise ValueError("SCENARIO_SEED must be an integer") from None

        return cls(
            root=project_root,
            openai_api_key=source.get("OPENAI_API_KEY") or None,
            openai_model=model,
            llm_mode=mode,
            price_verified_model=source.get("PRICE_VERIFIED_MODEL", "gpt-6-luna").strip(),
            price_input_usd_per_mtok=_as_float(source, "PRICE_INPUT_USD_PER_MTOK", 0.10),
            price_cached_input_usd_per_mtok=_as_float(source, "PRICE_CACHED_INPUT_USD_PER_MTOK", 0.01),
            price_output_usd_per_mtok=_as_float(source, "PRICE_OUTPUT_USD_PER_MTOK", 0.50),
            max_api_spend_usd=_as_float(source, "MAX_API_SPEND_USD", 5.0),
            max_model_requests=_as_int(source, "MAX_MODEL_REQUESTS", 200),
            max_total_tokens=_as_int(source, "MAX_TOTAL_TOKENS", 1_500_000),
            context_budget_tokens=_as_int(source, "CONTEXT_BUDGET_TOKENS", 3_000),
            max_output_tokens=_as_int(source, "MAX_OUTPUT_TOKENS", 1_200),
            max_model_turns=_as_int(source, "MAX_MODEL_TURNS", 4),
            max_tool_calls=_as_int(source, "MAX_TOOL_CALLS", 8),
            max_execution_tool_calls=_as_int(source, "MAX_EXECUTION_TOOL_CALLS", 8),
            max_wall_seconds=_as_float(source, "MAX_WALL_SECONDS", 30.0),
            model_http_timeout_seconds=_as_float(source, "MODEL_HTTP_TIMEOUT_SECONDS", 20.0),
            max_retries=_as_int(source, "MAX_RETRIES", 2),
            reasoning_effort_default=effort,
            allow_high_effort=_as_bool(source.get("ALLOW_HIGH_EFFORT", "0")),
            approval_ttl_seconds=_as_int(source, "APPROVAL_TTL_SECONDS", 600),
            proactive_poll_seconds=_as_int(source, "PROACTIVE_POLL_SECONDS", 5),
            max_user_chars=_as_int(source, "MAX_USER_CHARS", 1000),
            app_host=source.get("APP_HOST", "127.0.0.1").strip(),
            app_port=port,
            database_path=_path(project_root, source, "DATABASE_PATH", "data/errand_agent.sqlite3"),
            mock_database_path=_path(project_root, source, "MOCK_DATABASE_PATH", "data/mock_provider.sqlite3"),
            trace_dir=_path(project_root, source, "TRACE_DIR", "logs/traces"),
            scenario_seed=seed,
            dev_controls=_as_bool(source.get("DEV_CONTROLS", "0")),
        )
