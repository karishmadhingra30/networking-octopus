"""[1] Prompt enrichment: NL prompt -> structured discovery filters (Claude).

Returns a structured interpretation plus discovery filters. If the model raises
clarifying questions, the caller answers them in the terminal and enrichment is
re-run with the answers appended. Stored prompt deltas (from past learn runs)
are injected so refinements carry forward.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from .claude import ClaudeClient, load_prompt
from .config import Config
from .deltas import format_guidance, load_deltas
from .discover import ConfirmedFilters


@dataclass
class Enrichment:
    interpretation: str
    filters: ConfirmedFilters
    clarifying_questions: list[str]


def enrich(
    claude: ClaudeClient,
    config: Config,
    raw_prompt: str,
    clarifications: list[str] | None = None,
) -> Enrichment:
    """Call Claude to interpret the prompt and propose discovery filters."""
    system = load_prompt("enrich_system.txt")
    deltas = load_deltas()
    system += format_guidance(deltas, "enrich_guidance")

    user = raw_prompt.strip()
    if clarifications:
        user += "\n\nOperator answers to your clarifying questions:\n" + "\n".join(
            f"- {c}" for c in clarifications
        )

    data = claude.complete_json(
        model=config.model_draft,  # quality matters here; token count is small
        system=system,
        user=user,
        max_tokens=2048,
    )

    df = data.get("discovery_filters", {}) or {}
    filters = ConfirmedFilters(
        keywords=_as_str_list(df.get("keywords")),
        titles=_as_str_list(df.get("titles")),
        locations=_as_str_list(df.get("locations")),
    )
    return Enrichment(
        interpretation=str(data.get("interpretation", "")).strip(),
        filters=filters,
        clarifying_questions=_as_str_list(data.get("clarifying_questions")),
    )


def _as_str_list(value) -> list[str]:
    if not value:
        return []
    if isinstance(value, str):
        return [value.strip()] if value.strip() else []
    return [str(v).strip() for v in value if str(v).strip()]


def filters_to_text(filters: ConfirmedFilters) -> str:
    return json.dumps(
        {
            "keywords": filters.keywords,
            "titles": filters.titles,
            "locations": filters.locations,
        },
        indent=2,
    )
