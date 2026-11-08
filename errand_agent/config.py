"""Central configuration for runtime limits, model, pricing, and local storage."""

from __future__ import annotations

import os
from dataclasses import dataclass, field, replace
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


def _preferred_float(values: Mapping[str, str], primary_key: str, legacy_key: str, default: float) -> float:
    selected = primary_key if primary_key in values else legacy_key
    return _as_float(values, selected, default) if selected in values else default


def _path(root: Path, values: Mapping[str, str], key: str, default: str) -> Path:
    candidate = Path(values.get(key, default)).expanduser()
    return candidate if candidate.is_absolute() else (root / candidate).resolve()


@dataclass(frozen=True)
class AppConfig:
    root: Path
    openai_api_key: str | None = field(repr=False)
    gemini_api_key: str | None = field(repr=False)
    llm_provider: str
    llm_fallback_provider: str
    openai_model: str
    gemini_model: str
    openai_verified_model: str
    gemini_verified_model: str
    openai_price_input_usd_per_mtok: float
    openai_price_cached_input_usd_per_mtok: float
    openai_price_output_usd_per_mtok: float
    gemini_price_input_usd_per_mtok: float
    gemini_price_cached_input_usd_per_mtok: float
    gemini_price_output_usd_per_mtok: float
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
    def active_model(self) -> str:
        return self.gemini_model if self.llm_provider == "gemini" else self.openai_model

    @property
    def active_api_key(self) -> str | None:
        return self.gemini_api_key if self.llm_provider == "gemini" else self.openai_api_key

    @property
    def gemini_model_ready(self) -> bool:
        return bool(
            self.gemini_api_key
            and self.llm_mode == "live"
            and self.gemini_model == self.gemini_verified_model
            and self.gemini_price_input_usd_per_mtok > 0
            and self.gemini_price_output_usd_per_mtok > 0
        )

    @property
    def openai_model_ready(self) -> bool:
        return bool(
            self.openai_api_key
            and self.llm_mode == "live"
            and self.openai_model == self.openai_verified_model
            and self.openai_model == "gpt-6-luna"
            and self.openai_price_input_usd_per_mtok > 0
            and self.openai_price_output_usd_per_mtok > 0
        )

    @property
    def primary_model_ready(self) -> bool:
        return self.gemini_model_ready if self.llm_provider == "gemini" else self.openai_model_ready

    @property
    def fallback_model_ready(self) -> bool:
        return self.llm_fallback_provider == "openai" and self.openai_model_ready

    @property
    def live_model_ready(self) -> bool:
        return self.primary_model_ready or self.fallback_model_ready

    def for_provider(self, provider: str) -> "AppConfig":
        """Return this config with provider-specific model pricing for the shared ledger."""
        if provider == "gemini":
            return replace(
                self, llm_provider="gemini", llm_fallback_provider="none",
                price_verified_model=self.gemini_verified_model,
                price_input_usd_per_mtok=self.gemini_price_input_usd_per_mtok,
                price_cached_input_usd_per_mtok=self.gemini_price_cached_input_usd_per_mtok,
                price_output_usd_per_mtok=self.gemini_price_output_usd_per_mtok,
            )
        if provider == "openai":
            return replace(
                self, llm_provider="openai", llm_fallback_provider="none",
                price_verified_model=self.openai_verified_model,
                price_input_usd_per_mtok=self.openai_price_input_usd_per_mtok,
                price_cached_input_usd_per_mtok=self.openai_price_cached_input_usd_per_mtok,
                price_output_usd_per_mtok=self.openai_price_output_usd_per_mtok,
            )
        raise ValueError("Unsupported model provider")

    @classmethod
    def from_env(
        cls,
        root: Path | None = None,
        environ: Mapping[str, str] | None = None,
    ) -> "AppConfig":
        project_root = (root or Path(__file__).resolve().parents[1]).resolve()
        file_values = read_env_file(project_root / ".env")
        process_values = dict(os.environ if environ is None else environ)
        source = dict(file_values)
        source.update(process_values)

        # This repository runs locally from a project .env. Prefer a credential
        # explicitly stored there over ambient credentials inherited from the
        # launcher (for example, a developer tool's own API key). Process
        # credentials remain supported when the project file has no value.
        for canonical, aliases in (
            ("OPENAI_API_KEY", ("OPENAI_API_KEY",)),
            ("GEMINI_API_KEY", ("GEMINI_API_KEY", "GOOGLE_API_KEY", "GOOGLE_GENAI_API_KEY")),
        ):
            project_credential = next(
                (file_values[name] for name in aliases if file_values.get(name)), None,
            )
            if project_credential:
                source[canonical] = project_credential

        provider = source.get("LLM_PROVIDER", "gemini").strip().lower()
        if provider not in {"gemini", "openai"}:
            raise ValueError("LLM_PROVIDER must be gemini or openai")
        default_fallback = "openai" if provider == "gemini" else "none"
        fallback_provider = source.get("LLM_FALLBACK_PROVIDER", default_fallback).strip().lower()
        if fallback_provider not in {"none", "openai"} or (fallback_provider == "openai" and provider != "gemini"):
            raise ValueError("LLM_FALLBACK_PROVIDER must be none or openai with Gemini as primary")
        openai_model = source.get("OPENAI_MODEL", "gpt-6-luna").strip()
        gemini_model = source.get("GEMINI_MODEL", "gemini-flash-lite-latest").strip()
        mode = source.get("LLM_MODE", "live").strip().lower()
        if mode not in {"live", "scripted"}:
            raise ValueError("LLM_MODE must be live or scripted")
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

        openai_verified_model = source.get(
            "OPENAI_VERIFIED_MODEL", source.get("PRICE_VERIFIED_MODEL", "gpt-6-luna"),
        ).strip()
        gemini_verified_model = source.get("GEMINI_VERIFIED_MODEL", "gemini-flash-lite-latest").strip()
        openai_price_input = _preferred_float(source, "OPENAI_PRICE_INPUT_USD_PER_MTOK", "PRICE_INPUT_USD_PER_MTOK", 0.10)
        openai_price_cached_input = _preferred_float(source, "OPENAI_PRICE_CACHED_INPUT_USD_PER_MTOK", "PRICE_CACHED_INPUT_USD_PER_MTOK", 0.01)
        openai_price_output = _preferred_float(source, "OPENAI_PRICE_OUTPUT_USD_PER_MTOK", "PRICE_OUTPUT_USD_PER_MTOK", 0.50)
        gemini_price_input = _as_float(source, "GEMINI_PRICE_INPUT_USD_PER_MTOK", 0.30)
        gemini_price_cached_input = _as_float(source, "GEMINI_PRICE_CACHED_INPUT_USD_PER_MTOK", 0.03)
        gemini_price_output = _as_float(source, "GEMINI_PRICE_OUTPUT_USD_PER_MTOK", 2.50)
        if provider == "gemini":
            verified_model = gemini_verified_model
            input_price, cached_input_price, output_price = gemini_price_input, gemini_price_cached_input, gemini_price_output
        else:
            verified_model = openai_verified_model
            input_price, cached_input_price, output_price = openai_price_input, openai_price_cached_input, openai_price_output

        return cls(
            root=project_root,
            openai_api_key=source.get("OPENAI_API_KEY") or None,
            gemini_api_key=(source.get("GEMINI_API_KEY") or source.get("GOOGLE_API_KEY") or source.get("GOOGLE_GENAI_API_KEY")) or None,
            llm_provider=provider,
            llm_fallback_provider=fallback_provider,
            openai_model=openai_model,
            gemini_model=gemini_model,
            openai_verified_model=openai_verified_model,
            gemini_verified_model=gemini_verified_model,
            openai_price_input_usd_per_mtok=openai_price_input,
            openai_price_cached_input_usd_per_mtok=openai_price_cached_input,
            openai_price_output_usd_per_mtok=openai_price_output,
            gemini_price_input_usd_per_mtok=gemini_price_input,
            gemini_price_cached_input_usd_per_mtok=gemini_price_cached_input,
            gemini_price_output_usd_per_mtok=gemini_price_output,
            llm_mode=mode,
            price_verified_model=verified_model,
            price_input_usd_per_mtok=input_price,
            price_cached_input_usd_per_mtok=cached_input_price,
            price_output_usd_per_mtok=output_price,
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
