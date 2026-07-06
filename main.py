import os
import random
import re

from dotenv import load_dotenv

from services.sheets import get_first_pending_lead, mark_drafted
from services.docs import get_subjects, get_bodies
from services.llm import generate_email
from services.gmail import create_draft

load_dotenv()

SUBJECT_DOC_ID = os.getenv("SUBJECT_DOC_ID", "").strip()
BODY_DOC_ID = os.getenv("BODY_DOC_ID", "").strip()

DEFAULT_TO = "nishant.sh230408@gmail.com"


def parse_llm_output(text):
    """Parse 'SUBJECT:\\n...\\nBODY:\\n...' into (subject, body)."""
    match = re.search(
        r"SUBJECT\s*:\s*(.*?)\s*BODY\s*:\s*(.*)",
        text,
        re.DOTALL | re.IGNORECASE,
    )
    if not match:
        return None, None
    return match.group(1).strip(), match.group(2).strip()


def main():
    if not SUBJECT_DOC_ID or not BODY_DOC_ID:
        raise RuntimeError(
            "Set SUBJECT_DOC_ID and BODY_DOC_ID in .env before running."
        )

    # 1. Pick the first lead that isn't drafted yet.
    row, lead = get_first_pending_lead()
    if not lead:
        print("No pending leads.")
        return

    print(f"Using lead on row {row}: {lead}")

    # 2. Pick a random subject from the subjects doc.
    subjects = get_subjects(SUBJECT_DOC_ID)
    if not subjects:
        raise RuntimeError(f"No subjects found in doc {SUBJECT_DOC_ID}")
    chosen_subject = random.choice(subjects)

    # 3. Look up the body template by the lead's TYPE.
    body_type = (lead.get("TYPE") or "").strip().lower()
    if not body_type:
        raise RuntimeError(f"Lead on row {row} has no TYPE")

    bodies = get_bodies(BODY_DOC_ID)
    base_body = bodies.get(body_type)
    if not base_body:
        raise RuntimeError(
            f"No body template for type '{body_type}'. "
            f"Available: {list(bodies)}"
        )

    # 4. Ask Groq to elaborate / structure the email.
    company = (lead.get("COMPANY") or "").strip()
    llm_output = generate_email(
        company=company,
        subject=chosen_subject,
        body=base_body,
    )

    # 5. Split SUBJECT / BODY from the LLM response.
    subject, body = parse_llm_output(llm_output)
    if not subject or not body:
        print("Could not parse LLM output:")
        print(llm_output)
        return

    # 6. Create the Gmail draft.
    recipient = (lead.get("EMAIL") or DEFAULT_TO).strip()
    draft = create_draft(to=recipient, subject=subject, body=body)
    print(f"Draft created: id={draft.get('id')} for {recipient}")

    # 7. Mark the sheet row as drafted.
    mark_drafted(row)
    print(f"Row {row} marked as drafted.")


if __name__ == "__main__":
    main()