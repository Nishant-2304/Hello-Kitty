import asyncio
import logging
from datetime import datetime, timezone, timedelta
import db
import services.gmail as gmail
import services.sheets as sheets

logger = logging.getLogger(__name__)

def get_next_working_day_9am_ist(now_dt: datetime) -> datetime:
    """Calculate the next working day (Monday-Friday) 9:00 AM IST.

    If today is Friday, Saturday, or Sunday, the next working day is Monday.
    Otherwise (Monday-Thursday), the next working day is tomorrow.
    IST is UTC+5:30.
    """
    ist_tz = timezone(timedelta(hours=5, minutes=30))
    now_ist = now_dt.astimezone(ist_tz)
    
    current_weekday = now_ist.weekday()
    if current_weekday == 4:    # Friday -> Monday
        days_to_add = 3
    elif current_weekday == 5:  # Saturday -> Monday
        days_to_add = 2
    elif current_weekday == 6:  # Sunday -> Monday
        days_to_add = 1
    else:                       # Mon, Tue, Wed, Thu -> tomorrow
        days_to_add = 1
        
    next_day = now_ist + timedelta(days=days_to_add)
    return next_day.replace(hour=9, minute=0, second=0, microsecond=0)


async def start_email_scheduler():
    logger.info("Email scheduler started")
    while True:
        try:
            await send_pending_emails()
        except Exception as e:
            logger.error("Error in scheduler loop: %s", e)
        # Check every 60 seconds
        await asyncio.sleep(60)


async def send_pending_emails():
    # 1. Fetch all pending emails that are due (scheduled_at <= now in UTC)
    now_str = datetime.now(timezone.utc).isoformat()
    
    async with db.get_db() as database:
        async with database.execute(
            "SELECT * FROM scheduled_emails WHERE status = 'pending' AND scheduled_at <= ?",
            (now_str,),
        ) as cur:
            rows = await cur.fetchall()
            if not rows:
                return
            
            cols = [d[0] for d in cur.description]
            emails = [dict(zip(cols, r)) for r in rows]

    for email in emails:
        user = await db.get_user_by_id(email["user_id"])
        if not user:
            logger.error("User %s not found for email %s", email["user_id"], email["id"])
            continue

        try:
            # Refresh token offline
            import auth
            try:
                if user.get("refresh_token"):
                    await auth.refresh_access_token(user)
                    # Reload user from DB to get the new access token
                    user = await db.get_user_by_id(user["id"])
            except Exception as token_err:
                logger.error("Failed to refresh OAuth token for user %s: %s", user["id"], token_err)
                if not auth.is_dev_mode():
                    raise token_err

            # Send draft
            await gmail.send_draft(
                user,
                draft_id=email["draft_id"],
                client_id=auth.CLIENT_ID,
                client_secret=auth.CLIENT_SECRET,
            )

            # Mark sent in DB
            async with db.get_db() as database:
                await database.execute(
                    "UPDATE scheduled_emails SET status = 'sent', error = NULL WHERE id = ?",
                    (email["id"],),
                )
                await database.commit()

            # Mark sent in Google Sheet
            sheets.mark_sent(email["sheet_row"], user["username"])
            logger.info("Successfully sent scheduled email to %s for row %s", email["to_email"], email["sheet_row"])

        except Exception as e:
            error_str = str(e)
            logger.error("Failed to send scheduled email %s: %s", email["id"], error_str)
            # Mark failed in DB
            async with db.get_db() as database:
                await database.execute(
                    "UPDATE scheduled_emails SET status = 'failed', error = ? WHERE id = ?",
                    (error_str, email["id"]),
                )
                await database.commit()

            # Mark failed in Google Sheet
            sheets.mark_failed(email["sheet_row"], user["username"], error_str)
