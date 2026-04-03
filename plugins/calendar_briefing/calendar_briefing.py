"""
Calendar Briefing

Commands (works in any topic):
  /briefing  — Today's calendar briefing
  /tomorrow  — Tonight + tomorrow preview
  /weekahead — Week ahead preview
"""

import asyncio
import sys
from datetime import datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
from config_loader import get_config
from helpers.caldav_client import CalDAVClient
from helpers.logger import get_logger

log = get_logger("briefing")

# How often to check for upcoming events (seconds)
REMINDER_CHECK_INTERVAL = 120
# How many minutes before an event to send reminder
REMINDER_MINUTES = 30
# Track which events we've already reminded about (by UID + date)
_reminded: set[str] = set()
_reminded_lock = asyncio.Lock()


def _build_briefing() -> str | None:
    """Build today's briefing text. Returns None if no events."""
    try:
        cal = CalDAVClient()
        events = cal.get_events_today()
    except Exception as e:
        return f"❌ Calendar unavailable: {e}"

    cfg = get_config()
    tz = ZoneInfo(cfg["schedule"]["timezone"])
    now = datetime.now(tz)
    lines = [f"☀️ Good morning! — {now.strftime('%A, %d %B %Y')}\n"]

    if not events:
        lines.append("No events today. Deep work day! 🎯")
        return "\n".join(lines)

    lines.append(f"📅 {len(events)} event(s) today:\n")

    for ev in sorted(events, key=lambda e: e["start"]):
        summary = ev["summary"] or "(no title)"
        if ev.get("all_day"):
            time_str = "🏖️ All day"
        else:
            time_str = f"{ev['start'].strftime('%H:%M')}–{ev['end'].strftime('%H:%M')}"
        location = f"\n  📍 {ev['location']}" if ev.get("location") else ""
        lines.append(f"• {time_str}  {summary}{location}")

    try:
        free_slots = cal.get_free_slots(now.date(), duration_minutes=60)
        if free_slots:
            s = free_slots[0]
            lines.append(f"\n🟢 Next free hour: {s[0].strftime('%H:%M')}–{s[1].strftime('%H:%M')}")
    except Exception:
        pass

    return "\n".join(lines)


def _build_tonight_tomorrow_preview() -> str | None:
    """Build evening preview: remaining events tonight + all of tomorrow."""
    try:
        cfg = get_config()
        tz = ZoneInfo(cfg["schedule"]["timezone"])
        now = datetime.now(tz)
        today = now.date()
        tomorrow = today + timedelta(days=1)

        cal = CalDAVClient()

        # Fetch 3 days to work around Radicale CalDAV timezone bug
        # (single-day queries miss events when local TZ is ahead of UTC)
        all_events = cal.get_events_range(days=3)

        tonight_events = [
            ev for ev in all_events
            if ev["start"].date() == today and ev["end"] > now and not ev.get("all_day")
        ]

        tomorrow_events = [
            ev for ev in all_events
            if ev["start"].date() == tomorrow
        ]
    except Exception as e:
        return f"❌ Calendar unavailable: {e}"

    lines = [f"🌙 Evening preview — {now.strftime('%A, %d %B %Y')}\n"]

    if tonight_events:
        lines.append(f"🌆 Tonight ({len(tonight_events)}):\n")
        for ev in sorted(tonight_events, key=lambda e: e["start"]):
            summary = ev["summary"] or "(no title)"
            time_str = f"{ev['start'].strftime('%H:%M')}–{ev['end'].strftime('%H:%M')}"
            location = f"\n  📍 {ev['location']}" if ev.get("location") else ""
            lines.append(f"• {time_str}  {summary}{location}")
        lines.append("")

    if tomorrow_events:
        lines.append(f"📅 Tomorrow — {tomorrow.strftime('%A, %d %B')} ({len(tomorrow_events)}):\n")
        for ev in sorted(tomorrow_events, key=lambda e: e["start"]):
            summary = ev["summary"] or "(no title)"
            if ev.get("all_day"):
                time_str = "🏖️ All day"
            else:
                time_str = f"{ev['start'].strftime('%H:%M')}–{ev['end'].strftime('%H:%M')}"
            location = f"\n  📍 {ev['location']}" if ev.get("location") else ""
            lines.append(f"• {time_str}  {summary}{location}")

    if not tonight_events and not tomorrow_events:
        lines.append("Nothing tonight or tomorrow. Rest up! 😴")

    return "\n".join(lines)


async def _send_briefing(context: ContextTypes.DEFAULT_TYPE):
    """Job callback — send morning briefing."""
    cfg = get_config()
    chat_id = cfg["telegram"]["chat_id"]
    text = _build_briefing()
    if text:
        log.info("Sending calendar briefing")
        await context.bot.send_message(
            chat_id=chat_id,
            text=text,
            disable_web_page_preview=True,
        )


async def _send_evening_preview(context: ContextTypes.DEFAULT_TYPE):
    """Job callback — send tonight + tomorrow preview at 6 PM."""
    cfg = get_config()
    chat_id = cfg["telegram"]["chat_id"]
    text = _build_tonight_tomorrow_preview()
    if text:
        log.info("Sending evening preview")
        await context.bot.send_message(
            chat_id=chat_id,
            text=text,
            disable_web_page_preview=True,
        )


async def _check_upcoming_reminders(context: ContextTypes.DEFAULT_TYPE):
    """Job callback — check for events starting in ~1 hour and send reminder."""
    try:
        cfg = get_config()
        tz = ZoneInfo(cfg["schedule"]["timezone"])
        now = datetime.now(tz)
        chat_id = cfg["telegram"]["chat_id"]

        cal = CalDAVClient()
        # Look ahead 1 hour to catch events in the reminder window
        events = cal.get_events(now, now + timedelta(hours=1))
    except Exception as e:
        log.error("Reminder check failed: %s", e)
        return

    for ev in events:
        if ev.get("all_day"):
            continue

        start = ev["start"]
        minutes_until = (start - now).total_seconds() / 60

        # Remind if event is 50–70 minutes away (to account for check interval)
        if not (REMINDER_MINUTES - 10 <= minutes_until <= REMINDER_MINUTES + 10):
            continue

        # Deduplicate by UID + date
        key = f"{ev.get('uid', '')}_{start.date()}"
        async with _reminded_lock:
            if key in _reminded:
                continue
            _reminded.add(key)

        summary = ev["summary"] or "(no title)"
        location = f"\n📍 {ev['location']}" if ev.get("location") else ""
        delta_min = int(minutes_until)

        await context.bot.send_message(
            chat_id=chat_id,
            text=(
                f"⏰ *Reminder — in {delta_min} min*\n\n"
                f"*{summary}*\n"
                f"📆 {start.strftime('%H:%M')}–{ev['end'].strftime('%H:%M')}"
                f"{location}"
            ),
            parse_mode="Markdown",
            disable_web_page_preview=True,
        )
        log.info("Sent reminder for: %s (in %d min)", summary, delta_min)

    # Clean up old reminder keys (keep set from growing forever)
    today = now.date()
    async with _reminded_lock:
        stale = [k for k in _reminded if not k.endswith(str(today))]
        for k in stale:
            _reminded.discard(k)


def _build_week_ahead() -> str | None:
    """Build week-ahead preview: all events for the next 7 days."""
    try:
        cfg = get_config()
        tz = ZoneInfo(cfg["schedule"]["timezone"])
        now = datetime.now(tz)
        tomorrow = now.date() + timedelta(days=1)

        cal = CalDAVClient()
        start = datetime(tomorrow.year, tomorrow.month, tomorrow.day, 0, 0, tzinfo=tz)
        end = start + timedelta(days=7)
        events = cal.get_events(start, end)
        # Filter to only events starting within the week
        events = [ev for ev in events if tomorrow <= ev["start"].date() < (tomorrow + timedelta(days=7))]
    except Exception as e:
        return f"❌ Calendar unavailable: {e}"

    end_date = tomorrow + timedelta(days=6)
    lines = [f"📋 *Week ahead* — {tomorrow.strftime('%d %b')} to {end_date.strftime('%d %b %Y')}\n"]

    if not events:
        lines.append("No events next week. Plan something! 🎯")
        return "\n".join(lines)

    # Group by date
    by_date: dict = {}
    for ev in events:
        d = ev["start"].date()
        by_date.setdefault(d, []).append(ev)

    holiday_count = 0
    event_count = 0
    busy_days = 0

    for d in sorted(by_date):
        day_events = sorted(by_date[d], key=lambda e: e["start"])
        day_has_non_holiday = any(not e.get("all_day") for e in day_events)
        if day_has_non_holiday:
            busy_days += 1

        lines.append(f"\n*{d.strftime('%a %d %b')}*")
        for ev in day_events:
            summary = ev["summary"] or "(no title)"
            if ev.get("all_day"):
                time_str = "🏖️ All day"
                holiday_count += 1
            else:
                time_str = f"{ev['start'].strftime('%H:%M')}–{ev['end'].strftime('%H:%M')}"
                event_count += 1
            location = f"\n    📍 {ev['location']}" if ev.get("location") else ""
            lines.append(f"  • {time_str}  {summary}{location}")

    # Summary line
    parts = []
    if holiday_count:
        parts.append(f"{holiday_count} holiday(s)")
    if event_count:
        parts.append(f"{event_count} event(s)")
    if busy_days:
        parts.append(f"{busy_days} busy day(s)")
    if parts:
        lines.append(f"\n{', '.join(parts)}")

    return "\n".join(lines)


async def _send_week_ahead(context: ContextTypes.DEFAULT_TYPE):
    """Job callback — send week-ahead briefing on Sunday 6 PM."""
    cfg = get_config()
    chat_id = cfg["telegram"]["chat_id"]
    text = _build_week_ahead()
    if text:
        log.info("Sending week-ahead briefing")
        await context.bot.send_message(
            chat_id=chat_id,
            text=text,
            parse_mode="Markdown",
            disable_web_page_preview=True,
        )


async def cmd_weekahead(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message:
        return
    text = _build_week_ahead()
    if text:
        await update.message.reply_text(text, parse_mode="Markdown", disable_web_page_preview=True)
    else:
        await update.message.reply_text("Could not generate week-ahead preview.")


async def cmd_briefing(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message:
        return
    text = _build_briefing()
    if text:
        await update.message.reply_text(text, disable_web_page_preview=True)
    else:
        await update.message.reply_text("Could not generate briefing.")


async def cmd_tomorrow(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message:
        return
    text = _build_tonight_tomorrow_preview()
    if text:
        await update.message.reply_text(text, disable_web_page_preview=True)
    else:
        await update.message.reply_text("Could not generate preview.")


async def handle_command(msg, reply) -> bool:
    """Cross-platform calendar briefing commands."""
    if msg.command == "briefing":
        text = _build_briefing()
        await reply.reply_text(text or "Could not generate briefing.")
        return True
    elif msg.command == "tomorrow":
        text = _build_tonight_tomorrow_preview()
        await reply.reply_text(text or "Could not generate preview.")
        return True
    elif msg.command == "weekahead":
        text = _build_week_ahead()
        await reply.reply_text(text or "Could not generate week-ahead preview.")
        return True
    return False


def register(app: Application):
    app.add_handler(CommandHandler("briefing", cmd_briefing))
    app.add_handler(CommandHandler("tomorrow", cmd_tomorrow))
    app.add_handler(CommandHandler("weekahead", cmd_weekahead))

    cfg = get_config()
    tz = ZoneInfo(cfg["schedule"]["timezone"])

    # Morning briefing handled by daily_briefing plugin (includes calendar + baby + pipelines)

    # Tomorrow preview at 6:00 PM daily
    app.job_queue.run_daily(
        _send_evening_preview,
        time=time(18, 0, tzinfo=tz),
        name="tomorrow_preview",
    )

    # Week-ahead preview at 6:00 PM on Sundays
    app.job_queue.run_daily(
        _send_week_ahead,
        time=time(18, 0, tzinfo=tz),
        days=(6,),  # Sunday = 6
        name="week_ahead",
    )

    # Check for upcoming events every 2 minutes for reminders
    app.job_queue.run_repeating(
        _check_upcoming_reminders,
        interval=REMINDER_CHECK_INTERVAL,
        first=30,
        name="event_reminders",
    )
    log.info("Scheduled: tomorrow preview @18:00, week-ahead Sun @18:00, reminders every %ds",
             REMINDER_CHECK_INTERVAL)
