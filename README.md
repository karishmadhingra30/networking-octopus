# LinkedIn Networking Agent (CLI)

A single-operator command-line tool. You describe, in plain English, the kind of
people you want to network with. The agent:

1. **Enriches** your prompt into structured discovery filters (Claude), and
   confirms them with you in the terminal.
2. **Discovers** matching LinkedIn profiles via the Bright Data LinkedIn Scraper
   API (async discovery, no login, public data only).
3. **Dedupes** against profiles already in your Google Sheet.
4. **Ranks** the candidates for relevance (Claude).
5. **Drafts** a personalized outreach message for each selected profile (Claude),
   using your own LinkedIn profile for sender context.
6. **Writes** the results to your Google Sheet.

You review and send the messages manually. The tool never sends anything.

A separate `agent learn` run reads the scores and feedback you type into the
Sheet and refines the stored prompt guidance for future runs.

---

## 1. Prerequisites

- **Python 3.11+**
- A **Google account** (the Sheet lives on your own account).
- A **Bright Data account** (free tier: 5,000 records/month).
- An **Anthropic API key**.

## 2. Install

```bash
git clone <your-fork-url> linkedin-outreach-agent
cd linkedin-outreach-agent
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
# optional, for running tests:
pip install -e ".[dev]"
```

This installs an `agent` command on your PATH (inside the venv).

## 3. Google setup (OAuth, one-time)

The agent uses the OAuth **user** flow (your own Google account), not a service
account — so the Sheet stays on your account and no sharing is needed.

1. Go to the [Google Cloud Console](https://console.cloud.google.com/) and
   create a project (or reuse one).
2. Enable the **Google Sheets API** (and **Google Drive API**) for the project.
3. Go straight to it: console.cloud.google.com/auth/overview
         
         Confirm your project is selected in the top bar first.
         2. Click "Get started" (this is the wizard that replaced the user-type page). It asks for:
         
         App Information → app name (anything) + user support email (your email) → Next
         Audience → choose External ← this is where the External/Internal choice lives now → Next
         Contact Information → your email → Next
         Agree to the policy → Create
         3. Add yourself as a test user:
         
         Left nav → Audience
         Scroll to Test users → Add users → add your own Gmail → Save
         4. Create the OAuth client:
         
         Left nav → Clients → Create client
         Application type: Desktop app → Create
         Download the JSON.
         Then rename it to client_secret.json, drop it in the repo root

On your first `agent init` / `agent run`, a browser window opens for you to
authenticate. The resulting token is cached at `GOOGLE_TOKEN_CACHE`
(`./.google_token.json` by default) and reused on later runs.

## 4. Bright Data setup

1. In the Bright Data dashboard, get your **API token** (Account settings).
2. Setup SERP API and #TODO
3. Note the free tier: **5,000 records/month**.

> **Before your first real discovery run**, confirm the discovery request shape
> for the LinkedIn profiles dataset against
> [docs.brightdata.com](https://docs.brightdata.com). The trigger/poll/retrieve
> flow in `src/agent/discover.py` is stable; the `discover_by` value and the
> discovery request **body** field names are marked with `TODO(brightdata)` and
> are the only place you may need to adjust to match the current dataset schema.

## 5. Anthropic setup

Get an API key from the [Anthropic Console](https://console.anthropic.com/) and
put it in `ANTHROPIC_API_KEY`.

Models used (overridable in `.env`):
- Ranking: `claude-haiku-4-5-20251001` (cheap, fast).
- Enrichment / drafting / learn: `claude-opus-4-8` (quality).

## 6. Create the Sheet

Create a new Google Sheet on the **same** Google account you'll authenticate
with. Copy its id from the URL
(`https://docs.google.com/spreadsheets/d/<THIS_IS_THE_ID>/edit`) into `SHEET_ID`.
`agent init` creates the header row for you.

## 7. Configure `.env`

```bash
cp .env.example .env
# then fill in the blanks
```

## 8. Verify and run

```bash
agent init          # validates Anthropic + Bright Data + Google, creates the header row
agent run           # interactive: type your prompt, confirm filters, get a populated Sheet
```

Useful flags:

```bash
agent run --prompt "Founders building fertility / women's health startups in the US" \
          --profile https://www.linkedin.com/in/your-handle \
          --pool-size 40 --min-results 10
agent run --yes ...   # skip the interactive confirmation (trusted repeat prompts)
```

`--profile` is **your own** LinkedIn URL, used only to personalize drafts. It is
scraped once and cached locally (`.cache/operator_profile.json`).

## 9. Score, give feedback, and learn

Each run appends rows with two operator-owned columns left blank:

- **score** — your rating of the match (any scale you like, e.g. 1–5 or 0–100).
- **feedback** — free text on why a profile was good/bad.

Fill these in for whatever rows you have opinions on, then:

```bash
agent learn
```

The learn run reads the scored rows, asks Claude what high- and low-scored
profiles share, and writes refined guidance to
`src/agent/state/prompt_deltas.json`. Base prompts are never modified, so changes
are inspectable and reversible (it prints a diff of what changed). The next
`agent run` injects this guidance into enrichment and ranking automatically.

## 10. Cost note

Discovery is billed per profile record at ~$0.0015/record. A pool of 40 profiles
is roughly **$0.06 per run** — well within the Bright Data free tier at 1–2 runs
per week. Claude usage per run is a handful of small calls. The agent prints a
cost estimate before triggering discovery and **refuses to run** if the estimate
exceeds `COST_CEILING_USD` (default $1.00).

## Sheet schema

| Column | Filled by |
|---|---|
| run_id, run_date | agent |
| name, profile_url, headline, company, location | agent (candidate) |
| background_summary, match_rationale | agent (rank step) |
| draft_message | agent (draft step) |
| **score**, **feedback** | **you** (operator) |

`profile_url` is the dedup key; a second run never re-adds a profile already in
the Sheet.

## Partial runs

If fewer than `MIN_RESULTS` profiles survive discovery + dedup, the agent still
writes what it found and prints a clear
`PARTIAL RUN: wrote N of MIN_RESULTS, flagged as failed` message with the
`run_id` so you can identify it.

## Project layout

```
src/agent/
  cli.py          entrypoint (init / run / learn)
  config.py       env loading + validation
  enrich.py       [1] NL prompt -> filters (Claude)
  discover.py     [2] Bright Data trigger/poll/retrieve
  sheets.py       [3][6] Google Sheet read/write/dedup
  rank.py         [4] relevance scoring (Claude)
  draft.py        [5] outreach drafting (Claude)
  learn.py        feedback -> prompt deltas
  claude.py       shared Anthropic client + JSON parsing
  deltas.py       read/write learned prompt refinements
  models.py       Candidate / RankResult / SelectedProfile + URL normalization
  prompts/        system prompts (enrich/rank/draft/learn)
  state/          prompt_deltas.json (learned refinements)
tests/            dedup + sheet-schema unit tests
```

## Run the tests

```bash
pip install -e ".[dev]"
pytest -q
```
