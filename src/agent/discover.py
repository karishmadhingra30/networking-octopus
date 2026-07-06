"""Bright Data client — two-stage discovery + profile scrape.

Bright Data's LinkedIn "people search" dataset only finds profiles by exact
name, so it cannot do keyword/title/location prospecting. Instead we do the
standard two-stage approach:

  Stage 1 (SERP): Google-search `site:linkedin.com/in/ "<title>" <keywords>
                  <location>` via Bright Data's SERP API (the `/request`
                  endpoint) and harvest the LinkedIn profile URLs from the
                  organic results.
  Stage 2 (scrape): feed those URLs to the LinkedIn *people profiles* dataset
                    (collect-by-URL, `gd_l1viktl72bvl7bjuj0`) via the sync
                    `/datasets/v3/scrape` endpoint to get full profile records.

The operator's own profile is scraped the same way (stage 2 on a single URL)
and cached by the CLI.

Endpoint shapes confirmed against the Bright Data dashboard:
  * scrape (sync):  POST /datasets/v3/scrape?dataset_id=<id>&include_errors=true
                    body {"input": [{"url": ...}, ...], "limit_per_input": null}
  * SERP (direct):  POST https://api.brightdata.com/request
                    body {"zone": <serp_zone>, "url": <google url + brd_json=1>,
                          "format": "raw"}
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote_plus

import requests

from .config import Config
from .models import Candidate, normalize_url

DATASETS_BASE = "https://api.brightdata.com/datasets/v3"
SCRAPE_URL = f"{DATASETS_BASE}/scrape"
REQUEST_URL = "https://api.brightdata.com/request"

PROFILE_COST_PER_RECORD = 0.0015  # USD per profile record scraped
SERP_COST_PER_REQUEST = 0.0015    # USD per SERP request (approx; small)

SCRAPE_CHUNK = 10        # profiles per sync /scrape call
SERP_PAGE_SIZE = 20      # results requested per SERP page
MAX_SERP_PAGES = 12      # safety cap on pagination
REQUEST_TIMEOUT = 300    # seconds for a sync scrape chunk

RETRY_STATUSES = {400, 429, 500, 502, 503, 504}  # Bright Data can 400 transiently on cold scrapes
RETRY_ATTEMPTS = 3


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
        self._serp_zone = config.brightdata_serp_zone
        self._session = requests.Session()
        self._session.headers.update(
            {
                "Authorization": f"Bearer {config.brightdata_api_token}",
                "Content-Type": "application/json",
            }
        )

    # ------------------------------------------------------------------ auth
    def check_auth(self) -> None:
        """Lightweight auth probe used by `agent init`."""
        resp = self._session.get(f"{DATASETS_BASE}/datasets", timeout=30)
        if resp.status_code == 401:
            raise DiscoveryError("Bright Data token rejected (401). Check BRIGHTDATA_API_TOKEN.")
        resp.raise_for_status()

    # -------------------------------------------------- single-URL scrape
    def scrape_profile(self, profile_url: str) -> Candidate | None:
        """Scrape a single profile by URL (used for the operator's own profile)."""
        records = self._scrape_sync([profile_url])
        if not records:
            return None
        return _record_to_candidate(records[0])

    # ----------------------------------------------------- two-stage discovery
    def discover(self, filters: ConfirmedFilters, pool_size: int) -> list[Candidate]:
        """SERP-harvest profile URLs for the filters, then scrape them."""
        if not self._serp_zone:
            raise DiscoveryError(
                "BRIGHTDATA_SERP_ZONE is not set. Create a SERP API zone in the "
                "Bright Data dashboard (Web Access -> Add API -> SERP API) and put "
                "its zone name in .env as BRIGHTDATA_SERP_ZONE."
            )
        self._guard_cost(pool_size)

        query = _build_query(filters)
        print(f"[discover] SERP query: {query}")
        urls = self._serp_collect_profile_urls(query, pool_size)
        print(f"[discover] harvested {len(urls)} unique LinkedIn profile URLs")
        if not urls:
            return []

        urls = urls[:pool_size]
        print(f"[discover] scraping {len(urls)} profiles (collect-by-URL)...")
        records = self._scrape_sync(urls)
        candidates = [_record_to_candidate(r) for r in records if r]
        candidates = [c for c in candidates if c.url_key]
        print(f"[discover] retrieved {len(records)} records -> {len(candidates)} usable candidates")
        return candidates[:pool_size]

    # ---------------------------------------------------------- http w/ retry
    def _post(self, url: str, *, params: dict | None, payload: dict, timeout: int) -> requests.Response:
        """POST with retry on transient statuses; raises DiscoveryError on hard failure."""
        last: requests.Response | None = None
        for attempt in range(1, RETRY_ATTEMPTS + 1):
            resp = self._session.post(url, params=params, json=payload, timeout=timeout)
            if resp.status_code < 400:
                return resp
            last = resp
            if resp.status_code not in RETRY_STATUSES or attempt == RETRY_ATTEMPTS:
                break
            wait = 2 ** attempt
            print(f"[http] {resp.status_code} from {url.rsplit('/', 1)[-1]}; retry {attempt}/{RETRY_ATTEMPTS - 1} in {wait}s")
            time.sleep(wait)
        body = last.text[:300] if last is not None else "(no response)"
        code = last.status_code if last is not None else "?"
        raise DiscoveryError(f"Bright Data request to {url} failed ({code}): {body}")

    # ------------------------------------------------------------- serp probe
    def serp_probe(self, query: str) -> tuple[dict[str, Any], list[str]]:
        """Run one SERP page and return (raw json, extracted profile urls).

        Used by `agent test-serp` to verify the response shape cheaply.
        """
        if not self._serp_zone:
            raise DiscoveryError("BRIGHTDATA_SERP_ZONE is not set.")
        raw = self._serp_page(query, 0)
        return raw, _extract_profile_urls(raw)

    # ---------------------------------------------------------- SERP (stage 1)
    def _serp_collect_profile_urls(self, query: str, pool_size: int) -> list[str]:
        collected: list[str] = []
        seen: set[str] = set()
        for page in range(MAX_SERP_PAGES):
            start = page * SERP_PAGE_SIZE
            serp = self._serp_page(query, start)
            links = _extract_profile_urls(serp)
            new = 0
            for link in links:
                key = normalize_url(link)
                if not key or "linkedin.com/in/" not in key or key in seen:
                    continue
                seen.add(key)
                collected.append(link)
                new += 1
            print(f"[serp] page {page + 1}: +{new} new profile URLs (total {len(collected)})")
            if len(collected) >= pool_size or new == 0:
                break
        return collected

    def _serp_page(self, query: str, start: int) -> dict[str, Any]:
        google_url = (
            "https://www.google.com/search?"
            f"q={quote_plus(query)}&num={SERP_PAGE_SIZE}&start={start}&brd_json=1"
        )
        payload = {"zone": self._serp_zone, "url": google_url, "format": "raw"}
        resp = self._post(REQUEST_URL, params=None, payload=payload, timeout=90)
        # With brd_json=1 the body is Bright Data's parsed SERP JSON.
        try:
            return resp.json()
        except json.JSONDecodeError:
            try:
                return json.loads(resp.text)
            except json.JSONDecodeError as exc:
                raise DiscoveryError(
                    "SERP response was not JSON. Confirm brd_json=1 is honored for "
                    f"your SERP zone. First 300 chars: {resp.text[:300]}"
                ) from exc

    # -------------------------------------------------------- scrape (stage 2)
    def _scrape_sync(self, urls: list[str]) -> list[dict[str, Any]]:
        """Sync /scrape the given URLs in chunks; aggregate records."""
        out: list[dict[str, Any]] = []
        for i in range(0, len(urls), SCRAPE_CHUNK):
            chunk = urls[i : i + SCRAPE_CHUNK]
            payload = {
                "input": [{"url": u} for u in chunk],
                "limit_per_input": None,
            }
            resp = self._post(
                SCRAPE_URL,
                params={
                    "dataset_id": self._dataset_id,
                    "notify": "false",
                    "include_errors": "true",
                },
                payload=payload,
                timeout=REQUEST_TIMEOUT,
            )
            out.extend(_parse_scrape_response(resp))
            if len(urls) > SCRAPE_CHUNK:
                print(f"[scrape] {min(i + SCRAPE_CHUNK, len(urls))}/{len(urls)} profiles done")
                time.sleep(1)  # be gentle between chunks
        return out

    # -------------------------------------------------------------- helpers
    def _guard_cost(self, pool_size: int) -> None:
        # Cost ~= profiles scraped + a handful of SERP pages.
        est_pages = min(MAX_SERP_PAGES, max(1, pool_size // 10 + 1))
        estimate = pool_size * PROFILE_COST_PER_RECORD + est_pages * SERP_COST_PER_REQUEST
        print(
            f"[cost] estimated run cost: ${estimate:.4f} "
            f"({pool_size} profiles x ${PROFILE_COST_PER_RECORD} + "
            f"~{est_pages} SERP pages x ${SERP_COST_PER_REQUEST})"
        )
        if estimate > self._config.cost_ceiling_usd:
            raise CostCeilingExceeded(
                f"Estimated cost ${estimate:.4f} exceeds ceiling "
                f"${self._config.cost_ceiling_usd:.2f}. Lower POOL_SIZE or raise COST_CEILING_USD."
            )


# --- Scrape response parsing ------------------------------------------------
def _parse_scrape_response(resp: requests.Response) -> list[dict[str, Any]]:
    """Extract profile records from a /scrape response.

    The sync endpoint returns one of: a single record dict (one input), a JSON
    array of records, a wrapper dict ({"data": [...]}), or NDJSON.
    """
    try:
        data: Any = resp.json()
    except json.JSONDecodeError:
        # NDJSON fallback (one JSON object per line).
        rows = []
        for line in resp.text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        data = rows

    if isinstance(data, dict):
        for key in ("data", "records", "results"):
            if isinstance(data.get(key), list):
                data = data[key]
                break
        else:
            data = [data]  # a single record dict, not a wrapper

    if not isinstance(data, list):
        return []
    # Drop Bright Data error stubs (records that carry no usable identity).
    return [
        r
        for r in data
        if isinstance(r, dict)
        and (r.get("input_url") or r.get("url") or r.get("name") or r.get("id"))
    ]


# --- Google query construction ---------------------------------------------
def _build_query(filters: ConfirmedFilters) -> str:
    parts = ["site:linkedin.com/in/"]
    if filters.titles:
        titles = " OR ".join(f'"{t}"' for t in filters.titles)
        parts.append(f"({titles})")
    for kw in filters.keywords:
        parts.append(f'"{kw}"' if " " in kw else kw)
    if filters.locations:
        # A single location term reads best in a Google query.
        parts.append(f'"{filters.locations[0]}"')
    return " ".join(parts)


# --- SERP result parsing ----------------------------------------------------
def _extract_profile_urls(serp: dict[str, Any]) -> list[str]:
    """Pull LinkedIn /in/ URLs from a Bright Data parsed-SERP JSON object."""
    links: list[str] = []
    organic = serp.get("organic")
    if isinstance(organic, list):
        for item in organic:
            if not isinstance(item, dict):
                continue
            link = item.get("link") or item.get("url") or item.get("href")
            if isinstance(link, str) and "linkedin.com/in/" in link.lower():
                links.append(link)
    return links


# --- Record normalization ---------------------------------------------------
def _first(record: dict[str, Any], *keys: str, default: str = "") -> str:
    for key in keys:
        val = record.get(key)
        if isinstance(val, str) and val.strip():
            return val.strip()
    return default


def _record_to_candidate(record: dict[str, Any]) -> Candidate:
    """Normalize a Bright Data profile record into a `Candidate`.

    Field names vary slightly by dataset version; accept common aliases and
    keep the raw record for debugging.
    """
    experience = record.get("experience")
    if not isinstance(experience, list):
        experience = []

    return Candidate(
        name=_first(record, "name", "full_name", "fullName"),
        # Prefer input_url (the clean www.linkedin.com URL we requested) over `url`,
        # which Bright Data returns with a localized subdomain (e.g. co.linkedin.com).
        profile_url=_first(record, "input_url", "url", "profile_url", "linkedin_url"),
        headline=_first(record, "position", "headline", "current_position", "title"),
        company=_first(
            record, "current_company_name", "current_company", "company", "company_name"
        ),
        location=_first(record, "city", "location", "country"),
        about=_first(record, "about", "summary", "bio"),
        experience=experience,
        raw=record,
    )
