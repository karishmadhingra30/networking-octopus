"""Dedup / URL-normalization tests."""

from agent.models import Candidate, normalize_url


def test_normalize_strips_scheme_www_query_and_trailing_slash():
    variants = [
        "https://www.linkedin.com/in/jane-doe/",
        "http://linkedin.com/in/jane-doe",
        "https://LinkedIn.com/in/Jane-Doe/?utm_source=foo",
        "linkedin.com/in/jane-doe#about",
        "  https://www.linkedin.com/in/jane-doe  ",
    ]
    keys = {normalize_url(v) for v in variants}
    assert keys == {"linkedin.com/in/jane-doe"}


def test_normalize_handles_empty():
    assert normalize_url("") == ""
    assert normalize_url(None) == ""


def test_candidate_url_key_used_for_dedup():
    existing = {normalize_url("https://www.linkedin.com/in/alice/")}
    pool = [
        Candidate(name="Alice", profile_url="http://linkedin.com/in/alice"),
        Candidate(name="Bob", profile_url="https://www.linkedin.com/in/bob/"),
    ]
    deduped = [c for c in pool if c.url_key not in existing]
    assert [c.name for c in deduped] == ["Bob"]


def test_distinct_profiles_are_not_deduped():
    a = Candidate(name="A", profile_url="https://linkedin.com/in/a")
    b = Candidate(name="B", profile_url="https://linkedin.com/in/b")
    assert a.url_key != b.url_key
