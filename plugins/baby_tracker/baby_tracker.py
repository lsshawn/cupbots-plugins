"""
Baby Tracker

Commands (scoped to baby topic thread 147):
  /feed <ml|left|right|pump>  — Log feeding
  /sleep                      — Toggle sleep start/stop
  /diaper <wet|poop|both>     — Log diaper
  /milestone <text>           — Log milestone
  /stats                      — Today's summary
  /analyze [days]             — Chart for N days
  /ask <question>             — Ask Claude Code (continues session)
  /undo                       — Delete last entry
  /baby                       — Show baby commands
"""

import asyncio
import io
import json
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup, Update
from telegram.constants import ChatAction
from telegram.ext import Application, CallbackQueryHandler, ContextTypes, MessageHandler, filters

from cupbots.config import get_config as _get_cfg
BABY_DIR = Path(_get_cfg().get("allowed_paths", {}).get("notes", "/home/ss/projects/note")) / "baby-tracker"

sys.path.insert(0, str(BABY_DIR))
import db
import charts
from cupbots.topic_filter import topic_command
from cupbots.config import get_config, get_thread_id, get_data_dir
from cupbots.helpers.logger import get_logger
from cupbots.helpers.llm import run_claude_cli

log = get_logger("baby")
BABY_THREAD_ID = get_thread_id("baby") or 147

# Track last action for /undo
_last_action = {"table": None, "id": None}

# Track Claude Code session per chat for follow-up questions
_claude_sessions: dict[int, str] = {}  # chat_id -> session_id

# Protects _last_action and _claude_sessions from concurrent access
_state_lock = asyncio.Lock()


def _format_duration(td: timedelta) -> str:
    total_min = int(td.total_seconds() / 60)
    hours, mins = divmod(total_min, 60)
    if hours > 0:
        return f"{hours}h {mins}m"
    return f"{mins}m"


async def cmd_feed(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args or []
    feed_type = "bottle"
    amount_ml = None
    duration_min = None
    note = None

    if args:
        first = args[0].lower()
        if first in ("left", "breast_left", "l"):
            feed_type = "breast_left"
        elif first in ("right", "breast_right", "r"):
            feed_type = "breast_right"
        elif first in ("pump", "p"):
            feed_type = "pump"
        elif first in ("bottle", "b"):
            feed_type = "bottle"
        else:
            try:
                amount_ml = int(first.replace("ml", ""))
                feed_type = "bottle"
            except ValueError:
                note = " ".join(args)

        if len(args) > 1:
            try:
                val = int(args[1].replace("ml", "").replace("min", ""))
                if feed_type in ("breast_left", "breast_right"):
                    duration_min = val
                else:
                    amount_ml = val
            except ValueError:
                note = " ".join(args[1:])

    row_id = db.log_feeding(feed_type, amount_ml, duration_min, note)
    async with _state_lock:
        _last_action["table"] = "feedings"
        _last_action["id"] = row_id

    emoji = {"breast_left": "🤱⬅️", "breast_right": "🤱➡️", "bottle": "🍼", "pump": "🧴"}.get(feed_type, "🍼")
    parts = [f"{emoji} Feeding logged: *{feed_type}*"]
    if amount_ml:
        parts.append(f"{amount_ml}ml")
    if duration_min:
        parts.append(f"{duration_min}min")

    stats = db.get_today_stats()
    parts.append(f"\n📊 Today: {stats['feeding_count']} feedings")
    if stats["total_bottle_ml"] > 0:
        parts.append(f"Total bottle: {stats['total_bottle_ml']}ml")

    await update.message.reply_text(
        " | ".join(parts[:3]) + ("\n" + "\n".join(parts[3:]) if len(parts) > 3 else ""),
        parse_mode="Markdown",
    )


async def cmd_sleep(update: Update, context: ContextTypes.DEFAULT_TYPE):
    current = db.is_sleeping()
    if current:
        ended = db.log_sleep_end()
        if ended:
            start = datetime.fromisoformat(ended["started_at"])
            end = datetime.fromisoformat(ended["ended_at"])
            duration = end - start
            async with _state_lock:
                _last_action["table"] = "sleeps"
                _last_action["id"] = ended["id"]
            await update.message.reply_text(
                f"☀️ Baby woke up!\nSlept for *{_format_duration(duration)}*\n"
                f"({start.strftime('%H:%M')} → {end.strftime('%H:%M')})",
                parse_mode="Markdown",
            )
    else:
        note = " ".join(context.args) if context.args else None
        row_id = db.log_sleep_start(note)
        async with _state_lock:
            _last_action["table"] = "sleeps"
            _last_action["id"] = row_id
        await update.message.reply_text(
            f"😴 Baby sleeping since *{datetime.now().strftime('%H:%M')}*",
            parse_mode="Markdown",
        )


async def cmd_diaper(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args or []
    diaper_type = "wet"
    note = None

    if args:
        first = args[0].lower()
        if first in ("poop", "p", "💩"):
            diaper_type = "poop"
        elif first in ("both", "b"):
            diaper_type = "both"
        elif first in ("wet", "w"):
            diaper_type = "wet"
        else:
            note = " ".join(args)
        if len(args) > 1:
            note = " ".join(args[1:])

    row_id = db.log_diaper(diaper_type, note)
    async with _state_lock:
        _last_action["table"] = "diapers"
        _last_action["id"] = row_id

    emoji = {"wet": "💧", "poop": "💩", "both": "💧💩"}.get(diaper_type, "💧")
    stats = db.get_today_stats()
    await update.message.reply_text(
        f"{emoji} Diaper logged: *{diaper_type}*\n"
        f"📊 Today: {stats['wet_count']} wet, {stats['poop_count']} poop",
        parse_mode="Markdown",
    )


async def cmd_milestone(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: /milestone first smile")
        return
    desc = " ".join(context.args)
    row_id = db.log_milestone(desc)
    async with _state_lock:
        _last_action["table"] = "milestones"
        _last_action["id"] = row_id
    await update.message.reply_text(f"⭐ Milestone logged: *{desc}*", parse_mode="Markdown")


async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    stats = db.get_today_stats()
    lines = [f"📋 *Today's Summary* ({stats['date']})", ""]
    lines.append(f"🍼 *Feedings:* {stats['feeding_count']}")
    if stats["total_bottle_ml"] > 0:
        lines.append(f"   Bottle total: {stats['total_bottle_ml']}ml")
    if stats["since_last_feed"]:
        lines.append(f"   Last feed: {_format_duration(stats['since_last_feed'])} ago")
    lines.append(f"\n😴 *Sleep:* {stats['sleep_count']} naps, {stats['total_sleep_min']}min total")
    if stats["currently_sleeping"]:
        lines.append("   💤 Currently sleeping")
    lines.append(f"\n🧷 *Diapers:* {stats['diaper_count']} total")
    lines.append(f"   💧 {stats['wet_count']} wet, 💩 {stats['poop_count']} poop")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


async def cmd_analyze(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if context.args:
        # Direct usage: /analyze 14
        try:
            days = int(context.args[0])
        except ValueError:
            days = 7
        await _generate_chart(update.message, days)
    else:
        # Show buttons
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("7 days", callback_data="analyze:7"),
            InlineKeyboardButton("14 days", callback_data="analyze:14"),
            InlineKeyboardButton("30 days", callback_data="analyze:30"),
        ]])
        await update.message.reply_text("Chart for how many days?", reply_markup=kb)


async def _generate_chart(msg_or_query, days: int):
    """Generate and send chart. Works with both Message and CallbackQuery."""
    try:
        png_bytes = charts.generate_weekly_chart(days)
        # If called from callback, we need to use the message's chat
        if hasattr(msg_or_query, 'message'):
            await msg_or_query.edit_message_text(f"📊 Generating {days}-day report...")
            await msg_or_query.message.reply_photo(
                photo=io.BytesIO(png_bytes),
                caption=f"Baby Tracker — Last {days} days",
            )
        else:
            await msg_or_query.reply_text(f"📊 Generating {days}-day report...")
            await msg_or_query.reply_photo(
                photo=io.BytesIO(png_bytes),
                caption=f"Baby Tracker — Last {days} days",
            )
    except Exception as e:
        text = f"Chart generation failed: {e}"
        if hasattr(msg_or_query, 'message'):
            await msg_or_query.edit_message_text(text)
        else:
            await msg_or_query.reply_text(text)


async def _callback_analyze(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    days = int(query.data.split(":")[1])
    await _generate_chart(query, days)


async def _build_baby_context() -> str:
    """Build a summary of recent baby data for Claude Code context."""
    stats = db.get_today_stats()
    weekly = db.get_range_data(7)

    context_text = (
        f"Today's stats:\n"
        f"- Feedings: {stats['feeding_count']} (bottle total: {stats['total_bottle_ml']}ml)\n"
        f"- Sleep: {stats['total_sleep_min']}min across {stats['sleep_count']} naps\n"
        f"- Diapers: {stats['wet_count']} wet, {stats['poop_count']} poop\n\nLast 7 days:\n"
    )
    for d in weekly:
        context_text += (
            f"- {d['date']}: {d['feeding_count']} feeds, "
            f"{d['total_sleep_hours']}h sleep, "
            f"{d['diaper_count']} diapers ({d['wet_count']}W/{d['poop_count']}P), "
            f"{d['total_bottle_ml']}ml bottle\n"
        )
    return context_text


async def _run_claude(question: str, session_id: str | None = None) -> tuple[str, str]:
    """Run Claude Code CLI and return (response_text, session_id)."""
    db_path = str(get_data_dir() / "baby.db")
    cfg = get_config().get("claude", {})

    try:
        result = await run_claude_cli(
            question,
            model=cfg.get("model", "sonnet"),
            system_prompt=(
                "You are a helpful baby care assistant. Keep responses concise and practical. "
                f"The baby tracking SQLite database is at {db_path} — you can query it directly "
                "for detailed analysis. If data is insufficient, say so."
            ),
            tools="Bash,Read",
            max_turns=cfg.get("max_turns", 10),
            max_budget_usd=str(cfg.get("max_budget_usd", "0.50")),
            session_id=session_id,
            cwd=str(BABY_DIR),
            timeout=120,
        )
        return result["text"], result["session_id"]
    except RuntimeError as e:
        return str(e), ""


async def cmd_ask(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text(
            "Usage:\n"
            "`/ask is the sleep pattern improving?`\n"
            "`/ask new what's the feeding trend?` — start fresh session",
            parse_mode="Markdown",
        )
        return

    args = list(context.args)
    chat_id = update.effective_chat.id

    # /ask new ... forces a fresh session
    async with _state_lock:
        if args[0].lower() == "new":
            _claude_sessions.pop(chat_id, None)
            args = args[1:]
            if not args:
                await update.message.reply_text("Usage: /ask new [your question]")
                return

        question = " ".join(args)
        session_id = _claude_sessions.get(chat_id)

    # For new sessions, prepend context data so Claude has it
    if not session_id:
        context_text = await _build_baby_context()
        question = f"TRACKING DATA:\n{context_text}\n\nQUESTION: {question}"

    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action=ChatAction.TYPING)
    await update.message.reply_text("🤔 Thinking...")

    try:
        response_text, new_session_id = await _run_claude(question, session_id)
        if new_session_id:
            async with _state_lock:
                _claude_sessions[chat_id] = new_session_id
        # Telegram message limit is 4096 chars
        if len(response_text) > 4000:
            response_text = response_text[:4000] + "\n\n... (truncated)"
        await update.message.reply_text(response_text)
    except asyncio.TimeoutError:
        await update.message.reply_text("Claude timed out after 2 minutes. Try a simpler question.")
    except Exception as e:
        await update.message.reply_text(f"Error: {e}")


async def cmd_undo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    async with _state_lock:
        table = _last_action["table"]
        row_id = _last_action["id"]
    if not table or not row_id:
        await update.message.reply_text("Nothing to undo.")
        return
    conn = db.get_db()
    conn.execute(f"DELETE FROM {table} WHERE id = ?", (row_id,))
    conn.commit()
    conn.close()
    await update.message.reply_text(
        f"↩️ Undone last entry from *{table}* (#{row_id})",
        parse_mode="Markdown",
    )
    async with _state_lock:
        _last_action["table"] = None
        _last_action["id"] = None


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🍼 *Baby Tracker Commands*\n\n"
        "`/feed` — Log bottle feeding\n"
        "`/feed left` or `/feed l` — Breast left\n"
        "`/feed right` or `/feed r` — Breast right\n"
        "`/feed 120` — Bottle 120ml\n"
        "`/feed pump 90` — Pump 90ml\n\n"
        "`/sleep` — Toggle sleep (start/stop)\n\n"
        "`/diaper` — Log wet diaper\n"
        "`/diaper poop` — Log poop\n"
        "`/diaper both` — Log both\n\n"
        "`/milestone first smile` — Log milestone\n"
        "`/stats` — Today's summary\n"
        "`/analyze` — Weekly chart\n"
        "`/analyze 14` — 14-day chart\n"
        "`/ask [question]` — Ask Claude Code (continues session)\n"
        "`/ask new [question]` — Start fresh Claude session\n"
        "`/undo` — Delete last entry\n",
        parse_mode="Markdown",
    )


# Reply keyboard for quick logging
BABY_KEYBOARD = ReplyKeyboardMarkup(
    [
        ["🤱 Left", "🤱 Right", "🍼 Bottle"],
        ["😴 Sleep", "💧 Wet", "💩 Poop"],
        ["📊 Stats", "↩️ Undo"],
    ],
    resize_keyboard=True,
    selective=True,
)

# Map button text to actions
BUTTON_ACTIONS = {
    "🤱 Left": ("feed", ["left"]),
    "🤱 Right": ("feed", ["right"]),
    "🍼 Bottle": ("feed", []),
    "😴 Sleep": ("sleep", []),
    "💧 Wet": ("diaper", ["wet"]),
    "💩 Poop": ("diaper", ["poop"]),
    "📊 Stats": ("stats", []),
    "↩️ Undo": ("undo", []),
}

COMMAND_MAP = {
    "feed": cmd_feed,
    "sleep": cmd_sleep,
    "diaper": cmd_diaper,
    "stats": cmd_stats,
    "undo": cmd_undo,
}


async def _handle_baby_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle reply keyboard button presses in the baby topic."""
    msg = update.message
    if not msg or msg.message_thread_id != BABY_THREAD_ID:
        return
    text = msg.text
    if text not in BUTTON_ACTIONS:
        return
    cmd_name, args = BUTTON_ACTIONS[text]
    handler = COMMAND_MAP.get(cmd_name)
    if handler:
        context.args = args
        await handler(update, context)


async def cmd_keyboard(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show the baby tracker quick-action keyboard."""
    await update.message.reply_text("Baby tracker keyboard active.", reply_markup=BABY_KEYBOARD)


async def handle_command(msg, reply) -> bool:
    """Cross-platform baby tracker commands."""
    args = msg.args or []

    if msg.command == "feed":
        feed_type = "bottle"
        amount_ml = None
        duration_min = None
        note = None
        if args:
            first = args[0].lower()
            if first in ("left", "breast_left", "l"):
                feed_type = "breast_left"
            elif first in ("right", "breast_right", "r"):
                feed_type = "breast_right"
            elif first in ("pump", "p"):
                feed_type = "pump"
            elif first in ("bottle", "b"):
                feed_type = "bottle"
            else:
                try:
                    amount_ml = int(first.replace("ml", ""))
                    feed_type = "bottle"
                except ValueError:
                    note = " ".join(args)
            if len(args) > 1:
                try:
                    val = int(args[1].replace("ml", "").replace("min", ""))
                    if feed_type in ("breast_left", "breast_right"):
                        duration_min = val
                    else:
                        amount_ml = val
                except ValueError:
                    note = " ".join(args[1:])
        row_id = db.log_feeding(feed_type, amount_ml, duration_min, note)
        stats = db.get_today_stats()
        parts = [f"Feeding logged: {feed_type}"]
        if amount_ml:
            parts.append(f"{amount_ml}ml")
        if duration_min:
            parts.append(f"{duration_min}min")
        parts.append(f"\nToday: {stats['feeding_count']} feedings")
        if stats["total_bottle_ml"] > 0:
            parts.append(f"Total bottle: {stats['total_bottle_ml']}ml")
        await reply.reply_text(" | ".join(parts[:3]) + ("\n" + "\n".join(parts[3:]) if len(parts) > 3 else ""))
        return True

    elif msg.command == "sleep":
        current = db.is_sleeping()
        if current:
            ended = db.log_sleep_end()
            if ended:
                start = datetime.fromisoformat(ended["started_at"])
                end = datetime.fromisoformat(ended["ended_at"])
                duration = end - start
                await reply.reply_text(
                    f"Baby woke up! Slept for {_format_duration(duration)}\n"
                    f"({start.strftime('%H:%M')} -> {end.strftime('%H:%M')})")
        else:
            note = " ".join(args) if args else None
            db.log_sleep_start(note)
            await reply.reply_text(f"Baby sleeping since {datetime.now().strftime('%H:%M')}")
        return True

    elif msg.command == "diaper":
        diaper_type = "wet"
        note = None
        if args:
            first = args[0].lower()
            if first in ("poop", "p"):
                diaper_type = "poop"
            elif first in ("both", "b"):
                diaper_type = "both"
            elif first in ("wet", "w"):
                diaper_type = "wet"
            else:
                note = " ".join(args)
            if len(args) > 1:
                note = " ".join(args[1:])
        db.log_diaper(diaper_type, note)
        stats = db.get_today_stats()
        await reply.reply_text(
            f"Diaper logged: {diaper_type}\n"
            f"Today: {stats['wet_count']} wet, {stats['poop_count']} poop")
        return True

    elif msg.command == "milestone":
        if not args:
            await reply.reply_text("Usage: /milestone first smile")
            return True
        desc = " ".join(args)
        db.log_milestone(desc)
        await reply.reply_text(f"Milestone logged: {desc}")
        return True

    elif msg.command == "stats":
        stats = db.get_today_stats()
        lines = [f"Today's Summary ({stats['date']})", ""]
        lines.append(f"Feedings: {stats['feeding_count']}")
        if stats["total_bottle_ml"] > 0:
            lines.append(f"  Bottle total: {stats['total_bottle_ml']}ml")
        if stats["since_last_feed"]:
            lines.append(f"  Last feed: {_format_duration(stats['since_last_feed'])} ago")
        lines.append(f"\nSleep: {stats['sleep_count']} naps, {stats['total_sleep_min']}min total")
        if stats["currently_sleeping"]:
            lines.append("  Currently sleeping")
        lines.append(f"\nDiapers: {stats['diaper_count']} total")
        lines.append(f"  {stats['wet_count']} wet, {stats['poop_count']} poop")
        await reply.reply_text("\n".join(lines))
        return True

    elif msg.command == "analyze":
        days = 7
        if args:
            try:
                days = int(args[0])
            except ValueError:
                pass
        try:
            png_bytes = charts.generate_weekly_chart(days)
            # For WhatsApp, charts need image sending support — fallback to text
            weekly = db.get_range_data(days)
            lines = [f"Baby Tracker -- Last {days} days:\n"]
            for d in weekly:
                lines.append(
                    f"{d['date']}: {d['feeding_count']} feeds, "
                    f"{d['total_sleep_hours']}h sleep, "
                    f"{d['diaper_count']} diapers ({d['wet_count']}W/{d['poop_count']}P)")
            await reply.reply_text("\n".join(lines))
        except Exception as e:
            await reply.reply_error(f"Analysis failed: {e}")
        return True

    elif msg.command == "ask":
        if not args:
            await reply.reply_text("Usage: /ask is the sleep pattern improving?")
            return True
        from cupbots.helpers.llm import chat_sessions
        question = " ".join(args)
        session_id = chat_sessions.get_session(msg.chat_id, context_key="baby")
        if not session_id:
            context_text = await _build_baby_context()
            question = f"TRACKING DATA:\n{context_text}\n\nQUESTION: {question}"
        await reply.reply_text("Thinking...")
        try:
            response_text, new_session_id = await _run_claude(question, session_id)
            if new_session_id:
                chat_sessions.set_session(msg.chat_id, new_session_id, context_key="baby")
            if len(response_text) > 4000:
                response_text = response_text[:3997] + "..."
            await reply.reply_text(response_text)
        except Exception as e:
            await reply.reply_error(f"Error: {e}")
        return True

    elif msg.command == "undo":
        async with _state_lock:
            table = _last_action["table"]
            row_id = _last_action["id"]
        if not table or not row_id:
            await reply.reply_text("Nothing to undo.")
            return True
        conn = db.get_db()
        conn.execute(f"DELETE FROM {table} WHERE id = ?", (row_id,))
        conn.commit()
        conn.close()
        await reply.reply_text(f"Undone last entry from {table} (#{row_id})")
        async with _state_lock:
            _last_action["table"] = None
            _last_action["id"] = None
        return True

    elif msg.command == "baby":
        await reply.reply_text(
            "Baby Tracker Commands\n\n"
            "/feed -- Log bottle feeding\n"
            "/feed left / right -- Breast feed\n"
            "/feed 120 -- Bottle 120ml\n"
            "/sleep -- Toggle sleep start/stop\n"
            "/diaper / /diaper poop / /diaper both\n"
            "/milestone <text>\n"
            "/stats -- Today's summary\n"
            "/analyze [days] -- Summary\n"
            "/ask <question> -- Ask Claude\n"
            "/undo -- Delete last entry")
        return True

    return False


def register(app: Application):
    """Register all baby tracker commands, scoped to the baby topic thread."""
    commands = {
        "feed": cmd_feed,
        "sleep": cmd_sleep,
        "diaper": cmd_diaper,
        "milestone": cmd_milestone,
        "stats": cmd_stats,
        "analyze": cmd_analyze,
        "ask": cmd_ask,
        "undo": cmd_undo,
        "baby": cmd_help,
        "keyboard": cmd_keyboard,
    }
    for name, handler in commands.items():
        app.add_handler(topic_command(name, handler, thread_id=BABY_THREAD_ID))

    app.add_handler(CallbackQueryHandler(_callback_analyze, pattern=r"^analyze:"))

    # Handle reply keyboard button presses (must be before the claude catch-all)
    app.add_handler(MessageHandler(
        filters.TEXT & filters.Regex(r"^(🤱|🍼|😴|💧|💩|📊|↩️)"),
        _handle_baby_button,
    ), group=50)
