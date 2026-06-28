"""Configuration loading and credential validation.

All secrets come from `.env` (see `.env.example`). Nothing is hardcoded.
`load_config()` reads and validates presence on startup and returns a typed
`Config` object the rest of the package consumes.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

# Package root: .../src/agent
PACKAGE_DIR = Path(__file__).resolve().parent
PROMPTS_DIR = PACKAGE_DIR / "prompts"
STATE_DIR = PACKAGE_DIR / "state"
PROMPT_DELTAS_PATH = STATE_DIR / "prompt_deltas.json"


class ConfigError(RuntimeError):
    """Raised when required configuration is missing or malformed."""


@dataclass(frozen=True)
class Config:
    # Anthropic
    anthropic_api_key: str
    model_rank: str
    model_draft: str

    # Bright Data
    brightdata_api_token: str
    brightdata_profile_dataset_id: str

    # Google Sheets
    google_oauth_client_secrets: str
    google_token_cache: str
    sheet_id: str

    # Tunables
    pool_size: int
    min_results: int
    cost_ceiling_usd: float


def _require(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise ConfigError(
            f"Missing required environment variable: {name}. "
            "Copy .env.example to .env and fill it in."
        )
    return value


def _int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be an integer, got: {raw!r}") from exc


def _float(name: str, default: float) -> float:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be a number, got: {raw!r}") from exc


def load_config(env_file: str | None = None) -> Config:
    """Load and validate configuration from the environment / `.env`."""
    load_dotenv(env_file)

    return Config(
        anthropic_api_key=_require("ANTHROPIC_API_KEY"),
        model_rank=os.environ.get("MODEL_RANK", "claude-haiku-4-5-20251001").strip(),
        model_draft=os.environ.get("MODEL_DRAFT", "claude-opus-4-8").strip(),
        brightdata_api_token=_require("BRIGHTDATA_API_TOKEN"),
        brightdata_profile_dataset_id=_require("BRIGHTDATA_PROFILE_DATASET_ID"),
        google_oauth_client_secrets=os.environ.get(
            "GOOGLE_OAUTH_CLIENT_SECRETS", "./client_secret.json"
        ).strip(),
        google_token_cache=os.environ.get(
            "GOOGLE_TOKEN_CACHE", "./.google_token.json"
        ).strip(),
        sheet_id=_require("SHEET_ID"),
        pool_size=_int("POOL_SIZE", 40),
        min_results=_int("MIN_RESULTS", 10),
        cost_ceiling_usd=_float("COST_CEILING_USD", 1.00),
    )
