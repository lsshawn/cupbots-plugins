"""
WhatsApp

Commands (works in any topic):
  /wa              — Show WhatsApp status and recent chats
  /wa chats        — List chats with unread counts
  /wa read <n>     — Read last N messages from a chat
  /wa reply        — Reply to a WhatsApp chat
  /wa search <q>   — Search across all WhatsApp messages
  /wa <question>   — Ask anything about your WhatsApp messages (AI)
  /wa summary      — Summarize a WhatsApp chat using Claude
  /wa schedule <…> — Schedule a WhatsApp message (AI-parsed)
  /wa scheduled    — List all pending scheduled messages
  /wa unschedule   — Delete scheduled messages
  /wa sync         — Thorough sync of all chat history
  /wa pair         — Scan new QR code
  /wa reconnect    — Reconnect without re-pairing
"""

import asyncio
import os
from pathlib import Path

import httpx
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

from cupbots.config import get_config, get_thread_id
from cupbots.helpers.logger import get_logger
from cupbots.helpers.llm import run_claude_cli, add_history, get_history_context

log = get_logger("whatsapp")

WA_API = os.environ.get("WA_API_URL", "http://127.0.0.1:3100")
DEVOPS_THREAD_ID = get_thread_id("devops") or 134

# Conversation states
PICK_CHAT, WAITING_REPLY = range(2)

# Per-user pending state: user_id -> {action, chats, chat_id, ...}
_pending = {}
_pending_lock = asyncio.Lock()


async def _api_get(path, params=None):
    """GET request to WhatsApp API."""
    async with httpx.AsyncClient(timeout=10) as client:
        r = await client.get(f"{WA_API}{path}", params=params)
        r.raise_for_status()
        return r.json()


async def _api_post(path, data=None):
    """POST request to WhatsApp API."""
    async with httpx.AsyncClient(timeout=10) as client:
        r = await client.post(f"{WA_API}{path}", json=data)
        r.raise_for_status()
        return r.json()


def _format_phone(jid):
    """Convert JID to readable phone number."""
    return jid.replace("@s.whatsapp.net", "").replace("@g.us", " (group)")


def _short_name(chat):
    """Get short display name for a chat."""
    name = chat.get("name") or chat.get("id", "")
    if not name or name == chat.get("id"):
        return _format_phone(chat["id"])
    return name


async def cmd_wa(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Main /wa command — route to subcommands."""
    if not update.message:
        return

    args = context.args or []
    subcmd = args[0].lower() if args else "status"

    if subcmd in ("--help", "-h", "help"):
        await update.message.reply_text(__doc__.strip())
        return

    try:
        if subcmd == "status":
            await _wa_status(update)
        elif subcmd == "chats":
            await _wa_chats(update)
        elif subcmd == "read":
            limit = int(args[1]) if len(args) > 1 else 20
            await _wa_pick_chat(update, "read", limit=limit)
        elif subcmd == "reply":
            await _wa_pick_chat(update, "reply")
        elif subcmd == "search":
            query = " ".join(args[1:])
            if not query:
                await update.message.reply_text("Usage: /wa search <query>")
                return
            await _wa_search(update, query)
        elif subcmd == "summary":
            await _wa_pick_chat(update, "summary")
        elif subcmd == "sync":
            await _wa_sync(update, context)
        elif subcmd == "pair":
            await _wa_pair(update)
        elif subcmd == "reconnect":
            await _wa_reconnect(update)
        else:
            # Natural language query — use LLM to parse intent
            query = " ".join(args)
            await _wa_smart_search(update, query)
    except httpx.ConnectError:
        await update.message.reply_text("WhatsApp bot is not running.")
    except Exception as e:
        log.error("WhatsApp command error: %s", e)
        await update.message.reply_text(f"Error: {type(e).__name__}. See logs for details.")


async def _wa_pair(update: Update):
    """Trigger fresh QR pairing — clears auth and sends QR image to Telegram."""
    msg = await update.message.reply_text("🔄 Clearing session and generating new QR...")
    try:
        result = await _api_post("/pair")
        await msg.edit_text(
            "📱 Re-pairing WhatsApp...\n"
            "A QR code image will be sent here shortly. "
            "Scan it with WhatsApp > Linked Devices."
        )
        # Notify devops topic
        chat_id = int(get_config()["telegram"]["chat_id"])
        await update.get_bot().send_message(
            chat_id=chat_id,
            message_thread_id=DEVOPS_THREAD_ID,
            text="📱 WhatsApp re-pairing initiated. Waiting for QR scan.",
        )
    except Exception as e:
        await msg.edit_text(f"Pair failed: {e}")


async def _wa_reconnect(update: Update):
    """Reconnect without clearing auth (keeps existing session)."""
    msg = await update.message.reply_text("🔄 Reconnecting...")
    try:
        result = await _api_post("/reconnect")
        await msg.edit_text("✅ Reconnecting to WhatsApp...")
    except Exception as e:
        await msg.edit_text(f"Reconnect failed: {e}")


async def _wa_sync(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Trigger a thorough sync of all WhatsApp chats."""
    # Get current stats before sync
    try:
        pre_stats = await _api_get("/sync/stats")
        pre_count = pre_stats.get("totalMessages", 0)
    except Exception:
        pre_count = 0

    msg = await update.message.reply_text("🔄 Starting WhatsApp sync...")

    try:
        await _api_post("/sync")
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 409:
            status = await _api_get("/sync/status")
            await msg.edit_text(f"Sync already running: {status.get('progress', '?')}")
            return
        raise

    # Poll for progress
    for _ in range(120):  # 2 minutes max
        await asyncio.sleep(2)
        try:
            status = await _api_get("/sync/status")
        except Exception:
            continue

        running = status.get("running", False)
        progress = status.get("progress", "")
        total = status.get("totalMessages", 0)

        if not running:
            new_msgs = total - pre_count
            lines = [f"✅ Sync complete — {total} messages across {status.get('totalChats', '?')} chats"]
            if new_msgs > 0:
                lines.append(f"📥 {new_msgs} new messages synced")
            await msg.edit_text("\n".join(lines))
            return

        await msg.edit_text(f"🔄 {progress}\n📊 {total} messages so far")

    await msg.edit_text("⏰ Sync still running in background. Use /wa sync to check status.")


async def _wa_status(update: Update):
    """Show connection status and recent chats summary."""
    status = await _api_get("/status")
    chats = await _api_get("/chats", {"limit": 5})

    state = status.get("state", "unknown")
    icon = {"open": "🟢", "connecting": "🟡", "disconnected": "🔴"}.get(state, "⚪")

    lines = [f"{icon} WhatsApp: {state}"]

    if chats:
        lines.append("\nRecent chats:")
        for c in chats:
            unread = c.get("unread_count", 0)
            badge = f" ({unread} new)" if unread else ""
            lines.append(f"  • {_short_name(c)}{badge}")

    await update.message.reply_text("\n".join(lines))


async def _wa_chats(update: Update):
    """List all chats with unread counts."""
    chats = await _api_get("/chats", {"limit": 30})

    if not chats:
        await update.message.reply_text("No chats yet.")
        return

    lines = ["WhatsApp Chats:\n"]
    for i, c in enumerate(chats, 1):
        unread = c.get("unread_count", 0)
        badge = f" 🔴{unread}" if unread else ""
        group = " 👥" if c.get("is_group") else ""
        lines.append(f"{i}. {_short_name(c)}{group}{badge}")

    await update.message.reply_text("\n".join(lines))


async def _wa_pick_chat(update: Update, action: str, **kwargs):
    """Show inline keyboard to pick a chat."""
    chats = await _api_get("/chats", {"limit": 15})

    if not chats:
        await update.message.reply_text("No chats available.")
        return

    user_id = update.message.from_user.id
    async with _pending_lock:
        _pending[user_id] = {"action": action, "chats": chats, **kwargs}

    buttons = []
    for i, c in enumerate(chats):
        unread = c.get("unread_count", 0)
        badge = f" ({unread})" if unread else ""
        name = _short_name(c)
        buttons.append(
            [InlineKeyboardButton(f"{name}{badge}", callback_data=f"wa:{action}:{i}")]
        )

    await update.message.reply_text(
        f"Pick a chat to {action}:",
        reply_markup=InlineKeyboardMarkup(buttons),
    )


async def _handle_chat_pick(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle inline keyboard chat selection."""
    query = update.callback_query
    await query.answer()

    data = query.data  # wa:action:index
    parts = data.split(":")
    if len(parts) != 3 or parts[0] != "wa":
        return

    action = parts[1]
    idx = int(parts[2])
    user_id = query.from_user.id

    async with _pending_lock:
        pending = _pending.get(user_id)
        if not pending or pending["action"] != action:
            await query.edit_message_text("Session expired. Run the command again.")
            return
        chats = pending["chats"]
        limit = pending.get("limit", 20)

    if idx >= len(chats):
        await query.edit_message_text("Invalid selection.")
        return

    chat = chats[idx]
    chat_id = chat["id"]
    chat_name = _short_name(chat)

    try:
        if action == "read":
            await _do_read(query, chat_id, chat_name, limit)
        elif action == "reply":
            async with _pending_lock:
                _pending[user_id] = {"action": "waiting_reply", "chat_id": chat_id, "chat_name": chat_name}
            await query.edit_message_text(
                f"Reply to {chat_name}:\nType your message (or /cancel):"
            )
        elif action == "summary":
            await _do_summary(query, chat_id, chat_name)
    except Exception as e:
        log.error("WA chat pick error: %s", e)
        await query.edit_message_text(f"Error: {type(e).__name__}. See logs.")


async def _do_read(query, chat_id, chat_name, limit):
    """Read messages from a chat."""
    messages = await _api_get(f"/messages/{chat_id}", {"limit": limit})

    if not messages:
        await query.edit_message_text(f"No messages in {chat_name}.")
        return

    # Reverse to show oldest first
    messages.reverse()

    lines = [f"📱 {chat_name} (last {len(messages)} messages):\n"]
    for m in messages:
        sender = "You" if m.get("is_from_me") else (m.get("sender_name") or "?")
        text = m.get("content", "")
        media = f" [{m['media_type']}]" if m.get("media_type") else ""
        lines.append(f"[{sender}]{media} {text}")

    text = "\n".join(lines)
    if len(text) > 4000:
        text = text[:4000] + "\n\n... (truncated)"

    await query.edit_message_text(text)


async def _do_summary(query, chat_id, chat_name):
    """Summarize a chat using Claude."""
    await query.edit_message_text(f"Summarizing {chat_name}...")

    messages = await _api_get(f"/messages/{chat_id}", {"limit": 100})

    if not messages:
        await query.edit_message_text(f"No messages in {chat_name} to summarize.")
        return

    messages.reverse()

    # Build conversation text for Claude
    convo_lines = []
    for m in messages:
        sender = "You" if m.get("is_from_me") else (m.get("sender_name") or "?")
        convo_lines.append(f"{sender}: {m.get('content', '')}")
    convo_text = "\n".join(convo_lines)

    prompt = (
        f"Summarize this WhatsApp conversation from the chat '{chat_name}'. "
        "Include: key topics discussed, any action items or decisions made, "
        "and the overall tone. Be concise.\n\n"
        f"Conversation:\n{convo_text}"
    )

    try:
        from cupbots.helpers.llm import ask_llm
        summary = await ask_llm(prompt) or "Could not generate summary."
    except Exception as e:
        summary = f"Summary failed: {e}"

    text = f"📱 Summary: {chat_name}\n\n{summary}"
    if len(text) > 4000:
        text = text[:4000] + "\n\n... (truncated)"

    await query.edit_message_text(text)


async def _handle_reply_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle text message when user is in reply mode."""
    if not update.message or not update.message.from_user:
        return
    user_id = update.message.from_user.id

    async with _pending_lock:
        pending = _pending.get(user_id)
        if not pending or pending.get("action") != "waiting_reply":
            return
        # Copy what we need before releasing lock
        chat_id = pending["chat_id"]
        chat_name = pending["chat_name"]

    text = update.message.text
    if not text:
        return

    if text.strip().lower() == "/cancel":
        async with _pending_lock:
            _pending.pop(user_id, None)
        await update.message.reply_text("Reply cancelled.")
        return

    async with _pending_lock:
        _pending.pop(user_id, None)

    try:
        await _api_post("/send", {"chatId": chat_id, "text": text})
        await update.message.reply_text(f"✅ Sent to {chat_name}")
    except Exception as e:
        log.error("WA reply send error: %s", e)
        await update.message.reply_text(f"Failed to send: {type(e).__name__}")


async def _wa_smart_search(update: Update, query: str):
    """Delegate natural language WhatsApp query to Claude CLI."""
    cfg = get_config()
    from cupbots.config import get_repo_root
    project_root = Path(cfg.get("scripts_dir", str(get_repo_root())))
    claude_cfg = cfg.get("claude", {})

    # Prepend recent conversation context so short follow-ups work
    user_id = update.message.from_user.id if update.message.from_user else 0
    history = get_history_context(user_id)
    add_history(user_id, "user", query)  # store original, not history-prepended
    if history:
        query = history + "Current question: " + query

    thinking_msg = await update.message.reply_text("🔍 searching whatsapp...")

    wa_db = str(project_root) + "/scripts/whatsapp-bot/data/whatsapp.db"
    pb_url = "http://127.0.0.1:8091"

    system_prompt = (
        "You are a WhatsApp search assistant. Answer the user's question about their WhatsApp messages. "
        "Be concise — just give the answer, no preamble.\n\n"
        "WhatsApp DB: " + wa_db + "\n"
        "Contacts CRM: PocketBase at " + pb_url + "\n\n"
        "SCHEMA:\n"
        "  WhatsApp DB (sqlite3):\n"
        "    messages: id, chat_id, chat_name, sender_id, sender_name, content, media_type, timestamp (unix), is_from_me, quoted_id\n"
        "    chats: id, name, is_group, last_message_ts, unread_count, muted\n"
        "  Contacts CRM (PocketBase REST API):\n"
        "    contacts collection: id, name, handles, tags, notes, tier, location, last_contact, next_contact\n\n"
        "CRITICAL FACTS about this database:\n"
        "- sender_name is OFTEN a phone number (e.g. '+120363425055654179'), NOT a human name\n"
        "- chat_name in the messages table is OFTEN a phone number or group JID, NOT a human name\n"
        "- The chats table has the REAL group/chat name: SELECT name FROM chats WHERE id = '<chat_id>'\n"
        "- The contacts CRM has the REAL person name (use PocketBase API)\n"
        "- chat_id can be a phone JID (@s.whatsapp.net), group JID (@g.us), or Linked ID (@lid)\n"
        "- You CANNOT rely on sender_name to find who sent a message by name\n\n"
        "SEARCH STRATEGY (follow this EXACT order):\n"
        "1. ALWAYS start with a broad content search for keywords from the user's question:\n"
        "   sqlite3 " + wa_db + " \"SELECT content, sender_name, chat_id, datetime(timestamp,'unixepoch','localtime') as dt FROM messages WHERE content LIKE '%keyword%' COLLATE NOCASE ORDER BY timestamp DESC LIMIT 20\"\n"
        "2. If the user mentions a person's name, ALSO look up their phone in the CRM:\n"
        "   curl '" + pb_url + "/api/collections/contacts/records?filter=name~\"name\"&perPage=5'\n"
        "3. If CRM returns a phone (in handles field), search messages by that phone in sender_name or chat_id:\n"
        "   sqlite3 " + wa_db + " \"SELECT content, sender_name, chat_id, datetime(timestamp,'unixepoch','localtime') as dt FROM messages WHERE (sender_name LIKE '%phone%' OR chat_id LIKE '%phone%') ORDER BY timestamp DESC LIMIT 20\"\n"
        "4. ALSO try searching by name directly (sometimes sender_name IS a real name):\n"
        "   sqlite3 " + wa_db + " \"SELECT content, sender_name, chat_id, datetime(timestamp,'unixepoch','localtime') as dt FROM messages WHERE sender_name LIKE '%name%' COLLATE NOCASE ORDER BY timestamp DESC LIMIT 20\"\n"
        "5. Cross-reference: if step 1 found messages and step 2-4 found a phone/name, check if any messages from step 1 have matching sender_name or chat_id.\n\n"
        "RESOLVING NAMES (do this BEFORE responding to the user):\n"
        "- For chat names: sqlite3 " + wa_db + " \"SELECT name FROM chats WHERE id = '<chat_id>'\"\n"
        "- For sender names that are phone numbers: curl '" + pb_url + "/api/collections/contacts/records?filter=handles~\"<phone_digits>\"&perPage=1'\n"
        "  Strip the + and leading digits as needed — e.g. for '+275088377151634', try handles~'275088377151634'\n"
        "- NEVER show raw JIDs or phone-number sender_names to the user. Always resolve to human names first.\n\n"
        "NEVER say 'no results' until you have tried ALL steps above. The content search (step 1) is the most reliable.\n\n"
        "is_from_me=1 means the USER (bot owner) sent it. is_from_me=0 means the OTHER person sent it.\n\n"
        "Always end your response with references using RESOLVED human-readable names:\n"
        "---\n"
        "\U0001f4ce Chat: <resolved group/person name> | Date: <date> | From: <resolved sender name>"
    )

    try:
        result = await run_claude_cli(
            query,
            model="sonnet",
            system_prompt=system_prompt,
            tools="Bash,Read,Grep,Glob",
            max_turns=claude_cfg.get("max_turns", 10),
            max_budget_usd="0.15",
            cwd=str(project_root),
            timeout=60,
        )

        response_text = result["text"]
        add_history(user_id, "assistant", response_text[:500])
        context_window = claude_cfg.get("context_window", 200000)
        input_tokens = result["input_tokens"]
        if input_tokens > 0:
            pct = (input_tokens / context_window) * 100
            bar = f"\n\n· sonnet · {pct:.0f}% context"
        else:
            bar = "\n\n· sonnet"

        max_len = 4000 - len(bar)
        if len(response_text) > max_len:
            response_text = response_text[:max_len] + "\n\n... (truncated)"
        response_text += bar

        try:
            await thinking_msg.edit_text(response_text, parse_mode="Markdown")
        except Exception:
            await thinking_msg.edit_text(response_text)

    except asyncio.TimeoutError:
        await thinking_msg.edit_text("Search timed out.")
    except Exception as e:
        log.error("WhatsApp smart search error: %s", e)
        await thinking_msg.edit_text(f"Error: {type(e).__name__}. See logs.")


async def _wa_search(update: Update, query: str):
    """Search messages across all chats."""
    results = await _api_get("/search", {"q": query, "limit": 20})

    if not results:
        await update.message.reply_text(f"No messages matching '{query}'.")
        return

    lines = [f"🔍 Results for '{query}':\n"]
    for m in results:
        sender = "You" if m.get("is_from_me") else (m.get("sender_name") or "?")
        chat = m.get("chat_name") or _format_phone(m.get("chat_id", ""))
        text = m.get("content", "")
        if len(text) > 100:
            text = text[:100] + "..."
        lines.append(f"[{chat}] {sender}: {text}")

    text = "\n".join(lines)
    if len(text) > 4000:
        text = text[:4000] + "\n\n... (truncated)"

    await update.message.reply_text(text)


def register(app: Application):
    app.add_handler(CommandHandler("wa", cmd_wa))
    app.add_handler(CallbackQueryHandler(_handle_chat_pick, pattern=r"^wa:"))

    # Reply handler — low priority, only activates when user is in reply mode
    app.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            _handle_reply_text,
        ),
        group=50,  # Between topic handlers and claude catch-all (99)
    )

    log.info("WhatsApp control plugin active")
