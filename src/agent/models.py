"""Internal data structures shared across the pipeline."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

# Matches a leading country-code subdomain on linkedin.com, e.g. "co.linkedin.com",
# "in.linkedin.com", "uk.linkedin.com" — Bright Data returns these in `url`.
_CC_SUBDOMAIN_RE = re.compile(r"^[a-z]{2,3}\.linkedin\.com")


def normalize_url(url: str | None) -> str:
    """Normalize a LinkedIn profile URL for use as a dedup key.

    Lowercase, strip scheme/`www`, drop query strings and fragments, and strip
    trailing slashes so the same profile always maps to the same key.
    """
    if not url:
        return ""
    u = url.strip().lower()
    # Drop fragment and query string.
    u = u.split("#", 1)[0].split("?", 1)[0]
    # Strip scheme.
    for prefix in ("https://", "http://"):
        if u.startswith(prefix):
            u = u[len(prefix) :]
            break
    if u.startswith("www."):
        u = u[4:]
    # Collapse localized LinkedIn subdomains (co./in./uk./...) to the canonical host
    # so the same profile always dedupes to one key.
    u = _CC_SUBDOMAIN_RE.sub("linkedin.com", u)
    return u.rstrip("/")


@dataclass
class Candidate:
    """A scraped LinkedIn profile, normalized from a Bright Data record."""

    name: str
    profile_url: str
    headline: str = ""
    company: str = ""
    location: str = ""
    about: str = ""
    experience: list[dict[str, Any]] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def url_key(self) -> str:
        return normalize_url(self.profile_url)

    def to_prompt_dict(self) -> dict[str, Any]:
        """Compact representation handed to Claude for ranking/drafting."""
        return {
            "name": self.name,
            "profile_url": self.profile_url,
            "headline": self.headline,
            "company": self.company,
            "location": self.location,
            "about": self.about,
            "experience": self.experience,
        }


@dataclass
class RankResult:
    profile_url: str
    score: int
    rationale: str
    background_summary: str


@dataclass
class SelectedProfile:
    """A ranked candidate selected for outreach, with its generated draft."""

    candidate: Candidate
    rank: RankResult
    draft_message: str = ""
