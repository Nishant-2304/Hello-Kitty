"""Load templates.md + subjects.md, render with sheet row + user profile.

Placeholders (two syntaxes, both supported in any template body):

  {{snake_case}}    -> row['snake_case']   (sheet column, header lowercased,
                                             spaces -> underscores)
  [Token]           -> user[token] OR      (sender info, from logged-in user)
                       row[token]          (per-row, e.g. [Name], [Company])

Unknown placeholders are left as literal text so a later LLM pass can
find them and fill them in. Missing values render as empty string.

Subject is looked up by sheet's EVENT column (CLUEMINATI | COOKOFF) from
subjects.md, completely independent of the template body type.
"""
from pathlib import Path
import re

ROOT = Path(__file__).parent.parent
TEMPLATES_FILE = ROOT / "templates.md"
SUBJECTS_FILE = ROOT / "subjects.md"

_BRACKET_RE = re.compile(r"\[([^\[\]]+)\]")  # matches [Name], [Your Name], etc.
_BRACES_RE = re.compile(r"\{\{([a-z0-9_]+)\}\}")  # matches {{snake_case}}

# Sender placeholders the renderer pulls from the logged-in user dict, not
# from the sheet row. Keys are the literal text between the brackets,
# values are the keys we look up in the user dict.
SENDER_TOKENS = {
    "Your Name": "name",
}

# Sheet column header -> snake_case key. The aliases here cover the
# different spellings we've seen across sheets — the parser is
# case-insensitive, so capitalization doesn't matter. Add a new entry
# here if a future sheet uses a different name.
COLUMN_ALIASES = {
    # POC's name (the [Name] placeholder target)
    "name": "name",
    "poc name": "name",
    # Company
    "company": "company_name",
    "company name": "company_name",
    "comapany name": "company_name",
    # Type and categories
    "type": "type",
    "categories": "type",
    "category": "type",
    "event": "event",
    # Contact
    "email": "email_id",
    "email id": "email_id",
    "linkedin": "linkedin_profile",
    "linkedin profile": "linkedin_profile",
    "phone": "phone",
    # Sheet-side state columns (rarely used in templates, but harmless)
    "status": "status",
    "contacted": "contacted_by",
    "contacted by": "contacted_by",
    "next action": "next_action",
}


def _normalize_col(header: str) -> str:
    """Sheet header -> snake_case key, with alias table."""
    raw = header.strip().lower()
    return COLUMN_ALIASES.get(raw, raw.replace(" ", "_"))


def _row_to_context(row: dict) -> dict:
    """Build a flat context dict from a sheet row, with all keys normalized."""
    ctx = {}
    for header, value in row.items():
        if value is None:
            continue
        ctx[_normalize_col(header)] = str(value).strip()
    return ctx


def _parse_markdown(path: Path) -> dict[str, str]:
    """Parse a markdown file of the form:

        ## <Header>: <name>
        body...
        body...
        ## <Header>: <name2>
        body...

    Returns {name.lower(): body}. Header values are stripped of trailing
    colons, whitespace, and lowercased.
    """
    text = path.read_text(encoding="utf-8")
    sections = {}
    current_name = None
    current_lines: list[str] = []

    for line in text.split("\n"):
        if line.startswith("## "):
            if current_name is not None:
                sections[current_name] = "\n".join(current_lines).strip()
            # "## Type: Standard" -> "standard"
            # "## Event: CLUEMINATI" -> "clueminati"
            rest = line[3:].strip()
            if ":" in rest:
                rest = rest.split(":", 1)[1].strip()
            current_name = rest.rstrip(":").strip().lower()
            current_lines = []
        else:
            if current_name is not None:
                current_lines.append(line)

    if current_name is not None:
        sections[current_name] = "\n".join(current_lines).strip()

    return sections


# Cache: the files rarely change. Invalidate by restarting the process.
_templates_cache: dict[str, str] | None = None
_subjects_cache: dict[str, str] | None = None


def _load_templates() -> dict[str, str]:
    global _templates_cache
    if _templates_cache is None:
        _templates_cache = _parse_markdown(TEMPLATES_FILE)
    return _templates_cache


def _load_subjects() -> dict[str, str]:
    global _subjects_cache
    if _subjects_cache is None:
        _subjects_cache = _parse_markdown(SUBJECTS_FILE)
    return _subjects_cache


def get_body(type_value: str) -> str:
    """Return the template body for a sheet TYPE value (case-insensitive).
    Falls back to 'standard' or the first available template if missing."""
    templates = _load_templates()
    key = (type_value or "").strip().lower()
    if key in templates:
        return templates[key]
    if "standard" in templates:
        return templates["standard"]
    if templates:
        return next(iter(templates.values()))
    raise KeyError(f"No templates available")


def get_subject(event_value: str) -> str:
    """Return the email subject for a sheet EVENT value (case-insensitive).
    Falls back to 'clueminati' or the first available subject if missing."""
    subjects = _load_subjects()
    key = (event_value or "").strip().lower()
    if key in subjects:
        return subjects[key]
    if "clueminati" in subjects:
        return subjects["clueminati"]
    if subjects:
        return next(iter(subjects.values()))
    raise KeyError(f"No subjects available")


def _name_from_email(email: str) -> str:
    """Extract name from the part before '@' in an email address, capitalizing words."""
    if not email or "@" not in email:
        return "Team"
    handle = email.split("@")[0].strip()
    if not handle:
        return "Team"
    words = [w.capitalize() for w in re.split(r"[._\-]", handle) if w]
    return " ".join(words) if words else handle.capitalize()


def render(body: str, row: dict, user: dict) -> str:
    """Render a template body with row + user data.

    Order of substitution:
      1. {{snake_case}} placeholders -> row context
      2. [Token] placeholders -> if SENDER_TOKENS has it, use user[]; else
         fall back to row[] (so [Name] and [Company] work too)
    Unknown tokens are left literal.
    """
    ctx = _row_to_context(row)
    user_ctx = {
        "name": user.get("name", ""),
    }

    def _braces_sub(m: re.Match) -> str:
        return ctx.get(m.group(1), "")  # missing -> empty string

    def _brackets_sub(m: re.Match) -> str:
        token = m.group(1).strip()
        # Strip the comment marker if HR wrapped a placeholder in one
        if token.lower().startswith("personalize below"):
            return ""  # strip the [personalize below...] comment entirely
        if token in SENDER_TOKENS:
            return user_ctx.get(SENDER_TOKENS[token], "")
        # Fallback: try the row context (so [Name], [Company] work)
        key = token.lower()
        if key == "company":
            key = "company_name"
        if key in ctx and ctx[key]:
            return ctx[key]
        if key == "name":
            to_email = ctx.get("email_id") or ctx.get("email") or ""
            return _name_from_email(to_email)
        # Unknown [token] -> leave literal (LLM will fill later)
        return m.group(0)

    out = _BRACES_RE.sub(_braces_sub, body)
    out = _BRACKET_RE.sub(_brackets_sub, out)
    return out


def clear_cache() -> None:
    """For tests / when you edit the md files mid-process."""
    global _templates_cache, _subjects_cache
    _templates_cache = None
    _subjects_cache = None
