"""
DM Onboarding — Welcome first-time DM users with a taste of CupBots.

Sends a scripted 3-5 message flow when someone DMs the bot for the first
time, then drops the Stripe payment link. Returning users go straight
to AI chat as usual.

Commands:
  /dmonboarding stats    — Show onboarding stats
  /dmonboarding reset <phone> — Reset a user so they see onboarding again
  /dmonboarding test     — Trigger onboarding on yourself

Examples:
  /dmonboarding stats
  /dmonboarding reset 60163113186
  /dmonboarding test
"""

import asyncio

from cupbots.helpers.db import get_plugin_db
from cupbots.helpers.logger import get_logger
from cupbots.config import get_config

log = get_logger("dm_onboarding")

PLUGIN_NAME = "dm_onboarding"

# How long between each message (seconds) — feels like a real person typing
_MSG_DELAY = 1.5


def create_tables(conn):
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS dm_contacts (
            sender_id TEXT PRIMARY KEY,
            sender_name TEXT NOT NULL DEFAULT '',
            first_seen_at TEXT NOT NULL DEFAULT (datetime('now')),
            onboarded_at TEXT,
            intent TEXT NOT NULL DEFAULT ''
        );
    """)


def _db():
    return get_plugin_db(PLUGIN_NAME)


def _get_payment_links() -> dict:
    cfg = get_config()
    stripe_cfg = cfg.get("stripe", {})
    mode = stripe_cfg.get("mode", "live")
    return stripe_cfg.get(mode, {}).get("payment_links", {})


def _has_community(plan: str) -> bool:
    """Check if a plan has community groups configured."""
    cfg = get_config()
    communities = cfg.get("stripe", {}).get("communities", {})
    return bool(communities.get(plan, {}).get("groups"))


def _detect_intent(text: str) -> str:
    """Detect user intent from their first message."""
    t = text.lower()
    if any(w in t for w in ("subscription", "inner circle", "$9", "circle")):
        return "inner_circle"
    if any(w in t for w in ("hosted", "own bot", "my own", "assistant", "$399", "399")):
        return "hosted_bot"
    if any(w in t for w in ("custom", "build", "enterprise")):
        return "custom"
    return "general"


async def _send(reply, text: str, delay: float = _MSG_DELAY):
    """Send a message with typing indicator and natural delay."""
    await reply.send_typing()
    await asyncio.sleep(delay)
    await reply.reply_text(text)


async def _run_onboarding(msg, reply):
    """The onboarding flow — 3-5 messages then Stripe link."""
    intent = _detect_intent(msg.text)
    name = msg.sender_name.split()[0] if msg.sender_name else "there"
    links = _get_payment_links()

    # Save contact
    with _db() as conn:
        conn.execute(
            """INSERT INTO dm_contacts (sender_id, sender_name, intent)
               VALUES (?, ?, ?)
               ON CONFLICT(sender_id) DO UPDATE SET
                 sender_name = excluded.sender_name,
                 intent = excluded.intent""",
            (msg.sender_id, msg.sender_name or "", intent),
        )

    # Message 1 — warm welcome
    await _send(reply, f"Hey {name}! Welcome to CupBots 👋", delay=1)

    # Message 2 — what we do
    await _send(reply,
        "We build AI assistants that live right in your WhatsApp. "
        "Sales, ops, reminders, SEO — you just text it like a teammate."
    )

    # Message 3 — the hook (tailored to intent)
    if intent == "inner_circle":
        await _send(reply,
            "The Inner Circle gets you early access to new AI skills, "
            "weekly playbooks, and direct access to the dev team. "
            "It's $9/mo — cancel anytime."
        )
        cta = f"Here's the link to join:\n{links['inner_circle']}" if links.get("inner_circle") else ""
        if _has_community("inner_circle"):
            cta += "\n\nOnce you subscribe, you'll be auto-added to our private community."
        await _send(reply,
            f"{cta}\n\nOr just ask me anything — I'm an AI assistant too 🤖"
        )
    elif intent == "hosted_bot":
        await _send(reply,
            "Your own AI assistant on your own WhatsApp number — "
            "your data stays yours. We handle setup, hosting & updates."
        )
        cta = f"Here's the link to get started:\n{links['hosted_bot']}" if links.get("hosted_bot") else ""
        if _has_community("hosted_bot"):
            cta += "\n\nYou'll get access to our private support community too."
        await _send(reply,
            f"{cta}\n\nOr ask me anything — happy to answer questions first."
        )
    elif intent == "custom":
        await _send(reply,
            "Custom builds are our specialty. Tell me a bit about your business "
            "and what you'd like the bot to do — I'll get the team looped in."
        )
    else:
        # General interest — give them a taste, offer both
        await _send(reply,
            "Here's what people use it for:\n"
            "• Auto-reply to leads on WhatsApp\n"
            "• Daily business briefings\n"
            "• CRM & contact management\n"
            "• SEO monitoring & content ideas\n"
            "• Expense tracking — just snap a receipt"
        )
        community_note = ""
        if _has_community("inner_circle"):
            community_note = " + private community"
        await _send(reply,
            "Two ways to get started:\n\n"
            f"☕ *Inner Circle* — $9/mo\nEarly access + playbooks{community_note}\n"
            f"{links.get('inner_circle', '')}\n\n"
            f"🤖 *Your Own Bot* — $399/mo\nPrivate AI on your own WhatsApp number\n"
            f"{links.get('hosted_bot', '')}"
        )

    # Mark onboarded
    with _db() as conn:
        conn.execute(
            "UPDATE dm_contacts SET onboarded_at = datetime('now') WHERE sender_id = ?",
            (msg.sender_id,),
        )

    log.info("Onboarded %s (%s) intent=%s", msg.sender_id, msg.sender_name, intent)


def _is_first_contact(sender_id: str) -> bool:
    """Check if this sender has been seen before."""
    with _db() as conn:
        row = conn.execute(
            "SELECT 1 FROM dm_contacts WHERE sender_id = ?", (sender_id,)
        ).fetchone()
        return row is None


# ---------------------------------------------------------------------------
# handle_message — intercept first DMs
# ---------------------------------------------------------------------------

async def handle_message(msg, reply) -> str | bool:
    """Intercept first-time DMs for onboarding. Returns 'block' to prevent AI."""
    if msg.is_group or msg.command:
        return False

    if not _is_first_contact(msg.sender_id):
        return False

    # First time — run onboarding and block AI from also responding
    try:
        await _run_onboarding(msg, reply)
    except Exception as e:
        log.error("Onboarding failed for %s: %s", msg.sender_id, e, exc_info=True)
        return False  # let AI handle it if onboarding breaks

    return "block"


# ---------------------------------------------------------------------------
# Commands (admin only)
# ---------------------------------------------------------------------------

async def handle_command(msg, reply) -> bool:
    if msg.command != "dmonboarding":
        return False

    args = msg.args

    if not args or args[0] == "stats":
        with _db() as conn:
            total = conn.execute("SELECT COUNT(*) FROM dm_contacts").fetchone()[0]
            onboarded = conn.execute(
                "SELECT COUNT(*) FROM dm_contacts WHERE onboarded_at IS NOT NULL"
            ).fetchone()[0]
            intents = conn.execute(
                "SELECT intent, COUNT(*) FROM dm_contacts GROUP BY intent ORDER BY COUNT(*) DESC"
            ).fetchall()
        lines = [
            f"DM Onboarding Stats\n",
            f"Total contacts: {total}",
            f"Onboarded: {onboarded}",
            f"\nBy intent:",
        ]
        for intent, count in intents:
            lines.append(f"  {intent or 'unknown'}: {count}")
        await reply.reply_text("\n".join(lines))
        return True

    elif args[0] == "reset" and len(args) > 1:
        phone = args[1].lstrip("+").replace(" ", "").replace("-", "")
        jid = f"{phone}@s.whatsapp.net" if "@" not in phone else phone
        with _db() as conn:
            conn.execute("DELETE FROM dm_contacts WHERE sender_id = ?", (jid,))
        await reply.reply_text(f"Reset onboarding for {jid}")
        return True

    elif args[0] == "test":
        await _run_onboarding(msg, reply)
        return True

    elif args[0] in ("--help", "-h"):
        return False  # let framework handle --help

    else:
        await reply.reply_text("Unknown subcommand. Try /dmonboarding --help")
        return True

    return False
