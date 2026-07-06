from groq import Groq
from dotenv import load_dotenv
import os

load_dotenv()

client = Groq(
    api_key=os.getenv("GROQ_API_KEY")
)

def generate_email(company, subject, body):

    prompt = f"""
Company: {company}

Subject idea:
{subject}

Base outline:
{body}
do not alter the subject, and just write a body saying this is dummy body, with some random text, and a few emojis, in like 2-3 sentences.

SUBJECT:
<one-line subject>

BODY:
<full email body>
"""

    response = client.chat.completions.create(
        model="llama-3.3-70b-versatile",
        messages=[
            {
                "role": "user",
                "content": prompt
            }
        ]
    )

    return response.choices[0].message.content