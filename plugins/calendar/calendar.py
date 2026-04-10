"""
Calendar

Commands:
  /cal                             — Today's events
  /cal <N>                         — Next N days (e.g. /cal 5)
  /cal week                        — Next 7 days
  /cal next                        — Next upcoming event
  /cal <date>                      — Events on a date (e.g. thursday, 6 apr)
  /cal free [date]                 — Free slots (today or specific date)
  /cal connect                     — Connect a Google Calendar via OAuth (one click,
                                     works for both Workspace and consumer Gmail)
  /cal availability                — View/edit availability preferences
  /cal availability set <key> <v>  — Update preference (no_weekends, work_start,
                                     work_end, buffer_minutes)
  /cal availability block <date>   — Block a specific date
  /cal availability unblock <date> — Unblock a date
  /cal briefing                    — Today's calendar briefing
  /cal tomorrow                    — Tonight + tomorrow preview
  /cal weekahead                   — Week ahead preview
  /cal holidays [3m|6m|1y]         — Upcoming public holidays (default: 3m)
  /cal holidays sync [country]     — Sync holidays to calendar (default: malaysia)
  /event <natural language>        — Add event (refuses to create overlapping
                                     events; prompts yes/no/reschedule on
                                     conflict or availability violation)
  /event delete <search>           — Delete event by title (add "all" for recurring)

Availability preferences (plugin_settings.calendar.availability in config.yaml)
let the bot know your no-meeting rules — e.g. no weekends, no after 6pm —
so it refuses to add violating events and surfaces them to the AI agent
when suggesting times to other people.

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

from cupbots.config import update_config_key
from cupbots.helpers.calendar_client import get_calendar_client, CalendarClient
from cupbots.helpers.channel import WhatsAppReplyContext
from cupbots.helpers.logger import get_logger
from cupbots.helpers.oauth import register_provider, start_flow
from icalendar import Calendar as iCalendar

log = get_logger("calendar")
TZ = ZoneInfo("Asia/Kuala_Lumpur")

CALENDAR_OAUTH_SCOPES = ["https://www.googleapis.com/auth/calendar"]


def _get_client() -> CalendarClient:
    return get_calendar_client()


# ---------------------------------------------------------------------------
# Google Calendar OAuth (per-user, works for Workspace + consumer Gmail)
# ---------------------------------------------------------------------------

async def _on_calendar_connected(tokens: dict, metadata: dict):
    """OAuth success callback — persists the refresh token to plugin config.

    The hub holds the OAuth client_id / client_secret as env vars; we only
    need to remember the refresh_token, which the hub uses to mint fresh
    access tokens on demand via `oauth.refresh`.
    """
    refresh_token = tokens.get("refresh_token", "")
    if not refresh_token:
        log.error("Calendar OAuth: no refresh_token returned (user must re-consent)")
        return

    chat_id = metadata.get("chat_id", "")

    update_config_key(
        f"plugin_settings.{PLUGIN_NAME}.google_calendar_refresh_token",
        refresh_token,
    )
    update_config_key(
        f"plugin_settings.{PLUGIN_NAME}.CALENDAR_BACKEND",
        "google",
    )

    if chat_id:
        ctx = WhatsAppReplyContext(chat_id)
        await ctx.reply_text(
            "Google Calendar connected.\n\n"
            "Try `/cal` to see today's events. To switch back, "
            "set `CALENDAR_BACKEND` to `caldav` in plugin_settings.calendar."
        )
    log.info("Google Calendar OAuth completed")


register_provider("google_calendar", {
    "auth_url": "https://accounts.google.com/o/oauth2/v2/auth",
    "token_url": "https://oauth2.googleapis.com/token",
    "default_scopes": CALENDAR_OAUTH_SCOPES,
    "on_success": _on_calendar_connected,
})


# ---------------------------------------------------------------------------
# Availability preferences
#
# Stored in plugin_settings.calendar.availability (config.yaml). The AI agent
# and the /event flow consult these before scheduling. Edit via
# `/cal availability set <key> <value>`.
#
# Defaults are intentionally permissive — if the user has not configured
# anything, behaviour is unchanged from prior versions.
# ---------------------------------------------------------------------------

PLUGIN_NAME = "calendar"

_AVAIL_DEFAULTS = {
    "no_weekends": False,
    "work_start": "00:00",
    "work_end": "23:59",
    "buffer_minutes": 0,
    "blocked_days": [],
}


def _load_availability() -> dict:
    """Read availability preferences from config.yaml plugin_settings.calendar."""
    try:
        from cupbots.config import get_config
        cal_settings = (get_config().get("plugin_settings", {}) or {}).get(PLUGIN_NAME, {}) or {}
        avail = cal_settings.get("availability") or {}
    except Exception:
        avail = {}
    out = dict(_AVAIL_DEFAULTS)
    if isinstance(avail, dict):
        out.update({k: v for k, v in avail.items() if k in _AVAIL_DEFAULTS})
    # normalise types
    if isinstance(out.get("blocked_days"), str):
        out["blocked_days"] = [out["blocked_days"]]
    out["blocked_days"] = list(out.get("blocked_days") or [])
    try:
        out["buffer_minutes"] = int(out.get("buffer_minutes") or 0)
    except (TypeError, ValueError):
        out["buffer_minutes"] = 0
    return out


def _parse_iso(s: str) -> date | None:
    """Parse a strict YYYY-MM-DD string into a date, or None."""
    try:
        return datetime.strptime(s.strip(), "%Y-%m-%d").date()
    except (ValueError, AttributeError):
        return None


def _expand_blocked_days(entries) -> set[str]:
    """Expand blocked_days entries into a set of ISO date strings.

    Each entry may be:
      - a single ISO date: "2026-04-10"
      - a string range: "2026-04-10..2026-04-15" or "2026-04-10/2026-04-15"
      - a dict: {"from": "2026-04-10", "to": "2026-04-15"}

    Invalid entries are silently skipped (they remain visible in the
    formatted display so the user can correct them).
    """
    out: set[str] = set()
    if not entries:
        return out
    if isinstance(entries, (str, dict)):
        entries = [entries]

    for entry in entries:
        if isinstance(entry, dict):
            start = _parse_iso(str(entry.get("from", "")))
            end = _parse_iso(str(entry.get("to", "")))
            if not start:
                continue
            end = end or start
        else:
            text = str(entry).strip()
            if not text:
                continue
            sep = ".." if ".." in text else ("/" if "/" in text else None)
            if sep:
                a, _, b = text.partition(sep)
                start = _parse_iso(a)
                end = _parse_iso(b)
                if not start or not end:
                    continue
            else:
                start = _parse_iso(text)
                if not start:
                    continue
                end = start

        if end < start:
            start, end = end, start
        cur = start
        while cur <= end:
            out.add(cur.isoformat())
            cur += timedelta(days=1)
    return out


def _save_availability(avail: dict) -> None:
    from cupbots.config import update_config_key
    update_config_key(f"plugin_settings.{PLUGIN_NAME}.availability", avail)


def _parse_hhmm(raw: str) -> tuple[int, int] | None:
    m = re.match(r"^(\d{1,2}):(\d{2})$", raw.strip())
    if not m:
        return None
    h, mm = int(m.group(1)), int(m.group(2))
    if 0 <= h <= 23 and 0 <= mm <= 59:
        return h, mm
    return None


def _format_availability(avail: dict) -> str:
    lines = ["Availability preferences:"]
    lines.append(f"  no_weekends:    {'yes' if avail['no_weekends'] else 'no'}")
    lines.append(f"  work_start:     {avail['work_start']}")
    lines.append(f"  work_end:       {avail['work_end']}")
    lines.append(f"  buffer_minutes: {avail['buffer_minutes']}")
    raw_blocked = avail.get("blocked_days") or []
    if raw_blocked:
        # Show the raw entries (preserves user-friendly ranges) plus the
        # expanded total count.
        rendered = []
        for entry in raw_blocked:
            if isinstance(entry, dict):
                rendered.append(f"{entry.get('from','?')}..{entry.get('to','?')}")
            else:
                rendered.append(str(entry))
        expanded = _expand_blocked_days(raw_blocked)
        lines.append(f"  blocked_days:   {', '.join(rendered)}  ({len(expanded)} days)")
    else:
        lines.append("  blocked_days:   (none)")
    return "\n".join(lines)


def check_availability(dtstart: datetime, dtend: datetime, avail: dict | None = None) -> list[str]:
    """Return a list of human-readable reasons the slot violates user prefs.

    Empty list = slot is acceptable. Used by /event and exposed to the AI
    agent so it knows the user's preferences before suggesting times.
    """
    avail = avail or _load_availability()
    reasons: list[str] = []

    start_local = dtstart.astimezone(TZ) if dtstart.tzinfo else dtstart.replace(tzinfo=TZ)
    end_local = dtend.astimezone(TZ) if dtend.tzinfo else dtend.replace(tzinfo=TZ)

    if avail.get("no_weekends") and start_local.weekday() >= 5:
        reasons.append(f"falls on a weekend ({start_local.strftime('%A')})")

    ws = _parse_hhmm(str(avail.get("work_start", "00:00"))) or (0, 0)
    we = _parse_hhmm(str(avail.get("work_end", "23:59"))) or (23, 59)
    work_start_min = ws[0] * 60 + ws[1]
    work_end_min = we[0] * 60 + we[1]

    start_min = start_local.hour * 60 + start_local.minute
    end_min = end_local.hour * 60 + end_local.minute
    # Multi-day events: only check the start-day boundary
    if end_local.date() != start_local.date():
        end_min = 24 * 60

    if start_min < work_start_min:
        reasons.append(f"starts before work hours ({avail['work_start']})")
    if end_min > work_end_min:
        reasons.append(f"ends after work hours ({avail['work_end']})")

    blocked = _expand_blocked_days(avail.get("blocked_days"))
    if start_local.date().isoformat() in blocked:
        reasons.append(f"date {start_local.date().isoformat()} is blocked")

    return reasons


def _suggest_alternative_slots(dtstart: datetime, duration: timedelta,
                               avail: dict, max_slots: int = 3) -> list[tuple[datetime, datetime]]:
    """Find a few free slots over the next 7 days that satisfy availability prefs."""
    cal = _get_client()
    suggestions: list[tuple[datetime, datetime]] = []
    duration_min = max(15, int(duration.total_seconds() // 60))

    ws = _parse_hhmm(str(avail.get("work_start", "09:00"))) or (9, 0)
    we = _parse_hhmm(str(avail.get("work_end", "18:00"))) or (18, 0)

    blocked_set = _expand_blocked_days(avail.get("blocked_days"))
    for offset in range(0, 8):
        target = (dtstart + timedelta(days=offset)).date()
        if avail.get("no_weekends") and target.weekday() >= 5:
            continue
        if target.isoformat() in blocked_set:
            continue
        try:
            slots = cal.get_free_slots(
                target,
                duration_minutes=duration_min,
                start_hour=ws[0],
                end_hour=we[0] if we[1] == 0 else we[0] + 1,
            )
        except Exception as e:
            log.warning("get_free_slots failed for %s: %s", target, e)
            continue
        for s_start, s_end in slots:
            if (s_end - s_start) >= duration:
                fit_end = s_start + duration
                if not check_availability(s_start, fit_end, avail):
                    suggestions.append((s_start, fit_end))
                    if len(suggestions) >= max_slots:
                        return suggestions
    return suggestions


# ---------------------------------------------------------------------------
# Pending event confirmations
#
# When /event would create a conflict or violate availability, we DON'T add it.
# Instead we stash it here and wait for the user's next text message:
#   "yes"/"y"/"confirm"  → add anyway
#   "no"/"n"/"cancel"    → drop
#   "reschedule"         → suggest alternatives
# Keyed by sender_id with a TTL so stale entries expire.
# ---------------------------------------------------------------------------

import time as _time

_PENDING_TTL_SEC = 600  # 10 minutes
_pending_events: dict[str, dict] = {}


def _cleanup_pending() -> None:
    now = _time.time()
    expired = [k for k, v in _pending_events.items() if now - v.get("ts", 0) > _PENDING_TTL_SEC]
    for k in expired:
        _pending_events.pop(k, None)


def _stash_pending(sender_id: str, payload: dict) -> None:
    _cleanup_pending()
    payload["ts"] = _time.time()
    _pending_events[sender_id] = payload


def _take_pending(sender_id: str) -> dict | None:
    _cleanup_pending()
    return _pending_events.pop(sender_id, None)


def _peek_pending(sender_id: str) -> dict | None:
    _cleanup_pending()
    return _pending_events.get(sender_id)


def _format_event(ev: dict) -> str:
    """Format a single event for display."""
    start = ev["start"]
    end = ev["end"]

    if ev.get("all_day"):
        time_str = "🏖️ All day"
    else:
        start_local = start.astimezone(TZ) if start.tzinfo else start.replace(tzinfo=TZ)
        end_local = end.astimezone(TZ) if end.tzinfo else end.replace(tzinfo=TZ)
        time_str = f"{start_local.strftime('%H:%M')}–{end_local.strftime('%H:%M')}"

    summary = ev["summary"] or "(no title)"
    location = f" 📍 {ev['location']}" if ev.get("location") else ""
    url = f"\n    🔗 {ev['url']}" if ev.get("url") else ""
    return f"🕐 {time_str}  *{summary}*{location}{url}"


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

    # Try common date formats. We try the raw string AND a variant where
    # we insert a space between digits and letters, so things like "8apr",
    # "apr8", "8APR" parse the same way as "8 apr".
    candidates = [raw]
    spaced = re.sub(r"(\d)([a-z])", r"\1 \2", raw)
    spaced = re.sub(r"([a-z])(\d)", r"\1 \2", spaced)
    if spaced != raw:
        candidates.append(spaced)

    for candidate in candidates:
        for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y", "%d/%m", "%d-%m",
                    "%d %b", "%d %B", "%b %d", "%B %d"):
            try:
                parsed = datetime.strptime(candidate, fmt)
                if fmt in ("%d/%m", "%d-%m", "%d %b", "%d %B", "%b %d", "%B %d"):
                    parsed = parsed.replace(year=now.year)
                    # If the date is in the recent past (within ~30 days), keep
                    # this year — the user probably means a date they just had.
                    # Only roll forward to next year if it's clearly in the past.
                    delta_days = (now.date() - parsed.date()).days
                    if delta_days > 30:
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

    # Explicit date queries show ALL events that day, including past ones —
    # the user asked for the date, not "what's left to do".

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

    lines = [f"📋 *Agenda — next {days} day(s)*"]
    for d in sorted(by_date):
        day_label = _format_date_header(d)
        if d == now.date():
            day_label += " _(today)_"
        elif d == now.date() + timedelta(days=1):
            day_label += " _(tomorrow)_"
        lines.append(f"\n*{day_label}*")
        for ev in sorted(by_date[d], key=lambda e: e["start"]):
            lines.append(f"  {_format_event(ev)}")

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


from cupbots.helpers.llm import _extract_json, ask_llm


# Time words used by the tripwire check — if the orchestrator resolves
# `/event create` with a title made entirely of these, we refuse no
# matter the confidence. This is the "last line of defense" safety net.
_TIME_WORDS = {
    "today", "tomorrow", "yesterday", "now", "tonight",
    "monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday",
    "mon", "tue", "tues", "wed", "thu", "thur", "thurs", "fri", "sat", "sun",
    "morning", "afternoon", "evening", "noon", "midnight",
    "am", "pm", "a.m.", "p.m.",
    "next", "this", "last", "every", "each",
    "week", "weeks", "month", "months", "year", "years", "day", "days",
    "hour", "hours", "minute", "minutes", "min", "mins", "hr", "hrs",
    "jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "sept", "oct", "nov", "dec",
    "january", "february", "march", "april", "june", "july", "august",
    "september", "october", "november", "december",
    "at", "on", "in", "by", "for", "from", "to", "the", "a", "an", "event", "meeting",
}


def _is_pure_time_word_title(title: str) -> bool:
    """Tripwire: return True if the title is made entirely of time words.

    Used as a safety net after the orchestrator resolves /event create —
    if the LLM hallucinated 'Next Wednesday' as a title, we catch it here
    regardless of the confidence score it returned.
    """
    if not title or not title.strip():
        return True
    tokens = re.findall(r"[A-Za-z][A-Za-z'.]+", title.lower())
    if not tokens:
        return True
    return all(t in _TIME_WORDS for t in tokens)


def _parse_flag_args(args: list[str]) -> dict:
    """Parse a primitive's flag-style args into a dict.

    Accepts:
        --title "Some thing"
        --title Dentist
        --start 2026-04-09T14:00
        --end 2026-04-09T15:00
        --location "Zoom URL"
        --rrule FREQ=WEEKLY;BYDAY=MO
        --query "standup next wed"
        --uid abc-123

    Values may be quoted or bare. Everything up to the next --flag is the
    value. Flag names are normalized to lowercase without the leading --.
    """
    # First reconstruct the raw string and re-tokenize with shlex so that
    # quoted values with spaces work correctly.
    import shlex
    try:
        tokens = shlex.split(" ".join(args))
    except ValueError:
        tokens = list(args)

    out: dict = {}
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        if tok.startswith("--"):
            key = tok[2:].lower()
            # Collect value tokens until next --flag
            values = []
            i += 1
            while i < len(tokens) and not tokens[i].startswith("--"):
                values.append(tokens[i])
                i += 1
            out[key] = " ".join(values).strip()
        else:
            i += 1
    return out


def _parse_iso_datetime(raw: str) -> datetime | None:
    """Parse an ISO 8601 datetime string into a tz-aware datetime in TZ.

    Accepts naive ISO (assumed in TZ) and tz-aware ISO. Returns None on
    failure.
    """
    if not raw:
        return None
    raw = raw.strip()
    try:
        dt = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=TZ)
    return dt


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
    """Telegram legacy handler — kept only for the telegram `register()`
    path, which is not currently wired in bot.py. The WhatsApp router
    uses handle_command() + the orchestrator instead.
    """
    if not update.message:
        return
    await update.message.reply_text(
        "Event creation now goes through the AI orchestrator.\n"
        "On WhatsApp, just type natural language like:\n"
        "  /event Dentist tomorrow 2pm 30m\n"
        "  /event Coffee with Ahmad next thursday 10am",
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
    "`/cal thursday` — Events on a day name\n"
    "`/cal 6 apr` — Events on a specific date\n"
    "`/cal free` — Free slots today\n"
    "`/cal free tomorrow` — Free slots on date\n"
    "`/cal availability` — View/edit no-meeting preferences\n"
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
    "Conflicting events are blocked — reply yes/no/reschedule to resolve.\n"
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

    # /cal <date> with multiple args (e.g. "6 apr", "next friday") → specific day
    # Try date parsing first when there are multiple args, so "/cal 6 apr"
    # parses as a date instead of being misread as "6 days ahead".
    if len(args) > 1:
        target = _parse_date(" ".join(args), now)
        if target:
            context.args = args
            return await cmd_day(update, context)

    # /cal <number> → agenda (single bare number, e.g. /cal 5)
    try:
        days_count = int(sub)
        context.args = [str(max(1, min(days_count, 30)))]
        return await cmd_agenda(update, context)
    except ValueError:
        pass

    # /cal <date> → specific day (single arg like "thursday", "tomorrow")
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
    """Format event for WhatsApp (supports bold markdown)."""
    start = ev["start"]
    end = ev["end"]
    if ev.get("all_day"):
        time_str = "All day"
    else:
        start_local = start.astimezone(TZ) if start.tzinfo else start.replace(tzinfo=TZ)
        end_local = end.astimezone(TZ) if end.tzinfo else end.replace(tzinfo=TZ)
        time_str = f"{start_local.strftime('%H:%M')}–{end_local.strftime('%H:%M')}"
    summary = ev["summary"] or "(no title)"
    location = f"\n    📍 {ev['location']}" if ev.get("location") else ""
    return f"  🕐 {time_str}  *{summary}*{location}"


async def _cal_today(now, reply):
    try:
        cal = _get_client()
        all_today = cal.get_events_today()
        log.info("/cal today: now=%s, found %d events from CalDAV", now.isoformat(), len(all_today))
        for ev in all_today:
            log.info("/cal today: event '%s' end=%s (tzinfo=%s)", ev["summary"][:30], ev["end"].isoformat(), ev["end"].tzinfo)
        # Ensure timezone-aware comparison — convert event end to same tz as now
        events = []
        for ev in all_today:
            end = ev["end"]
            if end.tzinfo is None:
                end = end.replace(tzinfo=TZ)
            end_local = end.astimezone(TZ)
            if end_local > now:
                events.append(ev)
            else:
                log.info("/cal today: filtered out '%s' — end %s <= now %s", ev["summary"][:30], end_local.isoformat(), now.isoformat())
    except Exception as e:
        await reply.reply_error(f"CalDAV error: {e}")
        return
    if not events:
        await reply.reply_text("No more events today.")
        return
    lines = [f"📋 *Today — {_format_date_header(now.date())}*"]
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
    lines = [f"📋 *Agenda ({days_count} days)*"]
    for d in sorted(days):
        day_label = _format_date_header(d)
        if d == now.date():
            day_label += " _(today)_"
        elif d == now.date() + timedelta(days=1):
            day_label += " _(tomorrow)_"
        lines.append(f"\n*{day_label}*")
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
    # Note: when the user explicitly asks for a date (even today), show ALL
    # events that day — including past ones. Filtering "events that ended
    # already" only applies to the bare `/cal` shortcut for today.
    if not events:
        await reply.reply_text(f"No events on {label} ({_format_date_header(target)}).")
        return
    lines = [f"📋 *{label} — {_format_date_header(target)}*"]
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
    lines = [f"*Next event* ({time_until}):", _format_event_plain(ev)]
    await reply.reply_text("\n".join(lines))


def _split_block_args(rest: list[str]) -> tuple[str, str | None]:
    """Split "block <a> [to|.. <b>]" into (a_text, b_text_or_None)."""
    if not rest:
        return "", None
    text = " ".join(rest)
    for sep in (" to ", " - ", "..", "/"):
        if sep in text:
            a, _, b = text.partition(sep)
            return a.strip(), b.strip()
    return text.strip(), None


async def _cal_availability(args, reply):
    """View or update availability preferences."""
    avail = _load_availability()

    if not args or args[0].lower() in ("show", "view", "list"):
        await reply.reply_text(
            _format_availability(avail) +
            "\n\nUpdate with:\n"
            "  /cal availability set no_weekends true|false\n"
            "  /cal availability set work_start 09:00\n"
            "  /cal availability set work_end 18:00\n"
            "  /cal availability set buffer_minutes 15\n"
            "  /cal availability block 10 apr           — single day\n"
            "  /cal availability block 10 apr to 15 apr — range\n"
            "  /cal availability unblock 10 apr"
        )
        return

    sub = args[0].lower()

    if sub == "set" and len(args) >= 3:
        key = args[1].lower()
        value = " ".join(args[2:]).strip()
        if key not in _AVAIL_DEFAULTS or key == "blocked_days":
            await reply.reply_text(
                f"Unknown key '{key}'. Valid: no_weekends, work_start, work_end, buffer_minutes."
            )
            return
        if key == "no_weekends":
            avail[key] = value.lower() in ("true", "yes", "y", "1", "on")
        elif key in ("work_start", "work_end"):
            if not _parse_hhmm(value):
                await reply.reply_text(f"Invalid time '{value}'. Use HH:MM (e.g. 09:00).")
                return
            avail[key] = value
        elif key == "buffer_minutes":
            try:
                avail[key] = max(0, int(value))
            except ValueError:
                await reply.reply_text(f"Invalid integer '{value}'.")
                return
        _save_availability(avail)
        await reply.reply_text(f"Updated.\n\n{_format_availability(avail)}")
        return

    if sub == "block" and len(args) >= 2:
        now = datetime.now(TZ)
        a_text, b_text = _split_block_args(list(args[1:]))
        a = _parse_date(a_text, now) if a_text else None
        if not a:
            await reply.reply_text(f"Couldn't parse date '{a_text}'.")
            return
        b = _parse_date(b_text, now) if b_text else None
        if b_text and not b:
            await reply.reply_text(f"Couldn't parse end date '{b_text}'.")
            return

        blocked = list(avail.get("blocked_days") or [])
        if b and b != a:
            if b < a:
                a, b = b, a
            entry = f"{a.isoformat()}..{b.isoformat()}"
            added_label = entry
        else:
            entry = a.isoformat()
            added_label = entry

        if entry not in blocked:
            blocked.append(entry)
            blocked.sort()
            avail["blocked_days"] = blocked
            _save_availability(avail)
        await reply.reply_text(f"Blocked {added_label}.\n\n{_format_availability(avail)}")
        return

    if sub == "unblock" and len(args) >= 2:
        now = datetime.now(TZ)
        a_text, b_text = _split_block_args(list(args[1:]))
        a = _parse_date(a_text, now) if a_text else None
        if not a:
            await reply.reply_text(f"Couldn't parse date '{a_text}'.")
            return
        b = _parse_date(b_text, now) if b_text else None

        blocked = list(avail.get("blocked_days") or [])
        if b and b != a:
            # Drop any entry whose expansion is exactly the requested range,
            # or any single-day entry that falls inside the range.
            target_set = _expand_blocked_days([f"{a.isoformat()}..{b.isoformat()}"])
            kept = []
            for entry in blocked:
                entry_set = _expand_blocked_days([entry])
                if entry_set and entry_set.issubset(target_set):
                    continue
                kept.append(entry)
            avail["blocked_days"] = kept
        else:
            iso = a.isoformat()
            kept = []
            for entry in blocked:
                entry_set = _expand_blocked_days([entry])
                if entry_set == {iso}:
                    continue  # exact single-day match — drop
                kept.append(entry)
            avail["blocked_days"] = kept

        _save_availability(avail)
        await reply.reply_text(
            f"Unblocked {a.isoformat()}{(' to ' + b.isoformat()) if b and b != a else ''}.\n\n"
            + _format_availability(avail)
        )
        return

    await reply.reply_text(
        "Usage:\n"
        "  /cal availability                                — show preferences\n"
        "  /cal availability set <key> <value>              — update a key\n"
        "  /cal availability block <date>                   — block a single date\n"
        "  /cal availability block <date> to <date>         — block a range\n"
        "  /cal availability unblock <date> [to <date>]     — unblock a date or range"
    )


async def _cal_connect(msg, reply):
    """Kick off Google Calendar OAuth via the hub. Works for Workspace + consumer Gmail.

    The hub holds the OAuth client_id / client_secret as env vars, so there's
    no bot-side configuration. If the hub isn't connected or isn't configured,
    start_flow() will surface a clear error to the user.
    """
    await start_flow(
        provider="google_calendar",
        scopes=None,
        metadata={"chat_id": msg.chat_id},
        reply=reply,
        extra_params={"access_type": "offline", "prompt": "consent"},
    )


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
    "READ (use naturally):\n"
    "  /cal -- today's events\n"
    "  /cal week -- next 7 days\n"
    "  /cal <N> -- next N days (e.g. /cal 5)\n"
    "  /cal <date> -- events on a date (e.g. /cal 8 apr, /cal thursday)\n"
    "  /cal next -- next upcoming event\n"
    "  /cal free [date] -- free slots\n"
    "  /cal availability -- view/edit no-meeting preferences\n"
    "  /cal connect -- connect Google Calendar via OAuth (one click)\n"
    "  /cal briefing | tomorrow | weekahead -- previews\n"
    "  /cal holidays [3m|6m|1y] -- upcoming holidays\n"
    "\n"
    "WRITE (use EXACT flag syntax — for humans, just type natural language\n"
    "and the bot's orchestrator will convert it):\n"
    "  /event create --title X --start 2026-04-09T14:00 --end 2026-04-09T15:00\n"
    "  /event create --title X --start 2026-04-09T14:00 --duration 30m\n"
    "  /event delete --uid <uid>\n"
    "  /event delete --query \"standup\"\n"
    "  /event search --query \"standup\" [--days 30]\n"
    "\n"
    "On a conflict, /event will ask: yes / no / reschedule"
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

    # Prepare the Google Calendar access token cache if we're on the Google
    # backend. This is a no-op for CalDAV. It mints a fresh token via the hub
    # (or hits the cache) so subsequent sync `_get_client().service` calls can
    # build the Credentials object without blocking on a WebSocket roundtrip.
    # Skipped for `/cal connect` itself (we don't have a refresh token yet).
    if not (msg.args and msg.args[0].lower() == "connect" and msg.command == "cal"):
        try:
            cal_prep = _get_client()
            if hasattr(cal_prep, "async_prepare"):
                await cal_prep.async_prepare()
        except Exception as e:
            log.debug("Calendar async_prepare skipped: %s", e)

    now = datetime.now(TZ)

    # --- /event (write primitives only) ---
    #
    # /event is now a primitive command dispatched by the orchestrator
    # layer in wa_router.py. It takes ONLY flag-style args:
    #
    #   /event create --title X --start 2026-04-09T14:00 --end 2026-04-09T15:00
    #   /event create --title X --start 2026-04-09T14:00 --duration 30m
    #   /event delete --uid <uid>
    #   /event delete --query "standup"
    #   /event search --query "standup" [--days 30]
    #
    # The orchestrator (resolve_write_intent in helpers/llm.py) translates
    # natural-language `/event next wed 2pm dentist` into one of these
    # primitive calls, with a confidence score gating execution. The
    # plugin itself no longer parses natural language.
    if msg.command == "event":
        if not msg.args:
            await reply.reply_text(
                "Usage:\n"
                "  /event create --title X --start 2026-04-09T14:00 --end 2026-04-09T15:00\n"
                "  /event create --title X --start 2026-04-09T14:00 --duration 60m\n"
                "  /event delete --uid <uid>\n"
                "  /event delete --query \"standup\"\n"
                "  /event search --query \"standup\" [--days 30]\n"
                "\n"
                "Normally you won't call these directly — just type natural\n"
                "language like `/event Dentist tomorrow 2pm 30m` and the\n"
                "bot's orchestrator will resolve it for you."
            )
            return True

        action = msg.args[0].lower()
        if action not in ("create", "delete", "search"):
            # Unknown subcommand. Reply with the correct syntax so the AI
            # agent self-corrects on its next turn instead of hallucinating
            # variants like `/event list` forever.
            await reply.reply_text(
                f"'/event {action}' is not a valid subcommand.\n\n"
                "Valid subcommands:\n"
                "  /event create --title X --start 2026-04-09T14:00 --end 2026-04-09T15:00\n"
                "  /event create --title X --start 2026-04-09T14:00 --duration 30m\n"
                "  /event delete --uid <uid>\n"
                "  /event delete --query \"standup\"\n"
                "  /event search --query \"standup\" [--days 30]\n"
                "\n"
                "To LIST events, use /cal, /cal week, or /event search --query \"\"."
            )
            return True

        flags = _parse_flag_args(msg.args[1:])

        if action == "create":
            return await _handle_event_create_primitive(msg, reply, flags)
        if action == "delete":
            return await _handle_event_delete_primitive(msg, reply, flags)
        if action == "search":
            return await _handle_event_search_primitive(msg, reply, flags)
        return False  # unreachable

    # --- /delete (legacy alias) → pass-through to /event delete --query ---
    if msg.command == "delete":
        flags = {"query": " ".join(msg.args).strip()} if msg.args else {}
        return await _handle_event_delete_primitive(msg, reply, flags)

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

    if sub in ("availability", "avail", "prefs", "preferences"):
        await _cal_availability(args[1:], reply)
        return True

    if sub == "connect":
        await _cal_connect(msg, reply)
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

    # Multi-arg form (e.g. "6 apr", "next friday") → try date parse first
    # so /cal 6 apr is read as a date, not "6 days ahead".
    if len(args) > 1:
        target = _parse_date(" ".join(args), now)
        if target:
            await _cal_day(now, target, reply)
            return True

    # Single bare number → /cal N days agenda
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

    # We can't make sense of these args. Reply with the help text so the
    # AI agent (or confused user) sees the valid subcommands and can
    # self-correct. Return True so the router doesn't fall through.
    await reply.reply_text(
        f"'/cal {' '.join(args)}' is not a valid subcommand.\n\n"
        + _CAL_HELP
    )
    return True


def _format_delete_candidate(ev: dict, idx: int | None = None) -> str:
    """One-line label for a candidate event in the delete flow."""
    start = ev["start"]
    if start.tzinfo is None:
        start = start.replace(tzinfo=TZ)
    start_local = start.astimezone(TZ)
    end = ev["end"]
    if end.tzinfo is None:
        end = end.replace(tzinfo=TZ)
    end_local = end.astimezone(TZ)
    title = ev.get("summary") or "(no title)"
    if ev.get("all_day"):
        time_str = "all day"
    else:
        time_str = f"{start_local.strftime('%H:%M')}-{end_local.strftime('%H:%M')}"
    prefix = f"{idx}. " if idx is not None else "  "
    return f"{prefix}{title}  —  {start_local.strftime('%a %d %b')} {time_str}"


async def _handle_event_create_primitive(msg, reply, flags: dict) -> bool:
    """Primitive: /event create --title X --start Y --end Z [--location L] [--rrule R]

    Called by the orchestrator with pre-parsed, validated arguments. This
    function does NO natural-language parsing — it trusts its flags and
    enforces only deterministic safety checks (tripwire, conflicts,
    availability).
    """
    title = flags.get("title", "").strip()
    start_raw = flags.get("start", "")
    end_raw = flags.get("end", "")
    duration_raw = flags.get("duration", "")  # alt: "60m" or "1h"
    location = flags.get("location", "")
    rrule = flags.get("rrule", "") or flags.get("recurrence", "")

    # --- Tripwire: refuse pure time-word titles regardless of caller ---
    if _is_pure_time_word_title(title):
        await reply.reply_text(
            f"I won't create an event with the title \"{title}\" — that's "
            f"a time phrase, not a subject. Give me an actual title "
            f"(e.g. Dentist, Coffee with Ahmad)."
        )
        return True

    # --- Required args ---
    if not title:
        await reply.reply_text("Missing --title for /event create.")
        return True
    if not start_raw:
        await reply.reply_text("Missing --start for /event create (ISO 8601, e.g. 2026-04-09T14:00).")
        return True

    dtstart = _parse_iso_datetime(start_raw)
    if not dtstart:
        await reply.reply_text(f"Invalid --start {start_raw!r}. Use ISO 8601 like 2026-04-09T14:00.")
        return True

    dtend = None
    if end_raw:
        dtend = _parse_iso_datetime(end_raw)
        if not dtend:
            await reply.reply_text(f"Invalid --end {end_raw!r}.")
            return True
    elif duration_raw:
        total = timedelta()
        for match in re.finditer(r"(\d+)\s*(h|m)", duration_raw.lower()):
            val, unit = int(match.group(1)), match.group(2)
            if unit == "h":
                total += timedelta(hours=val)
            elif unit == "m":
                total += timedelta(minutes=val)
        if total > timedelta():
            dtend = dtstart + total
    if not dtend:
        dtend = dtstart + timedelta(hours=1)

    if dtend <= dtstart:
        await reply.reply_text("End time must be after start time.")
        return True

    now = datetime.now(TZ)

    # --- Safety: conflicts + availability (only for non-recurring) ---
    avail = _load_availability()
    conflicts: list[dict] = []
    violations: list[str] = []
    if not rrule:
        try:
            cal = _get_client()
            conflicts = cal.check_conflicts(dtstart, dtend)
        except Exception as e:
            await reply.reply_text(f"Calendar error: {e}")
            return True
        violations = check_availability(dtstart, dtend, avail)

    if conflicts or violations:
        _stash_pending(msg.sender_id, {
            "title": title,
            "start": dtstart,
            "end": dtend,
            "location": location,
            "rrule": rrule,
            "raw": msg.text,
        })
        tz_label = str(TZ).split("/")[-1]
        lines = [
            f"Cannot add '{title}' as-is — conflict or preference violation.",
            f"Requested: {dtstart.strftime('%a %d %b %H:%M')}–{dtend.strftime('%H:%M')} ({tz_label})",
        ]
        if conflicts:
            names = ", ".join(c["summary"] or "(no title)" for c in conflicts)
            lines.append(f"Conflicts with: {names}")
        for v in violations:
            lines.append(f"Violates preference: {v}")
        duration = dtend - dtstart
        suggestions = _suggest_alternative_slots(dtstart, duration, avail)
        if suggestions:
            lines.append("")
            lines.append("Suggested free slots:")
            for s_start, s_end in suggestions:
                lines.append(f"  - {s_start.strftime('%a %d %b %H:%M')}–{s_end.strftime('%H:%M')}")
        lines.append("")
        lines.append("Reply 'yes' to add anyway, 'no' to cancel, 'reschedule' to draft a reply.")
        await reply.reply_text("\n".join(lines))
        return True

    # --- Execute ---
    tz_label = str(TZ).split("/")[-1]
    try:
        cal = _get_client()
        cal.add_event(title, dtstart, dtend, location=location, rrule=rrule)
    except Exception as e:
        await reply.reply_text(f"Calendar error: {e}")
        return True

    parts = [f"Created: {title}",
             f"{dtstart.strftime('%a %d %b %H:%M')} – {dtend.strftime('%H:%M')} ({tz_label})"]
    if rrule:
        parts.append(f"Recurring: {rrule}")
    if location:
        parts.append(f"Location: {location}")
    await reply.reply_text("\n".join(parts))
    return True


async def _handle_event_delete_primitive(msg, reply, flags: dict) -> bool:
    """Primitive: /event delete --uid <uid>  OR  /event delete --query <text>

    --uid deletes exactly that event (no confirmation — the orchestrator or
    a previous /event search already picked it).
    --query is a fallback for when the orchestrator couldn't narrow the
    target; it runs the old fuzzy flow but still requires confirmation.
    """
    now = datetime.now(TZ)
    uid = flags.get("uid", "").strip()
    query = flags.get("query", "").strip()

    if uid:
        try:
            cal = _get_client()
            deleted = cal.delete_events_by_uid({uid}, now, now + timedelta(days=365))
        except Exception as e:
            await reply.reply_error(f"Failed to delete: {e}")
            return True
        if deleted:
            await reply.reply_text(f"Deleted event (uid={uid}).")
        else:
            await reply.reply_text(f"No event found with uid={uid}")
        return True

    if not query:
        await reply.reply_text("Missing --uid or --query for /event delete.")
        return True

    # Fuzzy-query fallback: list candidates and stash for user to pick.
    try:
        cal = _get_client()
        events = cal.get_events(now, now + timedelta(days=90))
    except Exception as e:
        await reply.reply_error(f"CalDAV error: {e}")
        return True

    q_lower = query.lower()
    q_words = [w for w in re.findall(r"[a-z0-9]+", q_lower) if w not in _TIME_WORDS]
    if q_words:
        matches = [ev for ev in events
                   if all(w in (ev.get("summary") or "").lower() for w in q_words)]
    else:
        matches = []

    if not matches:
        await reply.reply_text(
            f"No upcoming event matching \"{query}\". Try /cal to see your schedule."
        )
        return True

    if len(matches) == 1:
        _stash_pending(msg.sender_id, {
            "kind": "delete",
            "candidates": matches,
            "query": query,
        })
        await reply.reply_text(
            f"Found 1 match for \"{query}\":\n\n"
            f"{_format_delete_candidate(matches[0], idx=1)}\n\n"
            f"Reply 'yes' or '1' to delete, 'no' to cancel."
        )
        return True

    _stash_pending(msg.sender_id, {
        "kind": "delete",
        "candidates": matches,
        "query": query,
    })
    lines = [f"Multiple events match \"{query}\" — which one?", ""]
    for i, ev in enumerate(matches[:9], start=1):
        lines.append(_format_delete_candidate(ev, idx=i))
    if len(matches) > 9:
        lines.append(f"  … and {len(matches) - 9} more")
    lines.append("")
    lines.append("Reply with the number, 'all', or 'no'.")
    await reply.reply_text("\n".join(lines))
    return True


async def _handle_event_search_primitive(msg, reply, flags: dict) -> bool:
    """Primitive: /event search --query X [--days N]

    Returns a list of matching events with UIDs. Used by the orchestrator
    to find targets for subsequent /event delete or /event edit calls.
    """
    query = flags.get("query", "").strip()
    try:
        days = int(flags.get("days", "60"))
    except ValueError:
        days = 60

    now = datetime.now(TZ)
    try:
        cal = _get_client()
        events = cal.get_events(now, now + timedelta(days=days))
    except Exception as e:
        await reply.reply_error(f"CalDAV error: {e}")
        return True

    if query:
        q_lower = query.lower()
        matches = [ev for ev in events if q_lower in (ev.get("summary") or "").lower()]
    else:
        matches = events[:20]

    if not matches:
        await reply.reply_text(f"No events match '{query}' in the next {days} days.")
        return True

    # Include the FULL uid so the caller (often the AI agent) can use it
    # verbatim in a follow-up `/event delete --uid <uid>` call.
    lines = [f"{len(matches)} match(es):"]
    for ev in matches[:20]:
        start_local = ev["start"].astimezone(TZ) if ev["start"].tzinfo else ev["start"].replace(tzinfo=TZ)
        title = ev.get("summary") or "(no title)"
        lines.append(
            f"  uid={ev['uid']}  {start_local.strftime('%a %d %b %H:%M')}  {title}"
        )
    await reply.reply_text("\n".join(lines))
    return True


async def _execute_delete(cal, events: list[dict], now: datetime, reply,
                          delete_all: bool = False) -> None:
    """Delete the given events and reply with a summary."""
    uids = {ev["uid"] for ev in events}
    try:
        deleted_count = cal.delete_events_by_uid(
            uids, now, now + timedelta(days=365)
        )
    except Exception as e:
        await reply.reply_error(f"Failed to delete: {e}")
        return

    if deleted_count == 0:
        await reply.reply_text("Event found but couldn't delete it from calendar.")
        return

    if len(events) == 1:
        ev = events[0]
        start = ev["start"]
        if start.tzinfo is None:
            start = start.replace(tzinfo=TZ)
        start_local = start.astimezone(TZ)
        end = ev["end"]
        if end.tzinfo is None:
            end = end.replace(tzinfo=TZ)
        end_local = end.astimezone(TZ)
        if ev.get("all_day"):
            time_str = "all day"
        else:
            time_str = f"{start_local.strftime('%H:%M')}-{end_local.strftime('%H:%M')}"
        await reply.reply_text(
            f"Deleted: {ev.get('summary') or '(no title)'}\n"
            f"{start_local.strftime('%a %d %b')} {time_str}"
        )
        return

    lines = [f"Deleted {deleted_count} event(s):"]
    for ev in events[:10]:
        lines.append(_format_delete_candidate(ev))
    if len(events) > 10:
        lines.append(f"  … and {len(events) - 10} more")
    await reply.reply_text("\n".join(lines))


async def handle_message(msg, reply) -> bool:
    """Catch replies for pending calendar confirmations.

    Two flavours of pending state share `_pending_events[sender_id]`:

    - event-add (no "kind" key, or kind="event_add"): response is one of
      yes / no / reschedule for a conflicting event creation.
    - event-delete (kind="delete"): response is a number to pick which
      candidate to delete, "all" to delete everything listed, or "no"
      to cancel.

    Returns False for any unrelated message so other plugins and the AI
    router see it normally.
    """
    if not msg.text or msg.command:
        return False

    pending = _peek_pending(msg.sender_id)
    if not pending:
        return False

    text = msg.text.strip().lower()
    first = text.split()[0] if text else ""
    kind = pending.get("kind", "event_add")

    # --- Pending DELETE — expects a number, "yes"/"all", or "no" ---
    if kind == "delete":
        candidates: list[dict] = pending.get("candidates", [])
        if not candidates:
            _take_pending(msg.sender_id)
            return False

        if first in ("no", "n", "cancel", "drop", "skip"):
            _take_pending(msg.sender_id)
            await reply.reply_text("Delete cancelled.")
            return True

        # "yes" on a single-match pending confirms that one event.
        # "yes" on an N-match pending is ambiguous; require a number.
        if first in ("yes", "y", "confirm", "ok", "sure", "go"):
            if len(candidates) == 1:
                _take_pending(msg.sender_id)
                try:
                    cal = _get_client()
                except Exception as e:
                    await reply.reply_text(f"Calendar error: {e}")
                    return True
                await _execute_delete(cal, candidates, datetime.now(TZ), reply)
                return True
            # Multiple candidates — don't guess, prompt for a number.
            await reply.reply_text(
                f"Please reply with a number (1-{len(candidates)}), 'all', or 'no'."
            )
            return True

        if first in ("all", "everything"):
            _take_pending(msg.sender_id)
            try:
                cal = _get_client()
            except Exception as e:
                await reply.reply_text(f"Calendar error: {e}")
                return True
            await _execute_delete(
                cal, candidates, datetime.now(TZ), reply, delete_all=True,
            )
            return True

        # Numeric pick — supports "1", "#1", "1,2", "1 2 3"
        picked_indices: list[int] = []
        for token in re.findall(r"\d+", text):
            try:
                n = int(token)
            except ValueError:
                continue
            if 1 <= n <= len(candidates) and (n - 1) not in picked_indices:
                picked_indices.append(n - 1)

        if picked_indices:
            _take_pending(msg.sender_id)
            try:
                cal = _get_client()
            except Exception as e:
                await reply.reply_text(f"Calendar error: {e}")
                return True
            chosen = [candidates[i] for i in picked_indices]
            await _execute_delete(cal, chosen, datetime.now(TZ), reply)
            return True

        # Unrecognized reply — leave pending in place so the user can try
        # again, but don't hijack the message.
        return False

    # --- Pending EVENT-ADD (conflict confirmation) ---
    if first in ("yes", "y", "confirm", "ok", "sure", "go"):
        _take_pending(msg.sender_id)
        try:
            cal = _get_client()
            cal.add_event(
                pending["title"], pending["start"], pending["end"],
                location=pending.get("location", ""), rrule=pending.get("rrule", ""),
            )
        except Exception as e:
            await reply.reply_text(f"Calendar error: {e}")
            return True
        tz_label = str(TZ).split("/")[-1]
        await reply.reply_text(
            f"Added (with conflict): {pending['title']}\n"
            f"{pending['start'].strftime('%a %d %b %H:%M')}–{pending['end'].strftime('%H:%M')} ({tz_label})"
        )
        return True

    if first in ("no", "n", "cancel", "drop", "skip"):
        _take_pending(msg.sender_id)
        await reply.reply_text(f"Cancelled '{pending['title']}'.")
        return True

    if first in ("reschedule", "decline", "reply"):
        _take_pending(msg.sender_id)
        avail = _load_availability()
        duration = pending["end"] - pending["start"]
        suggestions = _suggest_alternative_slots(pending["start"], duration, avail)
        slot_lines = "\n".join(
            f"  - {s.strftime('%a %d %b %H:%M')}–{e.strftime('%H:%M')}"
            for s, e in suggestions
        ) or "  (no free slots in the next 7 days that match your preferences)"
        draft = (
            f"Hi! Thanks for the invite to '{pending['title']}'.\n\n"
            f"Unfortunately I have a conflict at "
            f"{pending['start'].strftime('%a %d %b, %H:%M')}–{pending['end'].strftime('%H:%M')} "
            f"and can't make it. Could we reschedule? A few times that work for me:\n\n"
            f"{slot_lines}\n\n"
            f"Let me know what suits you and I'll lock it in. Thanks!"
        )
        await reply.reply_text(
            "Draft reschedule reply (copy/paste or forward):\n\n" + draft
        )
        return True

    # Not a recognized confirmation verb — leave the message alone.
    return False


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

