"""
Reminders

Commands (works in any topic):
  /remind <time> <message>  — Set a reminder
  /reminders                — List pending reminders

Time formats: 30m, 2h, 1d, 1d2h30m, tomorrow, friday 3pm, march 25 2pm, 3pm
"""

import re
from datetime import datetime, timedelta, time as dtime

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes

from cupbots.helpers.jobs import enqueue, register_handler, cancel_job, get_pending_jobs
from cupbots.helpers.logger import get_logger

log = get_logger("remind")

# Time parsing patterns
TIME_PATTERNS = [
    (r"(\d+)s", lambda m: timedelta(seconds=int(m.group(1)))),
    (r"(\d+)m(?:in)?", lambda m: timedelta(minutes=int(m.group(1)))),
    (r"(\d+)h", lambda m: timedelta(hours=int(m.group(1)))),
    (r"(\d+)d", lambda m: timedelta(days=int(m.group(1)))),
]

DAY_NAMES = {
    "monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3,
    "friday": 4, "saturday": 5, "sunday": 6,
    "mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6,
}

MONTH_NAMES = {
    "january": 1, "february": 2, "march": 3, "april": 4, "may": 5, "june": 6,
    "july": 7, "august": 8, "september": 9, "october": 10, "november": 11, "december": 12,
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "jun": 6, "jul": 7,
    "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}


def _parse_time_of_day(s: str) -> dtime | None:
    """Parse '3pm', '15:00', '3:30pm' into a time object."""
    s = s.strip().lower()
    m = re.match(r"(\d{1,2})(?::(\d{2}))?\s*(am|pm)?$", s)
    if not m:
        return None
    hour = int(m.group(1))
    minute = int(m.group(2) or 0)
    ampm = m.group(3)
    if ampm == "pm" and hour < 12:
        hour += 12
    elif ampm == "am" and hour == 12:
        hour = 0
    if 0 <= hour <= 23 and 0 <= minute <= 59:
        return dtime(hour, minute)
    return None


def _parse_natural_time(text: str) -> tuple[timedelta | None, str]:
    """Parse natural language time expressions. Returns (delta, remaining_text)."""
    text = text.strip()
    now = datetime.now()

    # "tonight 8pm <msg>" or just "tonight <msg>"
    m = re.match(r"tonight(?:\s+(\d{1,2}(?::\d{2})?\s*(?:am|pm)?))?(?:\s+(.*))?$", text, re.IGNORECASE)
    if m:
        tod = _parse_time_of_day(m.group(1) or "20:00")
        if tod:
            target = now.replace(hour=tod.hour, minute=tod.minute, second=0, microsecond=0)
            if target <= now:
                target += timedelta(days=1)
            return target - now, (m.group(2) or "").strip()

    # "next? <day_name> [time] <msg>"
    m = re.match(
        r"(?:next\s+)?(" + "|".join(DAY_NAMES.keys()) + r")(?:\s+(\d{1,2}(?::\d{2})?\s*(?:am|pm)?))?(?:\s+(.*))?$",
        text, re.IGNORECASE,
    )
    if m:
        target_day = DAY_NAMES[m.group(1).lower()]
        days_ahead = (target_day - now.weekday()) % 7
        if days_ahead == 0:
            if text.lower().startswith("next"):
                days_ahead = 7
            else:
                tod = _parse_time_of_day(m.group(2) or "09:00")
                if tod:
                    target = (now + timedelta(days=0)).replace(hour=tod.hour, minute=tod.minute, second=0, microsecond=0)
                    if target <= now:
                        days_ahead = 7
        tod = _parse_time_of_day(m.group(2) or "09:00")
        if tod:
            target = (now + timedelta(days=days_ahead)).replace(
                hour=tod.hour, minute=tod.minute, second=0, microsecond=0
            )
            return target - now, (m.group(3) or "").strip()

    # "<month> <day> [time] <msg>"
    m = re.match(
        r"(" + "|".join(MONTH_NAMES.keys()) + r")\s+(\d{1,2})(?:\s+(\d{1,2}(?::\d{2})?\s*(?:am|pm)?))?(?:\s+(.*))?$",
        text, re.IGNORECASE,
    )
    if m:
        month = MONTH_NAMES[m.group(1).lower()]
        day = int(m.group(2))
        tod = _parse_time_of_day(m.group(3) or "09:00")
        if tod:
            try:
                target = now.replace(month=month, day=day, hour=tod.hour, minute=tod.minute, second=0, microsecond=0)
                if target <= now:
                    target = target.replace(year=target.year + 1)
                return target - now, (m.group(4) or "").strip()
            except ValueError:
                pass

    # Just a time: "3pm <msg>" or "15:00 <msg>"
    m = re.match(r"(\d{1,2}(?::\d{2})?\s*(?:am|pm))\s+(.+)$", text, re.IGNORECASE)
    if m:
        tod = _parse_time_of_day(m.group(1))
        if tod:
            target = now.replace(hour=tod.hour, minute=tod.minute, second=0, microsecond=0)
            if target <= now:
                target += timedelta(days=1)
            return target - now, m.group(2).strip()

    return None, text


def parse_time(text: str) -> tuple[timedelta | None, str]:
    """Parse time spec from the beginning of text. Returns (delta, remaining_text).

    Supports compound times like '1d2h30m' and words like 'tomorrow'.
    """
    text = text.strip()

    # Handle 'tomorrow'
    if text.lower().startswith("tomorrow"):
        rest = text[8:].strip()
        return timedelta(days=1), rest

    # Try compound time: extract all consecutive time tokens
    total = timedelta()
    remaining = text
    found_any = False

    while remaining:
        matched = False
        for pattern, builder in TIME_PATTERNS:
            m = re.match(pattern, remaining)
            if m:
                total += builder(m)
                remaining = remaining[m.end():].strip()
                matched = True
                found_any = True
                break
        if not matched:
            break

    if found_any:
        return total, remaining

    # Fall back to natural language parsing
    return _parse_natural_time(text)


def _format_delta(delta: timedelta) -> str:
    """Format a timedelta into a human-readable string."""
    secs = delta.total_seconds()
    if secs < 60:
        return f"{int(secs)}s"
    elif secs < 3600:
        return f"{int(secs // 60)}m"
    elif secs < 86400:
        h = int(secs // 3600)
        m = int((secs % 3600) // 60)
        return f"{h}h" + (f"{m}m" if m else "")
    else:
        d = int(secs // 86400)
        h = int((secs % 86400) // 3600)
        return f"{d}d" + (f"{h}h" if h else "")


async def cmd_remind(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not context.args:
        await update.message.reply_text(
            "Usage: /remind <time> <message>\n\n"
            "Times: 30s, 5m, 2h, 1d, 1d2h30m, tomorrow\n"
            "Natural: friday 3pm, march 25 2pm, tonight 8pm, 3pm",
        )
        return

    raw = " ".join(context.args)
    delta, message = parse_time(raw)

    if delta is None or not message:
        await update.message.reply_text(
            "Couldn't parse that. Try: `/remind 2h do the thing`",
            parse_mode="Markdown",
        )
        return

    fire_at = datetime.now() + delta

    job_id = enqueue(
        queue="remind",
        payload={
            "chat_id": update.message.chat_id,
            "thread_id": update.message.message_thread_id,
            "user_id": update.message.from_user.id,
            "message": message,
            "platform": "telegram",
        },
        run_at=fire_at,
        max_attempts=3,
    )

    time_str = _format_delta(delta)
    display = message if len(message) <= 60 else message[:57] + "..."
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("Cancel", callback_data=f"remind_cancel_job:{job_id}"),
    ]])
    await update.message.reply_text(f"\u23f0 Reminder set for {time_str}: {display}", reply_markup=kb)


async def cmd_reminders(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message:
        return

    import json
    user_id = update.message.from_user.id

    pending = get_pending_jobs(queue="remind")
    rows = []
    for job in pending:
        payload = json.loads(job["payload"]) if isinstance(job["payload"], str) else job["payload"]
        if payload.get("user_id") == user_id:
            rows.append((job["id"], payload.get("message", ""), job["run_at"]))

    if not rows:
        await update.message.reply_text("No pending reminders.")
        return

    lines = [f"\u23f0 *Your reminders* ({len(rows)}):\n"]
    for rid, msg, fire_at in rows:
        dt = datetime.fromisoformat(fire_at)
        remaining = dt - datetime.now()
        if remaining.total_seconds() <= 0:
            time_str = "any moment"
        elif remaining.total_seconds() < 3600:
            time_str = f"in {int(remaining.total_seconds() // 60)}m"
        elif remaining.total_seconds() < 86400:
            time_str = f"in {int(remaining.total_seconds() // 3600)}h"
        else:
            time_str = f"in {int(remaining.total_seconds() // 86400)}d"

        display = msg if len(msg) <= 40 else msg[:37] + "..."
        lines.append(f"\u2022 {display} ({time_str})")

    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


async def _handle_remind_job(payload: dict, bot=None):
    """Job queue handler — fires a reminder via Telegram or WhatsApp."""
    if not bot:
        log.warning("No bot instance for remind job")
        return

    platform = payload.get("platform", "telegram")
    message = payload.get("message", "")

    if platform == "telegram":
        await bot.send_message(
            chat_id=payload["chat_id"],
            message_thread_id=payload.get("thread_id"),
            text=f"\u23f0 Reminder: {message}",
        )
    elif platform == "whatsapp":
        import httpx
        wa_url = payload.get("wa_api_url", "http://127.0.0.1:3100")
        async with httpx.AsyncClient(timeout=15) as client:
            await client.post(
                f"{wa_url}/send",
                json={"chatId": payload["chat_id"], "text": f"Reminder: {message}"},
            )


async def _callback_cancel_reminder(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle reminder cancel button."""
    query = update.callback_query
    await query.answer()
    data = query.data

    if data.startswith("remind_cancel_job:"):
        job_id = data.split(":")[1]
        if cancel_job(job_id):
            await query.edit_message_text("Reminder cancelled.")
        else:
            await query.edit_message_text("Reminder already fired or not found.")


async def handle_command(msg, reply):
    """Platform-agnostic command handler for reminders."""
    import json as _json

    if msg.command == "remind":
        if not msg.args:
            await reply.reply_text(
                "Usage: /remind <time> <message>\n\n"
                "Times: 30s, 5m, 2h, 1d, 1d2h30m, tomorrow\n"
                "Natural: friday 3pm, march 25 2pm, tonight 8pm, 3pm"
            )
            return True

        raw = " ".join(msg.args)
        delta, message = parse_time(raw)

        if delta is None or not message:
            await reply.reply_text("Couldn't parse that. Try: /remind 2h do the thing")
            return True

        fire_at = datetime.now() + delta

        job_id = enqueue(
            queue="remind",
            payload={
                "chat_id": msg.chat_id,
                "message": message,
                "platform": msg.platform,
            },
            run_at=fire_at,
            max_attempts=3,
        )

        time_str = _format_delta(delta)
        display = message if len(message) <= 60 else message[:57] + "..."
        await reply.reply_text(f"Reminder set for {time_str}: {display}")
        return True

    elif msg.command == "reminders":
        pending = get_pending_jobs(queue="remind")
        rows = []
        for job in pending:
            payload = _json.loads(job["payload"]) if isinstance(job["payload"], str) else job["payload"]
            if payload.get("chat_id") == msg.chat_id:
                rows.append((job["id"], payload.get("message", ""), job["run_at"]))

        if not rows:
            await reply.reply_text("No pending reminders.")
            return True

        lines = [f"Your reminders ({len(rows)}):\n"]
        for rid, rmsg, fire_at in rows:
            dt = datetime.fromisoformat(fire_at)
            remaining = dt - datetime.now()
            if remaining.total_seconds() <= 0:
                time_str = "any moment"
            elif remaining.total_seconds() < 3600:
                time_str = f"in {int(remaining.total_seconds() // 60)}m"
            elif remaining.total_seconds() < 86400:
                time_str = f"in {int(remaining.total_seconds() // 3600)}h"
            else:
                time_str = f"in {int(remaining.total_seconds() // 86400)}d"

            display = rmsg if len(rmsg) <= 40 else rmsg[:37] + "..."
            lines.append(f"- {display} ({time_str})")

        await reply.reply_text("\n".join(lines))
        return True

    return False


def register(app: Application):
    # Register job queue handler for reminders
    register_handler("remind", _handle_remind_job)

    app.add_handler(CommandHandler("remind", cmd_remind))
    app.add_handler(CommandHandler("reminders", cmd_reminders))
    app.add_handler(CallbackQueryHandler(_callback_cancel_reminder, pattern=r"^remind_cancel"))
