"""
Heartbeat Agent

Scheduled cross-plugin synthesis that connects the dots you wouldn't think to query.
Runs morning + evening, or on demand. Uses Claude CLI to reason across all data sources.

Commands (works in any topic):
  /think [question]  — Run the agent now (optional: ask it something specific)
  /heartbeat         — Show when the next heartbeat runs
  /snapshot          — Record net worth snapshot now (auto-runs 1st of month)
"""

import asyncio
import json
import os
import subprocess
import sqlite3
import sys
from datetime import datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes

from cupbots.config import get_config, get_scripts_dir, get_data_dir
from cupbots.helpers.db import get_plugin_db
from cupbots.helpers.logger import get_logger
from cupbots.helpers.llm import run_claude_cli

log = get_logger("heartbeat")

PROJECT_ROOT = get_scripts_dir().parent
BOT_DIR = Path(__file__).resolve().parent.parent
VENV_PY = str(PROJECT_ROOT / "venv" / "bin" / "python3")


# ---------------------------------------------------------------------------
# Data gathering — pure Python, no LLM tokens
# ---------------------------------------------------------------------------

def _gather_calendar(tz: ZoneInfo) -> str:
    """Today's events + tomorrow preview."""
    try:
        from cupbots.helpers.calendar_client import get_calendar_client
        from dotenv import load_dotenv
        load_dotenv(PROJECT_ROOT / ".env")
        cal = get_calendar_client()
        today_events = cal.get_events_today()
        tomorrow = datetime.now(tz) + timedelta(days=1)
        tomorrow_start = tomorrow.replace(hour=0, minute=0, second=0, microsecond=0)
        tomorrow_end = tomorrow_start + timedelta(days=1)
        tomorrow_events = cal.get_events(tomorrow_start, tomorrow_end)
    except Exception as e:
        return f"Calendar unavailable: {e}"

    lines = []
    now = datetime.now(tz)

    if today_events:
        # Filter to remaining events
        remaining = [e for e in today_events if e["end"] > now]
        if remaining:
            lines.append(f"TODAY — {len(remaining)} event(s) remaining:")
            for ev in sorted(remaining, key=lambda e: e["start"]):
                if ev.get("all_day"):
                    lines.append(f"  All day: {ev['summary']}")
                else:
                    mins_until = int((ev["start"] - now).total_seconds() / 60)
                    time_str = f"{ev['start'].strftime('%H:%M')}-{ev['end'].strftime('%H:%M')}"
                    if mins_until > 0:
                        lines.append(f"  {time_str} {ev['summary']} (in {mins_until}min)")
                    else:
                        lines.append(f"  {time_str} {ev['summary']} (now)")
        else:
            lines.append("TODAY — No more events.")
    else:
        lines.append("TODAY — No events. Deep work day.")

    if tomorrow_events:
        lines.append(f"\nTOMORROW — {len(tomorrow_events)} event(s):")
        for ev in sorted(tomorrow_events, key=lambda e: e["start"]):
            if ev.get("all_day"):
                lines.append(f"  All day: {ev['summary']}")
            else:
                lines.append(f"  {ev['start'].strftime('%H:%M')}-{ev['end'].strftime('%H:%M')} {ev['summary']}")

    return "\n".join(lines) if lines else "No calendar events today or tomorrow."


def _gather_contacts(company_id: str = "") -> str:
    """Overdue contacts and upcoming check-ins, scoped to a company.

    TENANT NOTE: heartbeat is currently single-tenant. The morning/evening
    scheduled jobs target one Telegram chat (cfg["telegram"]["chat_id"]) and
    pass company_id="" (the untenanted bucket — same scope as the contacts
    plugin's Telegram code paths). To make heartbeat multi-tenant, register
    one job per company and route briefings to the company's notify_chat.
    """
    conn = get_plugin_db("contacts")
    today = datetime.now().strftime("%Y-%m-%d")

    overdue = conn.execute(
        """SELECT name, tier, next_contact, last_contact, notes
           FROM contacts
           WHERE company_id = ?
             AND next_contact IS NOT NULL AND next_contact <= ?
           ORDER BY next_contact ASC LIMIT 10""",
        (company_id, today),
    ).fetchall()

    # Upcoming in next 7 days
    week_ahead = (datetime.now() + timedelta(days=7)).strftime("%Y-%m-%d")
    upcoming = conn.execute(
        """SELECT name, tier, next_contact
           FROM contacts
           WHERE company_id = ?
             AND next_contact > ? AND next_contact <= ?
           ORDER BY next_contact ASC LIMIT 5""",
        (company_id, today, week_ahead),
    ).fetchall()

    # conn is shared — never close it

    lines = []
    if overdue:
        lines.append(f"OVERDUE CONTACTS ({len(overdue)}):")
        for c in overdue:
            days_late = (datetime.now() - datetime.fromisoformat(c["next_contact"])).days
            last = c["last_contact"] or "never"
            # Extract last note snippet
            note_snippet = ""
            if c["notes"]:
                last_note = c["notes"].strip().split("\n")[-1]
                if len(last_note) > 60:
                    last_note = last_note[:60] + "..."
                note_snippet = f" — {last_note}"
            lines.append(f"  [{c['tier']}] {c['name']} — {days_late}d overdue (last: {last}){note_snippet}")

    if upcoming:
        lines.append(f"\nUPCOMING CHECK-INS (next 7 days):")
        for c in upcoming:
            lines.append(f"  [{c['tier']}] {c['name']} — due {c['next_contact']}")

    return "\n".join(lines) if lines else "No contacts overdue or due this week."


def _gather_pipelines() -> str:
    """Pipeline run status."""
    cfg = get_config()
    scripts_dir = get_scripts_dir()
    today = datetime.now().strftime("%Y-%m-%d")
    lines = []

    for name, pcfg in cfg.get("pipelines", {}).items():
        log_path = scripts_dir / pcfg.get("log", "")
        if not log_path.exists():
            lines.append(f"  {name}: no log file")
            continue
        log_text = log_path.read_text(encoding="utf-8", errors="ignore")
        if today in log_text:
            lines.append(f"  {name}: ran today")
        else:
            lines.append(f"  {name}: NOT run today")

    return "PIPELINE STATUS:\n" + "\n".join(lines) if lines else "No pipelines configured."


def _gather_reminders() -> str:
    """Pending reminders."""
    conn = get_plugin_db("contacts")
    rows = conn.execute(
        "SELECT message, fire_at FROM reminders WHERE fired = 0 ORDER BY fire_at LIMIT 5"
    ).fetchall()
    # conn is shared — never close it

    if not rows:
        return "No pending reminders."

    lines = ["PENDING REMINDERS:"]
    now = datetime.now()
    for msg, fire_at in rows:
        dt = datetime.fromisoformat(fire_at)
        delta = dt - now
        if delta.total_seconds() <= 0:
            time_str = "overdue"
        elif delta.total_seconds() < 3600:
            time_str = f"in {int(delta.total_seconds() // 60)}m"
        elif delta.total_seconds() < 86400:
            time_str = f"in {int(delta.total_seconds() // 3600)}h"
        else:
            time_str = dt.strftime("%a %d %b %H:%M")
        lines.append(f"  {time_str}: {msg}")

    return "\n".join(lines)


def _gather_receivables() -> str:
    """Outstanding receivables from both beancount ledgers."""
    try:
        from plugins._finance_helpers import run_bql_raw

        lines = ["OUTSTANDING RECEIVABLES:"]
        for ledger in ("cupbots", "personal"):
            bql = (
                "SELECT account, narration, date, position "
                "WHERE account ~ 'Receivable' "
                "ORDER BY date"
            )
            result_types, result_rows = run_bql_raw(ledger, bql)

            # Sum by filtering for non-zero balances
            bql_bal = (
                "SELECT account, sum(position) AS balance "
                "WHERE account ~ 'Receivable' "
                "GROUP BY account"
            )
            _, bal_rows = run_bql_raw(ledger, bql_bal)
            for row in bal_rows:
                account = str(row[0])
                balance = str(row[1]) if row[1] else "0"
                if balance and balance != "0" and "0 " not in balance.split(",")[0][:3]:
                    lines.append(f"  [{ledger}] {account}: {balance}")

        return "\n".join(lines) if len(lines) > 1 else ""
    except Exception as e:
        log.warning("Failed to gather receivables: %s", e)
        return ""


def _gather_bot_errors() -> str:
    """Recent bot errors (last 24h)."""
    conn = get_plugin_db("contacts")
    cutoff = (datetime.now() - timedelta(hours=24)).isoformat()
    rows = conn.execute(
        """SELECT timestamp, logger, message FROM bot_logs
           WHERE level IN ('ERROR', 'WARNING') AND timestamp > ?
           ORDER BY timestamp DESC LIMIT 5""",
        (cutoff,),
    ).fetchall()
    # conn is shared — never close it

    if not rows:
        return "No errors in the last 24 hours."

    lines = ["RECENT ERRORS (24h):"]
    for ts, logger, msg in rows:
        short_ts = ts[11:16] if len(ts) > 16 else ts
        short_msg = msg[:100] + "..." if len(msg) > 100 else msg
        lines.append(f"  {short_ts} [{logger}] {short_msg}")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Agent — one Claude CLI call to synthesize + propose actions
# ---------------------------------------------------------------------------

HEARTBEAT_SYSTEM_PROMPT = """\
You are the Heartbeat Agent for a personal Telegram bot. Your job is to synthesize \
data from multiple sources into ONE concise, actionable briefing.

Rules:
- Lead with what NEEDS ATTENTION, not a summary of everything.
- If nothing needs attention in a category, skip it entirely.
- For calendar, highlight conflicts or tight transitions between events.
- For pipeline failures, say what failed and suggest retrying.
- For outstanding receivables (Sundays), highlight overdue invoices and total amount owed.
- Keep the entire response under 1500 characters.
- Use emoji sparingly: one per section header max.
- Do NOT use markdown headers (#). Use simple text with line breaks.
- End with a one-liner on what the rest of the day/evening looks like.
- If the user asked a specific question, answer that FIRST, then do the briefing.
"""


async def _run_heartbeat(
    question: str | None = None,
    period: str = "morning",
) -> str:
    """Gather data, send to Claude CLI, return synthesized briefing."""
    cfg = get_config()
    tz = ZoneInfo(cfg["schedule"]["timezone"])

    # Gather all data concurrently in threads (avoid blocking event loop)
    calendar, pipelines, reminders, errors, receivables = await asyncio.gather(
        asyncio.to_thread(_gather_calendar, tz),
        asyncio.to_thread(_gather_pipelines),
        asyncio.to_thread(_gather_reminders),
        asyncio.to_thread(_gather_bot_errors),
        asyncio.to_thread(_gather_receivables),
    )

    context_block = f"""=== HEARTBEAT CONTEXT ({period}) — {datetime.now(tz).strftime('%A %d %B %Y, %H:%M')} ===

{calendar}

{pipelines}

{reminders}

{errors}

{receivables}
"""

    prompt = context_block
    if question:
        prompt = f"User question: {question}\n\n{context_block}"

    try:
        result = await run_claude_cli(
            prompt,
            model=cfg.get("claude", {}).get("model", "haiku"),
            system_prompt=HEARTBEAT_SYSTEM_PROMPT,
            tools="Bash,Read,Grep",
            max_turns=5,
            max_budget_usd="0.10",
            cwd=str(BOT_DIR),
            timeout=90,
        )
        return result["text"]
    except Exception as e:
        log.error("Heartbeat agent failed: %s", e)
        return f"Heartbeat agent error: {e}"


# ---------------------------------------------------------------------------
# Action buttons for common follow-ups
# ---------------------------------------------------------------------------

def _build_action_buttons(has_failed_pipelines: bool) -> InlineKeyboardMarkup | None:
    """Build inline buttons for common heartbeat actions."""
    buttons = []

    if has_failed_pipelines:
        cfg = get_config()
        pipeline_buttons = []
        for name in cfg.get("pipelines", {}):
            pipeline_buttons.append(
                InlineKeyboardButton(f"🔄 Retry {name}", callback_data=f"hb:run:{name}")
            )
        if pipeline_buttons:
            buttons.append(pipeline_buttons)

    buttons.append([InlineKeyboardButton("🧠 Ask follow-up", callback_data="hb:followup")])

    return InlineKeyboardMarkup(buttons) if buttons else None


async def _handle_heartbeat_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle inline button presses from heartbeat messages."""
    query = update.callback_query
    if not query or not query.data or not query.data.startswith("hb:"):
        return
    await query.answer()

    action = query.data[3:]  # strip "hb:"

    if action.startswith("run:"):
        pipeline_name = action[4:]
        await query.message.reply_text(f"🔄 Retrying {pipeline_name}...")
        # Import and run pipeline
        try:
            from plugins.devops import _run_pipeline
            success, msg = await _run_pipeline(pipeline_name)
            await query.message.reply_text(msg)
        except Exception as e:
            await query.message.reply_text(f"Failed: {e}")

    elif action == "followup":
        await query.message.reply_text(
            "Just type your question in this chat — Claude will have the heartbeat context."
        )


# ---------------------------------------------------------------------------
# Scheduled jobs
# ---------------------------------------------------------------------------

async def _send_heartbeat(context: ContextTypes.DEFAULT_TYPE, period: str = "morning"):
    """Job callback — run heartbeat and send to chat."""
    cfg = get_config()
    chat_id = cfg["telegram"]["chat_id"]

    log.info("Running %s heartbeat", period)
    briefing = await _run_heartbeat(period=period)

    # Determine if we need action buttons
    pipelines = _gather_pipelines()
    has_failed = "NOT run" in pipelines

    buttons = _build_action_buttons(has_failed)

    await context.bot.send_message(
        chat_id=chat_id,
        text=briefing,
        reply_markup=buttons,
        disable_web_page_preview=True,
    )


async def _morning_heartbeat(context: ContextTypes.DEFAULT_TYPE):
    await _send_heartbeat(context, "morning")


async def _evening_heartbeat(context: ContextTypes.DEFAULT_TYPE):
    await _send_heartbeat(context, "evening")


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

async def cmd_think(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Run heartbeat agent on demand, optionally with a specific question."""
    if not update.message:
        return

    question = " ".join(context.args) if context.args else None
    await update.message.reply_text("🧠 Thinking..." if question else "🧠 Running heartbeat...")

    briefing = await _run_heartbeat(question=question, period="on-demand")

    pipelines = _gather_pipelines()
    has_failed = "NOT run" in pipelines
    buttons = _build_action_buttons(has_failed)

    await update.message.reply_text(
        briefing,
        reply_markup=buttons,
        disable_web_page_preview=True,
    )


async def cmd_heartbeat(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show heartbeat schedule."""
    if not update.message:
        return

    cfg = get_config()
    sched = cfg["schedule"]
    tz_name = sched["timezone"]
    tz = ZoneInfo(tz_name)
    now = datetime.now(tz)

    lines = [
        f"💓 Heartbeat Schedule ({tz_name})\n",
        f"  Morning: {sched['morning_hour']:02d}:{sched['morning_minute']:02d}",
        f"  Evening: {sched['evening_hour']:02d}:{sched['evening_minute']:02d}",
        f"\n  Now: {now.strftime('%H:%M')}",
        f"\n  /think — run now",
        f"  /think <question> — ask the agent something",
    ]
    await update.message.reply_text("\n".join(lines))


# ---------------------------------------------------------------------------
# Monthly snapshot
# ---------------------------------------------------------------------------

MONTHLY_SNAPSHOT_SCRIPT = str(PROJECT_ROOT / "finances" / "scripts" / "monthly_snapshot.py")


async def _run_monthly_snapshot() -> str:
    """Run monthly_snapshot.py and return output."""
    result = subprocess.run(
        [VENV_PY, MONTHLY_SNAPSHOT_SCRIPT],
        capture_output=True, text=True, timeout=120,
    )
    if result.returncode != 0:
        return f"Snapshot failed: {result.stderr[:500]}"
    return result.stdout.strip()



async def cmd_snapshot(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Record a net worth snapshot on demand."""
    if not update.message:
        return

    await update.message.reply_text("Recording net worth snapshot...")

    result = subprocess.run(
        [VENV_PY, MONTHLY_SNAPSHOT_SCRIPT],
        capture_output=True, text=True, timeout=120,
    )

    if result.returncode == 0:
        await update.message.reply_text(result.stdout.strip())
    else:
        await update.message.reply_text(f"Failed: {result.stderr[:500]}")


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------

async def handle_command(msg, reply) -> bool:
    """Cross-platform heartbeat commands."""
    if msg.command == "think":
        question = " ".join(msg.args) if msg.args else None
        await reply.reply_text("Thinking..." if question else "Running heartbeat...")
        briefing = await _run_heartbeat(question=question, period="on-demand")
        if len(briefing) > 4000:
            briefing = briefing[:3997] + "..."
        await reply.reply_text(briefing)
        return True

    elif msg.command == "heartbeat":
        cfg = get_config()
        sched = cfg["schedule"]
        tz_name = sched["timezone"]
        tz = ZoneInfo(tz_name)
        now = datetime.now(tz)
        lines = [
            f"Heartbeat Schedule ({tz_name})\n",
            f"  Morning: {sched['morning_hour']:02d}:{sched['morning_minute']:02d}",
            f"  Evening: {sched['evening_hour']:02d}:{sched['evening_minute']:02d}",
            f"\n  Now: {now.strftime('%H:%M')}",
            f"\n  /think -- run now",
        ]
        await reply.reply_text("\n".join(lines))
        return True

    elif msg.command == "snapshot":
        await reply.reply_text("Recording net worth snapshot...")
        result = subprocess.run(
            [VENV_PY, MONTHLY_SNAPSHOT_SCRIPT],
            capture_output=True, text=True, timeout=120,
        )
        if result.returncode == 0:
            await reply.reply_text(result.stdout.strip())
        else:
            await reply.reply_error(f"Failed: {result.stderr[:500]}")
        return True

    return False


def register(app: Application):
    app.add_handler(CommandHandler("think", cmd_think))
    app.add_handler(CommandHandler("heartbeat", cmd_heartbeat))
    app.add_handler(CallbackQueryHandler(_handle_heartbeat_callback, pattern=r"^hb:"))

    cfg = get_config()
    sched = cfg.get("schedule", {})
    tz = ZoneInfo(sched.get("timezone", "Asia/Kuala_Lumpur"))

    # Morning heartbeat — 5 min after morning pipelines so digests are ready
    morning_h = sched.get("morning_hour", 6)
    morning_m = sched.get("morning_minute", 0) + 5
    if morning_m >= 60:
        morning_h += 1
        morning_m -= 60

    app.job_queue.run_daily(
        _morning_heartbeat,
        time=time(morning_h, morning_m, tzinfo=tz),
        name="heartbeat_morning",
    )

    # Evening heartbeat — 5 min after evening pipelines
    evening_h = sched.get("evening_hour", 20)
    evening_m = sched.get("evening_minute", 0) + 5
    if evening_m >= 60:
        evening_h += 1
        evening_m -= 60

    app.job_queue.run_daily(
        _evening_heartbeat,
        time=time(evening_h, evening_m, tzinfo=tz),
        name="heartbeat_evening",
    )

    app.add_handler(CommandHandler("snapshot", cmd_snapshot))

    log.info(
        "Heartbeat scheduled: morning %02d:%02d, evening %02d:%02d (%s)",
        morning_h, morning_m, evening_h, evening_m, tz,
    )
