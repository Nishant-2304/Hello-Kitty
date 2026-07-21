import os
from datetime import datetime, timezone
from typing import Iterable

from dotenv import load_dotenv
from google.oauth2.service_account import Credentials
import gspread

# Load .env when this module is imported, so SPREADSHEET_ID is available
# regardless of how the module is invoked (script, FastAPI, test, etc.).
load_dotenv()

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

creds = Credentials.from_service_account_file(
    "service_account.json",
    scopes=SCOPES,
)
client = gspread.authorize(creds)

# Read from env so swapping sheets = .env change, no code change.
# Find the ID in the Google Sheet URL: /spreadsheets/d/<THIS_PART>/edit
SHEET_ID = os.getenv("SPREADSHEET_ID", "1xlZBpegeqY_sVK7Ya2W2I8wrwcHQlwbdOHzlsPAi6QU").strip()
if not SHEET_ID:
    raise RuntimeError(
        "SPREADSHEET_ID is not set. Add it to your .env file. "
        "Find it in the Google Sheet URL between /d/ and /edit."
    )

# Status values written to the STATUS column
STATUS_PENDING = ""           # empty cell = available
STATUS_CLAIMED = "claimed"
STATUS_DRAFTED = "drafted"

# Per-process lock for the /run claim step. Good enough for 10 users;
# if you scale beyond that, swap for a Redis lock keyed on SHEET_ID.
import asyncio
_sheet_lock = asyncio.Lock()


def _get_sheet():
    sh = client.open_by_key(SHEET_ID)
    target = "new categorized companies"
    for w in sh.worksheets():
        if w.title.strip().lower() == target:
            return w
    return sh.sheet1


def _find_header_row_index(all_values: list[list[str]]) -> int:
    """Find the 1-based index of the header row containing COMPANY NAME and EMAIL ID."""
    for idx, row in enumerate(all_values):
        row_lower = [cell.strip().lower() for cell in row]
        has_company = any("company name" in h or "comapany name" in h for h in row_lower)
        has_email = any("email id" in h or "email" in h for h in row_lower)
        if has_company and has_email:
            return idx + 1
    return 1


def _get_headers(sheet) -> list[str]:
    all_values = sheet.get_all_values()
    if not all_values:
        return []
    header_idx = _find_header_row_index(all_values)
    return [h.strip() for h in all_values[header_idx - 1]]


def _col_letter(col_index_1based: int) -> str:
    """1 -> A, 2 -> B, ..., 27 -> AA. Avoids gspread cell-by-cell writes."""
    letters = ""
    n = col_index_1based
    while n > 0:
        n, rem = divmod(n - 1, 26)
        letters = chr(ord("A") + rem) + letters
    return letters


def _find_col(headers: list[str], header_name: str) -> int | None:
    """Case-insensitive fuzzy header match. Returns 1-indexed column or None.

    Match rules (in order):
      1. Exact case-insensitive match ('Contacted By' == 'contacted by')
      2. Prefix match either way ('contacted' matches 'CONTACTED BY',
         'contacted by' matches 'CONTACTED')
    """
    target = header_name.lower()
    for i, h in enumerate(headers):
        if h.lower() == target:
            return i + 1
    for i, h in enumerate(headers):
        if h.lower().startswith(target) or target.startswith(h.lower()):
            return i + 1
    return None


def get_leads() -> list[dict]:
    """All rows as a list of dicts keyed by header (raw, not normalized)."""
    sheet = _get_sheet()
    all_values = sheet.get_all_values()
    if not all_values:
        return []

    header_idx = _find_header_row_index(all_values)
    headers = [h.strip() for h in all_values[header_idx - 1]]

    # Only keep headers that are non-empty to avoid duplicate empty header errors
    num_cols = len(headers)
    while num_cols > 0 and headers[num_cols - 1] == "":
        num_cols -= 1

    headers = headers[:num_cols]

    records = []
    for row in all_values[header_idx:]:
        # Pad or slice row to match headers count
        padded_row = row + [""] * (num_cols - len(row))
        padded_row = padded_row[:num_cols]
        # Skip empty rows (where every value mapped to headers is empty)
        if not any(padded_row):
            continue
        records.append(dict(zip(headers, padded_row)))
    return records


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def claim_rows(n: int, username: str) -> list[tuple[int, dict]]:
    """Find the first N rows with empty STATUS and empty CONTACTED BY,
    mark them 'claimed' by `username`, and return [(sheet_row, record), ...].

    Sheet write is batched (one update per column for the affected rows).
    Returns [] if fewer than N rows are available.
    """
    if n <= 0:
        return []

    sheet = _get_sheet()
    all_values = sheet.get_all_values()
    if not all_values:
        return []

    headers = _get_headers(sheet)

    status_col = _find_col(headers, "status")
    contacted_col = _find_col(headers, "contacted by")
    if status_col is None or contacted_col is None:
        raise ValueError(
            f"Sheet must have STATUS and CONTACTED BY columns. "
            f"Found headers: {headers}"
        )

    header_row_index = _find_header_row_index(all_values)
    claimed: list[tuple[int, dict]] = []

    for idx in range(header_row_index, len(all_values)):
        if len(claimed) >= n:
            break
        row = all_values[idx]
        # Build the record dict
        padded_row = row + [""] * (len(headers) - len(row))
        padded_row = padded_row[:len(headers)]
        record = dict(zip(headers, padded_row))

        # Skip empty/blank rows
        if not any(padded_row):
            continue

        status = str(record.get("STATUS", "")).strip()
        contacted = str(record.get("CONTACTED BY", "")).strip()
        email = str(record.get("EMAIL ID") or record.get("EMAIL") or record.get("EMAIL_ID") or "").strip()
        if status == "" and contacted == "" and email != "":
            sheet_row = idx + 1  # 1-based row index in worksheet
            claimed.append((sheet_row, record))

    if not claimed:
        return []

    timestamp = _now_iso()
    claim_value = STATUS_CLAIMED
    contact_value = f"{username} (claimed at {timestamp})"

    updates = [
        {"range": f"{_col_letter(status_col)}{r}", "values": [[claim_value]]}
        for r, _ in claimed
    ] + [
        {"range": f"{_col_letter(contacted_col)}{r}", "values": [[contact_value]]}
        for r, _ in claimed
    ]
    sheet.batch_update(updates)

    return claimed


def release_claim(rows: Iterable[int]) -> None:
    """Clear STATUS and CONTACTED BY on the given sheet rows.
    Used when a claim is stolen or when drafts fail before push to Gmail."""
    rows = list(rows)
    if not rows:
        return

    sheet = _get_sheet()
    headers = _get_headers(sheet)
    status_col = _find_col(headers, "status")
    contacted_col = _find_col(headers, "contacted by")
    if status_col is None or contacted_col is None:
        raise ValueError(
            f"Sheet must have STATUS and CONTACTED BY columns. "
            f"Found headers: {headers}"
        )

    for r in rows:
        sheet.update_cell(r, status_col, STATUS_PENDING)
        sheet.update_cell(r, contacted_col, "")


def verify_ownership(
    rows: Iterable[int], username: str
) -> tuple[list[int], list[int]]:
    """Re-read the given sheet rows. Return (still_ours, stolen).
    A row is 'ours' if CONTACTED BY starts with `<username> (claimed at`.
    """
    rows = list(rows)
    if not rows:
        return [], []

    sheet = _get_sheet()
    headers = _get_headers(sheet)
    contacted_col = _find_col(headers, "contacted by")
    if contacted_col is None:
        raise ValueError(
            f"Sheet missing CONTACTED column. Headers: {headers}"
        )

    prefix = f"{username} (claimed at"
    col_letter = _col_letter(contacted_col)
    values = sheet.get(
        f"{col_letter}{min(rows)}:{col_letter}{max(rows)}"
    )
    min_row = min(rows)
    still_ours: list[int] = []
    stolen: list[int] = []
    for r in rows:
        idx = r - min_row
        val = (values[idx][0] if idx < len(values) and values[idx] else "").strip()
        if val.startswith(prefix):
            still_ours.append(r)
        else:
            stolen.append(r)

    return still_ours, stolen


def mark_drafted(rows: Iterable[int], username: str) -> None:
    """Mark rows as 'drafted' by `username` (drafts pushed to Gmail)."""
    rows = list(rows)
    if not rows:
        return

    sheet = _get_sheet()
    headers = _get_headers(sheet)
    status_col = _find_col(headers, "status")
    contacted_col = _find_col(headers, "contacted by")
    if status_col is None or contacted_col is None:
        raise ValueError(
            f"Sheet must have STATUS and CONTACTED BY columns. "
            f"Found headers: {headers}"
        )

    timestamp = _now_iso()
    status_value = STATUS_DRAFTED
    contact_value = f"{username} (drafted at {timestamp})"

    updates = [
        {"range": f"{_col_letter(status_col)}{r}", "values": [[status_value]]}
        for r in rows
    ] + [
        {"range": f"{_col_letter(contacted_col)}{r}", "values": [[contact_value]]}
        for r in rows
    ]
    sheet.batch_update(updates)


def get_lock() -> asyncio.Lock:
    """The process-wide claim lock. Acquire this in /run before claiming."""
    return _sheet_lock
