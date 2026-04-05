"""
Daily Briefing

Commands (works in any topic):
  /daily  — Today's agenda (same as /cal)
"""

from datetime import datetime, time
from zoneinfo import ZoneInfo

from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

from cupbots.config import get_config
from cupbots.helpers.calendar_client import get_calendar_client
from cupbots.helpers.logger import get_logger

log = get_logger("daily")


def build_daily_briefing() -> str:
    """Build today's agenda (equivalent to /cal)."""
    cfg = get_config()
    tz = ZoneInfo(cfg["schedule"]["timezone"])
    now = datetime.now(tz)

    try:
        cal = get_calendar_client()
        events = cal.get_events_range(days=1)
    except Exception as e:
        return f"☀️ Daily Briefing — {now.strftime('%A, %d %B %Y')}\n\n📅 Calendar unavailable: {e}"

    # Filter out past events
    events = [ev for ev in events if ev["end"] > now]

    lines = [f"☀️ Daily Briefing — {now.strftime('%A, %d %B %Y')}", ""]

    if not events:
        lines.append("📅 No events today — deep work day!")
        return "\n".join(lines)

    for ev in sorted(events, key=lambda e: e["start"]):
        if ev.get("all_day"):
            time_str = "🏖️ All day"
        else:
            time_str = f"{ev['start'].strftime('%H:%M')}–{ev['end'].strftime('%H:%M')}"
        summary = ev["summary"] or "(no title)"
        location = f" 📍 {ev['location']}" if ev.get("location") else ""
        lines.append(f"  {time_str}  {summary}{location}")

    return "\n".join(lines)


async def _send_daily(context: ContextTypes.DEFAULT_TYPE):
    """Job callback — send daily briefing."""
    cfg = get_config()
    chat_id = cfg["telegram"]["chat_id"]
    text = build_daily_briefing()
    log.info("Sending daily briefing")
    await context.bot.send_message(
        chat_id=chat_id,
        text=text,
        disable_web_page_preview=True,
    )


async def cmd_daily(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message:
        return
    text = build_daily_briefing()
    await update.message.reply_text(text, disable_web_page_preview=True)


async def handle_command(msg, reply) -> bool:
    """Platform-agnostic command handler."""
    if msg.command == "daily":
        text = build_daily_briefing()
        await reply.reply_text(text)
        return True
    return False


def register(app: Application):
    app.add_handler(CommandHandler("daily", cmd_daily))

    cfg = get_config()
    tz = ZoneInfo(cfg["schedule"]["timezone"])
    app.job_queue.run_daily(
        _send_daily,
        time=time(cfg["schedule"]["morning_hour"], cfg["schedule"]["morning_minute"], tzinfo=tz),
        name="daily_briefing",
    )
