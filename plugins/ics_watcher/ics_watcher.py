"""
ICS Watcher — polls Gmail for calendar invites.

Automatically polls a Gmail mailbox via IMAP every 60s for .ics attachments,
then imports them into Radicale via CalDAV and posts a summary to Telegram.

Env vars:
  GMAIL_ADDRESS      — Gmail address to poll
  GMAIL_APP_PASSWORD — Gmail app password (from myaccount.google.com/apppasswords)
"""

import email
import imaplib
import os
from email import policy
from pathlib import Path

from telegram.ext import Application, ContextTypes

from cupbots.helpers.caldav_client import CalDAVClient
from cupbots.helpers.logger import get_logger

log = get_logger("ics-watcher")

IMAP_HOST = "imap.gmail.com"
IMAP_PORT = 993
POLL_INTERVAL = int(os.environ.get("ICS_POLL_INTERVAL", "60"))

GMAIL_ADDRESS = os.environ.get("GMAIL_ADDRESS", "")
GMAIL_APP_PASSWORD = os.environ.get("GMAIL_APP_PASSWORD", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")


def _format_event(ev: dict) -> str:
    start = ev["start"]
    end = ev["end"]
    if ev.get("all_day"):
        time_str = start.strftime("%a %d %b")
    else:
        time_str = f"{start.strftime('%a %d %b')} {start.strftime('%H:%M')}–{end.strftime('%H:%M')}"
    summary = ev["summary"] or "(no title)"
    location = f" 📍 {ev['location']}" if ev.get("location") else ""
    url = f"\n  🔗 {ev['url']}" if ev.get("url") else ""
    return f"• {time_str}  {summary}{location}{url}"


async def poll_gmail(context: ContextTypes.DEFAULT_TYPE):
    """Job callback: check Gmail for unread emails with .ics attachments."""
    if not GMAIL_ADDRESS or not GMAIL_APP_PASSWORD:
        return

    try:
        conn = imaplib.IMAP4_SSL(IMAP_HOST, IMAP_PORT)
        conn.login(GMAIL_ADDRESS, GMAIL_APP_PASSWORD)
        conn.select("INBOX")
        _, msg_ids = conn.search(None, "UNSEEN")

        if not msg_ids[0]:
            conn.logout()
            return

        cal = CalDAVClient()

        for msg_id in msg_ids[0].split():
            _, msg_data = conn.fetch(msg_id, "(RFC822)")
            raw = msg_data[0][1]
            msg = email.message_from_bytes(raw, policy=policy.default)
            subject = msg.get("Subject", "(no subject)")
            handled = False

            for part in msg.walk():
                content_type = part.get_content_type()
                filename = part.get_filename() or ""

                if content_type == "text/calendar" or filename.lower().endswith(".ics"):
                    ics_data = part.get_payload(decode=True)
                    if not ics_data:
                        continue

                    ics_text = ics_data.decode("utf-8")
                    imported = cal.add_event_from_ics(ics_text)
                    handled = True

                    if imported:
                        any_updated = any(ev.get("updated") for ev in imported)
                        action = "Updated" if any_updated else "Imported"
                        lines = [f"📅 *{action} from email:* {subject}\n"]
                        for ev in imported:
                            lines.append(_format_event(ev))
                            conflicts = cal.check_conflicts(ev["start"], ev["end"])
                            conflicts = [c for c in conflicts if c["uid"] != ev["uid"]]
                            if conflicts:
                                names = ", ".join(c["summary"] for c in conflicts)
                                lines.append(f"  ⚠️ Conflicts with: {names}")

                        await context.bot.send_message(
                            chat_id=TELEGRAM_CHAT_ID,
                            text="\n".join(lines),
                            parse_mode="Markdown",
                        )
                        log.info("Imported %d event(s) from: %s", len(imported), subject)

            # Only mark as read if we processed an .ics attachment
            if handled:
                conn.store(msg_id, "+FLAGS", "\\Seen")

        conn.logout()

    except imaplib.IMAP4.error as e:
        log.error("IMAP error: %s", e)
    except Exception as e:
        log.error("Poll error: %s", e)


def register(app: Application):
    if not GMAIL_ADDRESS or not GMAIL_APP_PASSWORD:
        log.warning("GMAIL_ADDRESS / GMAIL_APP_PASSWORD not set — ICS watcher disabled")
        return

    app.job_queue.run_repeating(poll_gmail, interval=POLL_INTERVAL, first=10)
    log.info("ICS watcher polling %s every %ds", GMAIL_ADDRESS, POLL_INTERVAL)
