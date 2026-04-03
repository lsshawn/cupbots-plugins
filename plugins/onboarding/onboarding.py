"""
Onboarding — Welcome new members with a guided setup.

Commands (works in any topic):
  /onboard              — Run setup wizard (first time) or show config
  /onboard set <key> <value> — Set a config value
  /onboard preview      — Preview the welcome message
  /onboard edit         — Re-run the setup wizard
  /onboard reset        — Clear all config for this group
  /onboard on           — Enable onboarding for this group
  /onboard off          — Disable onboarding for this group

Setup keys:
  welcome   — Welcome message ({name} = member name)
  rules     — Group rules
  intro     — Ask new members to introduce themselves (true/false)
  dm        — Send a private DM welcome (true/false)
  delay     — Seconds to wait before sending welcome (0-300)

Examples:
  /onboard
  /onboard set welcome Hey {name}, welcome to our community!
  /onboard set rules 1. Be kind  2. No spam  3. Share what you build
  /onboard set intro true
  /onboard set dm true
  /onboard set delay 5
  /onboard preview
"""

import json
import asyncio

from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

from cupbots.helpers.db import get_plugin_db
from cupbots.helpers.logger import get_logger

log = get_logger("onboarding")

PLUGIN_NAME = "onboarding"

# Setup wizard steps
_SETUP_STEPS = [
    {
        "key": "welcome",
        "prompt": (
            "Step 1/5: *Welcome message*\n"
            "What should new members see when they join?\n"
            "Use {name} for their name.\n\n"
            "Reply with your message, or \"skip\" to use the default."
        ),
        "default": "Welcome, {name}! We're glad you're here.",
    },
    {
        "key": "rules",
        "prompt": (
            "Step 2/5: *Group rules*\n"
            "What are your community rules?\n\n"
            "Reply with your rules, or \"skip\" to leave empty."
        ),
        "default": "",
    },
    {
        "key": "intro",
        "prompt": (
            "Step 3/5: *Ask new members to introduce themselves?*\n"
            "Reply \"yes\" or \"no\"."
        ),
        "default": "true",
        "parse": lambda v: "true" if v.lower() in ("yes", "y", "true", "1") else "false",
    },
    {
        "key": "dm",
        "prompt": (
            "Step 4/5: *Send a private DM welcome?*\n"
            "This sends the welcome message as a DM too.\n"
            "Reply \"yes\" or \"no\"."
        ),
        "default": "false",
        "parse": lambda v: "true" if v.lower() in ("yes", "y", "true", "1") else "false",
    },
    {
        "key": "delay",
        "prompt": (
            "Step 5/5: *Welcome delay*\n"
            "How many seconds to wait before sending the welcome? (0-300)\n"
            "A short delay (5-10s) feels more natural.\n\n"
            "Reply with a number, or \"skip\" for no delay."
        ),
        "default": "5",
        "parse": lambda v: str(max(0, min(300, int(v)))) if v.isdigit() else "5",
    },
]

# In-memory setup wizard sessions: chat_id -> {step: int, config: dict}
_setup_sessions: dict[str, dict] = {}


# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------

def create_tables(conn):
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS onboarding_config (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id TEXT NOT NULL,
            company_id TEXT NOT NULL DEFAULT '',
            config TEXT NOT NULL DEFAULT '{}',
            enabled INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            updated_at TEXT NOT NULL DEFAULT (datetime('now')),
            UNIQUE(chat_id)
        );
    """)


def _db():
    return get_plugin_db(PLUGIN_NAME)


def _get_config(chat_id: str) -> dict | None:
    """Get onboarding config for a chat."""
    row = _db().execute(
        "SELECT config, enabled FROM onboarding_config WHERE chat_id = ?",
        (chat_id,),
    ).fetchone()
    if not row:
        return None
    cfg = json.loads(row["config"]) if row["config"] else {}
    cfg["_enabled"] = bool(row["enabled"])
    return cfg


def _save_config(chat_id: str, config: dict, company_id: str = ""):
    """Save onboarding config for a chat."""
    enabled = config.pop("_enabled", True)
    _db().execute(
        """INSERT INTO onboarding_config (chat_id, company_id, config, enabled, updated_at)
           VALUES (?, ?, ?, ?, datetime('now'))
           ON CONFLICT(chat_id) DO UPDATE SET
             config = excluded.config,
             company_id = excluded.company_id,
             enabled = excluded.enabled,
             updated_at = datetime('now')""",
        (chat_id, company_id, json.dumps(config), int(enabled)),
    )
    _db().commit()


def _set_enabled(chat_id: str, enabled: bool):
    """Toggle onboarding on/off."""
    _db().execute(
        "UPDATE onboarding_config SET enabled = ?, updated_at = datetime('now') WHERE chat_id = ?",
        (int(enabled), chat_id),
    )
    _db().commit()


def _delete_config(chat_id: str):
    """Delete onboarding config for a chat."""
    _db().execute("DELETE FROM onboarding_config WHERE chat_id = ?", (chat_id,))
    _db().commit()


# ---------------------------------------------------------------------------
# Welcome message builder
# ---------------------------------------------------------------------------

def _build_welcome(config: dict, name: str) -> str:
    """Build the welcome message from config."""
    parts = []

    welcome = config.get("welcome", "Welcome, {name}!")
    parts.append(welcome.replace("{name}", name))

    rules = config.get("rules", "")
    if rules:
        parts.append(f"\n📋 *Rules:*\n{rules}")

    if config.get("intro") == "true":
        parts.append("\n💬 Tell us a bit about yourself!")

    return "\n".join(parts)


def _format_config_display(config: dict) -> str:
    """Format config for display."""
    enabled = config.get("_enabled", True)
    lines = [
        f"*Onboarding {'✅ ON' if enabled else '❌ OFF'}*",
        "",
        f"*Welcome:* {config.get('welcome', '(default)')}",
        f"*Rules:* {config.get('rules', '(none)')}",
        f"*Ask intro:* {config.get('intro', 'true')}",
        f"*DM welcome:* {config.get('dm', 'false')}",
        f"*Delay:* {config.get('delay', '5')}s",
    ]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Setup wizard
# ---------------------------------------------------------------------------

async def _start_wizard(chat_id: str, reply, existing_config: dict | None = None):
    """Start the interactive setup wizard."""
    _setup_sessions[chat_id] = {
        "step": 0,
        "config": dict(existing_config) if existing_config else {},
    }
    # Remove internal keys
    _setup_sessions[chat_id]["config"].pop("_enabled", None)

    await reply.reply_text(
        "👋 Let's set up onboarding for this group!\n\n"
        + _SETUP_STEPS[0]["prompt"]
    )


async def _advance_wizard(chat_id: str, text: str, reply, company_id: str = "") -> bool:
    """Process a wizard reply. Returns True if handled."""
    session = _setup_sessions.get(chat_id)
    if not session:
        return False

    step_idx = session["step"]
    if step_idx >= len(_SETUP_STEPS):
        return False

    step = _SETUP_STEPS[step_idx]

    # Handle skip
    if text.lower() == "skip":
        value = step["default"]
    else:
        parse_fn = step.get("parse")
        value = parse_fn(text) if parse_fn else text

    session["config"][step["key"]] = value

    # Advance to next step
    session["step"] += 1

    if session["step"] < len(_SETUP_STEPS):
        next_step = _SETUP_STEPS[session["step"]]
        await reply.reply_text(f"✅ Saved.\n\n{next_step['prompt']}")
    else:
        # Wizard complete — save config
        config = session["config"]
        _save_config(chat_id, dict(config), company_id)
        del _setup_sessions[chat_id]

        preview = _build_welcome(config, "John")
        await reply.reply_text(
            "✅ All set! Here's a preview:\n"
            "─────────────────\n"
            f"{preview}\n"
            "─────────────────\n\n"
            "Run /onboard to see your config, or /onboard edit to change it."
        )

    return True


# ---------------------------------------------------------------------------
# Core logic
# ---------------------------------------------------------------------------

async def _handle_onboard(args: list[str], chat_id: str, reply,
                          company_id: str = "") -> str | None:
    """Handle /onboard commands. Returns text or None if wizard was started."""
    if not args:
        # Check for existing config
        config = _get_config(chat_id)
        if config:
            return _format_config_display(config)
        else:
            # No config — start wizard
            await _start_wizard(chat_id, reply)
            return None

    sub = args[0].lower()

    if sub == "edit":
        config = _get_config(chat_id)
        await _start_wizard(chat_id, reply, config)
        return None

    if sub == "preview":
        config = _get_config(chat_id)
        if not config:
            return "No onboarding configured. Run /onboard to set up."
        preview = _build_welcome(config, "John")
        return f"*Preview:*\n─────────────────\n{preview}\n─────────────────"

    if sub == "on":
        config = _get_config(chat_id)
        if not config:
            return "No onboarding configured. Run /onboard to set up first."
        _set_enabled(chat_id, True)
        return "✅ Onboarding enabled for this group."

    if sub == "off":
        _set_enabled(chat_id, False)
        return "❌ Onboarding disabled for this group."

    if sub == "reset":
        _delete_config(chat_id)
        _setup_sessions.pop(chat_id, None)
        return "🗑️ Onboarding config cleared for this group."

    if sub == "set" and len(args) >= 3:
        key = args[1].lower()
        value = " ".join(args[2:])

        valid_keys = {s["key"] for s in _SETUP_STEPS}
        if key not in valid_keys:
            return f"Unknown key: {key}. Valid keys: {', '.join(sorted(valid_keys))}"

        # Find the step for parsing
        step = next(s for s in _SETUP_STEPS if s["key"] == key)
        parse_fn = step.get("parse")
        value = parse_fn(value) if parse_fn else value

        config = _get_config(chat_id) or {}
        config.pop("_enabled", None)
        config[key] = value
        _save_config(chat_id, config, company_id)
        return f"✅ Set *{key}* = {value}"

    if sub == "set":
        return "Usage: /onboard set <key> <value>\nKeys: welcome, rules, intro, dm, delay"

    return f"Unknown subcommand: {sub}. Try /onboard --help"


# ---------------------------------------------------------------------------
# Group event handler (member join/leave)
# ---------------------------------------------------------------------------

async def handle_group_event(data: dict, reply, group_cfg: dict | None = None):
    """Handle group participant events. Called by wa_router for join/leave."""
    action = data.get("action")
    if action != "add":
        return  # only handle joins

    chat_id = data.get("chatId", "")
    config = _get_config(chat_id)
    if not config or not config.get("_enabled", True):
        return

    participants = data.get("participants", [])
    participant_names = data.get("participantNames", [])

    delay = int(config.get("delay", "5"))
    if delay > 0:
        await asyncio.sleep(delay)

    for i, jid in enumerate(participants):
        name = participant_names[i] if i < len(participant_names) else jid.split("@")[0]
        welcome_text = _build_welcome(config, name)

        # Send to group
        try:
            await reply.reply_text(welcome_text)
            log.info("Sent welcome in %s for %s", chat_id, name)
        except Exception as e:
            log.error("Failed to send group welcome in %s: %s", chat_id, e)

        # Send DM if configured
        if config.get("dm") == "true":
            try:
                from cupbots.helpers.channel import WhatsAppReplyContext
                dm_reply = WhatsAppReplyContext(jid)
                dm_text = _build_welcome(config, name)
                await dm_reply.reply_text(dm_text)
                log.info("Sent DM welcome to %s", jid)
            except Exception as e:
                log.error("Failed to send DM welcome to %s: %s", jid, e)


# ---------------------------------------------------------------------------
# Cross-platform handler
# ---------------------------------------------------------------------------

async def handle_command(msg, reply) -> bool:
    """Platform-agnostic command handler."""
    # Check if this is a wizard reply (non-command text in an active session)
    if not msg.command and msg.chat_id in _setup_sessions:
        return await _advance_wizard(
            msg.chat_id, msg.text, reply, msg.company_id or ""
        )

    if msg.command != "onboard":
        return False

    result = await _handle_onboard(
        msg.args, msg.chat_id, reply, msg.company_id or ""
    )
    if result:
        await reply.reply_text(result)
    return True


# ---------------------------------------------------------------------------
# Telegram-specific handlers
# ---------------------------------------------------------------------------

async def cmd_onboard(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message:
        return
    chat_id = str(update.effective_chat.id)
    args = context.args or []

    from cupbots.helpers.channel import TelegramReplyContext
    reply = TelegramReplyContext(update)

    result = await _handle_onboard(args, chat_id, reply)
    if result:
        await reply.reply_text(result)


def register(app: Application):
    app.add_handler(CommandHandler("onboard", cmd_onboard))
