"""[4] Relevance ranking (Claude, cheap model).

Scores each deduped candidate 0-100 against the confirmed interpretation, with a
grounded rationale and a neutral background summary. Sends the pool in batches
to keep each request modest, then sorts by score descending.
"""

from __future__ import annotations

import json

from .claude import ClaudeClient, load_prompt
from .config import Config
from .deltas import format_guidance, load_deltas
from .models import Candidate, RankResult, normalize_url

BATCH_SIZE = 10


def rank_candidates(
    claude: ClaudeClient,
    config: Config,
    interpretation: str,
    candidates: list[Candidate],
) -> list[tuple[Candidate, RankResult]]:
    """Rank all candidates and return (candidate, result) pairs, score desc."""
    system = load_prompt("rank_system.txt")
    deltas = load_deltas()
    system += format_guidance(deltas, "rank_guidance")

    by_key = {c.url_key: c for c in candidates}
    results: dict[str, RankResult] = {}

    for start in range(0, len(candidates), BATCH_SIZE):
        batch = candidates[start : start + BATCH_SIZE]
        print(
            f"[rank] scoring candidates {start + 1}-{start + len(batch)} "
            f"of {len(candidates)}..."
        )
        user = _build_user(interpretation, batch)
        data = claude.complete_json(
            model=config.model_rank,
            system=system,
            user=user,
            max_tokens=4096,
        )
        for item in data.get("ranked", []) or []:
            key = normalize_url(item.get("profile_url"))
            if key not in by_key:
                continue
            results[key] = RankResult(
                profile_url=by_key[key].profile_url,
                score=_clamp_score(item.get("score")),
                rationale=str(item.get("rationale", "")).strip(),
                background_summary=str(item.get("background_summary", "")).strip(),
            )

    pairs = [(by_key[k], r) for k, r in results.items()]
    pairs.sort(key=lambda pr: pr[1].score, reverse=True)
    return pairs


def _build_user(interpretation: str, batch: list[Candidate]) -> str:
    payload = {
        "target_profile": interpretation,
        "candidates": [c.to_prompt_dict() for c in batch],
    }
    return (
        "Score each candidate for how well they match the target profile.\n\n"
        + json.dumps(payload, indent=2, ensure_ascii=False)
    )


def _clamp_score(value) -> int:
    try:
        score = int(round(float(value)))
    except (TypeError, ValueError):
        return 0
    return max(0, min(100, score))
