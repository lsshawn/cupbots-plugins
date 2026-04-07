"""
WhatsApp Scheduled Messages — Schedule WhatsApp messages with AI-parsed natural language.

Commands (works in any topic):
  /wa schedule <everything>  — Schedule a WhatsApp message (AI-parsed)
  /wa scheduled              — List all pending scheduled messages
  /wa unschedule             — Delete scheduled messages

Usage examples:
  /wa schedule Ahmad remind about meeting tomorrow 9am
  /wa schedule 016 6338 8589 message is "hello" in 5 minutes
  /wa schedule Mom happy birthday! on march 25 midnight
"""

import asyncio
import json
import os
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import httpx
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
)

from cupbots.config import get_config
from cupbots.helpers.jobs import enqueue, register_handler, cancel_job, get_pending_jobs
from cupbots.helpers.logger import get_logger
from cupbots.helpers.llm import ask_llm, _extract_json

log = get_logger("wa-schedule")

WA_API = os.environ.get("WA_API_URL", "http://127.0.0.1:3100")
try:
    from cupbots.helpers.db import resolve_plugin_setting
    _tz_name = resolve_plugin_setting("whatsapp_schedule", "bot_timezone") or "Asia/Kuala_Lumpur"
except Exception:
    _tz_name = "Asia/Kuala_Lumpur"
TZ = ZoneInfo(_tz_name)


# --- WhatsApp API helpers ---

async def _api_get(path, params=None):
    async with httpx.AsyncClient(timeout=10) as client:
        r = await client.get(f"{WA_API}{path}", params=params)
        r.raise_for_status()
        return r.json()


async def _api_post(path, data=None):
    async with httpx.AsyncClient(timeout=10) as client:
        r = await client.post(f"{WA_API}{path}", json=data)
        r.raise_for_status()
        return r.json()


def _short_name(chat):
    name = chat.get("name") or chat.get("id", "")
    if not name or name == chat.get("id"):
        return chat["id"].replace("@s.whatsapp.net", "").replace("@g.us", " (group)")
    return name


# --- AI parsing: one-shot schedule intent ---

async def _parse_schedule_intent(raw: str, chats: list[dict]) -> dict | None:
    """Use Claude CLI to parse a natural language schedule command."""
    now = datetime.now(TZ)

    chat_list = []
    for i, c in enumerate(chats):
        name = _short_name(c)
        phone = c["id"].replace("@s.whatsapp.net", "").replace("@g.us", "")
        chat_list.append(f"  {i}: name={name}, phone={phone}")

    prompt = (
        f"Parse this WhatsApp schedule command:\n\n"
        f"  {raw}\n\n"
        f"Current date/time: {now.strftime('%Y-%m-%d %H:%M %A')}. Timezone: {TZ}.\n\n"
        f"Available chats:\n" + "\n".join(chat_list) + "\n\n"
        f"Reply with ONLY a JSON object:\n"
        f'{{"recipient_index": N, "message": "...", "datetime": "YYYY-MM-DD HH:MM"}}\n\n'
        f"Rules:\n"
        f"- Match the recipient by name OR phone number (partial match ok, e.g. '016 6338' matches '60166388589')\n"
        f"- Strip country code variations: '016...' = '6016...' = '+6016...'\n"
        f"- The message is what should be sent — extract it from quotes, or after 'message is', 'say', 'tell them', etc.\n"
        f"- If no explicit message delimiter, use your best judgment to separate recipient + time from the message body\n"
        f"- Parse relative times: 'in 5 minutes', '5 minutes later', 'tomorrow 9am', 'friday 2pm', etc.\n"
        f"- Interpret emoji shortcodes like :wink: :smile: :heart: as actual emoji\n"
        f"- If you cannot determine any field, reply: null\n"
        f"- Do NOT use any tools. Just reply with the JSON object."
    )

    log.info("Parsing schedule intent: %s", raw)

    try:
        text = await ask_llm(prompt, json_mode=False, timeout=30)
    except Exception as e:
        log.error("LLM parse error: %s", e)
        return None

    log.info("LLM response: %s", text[:500] if text else "(empty)")
    data = _extract_json(text)

    if not data or not isinstance(data, dict):
        log.warning("Could not extract dict from response: %s", text[:200])
        return None

    try:
        idx = int(data["recipient_index"])
        if idx < 0 or idx >= len(chats):
            log.error("recipient_index %d out of range (0-%d)", idx, len(chats) - 1)
            return None
        dt = datetime.strptime(data["datetime"], "%Y-%m-%d %H:%M")
        return {
            "recipient_index": idx,
            "message": data["message"],
            "send_at": dt.replace(tzinfo=TZ),
        }
    except (KeyError, ValueError, TypeError) as e:
        log.error("Failed to extract fields: %s — data: %s", e, data)
        return None


def _format_delta(delta: timedelta) -> str:
    secs = int(delta.total_seconds())
    if secs < 0:
        return "overdue"
    days = secs // 86400
    hours = (secs % 86400) // 3600
    mins = (secs % 3600) // 60
    if days > 0:
        return f"in {days}d {hours}h"
    if hours > 0:
        return f"in {hours}h {mins}m"
    return f"in {mins}m"


# --- Core logic (shared between Telegram and cross-platform) ---

async def _do_schedule(raw: str, tg_chat_id: int | None = None) -> str:
    """Schedule a WhatsApp message. Returns status text."""
    if not raw:
        return (
            "Usage: /wa schedule <who> <message> <when>\n\n"
            "Examples:\n"
            "  /wa schedule Ahmad remind about meeting tomorrow 9am\n"
            "  /wa schedule 016 6338 8589 message is \"hello\" in 5 minutes\n"
            "  /wa schedule Mom happy birthday! on march 25 midnight"
        )

    try:
        chats = await _api_get("/chats", {"limit": 30})
    except httpx.ConnectError:
        return "WhatsApp bot is not running."

    if not chats:
        return "No WhatsApp chats available."

    log.info("Scheduling: raw=%s, chats=%d", raw, len(chats))

    try:
        parsed = await _parse_schedule_intent(raw, chats)
    except Exception as e:
        return f"Parse error: {e}"

    if not parsed:
        return (
            "Couldn't parse that. Check bot logs for details.\n"
            "Try: /wa schedule Ahmad hello! tomorrow 9am"
        )

    chat = chats[parsed["recipient_index"]]
    chat_id = chat["id"]
    chat_name = _short_name(chat)
    message = parsed["message"]
    send_at = parsed["send_at"]
    now = datetime.now(TZ)

    if send_at <= now:
        return (
            f"Parsed time {send_at.strftime('%a %d %b %H:%M')} is in the past. "
            "Be more specific about when."
        )

    payload = {
        "chat_id": chat_id,
        "chat_name": chat_name,
        "message": message,
    }
    # Include Telegram notification info if available
    if tg_chat_id is not None:
        payload["tg_chat_id"] = tg_chat_id

    job_id = enqueue(
        queue="wa_send",
        payload=payload,
        run_at=send_at,
        max_attempts=3,
    )
    delta = send_at - now

    return (
        f"Scheduled message #{job_id}\n\n"
        f"To: {chat_name}\n"
        f"Message: {message}\n"
        f"When: {send_at.strftime('%a %d %b %H:%M')} ({_format_delta(delta)})"
    )


def _do_list_scheduled() -> tuple[str, list[dict]]:
    """List pending scheduled messages. Returns (text, all_msgs)."""
    jobs = get_pending_jobs(queue="wa_send")

    all_msgs = []
    for job in jobs:
        payload = json.loads(job["payload"]) if isinstance(job["payload"], str) else job["payload"]
        all_msgs.append({
            "id": f"J{job['id']}",
            "chat_id": payload.get("chat_id", ""),
            "chat_name": payload.get("chat_name", "?"),
            "message": payload.get("message", ""),
            "send_at": job["run_at"],
            "job_id": job["id"],
        })

    if not all_msgs:
        return "No scheduled messages.", all_msgs

    now = datetime.now(TZ)
    lines = [f"{len(all_msgs)} scheduled message(s):\n"]
    for m in all_msgs:
        send_at = datetime.fromisoformat(m["send_at"])
        delta = send_at - now
        msg_preview = m["message"][:50] + ("..." if len(m["message"]) > 50 else "")
        # Show recipient type (group vs DM) and identifier
        chat_id = m["chat_id"]
        if chat_id.endswith("@g.us"):
            recipient = f"{m['chat_name']} (group)"
        elif chat_id.endswith("@s.whatsapp.net"):
            phone = chat_id.replace("@s.whatsapp.net", "")
            recipient = f"{m['chat_name']} (+{phone})"
        else:
            recipient = m["chat_name"]
        lines.append(
            f"#{m['id']} -> {recipient}\n"
            f"  {msg_preview}\n"
            f"  {send_at.strftime('%a %d %b %H:%M')} ({_format_delta(delta)})"
        )
        lines.append("")

    return "\n".join(lines), all_msgs


# --- Job queue handler for wa_send ---

async def _handle_wa_send_job(payload: dict, bot=None):
    """Job queue handler — send a scheduled WhatsApp message."""
    chat_id = payload["chat_id"]
    chat_name = payload.get("chat_name", chat_id)
    message = payload["message"]

    await _api_post("/send", {"chatId": chat_id, "text": message})
    log.info("Sent scheduled WA message to %s", chat_name)

    # Notify Telegram if configured
    if bot and payload.get("tg_chat_id"):
        try:
            await bot.send_message(
                chat_id=payload["tg_chat_id"],
                text=f"Scheduled WhatsApp sent to *{chat_name}*:\n{message}",
                parse_mode="Markdown",
            )
        except Exception:
            pass


# --- Cross-platform handler (REQUIRED — enables WhatsApp, future platforms) ---

async def handle_command(msg, reply) -> bool:
    """Platform-agnostic command handler for /wa schedule subcommands.

Usage:
  /wa schedule <who> <message> <when>  — Schedule a WhatsApp message (AI-parsed)
  /wa scheduled                        — List all pending scheduled messages
  /wa unschedule                       — Delete all pending scheduled messages

Examples:
  /wa schedule Ahmad remind about meeting tomorrow 9am
  /wa schedule 016 6338 8589 message is "hello" in 5 minutes
  /wa schedule Mom happy birthday! on march 25 midnight
"""
    if msg.command != "wa":
        return False

    args = msg.args
    subcmd = args[0].lower() if args else ""

    if subcmd == "schedule":
        if len(args) > 1 and args[1] == "--help":
            await reply.reply_text(
                "Schedule a WhatsApp message with natural language.\n\n"
                "Usage: /wa schedule <who> <message> <when>\n\n"
                "Examples:\n"
                "  /wa schedule Ahmad remind about meeting tomorrow 9am\n"
                '  /wa schedule 016 6338 8589 message is "hello" in 5 minutes\n'
                "  /wa schedule Mom happy birthday! on march 25 midnight"
            )
            return True
        raw = " ".join(args[1:])
        result = await _do_schedule(raw)
        await reply.reply_text(result)
        return True

    elif subcmd == "scheduled":
        if len(args) > 1 and args[1] == "--help":
            await reply.reply_text(
                "List all pending scheduled WhatsApp messages.\n\n"
                "Usage: /wa scheduled\n\n"
                "Shows job ID, recipient (name + phone/group JID), message preview, and send time."
            )
            return True
        text, _ = _do_list_scheduled()
        await reply.reply_text(text)
        return True

    elif subcmd == "unschedule":
        if len(args) > 1 and args[1] == "--help":
            await reply.reply_text(
                "Delete all pending scheduled WhatsApp messages.\n\n"
                "Usage: /wa unschedule\n\n"
                "Cancels every pending message in the schedule queue."
            )
            return True
        jobs = get_pending_jobs(queue="wa_send")
        if not jobs:
            await reply.reply_text("No scheduled messages to delete.")
            return True
        # Cross-platform: cancel all (no inline keyboards)
        for job in jobs:
            cancel_job(job["id"])
        await reply.reply_text(f"Deleted {len(jobs)} scheduled message(s).")
        return True

    return False


# --- Telegram-specific handlers ---

async def _start_schedule(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """One-shot schedule: AI parses recipient, message, and time from natural language."""
    args = context.args[1:] if context.args else []  # skip 'schedule'
    raw = " ".join(args)

    if not raw:
        await update.message.reply_text(
            "Usage: `/wa schedule <who> <message> <when>`\n\n"
            "Examples:\n"
            "  `/wa schedule Ahmad remind about meeting tomorrow 9am`\n"
            "  `/wa schedule 016 6338 8589 message is \"hello\" in 5 minutes`\n"
            "  `/wa schedule Mom happy birthday! on march 25 midnight`",
            parse_mode="Markdown",
        )
        return

    await update.message.reply_text("Parsing...")

    cfg = get_config()
    tg_chat_id = int(cfg["telegram"]["chat_id"])
    result = await _do_schedule(raw, tg_chat_id=tg_chat_id)
    await update.message.reply_text(result, parse_mode="Markdown")


async def _list_scheduled(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text, all_msgs = _do_list_scheduled()
    await update.message.reply_text(text, parse_mode="Markdown")


async def _unschedule(update: Update, context: ContextTypes.DEFAULT_TYPE):
    jobs = get_pending_jobs(queue="wa_send")

    if not jobs:
        await update.message.reply_text("No scheduled messages to delete.")
        return

    buttons = []
    for job in jobs:
        payload = json.loads(job["payload"]) if isinstance(job["payload"], str) else job["payload"]
        msg_preview = payload.get("message", "")[:30]
        label = f"#{job['id']} {payload.get('chat_name', '?')} — {msg_preview}"
        buttons.append(
            [InlineKeyboardButton(f"Delete {label}", callback_data=f"wasched:jdel:{job['id']}")]
        )

    buttons.append(
        [InlineKeyboardButton(f"Delete all ({len(jobs)})", callback_data="wasched:delall:0")]
    )

    await update.message.reply_text(
        "Tap to delete:",
        reply_markup=InlineKeyboardMarkup(buttons),
    )


async def _handle_schedule_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle inline keyboard callbacks for unscheduling."""
    query = update.callback_query
    await query.answer()

    parts = query.data.split(":")

    if parts[1] == "jdel":
        job_id = int(parts[2])
        if cancel_job(job_id):
            await query.edit_message_text(f"Deleted scheduled message #{job_id}")
        else:
            await query.edit_message_text("Already sent or not found.")

    elif parts[1] == "delall":
        jobs = get_pending_jobs(queue="wa_send")
        for job in jobs:
            cancel_job(job["id"])
        await query.edit_message_text(f"Deleted all {len(jobs)} scheduled message(s).")


# --- Hook into /wa subcommands ---

_original_cmd_wa = None


async def _cmd_wa_wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Intercept /wa to handle schedule subcommands, pass rest to original."""
    if not update.message:
        if _original_cmd_wa:
            await _original_cmd_wa(update, context)
        return

    args = context.args or []
    subcmd = args[0].lower() if args else ""

    if subcmd == "schedule":
        await _start_schedule(update, context)
    elif subcmd == "scheduled":
        await _list_scheduled(update, context)
    elif subcmd == "unschedule":
        await _unschedule(update, context)
    else:
        # Pass to original handler (includes --help, search, etc.)
        if _original_cmd_wa:
            await _original_cmd_wa(update, context)


def register(app: Application):
    global _original_cmd_wa

    # Register job queue handler for wa_send
    register_handler("wa_send", _handle_wa_send_job)

    # Find and wrap the existing /wa handler
    for group_handlers in app.handlers.values():
        for h in group_handlers:
            if isinstance(h, CommandHandler) and "wa" in h.commands:
                _original_cmd_wa = h.callback
                h.callback = _cmd_wa_wrapper
                break

    # Callback handler for unschedule inline buttons
    app.add_handler(CallbackQueryHandler(_handle_schedule_callback, pattern=r"^wasched:"))

    log.info("WhatsApp scheduled messages active")
