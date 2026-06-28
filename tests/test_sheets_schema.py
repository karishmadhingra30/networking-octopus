"""Sheet schema / row-shaping tests (no network or auth required)."""

from agent.models import Candidate, RankResult, SelectedProfile
from agent.sheets import HEADERS, _to_row


def test_headers_are_the_documented_contract():
    assert HEADERS == [
        "run_id",
        "run_date",
        "name",
        "profile_url",
        "headline",
        "company",
        "location",
        "background_summary",
        "match_rationale",
        "draft_message",
        "score",
        "feedback",
    ]


def test_row_matches_header_width_and_order():
    selected = SelectedProfile(
        candidate=Candidate(
            name="Jane Doe",
            profile_url="https://linkedin.com/in/jane",
            headline="Founder",
            company="Acme",
            location="SF",
        ),
        rank=RankResult(
            profile_url="https://linkedin.com/in/jane",
            score=91,
            rationale="Runs a relevant startup.",
            background_summary="Founder of Acme.",
        ),
        draft_message="Hi Jane, ...",
    )
    row = _to_row("run123", "2026-06-28", selected)

    assert len(row) == len(HEADERS)
    record = dict(zip(HEADERS, row))
    assert record["run_id"] == "run123"
    assert record["name"] == "Jane Doe"
    assert record["profile_url"] == "https://linkedin.com/in/jane"
    assert record["background_summary"] == "Founder of Acme."
    assert record["match_rationale"] == "Runs a relevant startup."
    assert record["draft_message"] == "Hi Jane, ..."
    # operator-filled columns are left blank by the agent
    assert record["score"] == ""
    assert record["feedback"] == ""
