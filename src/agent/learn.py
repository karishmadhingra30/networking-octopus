"""Learn run: operator scores/feedback -> refined prompt deltas.

Deterministic prompt refinement (no ML). Reads scored rows from the Sheet, asks
Claude to summarize what high- and low-scored profiles share, and proposes
concrete adjustments to discovery filters and ranking emphasis. The result is
persisted to `state/prompt_deltas.json` (never the base prompts) and a
diff-style summary is printed.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

from .claude import ClaudeClient, load_prompt
from .config import Config
from .deltas import load_deltas, save_deltas
from .sheets import SheetClient


def run_learn(claude: ClaudeClient, config: Config, sheet: SheetClient) -> None:
    rows = sheet.read_feedback_rows()
    print(f"[learn] found {len(rows)} scored/feedback rows.")
    if not rows:
        print("[learn] nothing to learn from yet — score some rows in the Sheet first.")
        return

    system = load_prompt("learn_system.txt")
    examples = [
        {
            "name": r.get("name", ""),
            "headline": r.get("headline", ""),
            "company": r.get("company", ""),
            "location": r.get("location", ""),
            "background_summary": r.get("background_summary", ""),
            "match_rationale": r.get("match_rationale", ""),
            "score": r.get("score", ""),
            "feedback": r.get("feedback", ""),
        }
        for r in rows
    ]
    current = load_deltas()
    user = (
        "Here is the current learned guidance (may be empty):\n"
        + json.dumps(
            {
                "enrich_guidance": current.get("enrich_guidance", []),
                "rank_guidance": current.get("rank_guidance", []),
            },
            indent=2,
        )
        + "\n\nHere are the operator-scored examples:\n"
        + json.dumps(examples, indent=2, ensure_ascii=False)
        + "\n\nPropose updated guidance lists as instructed."
    )

    data = claude.complete_json(
        model=config.model_draft,
        system=system,
        user=user,
        max_tokens=3072,
    )

    new_enrich = _as_str_list(data.get("enrich_guidance"))
    new_rank = _as_str_list(data.get("rank_guidance"))

    updated = dict(current)
    updated["version"] = int(current.get("version", 0)) + 1
    updated["enrich_guidance"] = new_enrich
    updated["rank_guidance"] = new_rank
    updated["updated_at"] = datetime.now(timezone.utc).isoformat()

    _print_diff("enrich_guidance", current.get("enrich_guidance", []), new_enrich)
    _print_diff("rank_guidance", current.get("rank_guidance", []), new_rank)

    save_deltas(updated)
    print(f"[learn] saved prompt_deltas.json (version {updated['version']}).")
    summary = str(data.get("summary", "")).strip()
    if summary:
        print(f"\n[learn] summary:\n{summary}")


def _print_diff(label: str, old: list[str], new: list[str]) -> None:
    old_set, new_set = set(old), set(new)
    print(f"\n[learn] {label}:")
    for item in old:
        marker = " " if item in new_set else "-"
        print(f"  {marker} {item}")
    for item in new:
        if item not in old_set:
            print(f"  + {item}")


def _as_str_list(value) -> list[str]:
    if not value:
        return []
    if isinstance(value, str):
        return [value.strip()] if value.strip() else []
    return [str(v).strip() for v in value if str(v).strip()]
