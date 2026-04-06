"""
Calendar

Commands:
  /cal                             — Today's events
  /cal <N>                         — Next N days (e.g. /cal 5)
  /cal week                        — Next 7 days
  /cal next                        — Next upcoming event
  /cal <date>                      — Events on a date (e.g. thursday, 25 jun)
  /cal free [date]                 — Free slots (today or specific date)
  /cal briefing                    — Today's calendar briefing
  /cal tomorrow                    — Tonight + tomorrow preview
  /cal weekahead                   — Week ahead preview
  /cal holidays [3m|6m|1y]         — Upcoming public holidays (default: 3m)
  /cal holidays sync [country]     — Sync holidays to calendar (default: malaysia)
  /event <natural language>        — Add event (supports recurring, locations, URLs)
  /event delete <search>           — Delete event by title (add "all" for recurring)

ICS forwarding:
  Forward an email with .ics attachment → bot parses and adds to calendar
"""

import re
from datetime import datetime, timedelta, date
from zoneinfo import ZoneInfo

from telegram import Update, Document
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from cupbots.helpers.calendar_client import get_calendar_client, CalendarClient
from cupbots.helpers.logger import get_logger
from icalendar import Calendar as iCalendar

log = get_logger("calendar")
TZ = ZoneInfo("Asia/Kuala_Lumpur")


def _get_client() -> CalendarClient:
    return get_calendar_client()


def _format_event(ev: dict) -> str:
    """Format a single event for display."""
    start = ev["start"]
    end = ev["end"]

    if ev.get("all_day"):
        time_str = "🏖️ All day"
    else:
        time_str = f"{start.strftime('%H:%M')}–{end.strftime('%H:%M')}"

    summary = ev["summary"] or "(no title)"
    location = f" 📍 {ev['location']}" if ev.get("location") else ""
    url = f"\n  🔗 {ev['url']}" if ev.get("url") else ""
    return f"• {time_str}  {summary}{location}{url}"


def _format_date_header(d: date) -> str:
    """Format date as 'Mon 24 Mar'."""
    return d.strftime("%a %d %b")


_DAY_NAMES = {
    "monday": 0, "mon": 0,
    "tuesday": 1, "tue": 1, "tues": 1,
    "wednesday": 2, "wed": 2,
    "thursday": 3, "thu": 3, "thurs": 3,
    "friday": 4, "fri": 4,
    "saturday": 5, "sat": 5,
    "sunday": 6, "sun": 6,
}


def _parse_date(raw: str, now: datetime) -> date | None:
    """Parse a flexible date string. Returns a date or None."""
    raw = raw.strip().lower()

    if raw in ("today", ""):
        return now.date()
    if raw == "tomorrow":
        return now.date() + timedelta(days=1)
    if raw == "yesterday":
        return now.date() - timedelta(days=1)

    # Day names: "thursday", "this thursday", "next friday"
    prefix = ""
    parts = raw.split()
    if parts[0] in ("this", "next"):
        prefix = parts[0]
        raw_day = " ".join(parts[1:])
    else:
        raw_day = raw

    if raw_day in _DAY_NAMES:
        target_weekday = _DAY_NAMES[raw_day]
        today_weekday = now.weekday()
        delta = (target_weekday - today_weekday) % 7
        if delta == 0 and prefix != "this":
            delta = 7  # "thursday" when today is thursday = next week
        if prefix == "next" and delta <= 0:
            delta += 7
        return now.date() + timedelta(days=delta)

    # Try common date formats
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y", "%d/%m", "%d-%m", "%d %b", "%d %B", "%b %d", "%B %d"):
        try:
            parsed = datetime.strptime(raw, fmt)
            if fmt in ("%d/%m", "%d-%m", "%d %b", "%d %B", "%b %d", "%B %d"):
                parsed = parsed.replace(year=now.year)
                # If the date has passed this year, assume next year
                if parsed.date() < now.date():
                    parsed = parsed.replace(year=now.year + 1)
            return parsed.date()
        except ValueError:
            continue

    return None


async def cmd_today(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message:
        return

    now = datetime.now(TZ)

    try:
        cal = _get_client()
        events = cal.get_events_today()
    except Exception as e:
        await update.message.reply_text(f"❌ CalDAV error: {e}")
        return

    events = [ev for ev in events if ev["end"] > now]

    if not events:
        await update.message.reply_text("📅 No more events today.")
        return

    lines = [f"📅 *Today — {_format_date_header(now.date())}*\n"]
    for ev in sorted(events, key=lambda e: e["start"]):
        lines.append(_format_event(ev))

    await update.message.reply_text(
        "\n".join(lines), parse_mode="Markdown", disable_web_page_preview=True
    )


async def cmd_day(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show events for a specific date."""
    if not update.message:
        return

    now = datetime.now(TZ)

    if not context.args:
        await update.message.reply_text(
            "Usage: `/day <date>`\n\n"
            "Examples:\n"
            "  `/day tomorrow`\n"
            "  `/day thursday`\n"
            "  `/day next friday`\n"
            "  `/day 25 jun`\n"
            "  `/day 25/06`",
            parse_mode="Markdown",
        )
        return

    raw = " ".join(context.args).lower().strip()
    target = _parse_date(raw, now)

    if not target:
        await update.message.reply_text(f"Couldn't parse \"{raw}\". Try: thursday, 25 jun, 25/06")
        return

    if target == now.date():
        label = "Today"
    elif target == now.date() + timedelta(days=1):
        label = "Tomorrow"
    else:
        label = _format_date_header(target)

    try:
        cal = _get_client()
        # Query a wider range to catch recurring events that CalDAV may
        # place at off-by-one boundaries, then filter by actual start date
        start = datetime(target.year, target.month, target.day, tzinfo=TZ) - timedelta(days=1)
        end = start + timedelta(days=3)
        all_events = cal.get_events(start, end)
        events = [ev for ev in all_events if ev["start"].date() == target]
    except Exception as e:
        await update.message.reply_text(f"❌ CalDAV error: {e}")
        return

    if target == now.date():
        events = [ev for ev in events if ev["end"] > now]

    if not events:
        await update.message.reply_text(f"📅 No events on {label}.")
        return

    lines = [f"📅 *{label} — {_format_date_header(target)}*\n"]
    for ev in sorted(events, key=lambda e: e["start"]):
        lines.append(_format_event(ev))

    await update.message.reply_text(
        "\n".join(lines), parse_mode="Markdown", disable_web_page_preview=True
    )


async def cmd_week(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message:
        return

    try:
        cal = _get_client()
        events = cal.get_events_range(days=7)
    except Exception as e:
        await update.message.reply_text(f"❌ CalDAV error: {e}")
        return

    if not events:
        await update.message.reply_text("📅 No events in the next 7 days.")
        return

    # Group by date
    by_date: dict[date, list] = {}
    for ev in events:
        d = ev["start"].date()
        by_date.setdefault(d, []).append(ev)

    lines = ["📅 *Next 7 days*\n"]
    for d in sorted(by_date):
        lines.append(f"*{_format_date_header(d)}*")
        for ev in sorted(by_date[d], key=lambda e: e["start"]):
            lines.append(f"  {_format_event(ev)}")
        lines.append("")

    await update.message.reply_text(
        "\n".join(lines), parse_mode="Markdown", disable_web_page_preview=True
    )


async def cmd_next(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show the next upcoming event."""
    if not update.message:
        return

    now = datetime.now(TZ)

    try:
        cal = _get_client()
        # Look ahead 30 days to find the next event
        events = cal.get_events(now, now + timedelta(days=30))
    except Exception as e:
        await update.message.reply_text(f"❌ CalDAV error: {e}")
        return

    # Filter to only future events
    upcoming = [ev for ev in events if ev["end"] > now]
    if not upcoming:
        await update.message.reply_text("📅 No upcoming events in the next 30 days.")
        return

    ev = upcoming[0]
    start = ev["start"]
    delta = start - now

    # Human-readable "in X"
    if delta.total_seconds() < 0:
        in_str = "now"
    elif delta.days > 0:
        in_str = f"in {delta.days}d"
    elif delta.seconds >= 3600:
        in_str = f"in {delta.seconds // 3600}h {(delta.seconds % 3600) // 60}m"
    else:
        in_str = f"in {delta.seconds // 60}m"

    summary = ev["summary"] or "(no title)"
    location = f"\n📍 {ev['location']}" if ev.get("location") else ""
    description = f"\n📝 {ev['description']}" if ev.get("description") else ""

    await update.message.reply_text(
        f"⏭ *Next event — {in_str}*\n\n"
        f"*{summary}*\n"
        f"📆 {start.strftime('%a %d %b %H:%M')}–{ev['end'].strftime('%H:%M')}"
        f"{location}{description}",
        parse_mode="Markdown",
        disable_web_page_preview=True,
    )


async def cmd_agenda(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show agenda for the next N days (default 3)."""
    if not update.message:
        return

    days = 3
    if context.args:
        try:
            days = int(context.args[0])
            days = max(1, min(days, 30))
        except ValueError:
            pass

    now = datetime.now(TZ)

    try:
        cal = _get_client()
        events = cal.get_events_range(days=days)
    except Exception as e:
        await update.message.reply_text(f"❌ CalDAV error: {e}")
        return

    # Filter out events that have already ended
    events = [ev for ev in events if ev["end"] > now]

    if not events:
        await update.message.reply_text(f"📅 No events in the next {days} day(s).")
        return

    # Group by date
    by_date: dict[date, list] = {}
    for ev in events:
        d = ev["start"].date()
        by_date.setdefault(d, []).append(ev)

    lines = [f"📋 *Agenda — next {days} day(s)*\n"]
    for d in sorted(by_date):
        day_label = _format_date_header(d)
        if d == now.date():
            day_label += " (today)"
        elif d == now.date() + timedelta(days=1):
            day_label += " (tomorrow)"
        lines.append(f"*{day_label}*")
        for ev in sorted(by_date[d], key=lambda e: e["start"]):
            lines.append(f"  {_format_event(ev)}")
        lines.append("")

    await update.message.reply_text(
        "\n".join(lines), parse_mode="Markdown", disable_web_page_preview=True
    )


async def cmd_free(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message:
        return

    # Parse optional date argument
    target = datetime.now(TZ).date()
    if context.args:
        raw = " ".join(context.args).lower()
        if raw == "tomorrow":
            target = target + timedelta(days=1)
        else:
            # Try parsing as date
            for fmt in ("%Y-%m-%d", "%d/%m", "%d-%m"):
                try:
                    parsed = datetime.strptime(raw, fmt)
                    if fmt in ("%d/%m", "%d-%m"):
                        parsed = parsed.replace(year=datetime.now().year)
                    target = parsed.date()
                    break
                except ValueError:
                    continue

    try:
        cal = _get_client()
        slots = cal.get_free_slots(target, duration_minutes=30)
    except Exception as e:
        await update.message.reply_text(f"❌ CalDAV error: {e}")
        return

    if not slots:
        await update.message.reply_text(
            f"📅 No free slots on {_format_date_header(target)} (9am–6pm)."
        )
        return

    lines = [f"🟢 *Free slots — {_format_date_header(target)}*\n"]
    for start, end in slots:
        duration = end - start
        hours = int(duration.total_seconds() // 3600)
        mins = int((duration.total_seconds() % 3600) // 60)
        dur_str = f"{hours}h" + (f"{mins}m" if mins else "") if hours else f"{mins}m"
        lines.append(f"• {start.strftime('%H:%M')}–{end.strftime('%H:%M')} ({dur_str})")

    await update.message.reply_text(
        "\n".join(lines), parse_mode="Markdown", disable_web_page_preview=True
    )


def _parse_event_args(args: list[str]) -> tuple[str, datetime, datetime] | None:
    """Parse /event arguments. Returns (title, start, end) or None.

    Formats:
      /event Meeting 2026-03-24 14:00
      /event Meeting 2026-03-24 14:00 1h30m
      /event Meeting tomorrow 14:00
      /event Meeting tomorrow 14:00 2h
    """
    if len(args) < 3:
        return None

    # Find the date/time parts from the end
    # Try to find a time pattern (HH:MM)
    time_idx = None
    for i, arg in enumerate(args):
        if re.match(r"^\d{1,2}:\d{2}$", arg):
            time_idx = i
            break

    if time_idx is None or time_idx < 2:
        return None

    # Title is everything before the date
    date_idx = time_idx - 1

    title = " ".join(args[:date_idx])
    if not title:
        return None

    # Parse date
    date_str = args[date_idx].lower()
    now = datetime.now(TZ)

    if date_str == "today":
        event_date = now.date()
    elif date_str == "tomorrow":
        event_date = now.date() + timedelta(days=1)
    else:
        for fmt in ("%Y-%m-%d", "%d/%m", "%d-%m"):
            try:
                parsed = datetime.strptime(date_str, fmt)
                if fmt in ("%d/%m", "%d-%m"):
                    parsed = parsed.replace(year=now.year)
                event_date = parsed.date()
                break
            except ValueError:
                continue
        else:
            return None

    # Parse time
    time_parts = args[time_idx].split(":")
    hour, minute = int(time_parts[0]), int(time_parts[1])

    dtstart = datetime(
        event_date.year, event_date.month, event_date.day,
        hour, minute, tzinfo=TZ,
    )

    # Parse optional duration (default 1h)
    duration = timedelta(hours=1)
    if time_idx + 1 < len(args):
        dur_str = args[time_idx + 1]
        total = timedelta()
        for match in re.finditer(r"(\d+)(h|m)", dur_str):
            val, unit = int(match.group(1)), match.group(2)
            if unit == "h":
                total += timedelta(hours=val)
            elif unit == "m":
                total += timedelta(minutes=val)
        if total > timedelta():
            duration = total

    dtend = dtstart + duration

    return title, dtstart, dtend


from cupbots.helpers.llm import _extract_json, run_claude_cli


async def _parse_event_with_llm(raw_input: str) -> dict | None:
    """Use Claude CLI (haiku) to parse natural language event input.

    Returns dict: title, start, end, location, rrule.
    """
    now = datetime.now(TZ)

    prompt = (
        f"Parse this event: {raw_input}\n\n"
        f"Current date/time: {now.strftime('%Y-%m-%d %H:%M')} ({now.strftime('%A')}). "
        f"Timezone: Asia/Kuala_Lumpur (UTC+8).\n\n"
        f"Reply with ONLY a JSON object:\n"
        f'{{"title": "...", "date": "YYYY-MM-DD", "time": "HH:MM", '
        f'"duration_minutes": N, "location": "...", "rrule": "..."}}\n\n'
        f"Rules:\n"
        f"- Infer the date from relative words (today, tomorrow, thursday, next week, etc.)\n"
        f"- For recurring events, set date to the first occurrence and rrule to an iCalendar RRULE value:\n"
        f"  e.g. every wednesday → date=next wednesday, rrule=FREQ=WEEKLY;BYDAY=WE\n"
        f"  e.g. every day → rrule=FREQ=DAILY\n"
        f"  e.g. every 2 weeks on monday → rrule=FREQ=WEEKLY;INTERVAL=2;BYDAY=MO\n"
        f"- Parse times like 10am, 2pm, 14:00, noon, etc.\n"
        f"- TIMEZONE CONVERSION (CRITICAL): The output date and time MUST be in Asia/Kuala_Lumpur (UTC+8).\n"
        f"  If the user specifies a different timezone or city, you MUST convert to Malaysia time.\n"
        f"  Common offsets: Melbourne/Sydney AEDT=UTC+11 (3h ahead of MYT), Tokyo JST=UTC+9 (1h ahead),\n"
        f"  London GMT=UTC+0 (8h behind), New York EST=UTC-5 (13h behind), LA PST=UTC-8 (16h behind).\n"
        f"  Example: 1pm Melbourne time on 31 Mar → 10am Malaysia time on 31 Mar (subtract 3 hours).\n"
        f"  Example: 9am London time → 5pm Malaysia time (add 8 hours). The DATE may also change.\n"
        f"- Default duration is 60 minutes unless specified\n"
        f"- Extract URLs or addresses as location\n"
        f"- The title is the event description without date/time/location parts\n"
        f"- Set location and rrule to null if not applicable\n"
        f"- If you cannot parse at all, reply with just: null"
    )

    result = await run_claude_cli(prompt, model="haiku", max_turns=1, timeout=30)
    data = _extract_json(result["text"])
    if not data or not isinstance(data, dict):
        return None

    try:
        event_date = datetime.strptime(data["date"], "%Y-%m-%d").date()
        time_parts = data["time"].split(":")
        hour, minute = int(time_parts[0]), int(time_parts[1])
        duration = timedelta(minutes=int(data.get("duration_minutes", 60)))

        dtstart = datetime(
            event_date.year, event_date.month, event_date.day,
            hour, minute, tzinfo=TZ,
        )
        dtend = dtstart + duration

        return {
            "title": data["title"],
            "start": dtstart,
            "end": dtend,
            "location": data.get("location") or "",
            "rrule": data.get("rrule") or "",
        }
    except (KeyError, ValueError):
        return None


# Map RRULE freq to human-readable label
_RRULE_LABELS = {
    "DAILY": "daily",
    "WEEKLY": "weekly",
    "MONTHLY": "monthly",
    "YEARLY": "yearly",
}


def _rrule_to_label(rrule: str) -> str:
    """Convert RRULE string to a short human label."""
    parts = dict(p.split("=", 1) for p in rrule.split(";") if "=" in p)
    freq = _RRULE_LABELS.get(parts.get("FREQ", ""), "recurring")
    interval = parts.get("INTERVAL", "")
    if interval and interval != "1":
        freq = f"every {interval} {freq.rstrip('ly')}s"
    return freq


async def cmd_event(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not context.args:
        await update.message.reply_text(
            "Usage:\n"
            "`/event Dentist tomorrow 14:00 30m`\n"
            "`/event Checkup at Sunway 10am this thursday`\n"
            "`/event Meeting with Larry every wed 10pm https://zoom.us/j/123`\n"
            "`/event delete <search>` — Delete event\n"
            "`/event delete all <search>` — Delete all recurring",
            parse_mode="Markdown",
        )
        return

    # /event delete → delegate to cmd_delete
    if context.args[0].lower() == "delete":
        context.args = context.args[1:]
        return await cmd_delete(update, context)

    raw = " ".join(context.args)
    location = ""
    rrule = ""

    # Try strict parser first, fall back to LLM
    parsed = _parse_event_args(context.args)
    if parsed:
        title, dtstart, dtend = parsed
    else:
        try:
            llm_result = await _parse_event_with_llm(raw)
        except Exception as e:
            await update.message.reply_text(f"❌ Parse error: {e}")
            return

        if not llm_result:
            await update.message.reply_text(
                "❌ Couldn't understand that. Try something like:\n"
                "`/event Team lunch tomorrow noon 1h30m`\n"
                "`/event Standup every monday 9am`",
                parse_mode="Markdown",
            )
            return

        title = llm_result["title"]
        dtstart = llm_result["start"]
        dtend = llm_result["end"]
        location = llm_result.get("location", "")
        rrule = llm_result.get("rrule", "")

    try:
        cal = _get_client()

        # Check conflicts
        conflicts = cal.check_conflicts(dtstart, dtend)
        conflict_warning = ""
        if conflicts:
            names = ", ".join(c["summary"] for c in conflicts)
            conflict_warning = f"\n⚠️ Conflicts with: {names}"

        uid = cal.add_event(title, dtstart, dtend, location=location, rrule=rrule)
    except Exception as e:
        log.error("CalDAV event creation failed: %s", e)
        await update.message.reply_text(f"❌ CalDAV error: {e}")
        return

    duration = dtend - dtstart
    hours = int(duration.total_seconds() // 3600)
    mins = int((duration.total_seconds() % 3600) // 60)
    dur_str = f"{hours}h" + (f"{mins}m" if mins else "") if hours else f"{mins}m"

    extras = ""
    if rrule:
        extras += f"\n🔁 Repeats {_rrule_to_label(rrule)}"
    if location:
        extras += f"\n📍 {location}"

    await update.message.reply_text(
        f"📅 Event created: *{title}*\n"
        f"📆 {dtstart.strftime('%a %d %b %H:%M')}–{dtend.strftime('%H:%M')} ({dur_str})"
        f"{extras}{conflict_warning}",
        parse_mode="Markdown",
    )


async def handle_ics_file(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle forwarded .ics file attachments."""
    if not update.message or not update.message.document:
        return

    doc: Document = update.message.document
    if not doc.file_name or not doc.file_name.lower().endswith(".ics"):
        return

    try:
        file = await doc.get_file()
        data = await file.download_as_bytearray()
        ics_text = data.decode("utf-8")
    except Exception as e:
        await update.message.reply_text(f"❌ Failed to download .ics: {e}")
        return

    try:
        cal = _get_client()
        imported = cal.add_event_from_ics(ics_text)
    except Exception as e:
        await update.message.reply_text(f"❌ Failed to import: {e}")
        return

    if not imported:
        await update.message.reply_text("No events found in the .ics file.")
        return

    lines = [f"📅 *Imported {len(imported)} event(s):*\n"]
    for ev in imported:
        lines.append(_format_event(ev))

        # Check conflicts
        conflicts = cal.check_conflicts(ev["start"], ev["end"])
        # Exclude the event itself
        conflicts = [c for c in conflicts if c["uid"] != ev["uid"]]
        if conflicts:
            names = ", ".join(c["summary"] for c in conflicts)
            lines.append(f"  ⚠️ Conflicts with: {names}")

    await update.message.reply_text(
        "\n".join(lines), parse_mode="Markdown", disable_web_page_preview=True
    )


async def cmd_delete(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Delete an event by searching its title. Use 'all' prefix to delete all recurring instances."""
    if not update.message or not context.args:
        await update.message.reply_text(
            "Usage:\n"
            "`/delete <search>` — Delete next matching event\n"
            "`/delete all <search>` — Delete all occurrences (recurring)",
            parse_mode="Markdown",
        )
        return

    args = list(context.args)
    delete_all = args[0].lower() == "all"
    if delete_all:
        args = args[1:]
    if not args:
        await update.message.reply_text("Please provide a search term.")
        return

    query = " ".join(args).lower()

    try:
        cal = _get_client()
        now = datetime.now(TZ)
        # Look further ahead for recurring events
        events = cal.get_events(now, now + timedelta(days=365 if delete_all else 30))
    except Exception as e:
        await update.message.reply_text(f"❌ CalDAV error: {e}")
        return

    # Find matching events
    matches = [ev for ev in events if query in (ev["summary"] or "").lower()]
    if not matches:
        await update.message.reply_text(f"❌ No upcoming event matching \"{query}\".")
        return

    # Collect unique UIDs to delete
    if delete_all:
        uids_to_delete = {ev["uid"] for ev in matches}
    else:
        uids_to_delete = {matches[0]["uid"]}

    try:
        deleted_count = cal.delete_events_by_uid(
            uids_to_delete, now, now + timedelta(days=365)
        )
    except Exception as e:
        await update.message.reply_text(f"❌ Failed to delete: {e}")
        return

    if deleted_count == 0:
        await update.message.reply_text("❌ Event found but couldn't delete it from calendar.")
        return

    if delete_all and len(matches) > 1:
        await update.message.reply_text(
            f"🗑 *Deleted {deleted_count} event(s):* {matches[0]['summary']}\n"
            f"(all recurring instances)",
            parse_mode="Markdown",
        )
    else:
        ev = matches[0]
        await update.message.reply_text(
            f"🗑 *Deleted:* {ev['summary']}\n"
            f"📆 {ev['start'].strftime('%a %d %b %H:%M')}–{ev['end'].strftime('%H:%M')}",
            parse_mode="Markdown",
        )


# Google Calendar public holiday ICS URL pattern
_HOLIDAY_ICS_URL = (
    "https://calendar.google.com/calendar/ical/"
    "en.{country}%23holiday%40group.v.calendar.google.com/public/basic.ics"
)

# Common country identifiers for Google Calendar (not ISO codes)
_COUNTRY_IDS = {
    "malaysia": "malaysia",
    "my": "malaysia",
    "singapore": "singapore",
    "sg": "singapore",
    "usa": "usa",
    "us": "usa",
    "uk": "uk",
    "gb": "uk",
    "japan": "japanese",
    "jp": "japanese",
    "india": "indian",
    "in": "indian",
    "australia": "australian",
    "au": "australian",
    "indonesia": "indonesian",
    "id": "indonesian",
    "thailand": "thai",
    "th": "thai",
    "germany": "german",
    "de": "german",
    "france": "french",
    "fr": "french",
    "china": "china",
    "cn": "china",
    "korea": "south_korea",
    "kr": "south_korea",
    "philippines": "philippines",
    "ph": "philippines",
    "vietnam": "vietnamese",
    "vn": "vietnamese",
    "taiwan": "taiwan",
    "tw": "taiwan",
}

# Malaysian state holidays — these are regional, not national
_MY_STATE_HOLIDAYS = {
    "johor", "kedah", "kelantan", "melaka", "negeri sembilan",
    "pahang", "penang", "perak", "perlis", "sabah", "sarawak",
    "selangor", "terengganu", "kuala lumpur", "putrajaya", "labuan",
}


def _is_national_holiday(ev_summary: str, ev_description: str, country: str) -> bool:
    """Check if a holiday is national (not state/regional).

    For Malaysia, Google Calendar descriptions list applicable states.
    If it applies to all states or has no state qualifier, it's national.
    """
    if country != "malaysia":
        return True  # For other countries, import all

    desc = ev_description.lower()
    if not desc:
        return True  # No description = assume national

    # Google format: "Public holiday" or "Observance" with state names
    # National holidays typically say "Public holiday" without state restrictions
    # State holidays list specific states like "Johor, Kedah"
    # If description contains "observance" it's not a public holiday
    if "observance" in desc:
        return False

    return True


_PERIOD_MAP = {
    "3m": 90, "6m": 180, "1y": 365, "1m": 30, "2m": 60,
    "3": 90, "6": 180, "12": 365,
}


async def cmd_holidays(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show upcoming public holidays or sync them to calendar."""
    if not update.message:
        return

    args = [a.lower() for a in (context.args or [])]

    # /holidays sync [country] — delegate to sync
    if args and args[0] == "sync":
        context.args = args[1:] or ["malaysia"]
        return await cmd_holidays_sync(update, context)

    # Parse period: /holidays [3m|6m|1y]
    period_arg = args[0] if args else "3m"
    days = _PERIOD_MAP.get(period_arg, None)
    if days is None:
        # Maybe they passed a country name, show help
        await update.message.reply_text(
            "📅 *Holidays usage:*\n\n"
            "`/holidays` — Next 3 months\n"
            "`/holidays 6m` — Next 6 months\n"
            "`/holidays 1y` — Next year\n"
            "`/holidays sync` — Sync Malaysia holidays to calendar\n"
            "`/holidays sync singapore` — Sync other country",
            parse_mode="Markdown",
        )
        return

    now = datetime.now(TZ)
    end = now + timedelta(days=days)

    try:
        cal = _get_client()
        events = cal.get_events(now, end)
    except Exception as e:
        await update.message.reply_text(f"❌ CalDAV error: {e}")
        return

    # Filter to holiday-like events (all-day events from the holidays import)
    # Match by checking if it's an all-day event — holidays are always all-day
    holidays = [
        ev for ev in events
        if ev.get("all_day")
    ]

    if not holidays:
        await update.message.reply_text(
            f"📅 No holidays in the next {period_arg}.\n"
            "Run `/holidays sync` to import public holidays first.",
            parse_mode="Markdown",
        )
        return

    holidays.sort(key=lambda e: e["start"])

    lines = [f"📅 *Upcoming holidays — next {period_arg}*\n"]
    current_month = None
    for ev in holidays:
        d = ev["start"].date()
        month_label = d.strftime("%B %Y")
        if month_label != current_month:
            current_month = month_label
            lines.append(f"\n*{month_label}*")

        day_name = d.strftime("%a")
        days_away = (d - now.date()).days
        if days_away == 0:
            rel = "today"
        elif days_away == 1:
            rel = "tomorrow"
        elif days_away < 7:
            rel = f"in {days_away}d"
        elif days_away < 30:
            rel = f"in {days_away // 7}w"
        else:
            rel = f"in {days_away}d"

        lines.append(f"• {d.strftime('%d %b')} ({day_name})  {ev['summary']}  _({rel})_")

    lines.append(f"\n{len(holidays)} holiday(s)")

    await update.message.reply_text(
        "\n".join(lines), parse_mode="Markdown", disable_web_page_preview=True
    )


async def cmd_holidays_sync(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Sync public holidays from Google Calendar into CalDAV."""
    if not update.message:
        return

    # Parse country argument
    raw = " ".join(context.args).lower().strip() if context.args else "malaysia"
    country_id = _COUNTRY_IDS.get(raw, raw)
    country_key = raw  # For display and national filter logic

    url = _HOLIDAY_ICS_URL.format(country=country_id)

    await update.message.reply_text(f"⏳ Fetching {raw} public holidays...")

    try:
        import httpx
        log.info("/holidays: fetching ICS for %s from Google Calendar", raw)
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            ics_data = resp.text
        log.info("/holidays: fetched ICS (%d bytes)", len(ics_data))
    except Exception as e:
        log.error("/holidays: fetch failed: %s", e)
        await update.message.reply_text(
            f"❌ Failed to fetch holidays for \"{raw}\".\n"
            f"Error: {e}\n\n"
            f"Supported: {', '.join(sorted(set(_COUNTRY_IDS.keys())))}"
        )
        return

    try:
        vcal = iCalendar.from_ical(ics_data)
    except Exception as e:
        await update.message.reply_text(f"❌ Failed to parse ICS: {e}")
        return

    # Scope: this year + next year only
    cal = _get_client()
    now = datetime.now(TZ)
    this_year = now.year
    next_year = this_year + 1
    scope_start = datetime(this_year, 1, 1, tzinfo=TZ)
    scope_end = datetime(next_year + 1, 1, 1, tzinfo=TZ)

    # Parse incoming holidays (filtered)
    log.info("/holidays: parsing ICS and filtering")
    incoming = {}  # name_lower -> (summary, start_date, component)
    skipped_regional = 0
    skipped_out_of_scope = 0

    for component in vcal.walk("VEVENT"):
        summary = str(component.get("summary", "")).strip()
        description = str(component.get("description", ""))
        dtstart = component.get("dtstart")

        if not summary or not dtstart:
            continue

        start = dtstart.dt
        start_date = start.date() if isinstance(start, datetime) else start

        # Skip out of scope (only this year + next year, and not past)
        if start_date < now.date() or start_date.year > next_year:
            skipped_out_of_scope += 1
            continue

        # Skip state/regional holidays for Malaysia
        if not _is_national_holiday(summary, description, country_key):
            skipped_regional += 1
            continue

        incoming[summary.lower()] = (summary, start_date, component)

    # Get existing events for dedup and date-change detection
    log.info("/holidays: checking existing calendar for sync")
    existing = cal.get_events(scope_start, scope_end)
    # Map: name_lower -> list of (date, uid)
    existing_by_name: dict[str, list[tuple[date, str]]] = {}
    for e in existing:
        name_key = e["summary"].strip().lower()
        existing_by_name.setdefault(name_key, []).append((e["start"].date(), e["uid"]))

    imported = []
    updated = []
    skipped_dup = 0

    for name_lower, (summary, start_date, component) in incoming.items():
        existing_entries = existing_by_name.get(name_lower, [])

        # Check if already exists on the same date
        if any(d == start_date for d, _ in existing_entries):
            skipped_dup += 1
            continue

        # Date changed — delete old entries for this holiday, then add new
        if existing_entries:
            old_uids = {uid for _, uid in existing_entries}
            try:
                cal.delete_events_by_uid(old_uids, scope_start, scope_end)
            except Exception as e:
                log.error("Failed to delete old holiday %s: %s", summary, e)

            old_dates = ", ".join(d.strftime('%d %b') for d, _ in existing_entries)
            updated.append((summary, start_date, old_dates))
        else:
            imported.append((summary, start_date))

        # Add the new/updated holiday
        single = iCalendar()
        single.add("prodid", "-//CupBots//Holidays//EN")
        single.add("version", "2.0")
        single.add_component(component)

        try:
            cal.save_raw_ics(single.to_ical().decode())
        except Exception as e:
            log.error("Failed to import holiday %s: %s", summary, e)

    log.info("/holidays: done — %d new, %d updated, %d unchanged, %d regional, %d out of scope",
             len(imported), len(updated), skipped_dup, skipped_regional, skipped_out_of_scope)

    # Build response — only show this year + next year
    lines = [f"🎉 *Holiday sync — {raw.title()}*\n"]

    all_changes = [(n, d, None) for n, d in imported] + [(n, d, old) for n, d, old in updated]
    all_changes.sort(key=lambda x: x[1])

    if all_changes:
        current_year = None
        for name, d, old_dates in all_changes:
            if d.year != current_year:
                current_year = d.year
                lines.append(f"\n*{current_year}*")
            if old_dates:
                lines.append(f"• {d.strftime('%d %b')}  {name}  _(was {old_dates})_")
            else:
                lines.append(f"• {d.strftime('%d %b')}  {name}")

    stats = []
    if imported:
        stats.append(f"{len(imported)} added")
    if updated:
        stats.append(f"{len(updated)} date changed")
    if skipped_dup:
        stats.append(f"{skipped_dup} unchanged")
    if skipped_regional:
        stats.append(f"{skipped_regional} regional/observance")
    if skipped_out_of_scope:
        stats.append(f"{skipped_out_of_scope} out of scope")
    if stats:
        lines.append(f"\n{', '.join(stats)}")

    if not all_changes:
        lines.append("✅ All holidays up to date!")

    # Split into messages if too long (Telegram limit: 4096 chars)
    chunks = []
    current_chunk = []
    current_len = 0
    for line in lines:
        if current_len + len(line) + 1 > 4000:
            chunks.append("\n".join(current_chunk))
            current_chunk = []
            current_len = 0
        current_chunk.append(line)
        current_len += len(line) + 1
    if current_chunk:
        chunks.append("\n".join(current_chunk))

    for chunk in chunks:
        await update.message.reply_text(
            chunk, parse_mode="Markdown", disable_web_page_preview=True
        )


_TG_CAL_HELP = (
    "📅 *Calendar commands:*\n\n"
    "`/cal` — Today's events\n"
    "`/cal 5` — Next 5 days\n"
    "`/cal week` — Next 7 days\n"
    "`/cal next` — Next upcoming event\n"
    "`/cal thursday` — Events on a date\n"
    "`/cal free` — Free slots today\n"
    "`/cal free tomorrow` — Free slots on date\n"
    "`/cal briefing` — Today's calendar briefing\n"
    "`/cal tomorrow` — Tonight + tomorrow preview\n"
    "`/cal weekahead` — Week ahead preview\n"
    "`/cal holidays` — Upcoming holidays (3m)\n"
    "`/cal holidays sync` — Sync holidays to calendar\n\n"
    "`/event Dentist tomorrow 2pm 30m` — Add event\n"
    "`/event Yoga every monday 7am` — Recurring\n"
    "`/event Call with Bob wed 3pm https://zoom.us/j/123` — With location\n"
    "`/event delete <search>` — Delete event by title\n"
    "`/event delete all <search>` — Delete all recurring\n\n"
    "📎 Forward a `.ics` file to import events."
)


async def cmd_cal(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Unified /cal command for Telegram."""
    if not update.message:
        return

    now = datetime.now(TZ)
    args = context.args or []

    # /cal (no args) → today
    if not args:
        return await cmd_today(update, context)

    sub = args[0].lower()

    # /cal help
    if sub in ("help", "--help", "-h"):
        await update.message.reply_text(_TG_CAL_HELP, parse_mode="Markdown")
        return

    # /cal week
    if sub == "week":
        return await cmd_week(update, context)

    # /cal next
    if sub == "next":
        return await cmd_next(update, context)

    # /cal free [date]
    if sub == "free":
        context.args = args[1:]
        return await cmd_free(update, context)

    # /cal holidays [args]
    if sub == "holidays":
        context.args = args[1:]
        return await cmd_holidays(update, context)

    # /cal briefing
    if sub == "briefing":
        text = _build_briefing()
        await update.message.reply_text(text, disable_web_page_preview=True)
        return

    # /cal tomorrow
    if sub == "tomorrow":
        text = _build_tonight_tomorrow()
        await update.message.reply_text(text, disable_web_page_preview=True)
        return

    # /cal weekahead
    if sub == "weekahead":
        text = _build_week_ahead()
        await update.message.reply_text(text, disable_web_page_preview=True)
        return

    # /cal <number> → agenda
    try:
        days_count = int(sub)
        context.args = [str(max(1, min(days_count, 30)))]
        return await cmd_agenda(update, context)
    except ValueError:
        pass

    # /cal <date> → specific day
    context.args = args
    return await cmd_day(update, context)


def _build_briefing() -> str:
    """Build today's briefing text."""
    try:
        from cupbots.config import get_config
        cfg = get_config()
        tz = ZoneInfo(cfg["schedule"]["timezone"])
    except Exception:
        tz = TZ

    now = datetime.now(tz)
    try:
        cal = _get_client()
        events = cal.get_events_today()
    except Exception as e:
        return f"Calendar unavailable: {e}"

    lines = [f"Good morning! -- {now.strftime('%A, %d %B %Y')}\n"]

    if not events:
        lines.append("No events today. Deep work day!")
        return "\n".join(lines)

    lines.append(f"{len(events)} event(s) today:\n")

    for ev in sorted(events, key=lambda e: e["start"]):
        summary = ev["summary"] or "(no title)"
        if ev.get("all_day"):
            time_str = "All day"
        else:
            time_str = f"{ev['start'].strftime('%H:%M')}-{ev['end'].strftime('%H:%M')}"
        location = f"\n  @ {ev['location']}" if ev.get("location") else ""
        lines.append(f"  {time_str}  {summary}{location}")

    try:
        free_slots = cal.get_free_slots(now.date(), duration_minutes=60)
        if free_slots:
            s = free_slots[0]
            lines.append(f"\nNext free hour: {s[0].strftime('%H:%M')}-{s[1].strftime('%H:%M')}")
    except Exception:
        pass

    return "\n".join(lines)


def _build_tonight_tomorrow() -> str:
    """Build evening preview: remaining events tonight + all of tomorrow."""
    try:
        from cupbots.config import get_config
        cfg = get_config()
        tz = ZoneInfo(cfg["schedule"]["timezone"])
    except Exception:
        tz = TZ

    now = datetime.now(tz)
    today = now.date()
    tomorrow = today + timedelta(days=1)

    try:
        cal = _get_client()
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
        return f"Calendar unavailable: {e}"

    lines = [f"Evening preview -- {now.strftime('%A, %d %B %Y')}\n"]

    if tonight_events:
        lines.append(f"Tonight ({len(tonight_events)}):\n")
        for ev in sorted(tonight_events, key=lambda e: e["start"]):
            summary = ev["summary"] or "(no title)"
            time_str = f"{ev['start'].strftime('%H:%M')}-{ev['end'].strftime('%H:%M')}"
            location = f"\n  @ {ev['location']}" if ev.get("location") else ""
            lines.append(f"  {time_str}  {summary}{location}")
        lines.append("")

    if tomorrow_events:
        lines.append(f"Tomorrow -- {tomorrow.strftime('%A, %d %B')} ({len(tomorrow_events)}):\n")
        for ev in sorted(tomorrow_events, key=lambda e: e["start"]):
            summary = ev["summary"] or "(no title)"
            if ev.get("all_day"):
                time_str = "All day"
            else:
                time_str = f"{ev['start'].strftime('%H:%M')}-{ev['end'].strftime('%H:%M')}"
            location = f"\n  @ {ev['location']}" if ev.get("location") else ""
            lines.append(f"  {time_str}  {summary}{location}")

    if not tonight_events and not tomorrow_events:
        lines.append("Nothing tonight or tomorrow. Rest up!")

    return "\n".join(lines)


def _build_week_ahead() -> str:
    """Build week-ahead preview: all events for the next 7 days."""
    try:
        from cupbots.config import get_config
        cfg = get_config()
        tz = ZoneInfo(cfg["schedule"]["timezone"])
    except Exception:
        tz = TZ

    now = datetime.now(tz)
    tomorrow = now.date() + timedelta(days=1)

    try:
        cal = _get_client()
        start = datetime(tomorrow.year, tomorrow.month, tomorrow.day, 0, 0, tzinfo=tz)
        end = start + timedelta(days=7)
        events = cal.get_events(start, end)
        events = [ev for ev in events if tomorrow <= ev["start"].date() < (tomorrow + timedelta(days=7))]
    except Exception as e:
        return f"Calendar unavailable: {e}"

    end_date = tomorrow + timedelta(days=6)
    lines = [f"Week ahead -- {tomorrow.strftime('%d %b')} to {end_date.strftime('%d %b %Y')}\n"]

    if not events:
        lines.append("No events next week. Plan something!")
        return "\n".join(lines)

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

        lines.append(f"\n{d.strftime('%a %d %b')}")
        for ev in day_events:
            summary = ev["summary"] or "(no title)"
            if ev.get("all_day"):
                time_str = "All day"
                holiday_count += 1
            else:
                time_str = f"{ev['start'].strftime('%H:%M')}-{ev['end'].strftime('%H:%M')}"
                event_count += 1
            location = f"\n    @ {ev['location']}" if ev.get("location") else ""
            lines.append(f"  {time_str}  {summary}{location}")

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


def _format_event_plain(ev: dict) -> str:
    """Format event without emoji for cross-platform."""
    start = ev["start"]
    end = ev["end"]
    if ev.get("all_day"):
        time_str = "All day"
    else:
        time_str = f"{start.strftime('%H:%M')}-{end.strftime('%H:%M')}"
    summary = ev["summary"] or "(no title)"
    location = f" @ {ev['location']}" if ev.get("location") else ""
    return f"  {time_str}  {summary}{location}"


async def _cal_today(now, reply):
    try:
        cal = _get_client()
        events = [ev for ev in cal.get_events_today() if ev["end"] > now]
    except Exception as e:
        await reply.reply_error(f"CalDAV error: {e}")
        return
    if not events:
        await reply.reply_text("No more events today.")
        return
    lines = [f"Today -- {_format_date_header(now.date())}\n"]
    for ev in sorted(events, key=lambda e: e["start"]):
        lines.append(_format_event_plain(ev))
    await reply.reply_text("\n".join(lines))


async def _cal_agenda(now, days_count, reply):
    try:
        cal = _get_client()
        events = cal.get_events_range(days=days_count)
    except Exception as e:
        await reply.reply_error(f"CalDAV error: {e}")
        return
    if not events:
        await reply.reply_text(f"No events in the next {days_count} days.")
        return
    days = {}
    for ev in sorted(events, key=lambda e: e["start"]):
        d = ev["start"].date()
        days.setdefault(d, []).append(ev)
    lines = [f"Agenda ({days_count} days):\n"]
    for d in sorted(days):
        lines.append(f"{_format_date_header(d)}:")
        for ev in days[d]:
            lines.append(_format_event_plain(ev))
    await reply.reply_text("\n".join(lines))


async def _cal_day(now, target, reply):
    if target == now.date():
        label = "Today"
    elif target == now.date() + timedelta(days=1):
        label = "Tomorrow"
    else:
        label = _format_date_header(target)
    try:
        cal = _get_client()
        start = datetime(target.year, target.month, target.day, tzinfo=TZ) - timedelta(days=1)
        end = start + timedelta(days=3)
        all_events = cal.get_events(start, end)
        events = [ev for ev in all_events if ev["start"].date() == target]
    except Exception as e:
        await reply.reply_error(f"CalDAV error: {e}")
        return
    if target == now.date():
        events = [ev for ev in events if ev["end"] > now]
    if not events:
        await reply.reply_text(f"No events on {label}.")
        return
    lines = [f"{label} -- {_format_date_header(target)}\n"]
    for ev in sorted(events, key=lambda e: e["start"]):
        lines.append(_format_event_plain(ev))
    await reply.reply_text("\n".join(lines))


async def _cal_next(now, reply):
    try:
        cal = _get_client()
        events = cal.get_events_range(days=14)
    except Exception as e:
        await reply.reply_error(f"CalDAV error: {e}")
        return
    future = [ev for ev in events if ev["start"] > now]
    if not future:
        await reply.reply_text("No upcoming events in the next 2 weeks.")
        return
    ev = sorted(future, key=lambda e: e["start"])[0]
    delta = ev["start"] - now
    if delta.days > 0:
        time_until = f"in {delta.days}d"
    else:
        hours = delta.seconds // 3600
        mins = (delta.seconds % 3600) // 60
        time_until = f"in {hours}h{mins}m"
    lines = [f"Next event ({time_until}):", _format_event_plain(ev)]
    await reply.reply_text("\n".join(lines))


async def _cal_free(now, args, reply):
    target = now.date()
    if args:
        parsed = _parse_date(" ".join(args), now)
        if parsed:
            target = parsed
    try:
        cal = _get_client()
        slots = cal.get_free_slots(target)
    except Exception as e:
        await reply.reply_error(f"CalDAV error: {e}")
        return
    if not slots:
        await reply.reply_text(f"No free slots on {_format_date_header(target)}.")
        return
    lines = [f"Free slots -- {_format_date_header(target)}:\n"]
    for slot_start, slot_end in slots:
        lines.append(f"  {slot_start.strftime('%H:%M')}-{slot_end.strftime('%H:%M')}")
    await reply.reply_text("\n".join(lines))


_CAL_HELP = (
    "Calendar commands:\n\n"
    "/cal -- Today's events\n"
    "/cal <N> -- Next N days\n"
    "/cal week -- Next 7 days\n"
    "/cal next -- Next upcoming event\n"
    "/cal <date> -- Events on a date (e.g. thursday, 25 jun)\n"
    "/cal free [date] -- Free slots\n"
    "/cal briefing -- Today's calendar briefing\n"
    "/cal tomorrow -- Tonight + tomorrow preview\n"
    "/cal weekahead -- Week ahead preview\n"
    "/cal holidays [3m|6m|1y] -- Upcoming holidays\n"
    "/cal holidays sync [country] -- Sync holidays\n"
    "/event <description> -- Add event\n"
    "/event delete <search> -- Delete event"
)


async def handle_command(msg, reply) -> bool:
    """Platform-agnostic calendar commands."""
    if msg.command not in ("cal", "event", "today", "day", "week", "next",
                           "agenda", "free", "delete", "holidays",
                           "briefing", "tomorrow", "weekahead", "daily"):
        return False

    if msg.args and msg.args[0] in ("--help", "-h") and msg.command != "event":
        await reply.reply_text(__doc__.strip())
        return True

    now = datetime.now(TZ)

    # --- /event (write commands) ---
    if msg.command == "event":
        if msg.args and msg.args[0] == "delete":
            msg.args = msg.args[1:]
            return await _handle_delete(msg, reply)
        return False

    # --- /delete (legacy alias) ---
    if msg.command == "delete":
        return await _handle_delete(msg, reply)

    # --- Legacy aliases: route old commands into /cal logic ---
    if msg.command == "today":
        await _cal_today(now, reply)
        return True
    if msg.command == "day":
        if msg.args:
            target = _parse_date(" ".join(msg.args), now)
            if target:
                await _cal_day(now, target, reply)
                return True
        await reply.reply_text("Usage: /cal <date>  (e.g. thursday, 25 jun)")
        return True
    if msg.command == "week":
        await _cal_agenda(now, 7, reply)
        return True
    if msg.command == "next":
        await _cal_next(now, reply)
        return True
    if msg.command == "agenda":
        days_count = 3
        if msg.args:
            try:
                days_count = int(msg.args[0])
            except ValueError:
                pass
        await _cal_agenda(now, days_count, reply)
        return True
    if msg.command == "free":
        await _cal_free(now, msg.args, reply)
        return True
    if msg.command == "holidays":
        return False

    # --- Legacy briefing aliases ---
    if msg.command in ("briefing", "daily"):
        await reply.reply_text(_build_briefing())
        return True
    if msg.command == "tomorrow":
        await reply.reply_text(_build_tonight_tomorrow())
        return True
    if msg.command == "weekahead":
        await reply.reply_text(_build_week_ahead())
        return True

    # --- /cal (unified command) ---
    args = msg.args or []

    if not args:
        await _cal_today(now, reply)
        return True

    sub = args[0].lower()

    if sub in ("--help", "-h", "help"):
        await reply.reply_text(_CAL_HELP)
        return True

    if sub == "week":
        await _cal_agenda(now, 7, reply)
        return True

    if sub == "next":
        await _cal_next(now, reply)
        return True

    if sub == "free":
        await _cal_free(now, args[1:], reply)
        return True

    if sub == "holidays":
        return False

    if sub == "briefing":
        await reply.reply_text(_build_briefing())
        return True

    if sub == "tomorrow":
        await reply.reply_text(_build_tonight_tomorrow())
        return True

    if sub == "weekahead":
        await reply.reply_text(_build_week_ahead())
        return True

    try:
        days_count = int(sub)
        await _cal_agenda(now, max(1, min(days_count, 30)), reply)
        return True
    except ValueError:
        pass

    target = _parse_date(" ".join(args), now)
    if target:
        await _cal_day(now, target, reply)
        return True

    await reply.reply_text(f"Unknown: {' '.join(args)}\n\n{_CAL_HELP}")
    return True


async def _handle_delete(msg, reply) -> bool:
    """Handle event deletion (from /delete or /event delete)."""
    if not msg.args:
        await reply.reply_text(
            "Usage:\n"
            "/event delete <search> -- Delete next matching event\n"
            "/event delete all <search> -- Delete all recurring instances"
        )
        return True

    args = list(msg.args)
    delete_all = args[0].lower() == "all"
    if delete_all:
        args = args[1:]
    if not args:
        await reply.reply_text("Please provide a search term.")
        return True

    query = " ".join(args).lower()
    now = datetime.now(TZ)

    try:
        cal = _get_client()
        events = cal.get_events(now, now + timedelta(days=365 if delete_all else 30))
    except Exception as e:
        await reply.reply_error(f"CalDAV error: {e}")
        return True

    matches = [ev for ev in events if query in (ev["summary"] or "").lower()]
    if not matches:
        await reply.reply_text(f"No upcoming event matching \"{query}\".")
        return True

    if delete_all:
        uids_to_delete = {ev["uid"] for ev in matches}
    else:
        uids_to_delete = {matches[0]["uid"]}

    try:
        deleted_count = cal.delete_events_by_uid(
            uids_to_delete, now, now + timedelta(days=365)
        )
    except Exception as e:
        await reply.reply_error(f"Failed to delete: {e}")
        return True

    if deleted_count == 0:
        await reply.reply_text("Event found but couldn't delete it from calendar.")
        return True

    if delete_all and len(matches) > 1:
        await reply.reply_text(f"Deleted {deleted_count} event(s): {matches[0]['summary']} (all recurring)")
    else:
        ev = matches[0]
        await reply.reply_text(
            f"Deleted: {ev['summary']}\n"
            f"{ev['start'].strftime('%a %d %b %H:%M')}-{ev['end'].strftime('%H:%M')}"
        )
    return True


def register(app: Application):
    # Primary commands
    app.add_handler(CommandHandler("cal", cmd_cal))
    app.add_handler(CommandHandler("event", cmd_event))

    # Legacy aliases (kept for transition)
    app.add_handler(CommandHandler("today", cmd_today))
    app.add_handler(CommandHandler("day", cmd_day))
    app.add_handler(CommandHandler("week", cmd_week))
    app.add_handler(CommandHandler("next", cmd_next))
    app.add_handler(CommandHandler("agenda", cmd_agenda))
    app.add_handler(CommandHandler("free", cmd_free))
    app.add_handler(CommandHandler("delete", cmd_delete))
    app.add_handler(CommandHandler("holidays", cmd_holidays))

    # Handle .ics file attachments
    app.add_handler(MessageHandler(filters.Document.ALL, handle_ics_file))

