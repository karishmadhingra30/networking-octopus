"""Google Sheets integration: auth, header init, dedup read, append rows.

Uses the OAuth *user* flow (the operator's own Google account) via gspread, not
a service account. On first run the operator authenticates in a browser; the
token is cached at GOOGLE_TOKEN_CACHE and reused afterwards. Because the Sheet
lives on the operator's account, no sheet sharing is required.
"""

from __future__ import annotations

from typing import Iterable

import gspread
from gspread.utils import rowcol_to_a1
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow

from .config import Config
from .models import SelectedProfile, normalize_url

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive.file",
]

# Column order is the Sheet's contract. `score` and `feedback` are operator-filled.
HEADERS = [
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

PROFILE_URL_COL = HEADERS.index("profile_url")  # 0-based
SCORE_COL = HEADERS.index("score")
FEEDBACK_COL = HEADERS.index("feedback")


class SheetsError(RuntimeError):
    pass


def _load_credentials(config: Config) -> Credentials:
    """Load cached OAuth credentials or run the installed-app browser flow."""
    creds: Credentials | None = None
    token_path = config.google_token_cache

    try:
        creds = Credentials.from_authorized_user_file(token_path, SCOPES)
    except (FileNotFoundError, ValueError):
        creds = None

    if creds and creds.valid:
        return creds

    if creds and creds.expired and creds.refresh_token:
        creds.refresh(Request())
    else:
        flow = InstalledAppFlow.from_client_secrets_file(
            config.google_oauth_client_secrets, SCOPES
        )
        creds = flow.run_local_server(port=0)

    with open(token_path, "w", encoding="utf-8") as fh:
        fh.write(creds.to_json())
    return creds


class SheetClient:
    def __init__(self, config: Config):
        self._config = config
        creds = _load_credentials(config)
        self._gc = gspread.authorize(creds)
        try:
            self._spreadsheet = self._gc.open_by_key(config.sheet_id)
        except gspread.SpreadsheetNotFound as exc:
            raise SheetsError(
                f"Could not open Sheet {config.sheet_id!r}. Confirm SHEET_ID and "
                "that it lives on the authenticated Google account."
            ) from exc
        self._ws = self._spreadsheet.sheet1

    # ------------------------------------------------------------- init
    def ensure_header(self) -> bool:
        """Create the header row if missing. Returns True if it was written.

        Always (re)applies header formatting — bold text, frozen first row — so
        sheets created before formatting existed get upgraded on the next run.
        """
        existing = self._ws.row_values(1)
        created = False
        if existing[: len(HEADERS)] != HEADERS:
            if any(cell.strip() for cell in existing):
                raise SheetsError(
                    "Row 1 has unexpected content and does not match the expected "
                    f"header. Expected: {HEADERS}. Found: {existing}"
                )
            self._ws.update("A1", [HEADERS])
            created = True
        self._format_header()
        return created

    def _format_header(self) -> None:
        header_range = f"A1:{rowcol_to_a1(1, len(HEADERS))}"
        self._ws.format(header_range, {"textFormat": {"bold": True}})
        self._ws.freeze(rows=1)

    @property
    def title(self) -> str:
        return self._spreadsheet.title

    # ------------------------------------------------------------- dedup
    def existing_url_keys(self) -> set[str]:
        """Return the set of normalized profile_url keys already in the Sheet."""
        col = self._ws.col_values(PROFILE_URL_COL + 1)  # 1-based; includes header
        keys = {normalize_url(v) for v in col[1:] if v.strip()}
        keys.discard("")
        return keys

    # ------------------------------------------------------------- write
    def append_rows(self, run_id: str, run_date: str, selected: Iterable[SelectedProfile]) -> int:
        """Append one row per selected profile. Returns the number written."""
        rows = [_to_row(run_id, run_date, s) for s in selected]
        if not rows:
            return 0
        self._ws.append_rows(rows, value_input_option="USER_ENTERED")
        return len(rows)

    # ------------------------------------------------- learn (feedback read)
    def read_feedback_rows(self) -> list[dict[str, str]]:
        """Return rows that have a non-empty score and/or feedback."""
        records = self._ws.get_all_records(expected_headers=HEADERS)
        out: list[dict[str, str]] = []
        for rec in records:
            score = str(rec.get("score", "")).strip()
            feedback = str(rec.get("feedback", "")).strip()
            if score or feedback:
                out.append({k: str(v) for k, v in rec.items()})
        return out


def _to_row(run_id: str, run_date: str, s: SelectedProfile) -> list[str]:
    c = s.candidate
    row = [
        run_id,
        run_date,
        c.name,
        c.profile_url,
        c.headline,
        c.company,
        c.location,
        s.rank.background_summary,
        s.rank.rationale,
        s.draft_message,
        "",  # score (operator)
        "",  # feedback (operator)
    ]
    assert len(row) == len(HEADERS)
    return row
