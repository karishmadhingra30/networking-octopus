"""CLI entrypoint (typer): `agent init`, `agent run`, `agent learn`."""

from __future__ import annotations

import json
import sys
import uuid
from datetime import date
from pathlib import Path

import typer

from . import __version__
from .claude import ClaudeClient
from .config import Config, ConfigError, load_config
from .discover import (
    BrightDataClient,
    ConfirmedFilters,
    CostCeilingExceeded,
    DiscoveryError,
)
from .draft import draft_messages
from .enrich import enrich, filters_to_text
from .models import Candidate, SelectedProfile
from .rank import rank_candidates
from .sheets import SheetClient, SheetsError

app = typer.Typer(
    add_completion=False,
    help="LinkedIn Networking Agent — find, rank, and draft outreach to LinkedIn profiles.",
)

OPERATOR_CACHE = Path(".cache/operator_profile.json")


def _load_config_or_exit() -> Config:
    try:
        return load_config()
    except ConfigError as exc:
        typer.secho(f"Configuration error: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=2)


# --------------------------------------------------------------------- init
@app.command()
def init() -> None:
    """Validate all credentials and Sheet access; create the header row if missing."""
    config = _load_config_or_exit()
    typer.echo(f"linkedin-outreach-agent v{__version__}")

    typer.echo("\n[1/3] Anthropic...")
    try:
        claude = ClaudeClient(config)
        claude.complete(
            model=config.model_rank,
            system="You are a connectivity probe.",
            user="Reply with the single word: ok",
            max_tokens=16,
        )
        typer.secho("  ✓ Anthropic key works.", fg=typer.colors.GREEN)
    except Exception as exc:  # noqa: BLE001 - surface any failure clearly
        typer.secho(f"  ✗ Anthropic check failed: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1)

    typer.echo("\n[2/3] Bright Data...")
    try:
        BrightDataClient(config).check_auth()
        typer.secho("  ✓ Bright Data token authenticates.", fg=typer.colors.GREEN)
    except Exception as exc:  # noqa: BLE001
        typer.secho(f"  ✗ Bright Data check failed: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1)

    typer.echo("\n[3/3] Google Sheets (a browser window may open on first run)...")
    try:
        sheet = SheetClient(config)
        created = sheet.ensure_header()
        msg = "created header row" if created else "header row already present"
        typer.secho(f"  ✓ Opened '{sheet.title}' ({msg}).", fg=typer.colors.GREEN)
    except Exception as exc:  # noqa: BLE001
        typer.secho(f"  ✗ Google Sheets check failed: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1)

    typer.secho("\nAll checks passed. Ready to `agent run`.", fg=typer.colors.GREEN)


# ---------------------------------------------------------------------- run
@app.command()
def run(
    prompt: str = typer.Option(None, "--prompt", help="NL description of who to network with."),
    profile: str = typer.Option(None, "--profile", help="Operator's own LinkedIn URL (personalization)."),
    pool_size: int = typer.Option(None, "--pool-size", help="Override POOL_SIZE."),
    min_results: int = typer.Option(None, "--min-results", help="Override MIN_RESULTS."),
    yes: bool = typer.Option(False, "--yes", help="Skip the interactive confirmation."),
) -> None:
    """Run the full pipeline: enrich -> discover -> dedup -> rank -> draft -> write."""
    config = _load_config_or_exit()
    pool_size = pool_size or config.pool_size
    min_results = min_results or config.min_results

    claude = ClaudeClient(config)

    # [1] Enrichment + interactive confirmation -------------------------------
    raw_prompt = prompt or typer.prompt("Describe the kind of people you want to network with")
    enrichment = _enrich_with_confirmation(claude, config, raw_prompt, yes)

    # Operator profile (scraped once, cached) ---------------------------------
    brightdata = BrightDataClient(config)
    operator_profile = _resolve_operator_profile(brightdata, profile)

    # [2] Discovery -----------------------------------------------------------
    typer.echo("\n[2] Discovery (Bright Data, async; this can take a few minutes)...")
    try:
        candidates = brightdata.discover(enrichment.filters, pool_size)
    except CostCeilingExceeded as exc:
        typer.secho(f"Refusing to run: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1)
    except DiscoveryError as exc:
        typer.secho(f"Discovery failed: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1)
    typer.echo(f"    count in: {len(candidates)} candidates")

    # [3] Dedup against the Sheet --------------------------------------------
    sheet = SheetClient(config)
    sheet.ensure_header()
    seen = sheet.existing_url_keys()
    deduped = [c for c in candidates if c.url_key not in seen]
    typer.echo(f"[3] Dedup: dropped {len(candidates) - len(deduped)} already in Sheet -> {len(deduped)} remain")

    if not deduped:
        typer.secho("No new candidates after dedup. Nothing to write.", fg=typer.colors.YELLOW)
        raise typer.Exit(code=0)

    # [4] Ranking -------------------------------------------------------------
    typer.echo("[4] Ranking...")
    ranked = rank_candidates(claude, config, enrichment.interpretation, deduped)
    selected_pairs = ranked[:min_results] if len(ranked) >= min_results else ranked
    selected = [SelectedProfile(candidate=c, rank=r) for c, r in selected_pairs]
    typer.echo(f"    selected top {len(selected)} of {len(ranked)} ranked")

    # [5] Draft generation ----------------------------------------------------
    typer.echo("[5] Drafting outreach messages...")
    draft_messages(claude, config, selected, operator_profile)

    # [6] Write to Sheet ------------------------------------------------------
    run_id = uuid.uuid4().hex[:12]
    run_date = date.today().isoformat()
    partial = len(selected) < min_results
    written = sheet.append_rows(run_id, run_date, selected)

    typer.echo(f"\n[6] Wrote {written} rows to '{sheet.title}'. run_id={run_id}")
    if partial:
        typer.secho(
            f"PARTIAL RUN: wrote {written} of MIN_RESULTS={min_results}, flagged as failed. "
            f"(run_id={run_id})",
            fg=typer.colors.YELLOW,
        )
    else:
        typer.secho("Done.", fg=typer.colors.GREEN)


# -------------------------------------------------------------------- learn
@app.command()
def learn() -> None:
    """Read operator scores/feedback from the Sheet and update prompt_deltas.json."""
    from .learn import run_learn

    config = _load_config_or_exit()
    claude = ClaudeClient(config)
    try:
        sheet = SheetClient(config)
    except SheetsError as exc:
        typer.secho(f"Sheet error: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1)
    run_learn(claude, config, sheet)


# ------------------------------------------------------------------ helpers
def _enrich_with_confirmation(
    claude: ClaudeClient, config: Config, raw_prompt: str, yes: bool
):
    clarifications: list[str] = []
    while True:
        typer.echo("\n[1] Interpreting your prompt with Claude...")
        enrichment = enrich(claude, config, raw_prompt, clarifications or None)

        if enrichment.clarifying_questions and not yes:
            typer.secho("\nClaude has a few clarifying questions:", fg=typer.colors.CYAN)
            for q in enrichment.clarifying_questions:
                answer = typer.prompt(f"  • {q}")
                clarifications.append(f"{q} -> {answer}")
            continue  # re-run enrichment with answers appended

        typer.secho("\nInterpretation:", fg=typer.colors.CYAN)
        typer.echo(f"  {enrichment.interpretation}")
        typer.secho("\nDiscovery filters:", fg=typer.colors.CYAN)
        typer.echo(filters_to_text(enrichment.filters))

        if yes:
            return enrichment

        choice = typer.prompt(
            "\nProceed with these filters? (y = yes, e = edit, n = abort)",
            default="y",
        ).strip().lower()
        if choice == "y":
            return enrichment
        if choice == "n":
            typer.secho("Aborted.", fg=typer.colors.YELLOW)
            raise typer.Exit(code=0)
        if choice == "e":
            enrichment.filters = _edit_filters(enrichment.filters)
            return enrichment


def _edit_filters(filters: ConfirmedFilters) -> ConfirmedFilters:
    typer.echo("Enter comma-separated values (blank keeps current).")

    def edit(label: str, current: list[str]) -> list[str]:
        raw = typer.prompt(f"  {label} [{', '.join(current)}]", default="", show_default=False)
        if not raw.strip():
            return current
        return [v.strip() for v in raw.split(",") if v.strip()]

    filters.titles = edit("titles", filters.titles)
    filters.keywords = edit("keywords", filters.keywords)
    filters.locations = edit("locations", filters.locations)
    return filters


def _resolve_operator_profile(
    brightdata: BrightDataClient, profile_url: str | None
) -> Candidate | None:
    if not profile_url:
        typer.secho(
            "  (no --profile given; drafts will use a neutral sender voice)",
            fg=typer.colors.YELLOW,
        )
        return None

    cached = _load_operator_cache(profile_url)
    if cached is not None:
        typer.echo("  using cached operator profile")
        return cached

    typer.echo("  scraping operator profile once (cached for next time)...")
    try:
        candidate = brightdata.scrape_profile(profile_url)
    except (DiscoveryError, Exception) as exc:  # noqa: BLE001
        typer.secho(f"  could not scrape operator profile ({exc}); using neutral voice", fg=typer.colors.YELLOW)
        return None
    if candidate:
        _save_operator_cache(profile_url, candidate)
    return candidate


def _load_operator_cache(profile_url: str) -> Candidate | None:
    if not OPERATOR_CACHE.exists():
        return None
    try:
        data = json.loads(OPERATOR_CACHE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    if data.get("profile_url_input") != profile_url:
        return None
    c = data.get("candidate", {})
    return Candidate(
        name=c.get("name", ""),
        profile_url=c.get("profile_url", ""),
        headline=c.get("headline", ""),
        company=c.get("company", ""),
        location=c.get("location", ""),
        about=c.get("about", ""),
        experience=c.get("experience", []),
    )


def _save_operator_cache(profile_url: str, candidate: Candidate) -> None:
    OPERATOR_CACHE.parent.mkdir(parents=True, exist_ok=True)
    OPERATOR_CACHE.write_text(
        json.dumps(
            {"profile_url_input": profile_url, "candidate": candidate.to_prompt_dict()},
            indent=2,
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )


def main() -> None:
    try:
        app()
    except KeyboardInterrupt:
        typer.secho("\nInterrupted.", fg=typer.colors.YELLOW, err=True)
        sys.exit(130)


if __name__ == "__main__":
    main()
