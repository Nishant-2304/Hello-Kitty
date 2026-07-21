"""The /run orchestrator: claim -> build drafts -> verify -> push -> mark.

This is the heart of the bot. The state machine:

  1. Acquire the per-sheet asyncio lock (prevents two /run calls
     from claiming the same rows at the same time).
  2. Read sheet, find the first N rows with empty STATUS and empty
     CONTACTED BY. Mark them 'claimed' by the user. Release the lock
     would be wrong — we hold it until the end so a second /run
     cannot interleave between our claim and our mark.
  3. For each claimed row: build a draft in memory (subject + body).
  4. Re-read those rows. For any row whose CONTACTED BY no longer
     starts with '<username> (claimed at', discard those drafts,
     release the claim on those rows, and retry the whole claim
     step (max 3 retries).
  5. For each surviving draft: call gmail.create_draft(). If it
     fails, log + skip; don't mark that row drafted.
  6. Mark successful rows as 'drafted' in the sheet.
  7. Return a per-row summary to the caller.

Why hold the lock for the whole flow: between Step 2 (claim) and
Step 6 (mark), a second /run call must not be able to claim a
different batch, because the second call's verify_ownership in
Step 4 could see the first call's claimed rows and decide they
were 'stolen'. The lock is the only thing keeping the world sane
at >1 user.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any

import services.sheets as sheets
import services.templates as templates
import services.gmail as gmail

# How many rows to claim per /run call. Hardcoded for v1.
BATCH_SIZE = 10

# How many times to retry the claim->verify->mark loop if a row is
# stolen. v1: 3 retries is plenty.
MAX_RETRIES = 3

CLIENT_ID = os.getenv("GOOGLE_OAUTH_CLIENT_ID", "").strip()
CLIENT_SECRET = os.getenv("GOOGLE_OAUTH_CLIENT_SECRET", "").strip()


@dataclass
class DraftResult:
    sheet_row: int
    company: str
    to_email: str
    subject: str
    body: str
    pushed: bool = False
    error: str | None = None
    draft_id: str | None = None


@dataclass
class RunResult:
    user_id: str
    username: str
    attempts: int = 0
    drafted: list[DraftResult] = field(default_factory=list)
    failed: list[DraftResult] = field(default_factory=list)
    abandoned: list[int] = field(default_factory=list)  # sheet rows we couldn't claim

    @property
    def total_pushed(self) -> int:
        return sum(1 for d in self.drafted if d.pushed)

    def to_dict(self) -> dict[str, Any]:
        return {
            "username": self.username,
            "attempts": self.attempts,
            "drafted": [
                {
                    "sheet_row": d.sheet_row,
                    "company": d.company,
                    "to": d.to_email,
                    "subject": d.subject,
                    "pushed": d.pushed,
                    "draft_id": d.draft_id,
                    "error": d.error,
                }
                for d in self.drafted
            ],
            "failed": [
                {
                    "sheet_row": d.sheet_row,
                    "company": d.company,
                    "to": d.to_email,
                    "subject": d.subject,
                    "error": d.error,
                }
                for d in self.failed
            ],
            "abandoned": self.abandoned,
            "summary": {
                "claimed": len(self.drafted) + len(self.failed),
                "pushed_to_gmail": self.total_pushed,
                "gmail_failures": len(self.failed),
            },
        }


async def run(user: dict[str, Any]) -> RunResult:
    """The full claim -> draft -> verify -> push -> mark flow.

    `user` is a row from the users table (has google sub, username,
    name, phone, linkedin, access_token).
    """
    result = RunResult(user_id=user["id"], username=user["username"])

    async with sheets.get_lock():
        for attempt in range(1, MAX_RETRIES + 1):
            result.attempts = attempt
            attempt_outcome = await _attempt(user, result)
            if attempt_outcome == "ok":
                return result
            if attempt_outcome == "no_more_rows":
                # We successfully claimed a partial batch and there are
                # no more rows available — that's not an error, return.
                return result
            # attempt_outcome == "stolen": loop and try again.
        # Retries exhausted. Release whatever drafts we built.
        sheets.release_claim([d.sheet_row for d in result.drafted])
        sheets.release_claim([d.sheet_row for d in result.failed])
        return result


async def _attempt(user: dict[str, Any], result: RunResult) -> str:
    """One claim->draft->verify->push->mark attempt.

    Returns:
        "ok"             — drafts built, verified, pushed, marked. Done.
        "no_more_rows"   — couldn't fill a batch; the partial batch was
                           processed or released. Done.
        "stolen"         — at least one row was stolen mid-flow. Caller
                           should retry.
    """
    username = user["username"]
    pending_count = _count_pending()

    if pending_count == 0:
        return "no_more_rows"

    claimed = sheets.claim_rows(BATCH_SIZE, username)
    if not claimed:
        return "no_more_rows"

    # Build drafts in memory.
    drafts: list[DraftResult] = []
    for sheet_row, record in claimed:
        try:
            subject = templates.get_subject(record.get("EVENT", ""))
        except KeyError as e:
            # Sheet has a TYPE/EVENT we don't have a template for — skip.
            result.abandoned.append(sheet_row)
            continue

        try:
            body = templates.get_body(record.get("CATEGORY") or record.get("CATEGORIES") or record.get("TYPE", ""))
        except KeyError:
            result.abandoned.append(sheet_row)
            continue

        rendered_body = templates.render(body, record, user)
        drafts.append(
            DraftResult(
                sheet_row=sheet_row,
                company=record.get("COMPANY NAME") or record.get("COMAPANY NAME") or record.get("NAME", ""),
                to_email=record.get("EMAIL ID") or record.get("EMAIL", ""),
                subject=subject,
                body=rendered_body,
            )
        )

    if not drafts:
        # No drafts could be built (every row had an unknown TYPE/EVENT).
        sheets.release_claim([r for r, _ in claimed])
        return "no_more_rows"

    # Re-verify ownership before pushing anything to Gmail.
    rows = [d.sheet_row for d in drafts]
    still_ours, stolen = sheets.verify_ownership(rows, username)

    if stolen:
        # Release the stolen rows, retry with a smaller effective batch.
        sheets.release_claim(stolen)
        # Keep only the still-ours drafts for the push step.
        drafts = [d for d in drafts if d.sheet_row in set(still_ours)]
        if not drafts:
            return "stolen"
        # Fall through and push the surviving drafts anyway.
        # (We don't release the still-ours drafts; they're still ours.)

    # Push to Gmail.
    for d in drafts:
        try:
            resp = await gmail.create_draft(
                user,
                to=d.to_email,
                subject=d.subject,
                body=d.body,
                client_id=CLIENT_ID,
                client_secret=CLIENT_SECRET,
            )
            d.pushed = True
            d.draft_id = resp.get("id")
            result.drafted.append(d)
        except Exception as e:
            d.error = str(e)
            result.failed.append(d)

    # Mark the successfully pushed rows as drafted.
    pushed_rows = [d.sheet_row for d in result.drafted if d.pushed]
    if pushed_rows:
        sheets.mark_drafted(pushed_rows, username)

    # Release the rows where Gmail failed — they shouldn't stay
    # 'claimed' forever.
    failed_rows = [d.sheet_row for d in result.failed]
    if failed_rows:
        sheets.release_claim(failed_rows)

    return "ok"


def _count_pending() -> int:
    """Count rows with empty STATUS, empty CONTACTED BY, and non-empty EMAIL ID."""
    leads = sheets.get_leads()
    return sum(
        1 for r in leads
        if str(r.get("STATUS", "")).strip() == ""
        and str(r.get("CONTACTED BY", "")).strip() == ""
        and str(r.get("EMAIL ID") or r.get("EMAIL") or "").strip() != ""
    )


async def preview_next_batch() -> list[dict[str, Any]]:
    """Return the next BATCH_SIZE rows that WOULD be claimed, read-only."""
    leads = sheets.get_leads()
    pending = [
        r for r in leads
        if str(r.get("STATUS", "")).strip() == ""
        and str(r.get("CONTACTED BY", "")).strip() == ""
        and str(r.get("EMAIL ID") or r.get("EMAIL") or "").strip() != ""
    ]
    return pending[:BATCH_SIZE]
