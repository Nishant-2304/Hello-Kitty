from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build


SCOPES = [
    "https://www.googleapis.com/auth/documents.readonly"
]

creds = Credentials.from_service_account_file(
    "service_account.json",
    scopes=SCOPES
)

docs_service = build(
    "docs",
    "v1",
    credentials=creds
)


def get_doc_text(document_id):
    doc = docs_service.documents().get(
        documentId=document_id
    ).execute()

    content = doc["body"]["content"]

    text = ""

    for element in content:
        if "paragraph" not in element:
            continue

        paragraph = element["paragraph"]

        for item in paragraph.get("elements", []):
            if "textRun" in item:
                text += item["textRun"]["content"]

    return text


def get_subjects(document_id):
    text = get_doc_text(document_id)

    return [
        line.strip()
        for line in text.split("\n")
        if line.strip()
    ]


def get_bodies(document_id):
    """Parse a doc of the form:

        Type: outreach
        line one of body
        line two of body

        Type: followup
        ...

    Returns {type_name_lower: body_text}. Type names are lowercased so the
    sheet's TYPE column can match directly."""
    text = get_doc_text(document_id)

    templates = {}

    current_type = None
    current_body = []

    for line in text.split("\n"):
        line = line.strip()

        if not line:
            continue

        if line.lower().startswith("type"):
            if current_type:
                templates[current_type] = "\n".join(current_body).strip()

            # Take whatever is after the first colon, e.g. "Type: Outreach" -> "Outreach"
            if ":" in line:
                type_name = line.split(":", 1)[1].strip()
            else:
                type_name = line[4:].strip()  # drop the "type" prefix

            current_type = type_name.lower()
            current_body = []

        else:
            current_body.append(line)

    if current_type:
        templates[current_type] = "\n".join(current_body).strip()

    return templates