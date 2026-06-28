"""Read/write learned prompt refinements (`state/prompt_deltas.json`).

Kept separate from the base prompt files so learned guidance is inspectable and
reversible. The learn run writes here; enrich and rank read from here.
"""

from __future__ import annotations

import json
from typing import Any

from .config import PROMPT_DELTAS_PATH, STATE_DIR

_DEFAULT: dict[str, Any] = {
    "version": 0,
    "enrich_guidance": [],
    "rank_guidance": [],
    "updated_at": None,
}


def load_deltas() -> dict[str, Any]:
    if not PROMPT_DELTAS_PATH.exists():
        return dict(_DEFAULT)
    try:
        data = json.loads(PROMPT_DELTAS_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return dict(_DEFAULT)
    merged = dict(_DEFAULT)
    merged.update(data)
    return merged


def save_deltas(deltas: dict[str, Any]) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    PROMPT_DELTAS_PATH.write_text(
        json.dumps(deltas, indent=2, ensure_ascii=False), encoding="utf-8"
    )


def format_guidance(deltas: dict[str, Any], key: str) -> str:
    """Render a guidance list as a bullet block for injection into a prompt."""
    items = deltas.get(key) or []
    if not items:
        return ""
    bullets = "\n".join(f"- {item}" for item in items)
    return (
        "\n\nLearned guidance from prior operator feedback "
        "(apply these refinements):\n" + bullets
    )
