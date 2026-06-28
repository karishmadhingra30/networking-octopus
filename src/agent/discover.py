"""Bright Data LinkedIn Scraper client — discovery + single-URL scrape.

Two paths:
  * `scrape_profile(url)`     — sync single-URL collection (used to cache the
                                operator's own profile for personalization).
  * `discover(filters, ...)`  — async discovery: trigger -> poll -> retrieve.

NOTE (open item to confirm against live docs at docs.brightdata.com before a
real run): the trigger/poll/retrieve *skeleton* below is stable, but the exact
`discover_by` value and the discovery request **body** schema for the LinkedIn
profiles dataset must be confirmed in the Bright Data dashboard/docs. The
`_build_discovery_payload` function is the single place to adjust. See the
TODO markers there.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

import requests

from .config import Config
from .models import Candidate

BASE = "https://api.brightdata.com/datasets/v3"
PROFILE_COST_PER_RECORD = 0.0015  # USD per profile record (profiles-only runs)

POLL_INTERVAL_SECONDS = 5
POLL_MAX_WAIT_SECONDS = 300


class DiscoveryError(RuntimeError):
    pass


class CostCeilingExceeded(DiscoveryError):
    pass


@dataclass
class ConfirmedFilters:
    keywords: list[str]
    titles: list[str]
    locations: list[str]


class BrightDataClient:
    def __init__(self, config: Config):
        self._config = config
        self._dataset_id = config.brightdata_profile_dataset_id
        self._session = requests.Session()
        self._session.headers.update(
            {
                "Authorization": f"Bearer {config.brightdata_api_token}",
                "Content-Type": "application/json",
            }
        )

    # ------------------------------------------------------------------ auth
    def check_auth(self) -> None:
        """Lightweight auth probe used by `agent init`.

        Hits the datasets listing endpoint; raises on a non-2xx so credential
        problems surface clearly at startup.
        """
        resp = self._session.get(f"{BASE}/datasets", timeout=30)
        if resp.status_code == 401:
            raise DiscoveryError("Bright Data token rejected (401). Check BRIGHTDATA_API_TOKEN.")
        resp.raise_for_status()

    # -------------------------------------------------- single-URL scrape
    def scrape_profile(self, profile_url: str) -> Candidate | None:
        """Synchronously scrape a single profile by URL.

        Used once for the operator's own profile. Triggers a non-discovery
        collection job and polls for the single record.
        """
        trigger = self._session.post(
            f"{BASE}/trigger",
            params={"dataset_id": self._dataset_id, "format": "json"},
            json=[{"url": profile_url}],
            timeout=60,
        )
        trigger.raise_for_status()
        snapshot_id = trigger.json().get("snapshot_id")
        if not snapshot_id:
            raise DiscoveryError(f"No snapshot_id in trigger response: {trigger.text[:300]}")

        records = self._poll_and_retrieve(snapshot_id)
        if not records:
            return None
        return _record_to_candidate(records[0])

    # ----------------------------------------------------- async discovery
    def discover(self, filters: ConfirmedFilters, pool_size: int) -> list[Candidate]:
        """Run discovery for the confirmed filters and return up to `pool_size`."""
        self._guard_cost(pool_size)

        payload = _build_discovery_payload(filters, pool_size)
        print(f"[discover] triggering Bright Data discovery (limit {pool_size})...")
        trigger = self._session.post(
            f"{BASE}/trigger",
            params={
                "dataset_id": self._dataset_id,
                "type": "discover_new",
                "discover_by": _DISCOVER_BY,  # TODO: confirm valid value (see below)
                "format": "json",
            },
            json=payload,
            timeout=60,
        )
        trigger.raise_for_status()
        snapshot_id = trigger.json().get("snapshot_id")
        if not snapshot_id:
            raise DiscoveryError(f"No snapshot_id in trigger response: {trigger.text[:300]}")
        print(f"[discover] snapshot_id={snapshot_id}; polling...")

        records = self._poll_and_retrieve(snapshot_id)
        candidates = [_record_to_candidate(r) for r in records if r]
        candidates = [c for c in candidates if c.url_key]
        print(f"[discover] retrieved {len(records)} records -> {len(candidates)} usable candidates")
        return candidates[:pool_size]

    # -------------------------------------------------------------- helpers
    def _guard_cost(self, pool_size: int) -> None:
        estimate = pool_size * PROFILE_COST_PER_RECORD
        print(
            f"[cost] estimated discovery cost: ${estimate:.4f} "
            f"({pool_size} records x ${PROFILE_COST_PER_RECORD}/record)"
        )
        if estimate > self._config.cost_ceiling_usd:
            raise CostCeilingExceeded(
                f"Estimated cost ${estimate:.4f} exceeds ceiling "
                f"${self._config.cost_ceiling_usd:.2f}. Lower POOL_SIZE or raise COST_CEILING_USD."
            )

    def _poll_and_retrieve(self, snapshot_id: str) -> list[dict[str, Any]]:
        deadline = time.monotonic() + POLL_MAX_WAIT_SECONDS
        while True:
            progress = self._session.get(
                f"{BASE}/progress/{snapshot_id}", timeout=30
            )
            progress.raise_for_status()
            status = progress.json().get("status")
            if status == "ready":
                break
            if status == "failed":
                raise DiscoveryError(f"Bright Data job {snapshot_id} failed.")
            if time.monotonic() > deadline:
                raise DiscoveryError(
                    f"Bright Data job {snapshot_id} not ready after "
                    f"{POLL_MAX_WAIT_SECONDS}s (last status: {status})."
                )
            time.sleep(POLL_INTERVAL_SECONDS)

        snapshot = self._session.get(
            f"{BASE}/snapshot/{snapshot_id}",
            params={"format": "json"},
            timeout=120,
        )
        snapshot.raise_for_status()
        data = snapshot.json()
        # The snapshot endpoint returns a JSON array of records.
        if isinstance(data, dict):
            data = data.get("data", []) or data.get("records", [])
        return data if isinstance(data, list) else []


# --- Discovery payload shape (CONFIRM AGAINST LIVE DOCS) --------------------
#
# TODO(brightdata): Confirm the exact `discover_by` value and request body for
# the LinkedIn *profiles* dataset against docs.brightdata.com. As of writing,
# discovery on the profiles dataset is commonly driven by a keyword/role/location
# search. The skeleton below sends one discovery object built from the confirmed
# filters. If the dashboard shows a different field set (e.g. `search_keywords`,
# `job_title`, `location`), adjust the keys here only — the rest of the client
# does not depend on this shape.
_DISCOVER_BY = "keyword"  # TODO: confirm (e.g. "keyword" | "name" | ...)


def _build_discovery_payload(
    filters: ConfirmedFilters, pool_size: int
) -> list[dict[str, Any]]:
    keyword = " ".join(
        part
        for part in (
            " ".join(filters.titles),
            " ".join(filters.keywords),
        )
        if part
    ).strip()
    location = filters.locations[0] if filters.locations else ""

    # One discovery request object. Bright Data accepts an array of these.
    return [
        {
            # TODO(brightdata): confirm these field names for the profiles dataset.
            "keyword": keyword,
            "location": location,
            "limit": pool_size,
        }
    ]


# --- Record normalization --------------------------------------------------
def _first(record: dict[str, Any], *keys: str, default: str = "") -> str:
    for key in keys:
        val = record.get(key)
        if isinstance(val, str) and val.strip():
            return val.strip()
    return default


def _record_to_candidate(record: dict[str, Any]) -> Candidate:
    """Normalize a Bright Data profile record into a `Candidate`.

    Bright Data field names vary slightly by dataset version; we accept the
    common aliases for each field and keep the raw record for debugging.
    """
    experience = record.get("experience")
    if not isinstance(experience, list):
        experience = []

    return Candidate(
        name=_first(record, "name", "full_name", "fullName"),
        profile_url=_first(record, "url", "input_url", "profile_url", "linkedin_url"),
        headline=_first(record, "position", "headline", "current_position", "title"),
        company=_first(
            record, "current_company_name", "current_company", "company", "company_name"
        ),
        location=_first(record, "city", "location", "country"),
        about=_first(record, "about", "summary", "bio"),
        experience=experience,
        raw=record,
    )
