"""[5] Outreach draft generation (Claude, quality model).

One short, specific message per selected profile, personalized with the
operator's own profile for sender context and shared hooks. Drafts only — the
operator reviews and sends manually.
"""

from __future__ import annotations

import json

from .claude import ClaudeClient, load_prompt
from .config import Config
from .models import Candidate, SelectedProfile


def draft_messages(
    claude: ClaudeClient,
    config: Config,
    selected: list[SelectedProfile],
    operator_profile: Candidate | None,
) -> None:
    """Generate a draft for each selected profile, in place."""
    system = load_prompt("draft_system.txt")
    operator_blob = (
        json.dumps(operator_profile.to_prompt_dict(), indent=2, ensure_ascii=False)
        if operator_profile
        else "(operator profile unavailable — write from a neutral first-person sender)"
    )

    for i, s in enumerate(selected, 1):
        print(f"[draft] writing message {i}/{len(selected)} for {s.candidate.name}...")
        user = (
            "Write one concise, personalized LinkedIn outreach message.\n\n"
            "SENDER (the operator) profile:\n"
            f"{operator_blob}\n\n"
            "RECIPIENT profile:\n"
            f"{json.dumps(s.candidate.to_prompt_dict(), indent=2, ensure_ascii=False)}\n\n"
            "Why they were matched (for your context, do not quote verbatim):\n"
            f"{s.rank.rationale}\n\n"
            "Return ONLY the message text — no preamble, no subject line, no quotes."
        )
        s.draft_message = claude.complete(
            model=config.model_draft,
            system=system,
            user=user,
            max_tokens=1024,
        ).strip()
