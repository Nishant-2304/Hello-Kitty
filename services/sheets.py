from google.oauth2.service_account import Credentials
import gspread

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive"
]

creds = Credentials.from_service_account_file(
    "service_account.json",
    scopes=SCOPES
)

client = gspread.authorize(creds)

SHEET_NAME = "HelloKitty_dummy_S"
STATUS_DRAFTED = "drafted"


def _get_sheet():
    return client.open(SHEET_NAME).sheet1


def get_leads():
    return _get_sheet().get_all_records()


def get_first_pending_lead():
    """Return (sheet_row_number, record) for the first lead without STATUS set
    to 'drafted'. Row 1 is the header, so first data row is 2."""
    sheet = _get_sheet()
    records = sheet.get_all_records()
    for offset, record in enumerate(records):
        if str(record.get("STATUS", "")).strip().lower() != STATUS_DRAFTED:
            return offset + 2, record
    return None, None


def mark_drafted(row_number):
    sheet = _get_sheet()
    headers = sheet.row_values(1)
    if "STATUS" not in headers:
        raise ValueError("Sheet has no STATUS column")
    status_col = headers.index("STATUS") + 1  # gspread is 1-indexed
    sheet.update_cell(row_number, status_col, STATUS_DRAFTED)