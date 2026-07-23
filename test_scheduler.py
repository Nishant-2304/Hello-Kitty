from datetime import datetime, timezone, timedelta
import unittest
from unittest.mock import AsyncMock, patch, MagicMock
import sys
from pathlib import Path

# Add root folder to sys.path
sys.path.insert(0, str(Path(__file__).parent))

import db
from services.scheduler import get_next_working_day_9am_ist, send_pending_emails

class TestScheduler(unittest.IsolatedAsyncioTestCase):
    def test_next_working_day_calculations(self):
        ist_tz = timezone(timedelta(hours=5, minutes=30))
        
        # Test Monday -> Tuesday
        # 2026-07-20 is a Monday
        dt_monday = datetime(2026, 7, 20, 10, 0, 0, tzinfo=ist_tz)
        scheduled = get_next_working_day_9am_ist(dt_monday)
        self.assertEqual(scheduled.year, 2026)
        self.assertEqual(scheduled.month, 7)
        self.assertEqual(scheduled.day, 21) # Tuesday
        self.assertEqual(scheduled.hour, 9)
        self.assertEqual(scheduled.minute, 0)
        self.assertEqual(scheduled.tzinfo.utcoffset(scheduled), timedelta(hours=5, minutes=30))

        # Test Thursday -> Friday
        # 2026-07-23 is a Thursday
        dt_thursday = datetime(2026, 7, 23, 15, 0, 0, tzinfo=ist_tz)
        scheduled = get_next_working_day_9am_ist(dt_thursday)
        self.assertEqual(scheduled.day, 24) # Friday
        self.assertEqual(scheduled.hour, 9)

        # Test Friday -> Monday
        # 2026-07-24 is a Friday
        dt_friday = datetime(2026, 7, 24, 18, 0, 0, tzinfo=ist_tz)
        scheduled = get_next_working_day_9am_ist(dt_friday)
        self.assertEqual(scheduled.day, 27) # Monday
        self.assertEqual(scheduled.hour, 9)

        # Test Saturday -> Monday
        # 2026-07-25 is a Saturday
        dt_saturday = datetime(2026, 7, 25, 12, 0, 0, tzinfo=ist_tz)
        scheduled = get_next_working_day_9am_ist(dt_saturday)
        self.assertEqual(scheduled.day, 27) # Monday
        self.assertEqual(scheduled.hour, 9)

        # Test Sunday -> Monday
        # 2026-07-26 is a Sunday
        dt_sunday = datetime(2026, 7, 26, 23, 0, 0, tzinfo=ist_tz)
        scheduled = get_next_working_day_9am_ist(dt_sunday)
        self.assertEqual(scheduled.day, 27) # Monday
        self.assertEqual(scheduled.hour, 9)

    @patch("services.gmail.send_draft", new_callable=AsyncMock)
    @patch("services.sheets.mark_sent")
    @patch("services.sheets.mark_failed")
    @patch("auth.refresh_access_token", new_callable=AsyncMock)
    async def test_send_pending_emails(self, mock_refresh, mock_mark_failed, mock_mark_sent, mock_send_draft):
        # 1. Create a dummy user in SQLite
        user = await db.upsert_user(
            google_sub="test-sched-user",
            email="sched@kitty.com",
            name="Sched User",
            username="sched_user",
            phone="123",
            linkedin="in/sched",
            access_token="fake-access",
            refresh_token="fake-refresh",
        )

        # 2. Insert a pending email that is due (scheduled in the past)
        past_str = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
        async with db.get_db() as database:
            # First clear table to avoid pollution
            await database.execute("DELETE FROM scheduled_emails")
            await database.execute(
                """
                INSERT INTO scheduled_emails (user_id, sheet_row, to_email, subject, body, draft_id, scheduled_at, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                ("test-sched-user", 42, "target@test.com", "Test Subject", "Test Body", "draft-xyz-123", past_str, past_str),
            )
            await database.commit()

        # 3. Run the scheduler check
        await send_pending_emails()

        # 4. Verify mock calls
        mock_send_draft.assert_called_once()
        sent_user_arg = mock_send_draft.call_args[0][0]
        self.assertEqual(sent_user_arg["id"], "test-sched-user")
        self.assertEqual(mock_send_draft.call_args[1]["draft_id"], "draft-xyz-123")

        mock_mark_sent.assert_called_once_with(42, "sched_user")
        mock_mark_failed.assert_not_called()

        # 5. Check DB status changed to sent
        async with db.get_db() as database:
            async with database.execute("SELECT status FROM scheduled_emails WHERE sheet_row = 42") as cur:
                row = await cur.fetchone()
                self.assertEqual(row[0], "sent")

    @patch("httpx.AsyncClient.post", new_callable=AsyncMock)
    async def test_create_draft_with_brochure(self, mock_post):
        import services.gmail as gmail
        import base64
        from email.parser import BytesParser

        # Setup mock response
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"id": "real-draft-123"}
        mock_post.return_value = mock_resp

        user = {
            "id": "test-user",
            "access_token": "token",
            "access_expires_at": "9999-12-31T23:59:59Z", # mock unexpired
        }

        resp = await gmail.create_draft(
            user,
            to="recipient@test.com",
            subject="Hey there!",
            body="Hello body text.",
            client_id="id",
            client_secret="secret",
        )

        self.assertEqual(resp["id"], "real-draft-123")
        mock_post.assert_called_once()
        
        # Verify the raw request payload
        call_json = mock_post.call_args[1]["json"]
        raw_b64 = call_json["message"]["raw"]
        raw_bytes = base64.urlsafe_b64decode(raw_b64)
        
        # Parse the raw MIME message to verify structure
        parsed_msg = BytesParser().parsebytes(raw_bytes)
        self.assertEqual(parsed_msg["to"], "recipient@test.com")
        self.assertEqual(parsed_msg["subject"], "Hey there!")
        self.assertTrue(parsed_msg.is_multipart())
        
        parts = parsed_msg.get_payload()
        self.assertEqual(len(parts), 2) # Text body and PDF attachment
        
        self.assertEqual(parts[0].get_content_type(), "text/plain")
        self.assertEqual(parts[0].get_payload(decode=True).decode().strip(), "Hello body text.")
        
        self.assertEqual(parts[1].get_content_type(), "application/pdf")
        self.assertEqual(parts[1].get_filename(), "CodeChef-VIT graVITas'26 Brochure.pdf")

if __name__ == "__main__":
    unittest.main()
