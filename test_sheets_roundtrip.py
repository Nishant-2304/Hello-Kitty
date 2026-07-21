"""PR 1 round-trip test. Runs against the REAL sheet (service account must
be Editor on the sheet, and SPREADSHEET_ID must be in .env).

This test does NOT touch Gmail. It only proves the claim -> verify ->
release round-trip works on the sheet, which is the part most likely to
have edge-case bugs (column lookups, batch writes, batch reads).

v1: runs on a single pending row (the sheet currently only has 1).
v2: once the production sheet has 700+ rows, change N back to 2 or 10.

Usage:
    python test_sheets_roundtrip.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from services.sheets import (
    SHEET_ID,
    claim_rows,
    get_leads,
    mark_drafted,
    release_claim,
    verify_ownership,
)

N = 1  # change to 2 or 10 once the production sheet is populated


def hr(s: str) -> str:
    return f"\n{'=' * len(s)}\n{s}\n{'=' * len(s)}"


def main() -> int:
    print(hr(f"Connected to sheet ID: {SHEET_ID}"))
    leads = get_leads()
    pending = [
        (i, r) for i, r in enumerate(leads)
        if str(r.get("STATUS", "")).strip() == ""
        and str(r.get("CONTACTED BY", "")).strip() == ""
    ]
    print(f"Total rows: {len(leads)}")
    print(f"Pending rows: {len(pending)}")

    if len(pending) < N:
        print(
            f"Need at least {N} pending row(s) to run this test. "
            f"Either wait for the team to draft more, or manually clear "
            f"STATUS and CONTACTED BY on {N} row(s) in the sheet, and re-run."
        )
        return 1

    test_user = "test_roundtrip"

    print(hr(f"Step 1: claim {N} row(s) as '{test_user}'"))
    claimed = claim_rows(N, test_user)
    claimed_rows = [r for r, _ in claimed]
    print(f"Claimed sheet rows: {claimed_rows}")
    for r, record in claimed:
        name = (
            record.get("COMAPANY NAME")
            or record.get("COMPANY NAME")
            or record.get("NAME")
            or "(no name)"
        )
        print(f"  row {r}: {name}")

    print(hr("Step 2: verify ownership (should all be ours)"))
    still_ours, stolen = verify_ownership(claimed_rows, test_user)
    print(f"still_ours: {still_ours}")
    print(f"stolen:     {stolen}")
    assert not stolen, f"Unexpected stolen rows: {stolen}"
    assert sorted(still_ours) == sorted(claimed_rows), "Mismatch in still_ours"

    print(hr("Step 3: simulate theft — release the claim, re-verify"))
    # Release the claim to simulate another user stealing it.
    release_claim(claimed_rows)
    still_ours2, stolen2 = verify_ownership(claimed_rows, test_user)
    print(f"still_ours: {still_ours2}")
    print(f"stolen:     {stolen2}")
    for r in claimed_rows:
        assert r in stolen2, f"Released row {r} should appear stolen"
    assert not still_ours2, f"Released rows should not be ours: {still_ours2}"

    print(hr("Step 4: re-claim and mark drafted (end-to-end happy path)"))
    # Re-claim the same row, then mark it as drafted (no Gmail involved).
    reclaimed = claim_rows(N, test_user)
    reclaimed_rows = [r for r, _ in reclaimed]
    print(f"Re-claimed: {reclaimed_rows}")
    mark_drafted(reclaimed_rows, test_user)

    print(hr("Step 5: verify final state"))
    still_ours3, _ = verify_ownership(reclaimed_rows, test_user)
    # After mark_drafted, the prefix is "drafted at", not "claimed at",
    # so verify_ownership should report them as stolen (which is correct —
    # the claim is over).
    print(f"After mark_drafted — still_ours: {still_ours3} (expected: [])")
    assert not still_ours3, f"After mark_drafted, no rows should still be claimed: {still_ours3}"

    print(hr("Step 6: clean up — clear the drafted row so the sheet is back to pending"))
    release_claim(reclaimed_rows)

    print(hr("PASS"))
    print(
        "The claim/release/verify/mark round-trip works against your real sheet.\n"
        f"Claimed {N} row(s), simulated a theft, re-claimed, marked drafted, "
        f"and cleaned up. No Gmail was involved."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
