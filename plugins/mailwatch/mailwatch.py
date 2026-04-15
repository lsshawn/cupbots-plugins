"""
Mailwatch — Watch email inboxes and process messages with rules.

Commands:
  /mailwatch                  — Show help
  /mailwatch status           — Connection status, last poll, rule count
  /mailwatch rules            — List all rules
  /mailwatch add <rule>       — Add a rule (see examples)
  /mailwatch remove <id>      — Remove a rule
  /mailwatch start            — Test connection and begin polling
  /mailwatch stop             — Stop polling
  /mailwatch test <rule_id>   — Test a rule against recent emails
  /mailwatch account add <email> <app_password> [host]  — Add an email account (saves to config.yaml)
  /mailwatch account remove <email>  — Remove an account (from config.yaml)
  /mailwatch account list     — List all accounts
  /mailwatch send <to> <subject> -- <body>  — Send email via AgentMail
  /mailwatch sent [N]         — Show N most recent sent emails (default 10)
  /mailwatch autosend         — Show auto-send rules

Rule format: /mailwatch add <type> [field] <pattern> -> <action>
  Types: keyword, regex, attachment, ai
  Fields: subject, sender, body, any (default: any)
  Actions: notify, calendar, crm_update, draft_reply

Examples:
  /mailwatch add keyword subject "invoice" -> notify
  /mailwatch add regex sender ".*@acme\\.com" -> notify
  /mailwatch add attachment .ics -> calendar
  /mailwatch add ai "is this a client project update?" -> crm_update
  /mailwatch add keyword any "urgent" -> notify
  /mailwatch account add user@gmail.com abcd-efgh-ijkl-mnop
  /mailwatch account add user@company.com p4ssw0rd imap.company.com
  /mailwatch send user@example.com "Meeting follow-up" -- Hi, just following up...
"""

import email as email_lib
import email.utils
import imaplib
import json
import os
import re
import time
from datetime import datetime, timedelta
from email import policy

from cupbots.helpers.channel import WhatsAppReplyContext
from cupbots.helpers.db import get_plugin_db, resolve_plugin_setting
from cupbots.helpers.jobs import register_handler, enqueue
from cupbots.helpers.logger import get_logger

log = get_logger("mailwatch")

PLUGIN_NAME = "mailwatch"
WA_API_URL = os.environ.get("WA_API_URL", "http://127.0.0.1:3100")
VALID_RULE_TYPES = ("keyword", "regex", "attachment", "ai")
VALID_FIELDS = ("subject", "sender", "body", "any")
VALID_ACTIONS = ("notify", "calendar", "crm_update", "draft_reply")
_APPROVAL_TTL = 3600  # 1 hour, matches wa_router


# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------

def create_tables(conn):
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS mailbox_state (
            address TEXT PRIMARY KEY,
            last_poll TEXT NOT NULL DEFAULT '',
            last_error TEXT NOT NULL DEFAULT '',
            active INTEGER NOT NULL DEFAULT 1
        );

        CREATE TABLE IF NOT EXISTS rule_stats (
            rule_key TEXT PRIMARY KEY,
            hits INTEGER NOT NULL DEFAULT 0,
            last_hit TEXT NOT NULL DEFAULT ''
        );

        CREATE TABLE IF NOT EXISTS processed_emails (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            company_id TEXT NOT NULL DEFAULT '',
            mailbox_id INTEGER NOT NULL DEFAULT 1,
            uid TEXT NOT NULL,
            message_id TEXT NOT NULL DEFAULT '',
            subject TEXT NOT NULL DEFAULT '',
            sender TEXT NOT NULL DEFAULT '',
            rule_id INTEGER,
            action_taken TEXT NOT NULL DEFAULT '',
            processed_at TEXT NOT NULL DEFAULT (datetime('now'))
        );
        CREATE UNIQUE INDEX IF NOT EXISTS idx_processed_uid
            ON processed_emails (company_id, mailbox_id, uid);

        CREATE TABLE IF NOT EXISTS sent_emails (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            to_addr TEXT NOT NULL,
            subject TEXT NOT NULL DEFAULT '',
            reply_to_addr TEXT NOT NULL DEFAULT '',
            in_reply_to TEXT NOT NULL DEFAULT '',
            agentmail_message_id TEXT NOT NULL DEFAULT '',
            category TEXT NOT NULL DEFAULT '',
            auto_sent INTEGER NOT NULL DEFAULT 0,
            approved_by TEXT NOT NULL DEFAULT '',
            sent_at TEXT NOT NULL DEFAULT (datetime('now'))
        );
    """)


def _db():
    return get_plugin_db(PLUGIN_NAME)


# ---------------------------------------------------------------------------
# Config helpers — mailboxes live in config.yaml, not DB
# ---------------------------------------------------------------------------

def _get_plugin_settings() -> dict:
    """Get full mailwatch plugin_settings dict from config.yaml."""
    from cupbots.config import get_config
    settings = get_config().get("plugin_settings", {}) or {}
    return settings.get("mailwatch", {}) or {}


def _get_mailboxes(company_id: str = "") -> list[dict]:
    """Get all mailboxes from config.yaml."""
    settings = _get_plugin_settings()
    mailboxes_cfg = settings.get("mailboxes", []) or []

    result = []
    for i, mb in enumerate(mailboxes_cfg):
        address = mb.get("address", "")
        if not address:
            continue
        state = _get_mailbox_state(address)
        result.append({
            "id": i + 1,
            "email": address,
            "app_password": mb.get("app_password", ""),
            "imap_host": mb.get("imap_host", "imap.gmail.com"),
            "imap_port": mb.get("imap_port", 993),
            "notify_chat": mb.get("notify_chat", ""),
            "poll_interval": mb.get("poll_interval", 300),
            "active": state.get("active", 1),
            "last_poll": state.get("last_poll", ""),
            "last_error": state.get("last_error", ""),
        })
    return result


def _get_mailbox(company_id: str = "", mailbox_id: int | None = None) -> dict | None:
    """Get a specific mailbox by 1-based index, or the first one."""
    mailboxes = _get_mailboxes(company_id)
    if not mailboxes:
        return None
    if mailbox_id:
        for mb in mailboxes:
            if mb["id"] == mailbox_id:
                return mb
        return None
    return mailboxes[0]


def _get_mailbox_state(address: str) -> dict:
    """Get runtime state (last_poll, last_error, active) from DB."""
    row = _db().execute(
        "SELECT * FROM mailbox_state WHERE address = ?", (address,)
    ).fetchone()
    return dict(row) if row else {"last_poll": "", "last_error": "", "active": 1}


def _update_mailbox_state(address: str, **kwargs):
    """Update runtime state for a mailbox in DB."""
    _db().execute(
        "INSERT INTO mailbox_state (address) VALUES (?) ON CONFLICT(address) DO NOTHING",
        (address,),
    )
    for key, val in kwargs.items():
        if key in ("last_poll", "last_error", "active"):
            _db().execute(
                f"UPDATE mailbox_state SET {key} = ? WHERE address = ?",
                (val, address),
            )
    _db().commit()


def _get_notify_chat(mb: dict | None = None) -> str:
    """Get notify chat — per-mailbox override or global fallback."""
    if mb and mb.get("notify_chat"):
        return mb["notify_chat"]
    return resolve_plugin_setting(PLUGIN_NAME, "mailwatch_notify_chat") or ""


# ---------------------------------------------------------------------------
# AgentMail outbound
def _get_rules() -> list[dict]:
    """Get rules from config.yaml. Returns list of rule dicts with synthetic id."""
    settings = _get_plugin_settings()
    rules_cfg = settings.get("rules", []) or []

    result = []
    for i, r in enumerate(rules_cfg):
        rule_type = r.get("type", "keyword")
        if rule_type not in VALID_RULE_TYPES:
            continue
        action = r.get("action", "notify")
        if action not in VALID_ACTIONS:
            continue
        field = r.get("field", "any")
        pattern = r.get("pattern", "")
        if not pattern:
            continue

        rule_key = f"{rule_type}_{field}_{pattern}_{action}"
        stats = _get_rule_stats(rule_key)
        result.append({
            "id": i + 1,
            "rule_key": rule_key,
            "rule_type": rule_type,
            "match_field": field,
            "match_pattern": pattern,
            "action": action,
            "action_config": json.dumps(r.get("action_config", {})),
            "name": r.get("name", f"{rule_type}_{action}"),
            "active": 1 if r.get("active", True) else 0,
            "priority": r.get("priority", 0),
            "hits": stats.get("hits", 0),
            "last_hit": stats.get("last_hit", ""),
        })
    return result


def _get_rule_stats(rule_key: str) -> dict:
    """Get hit stats for a rule from DB."""
    row = _db().execute(
        "SELECT * FROM rule_stats WHERE rule_key = ?", (rule_key,)
    ).fetchone()
    return dict(row) if row else {"hits": 0, "last_hit": ""}


def _update_rule_stats(rule_key: str):
    """Increment hit count for a rule."""
    _db().execute(
        "INSERT INTO rule_stats (rule_key, hits, last_hit) VALUES (?, 1, datetime('now')) "
        "ON CONFLICT(rule_key) DO UPDATE SET hits = hits + 1, last_hit = datetime('now')",
        (rule_key,),
    )
    _db().commit()


def _ai_fallback_enabled() -> bool:
    """Check if AI fallback for unmatched emails is enabled. Default: true."""
    settings = _get_plugin_settings()
    return bool(settings.get("ai_fallback", True))


# ---------------------------------------------------------------------------

_agentmail_client = None


def _get_agentmail():
    """Lazy-init async AgentMail client. Returns None if not configured."""
    global _agentmail_client
    if _agentmail_client is None:
        api_key = resolve_plugin_setting(PLUGIN_NAME, "agentmail_api_key")
        if api_key:
            try:
                from agentmail import AsyncAgentMail
                _agentmail_client = AsyncAgentMail(api_key=api_key)
            except ImportError:
                log.warning("agentmail package not installed — pip install agentmail")
                return None
    return _agentmail_client


def _extract_email_addr(header_value: str) -> str:
    """Extract bare email address from a header like 'Name <user@example.com>'."""
    _, addr = email.utils.parseaddr(header_value)
    return addr


def _get_auto_send_rules() -> list[dict]:
    """Load auto-send rules from config.yaml."""
    from cupbots.config import get_config
    settings = get_config().get("plugin_settings", {}).get("mailwatch", {})
    return settings.get("auto_send_rules", [])


def _should_auto_send(category: str) -> bool:
    """Check if a category should auto-send. Default: False (require approval)."""
    for rule in _get_auto_send_rules():
        if rule.get("category") == category:
            return rule.get("auto_send", False)
    return False


async def _agentmail_send(
    to: str,
    subject: str,
    body: str,
    reply_to: str = "",
    in_reply_to: str = "",
    references: str = "",
    category: str = "",
    auto_sent: bool = False,
    approved_by: str = "",
) -> dict | None:
    """Send email via AgentMail. Returns send result or None on failure."""
    client = _get_agentmail()
    if not client:
        log.warning("AgentMail not configured — cannot send email")
        return None

    inbox_id = (resolve_plugin_setting(PLUGIN_NAME, "agentmail_address")
                or resolve_plugin_setting(PLUGIN_NAME, "agentmail_inbox_id") or "")
    if not inbox_id:
        log.warning("agentmail_address not configured")
        return None

    # Resolve reply-to: use provided (from inbound email), else config fallback
    if not reply_to:
        reply_to = resolve_plugin_setting(PLUGIN_NAME, "agentmail_reply_to") or ""

    # Threading headers for In-Reply-To / References
    headers = {}
    if in_reply_to:
        headers["In-Reply-To"] = in_reply_to
        headers["References"] = references or in_reply_to

    try:
        result = await client.inboxes.messages.send(
            inbox_id=inbox_id,
            to=to,
            subject=subject,
            text=body,
            reply_to=reply_to or None,
            headers=headers or None,
        )

        # Record in DB
        msg_id = getattr(result, "message_id", "") or ""
        try:
            _db().execute(
                "INSERT INTO sent_emails "
                "(to_addr, subject, reply_to_addr, in_reply_to, agentmail_message_id, "
                "category, auto_sent, approved_by) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (to, subject[:200], reply_to, in_reply_to, str(msg_id),
                 category, 1 if auto_sent else 0, approved_by),
            )
            _db().commit()
        except Exception as e:
            log.warning("Failed to record sent email: %s", e)

        # Emit event
        try:
            from cupbots.helpers.events import emit
            await emit("email.sent", {
                "to": to,
                "subject": subject,
                "reply_to": reply_to,
                "category": category,
                "auto_sent": auto_sent,
            })
        except Exception as e:
            log.debug("email.sent event emission failed: %s", e)

        log.info("Email sent via AgentMail to %s — Re: %s", to, subject[:60])
        return {"message_id": msg_id, "status": "sent"}

    except Exception as e:
        log.error("AgentMail send failed: %s", e)
        return None


# Pending email approvals (msg_id -> email data)
_pending_email_approvals: dict[str, dict] = {}


def _cleanup_expired_approvals():
    """Remove approvals older than TTL."""
    now = time.time()
    expired = [k for k, v in _pending_email_approvals.items()
               if now - v.get("timestamp", 0) > _APPROVAL_TTL]
    for k in expired:
        del _pending_email_approvals[k]


async def handle_approval(data: dict, wa_api_url: str) -> bool:
    """Handle WhatsApp reaction approval for email drafts."""
    _cleanup_expired_approvals()

    reacted_msg_id = data.get("reactedMsgId", "")
    emoji_val = data.get("emoji", "")

    pending = _pending_email_approvals.pop(reacted_msg_id, None)
    if not pending:
        return False  # not ours

    chat = _get_notify_chat()
    ctx = WhatsAppReplyContext(chat, wa_api_url) if chat else None
    approved = emoji_val in ("\U0001f44d", "\u2705", "\U0001f44c")  # 👍 ✅ 👌

    if approved:
        result = await _agentmail_send(
            to=pending["to"],
            subject=pending["subject"],
            body=pending["body"],
            reply_to=pending.get("reply_to", ""),
            in_reply_to=pending.get("in_reply_to", ""),
            references=pending.get("references", ""),
            category=pending.get("category", ""),
            approved_by=data.get("sender", ""),
        )
        if ctx:
            if result:
                await ctx.reply_text(f"Email sent to {pending['to']}")
            else:
                await ctx.reply_text(f"Failed to send email to {pending['to']}")
    else:
        if ctx:
            await ctx.reply_text(f"Email draft discarded for {pending['to']}")

    return True


# ---------------------------------------------------------------------------
# Email parsing
# ---------------------------------------------------------------------------

def _parse_email(raw_bytes: bytes) -> dict:
    """Parse raw email into structured dict."""
    msg = email_lib.message_from_bytes(raw_bytes, policy=policy.default)
    result = {
        "subject": str(msg.get("Subject", "(no subject)")),
        "sender": str(msg.get("From", "")),
        "to": str(msg.get("To", "")),
        "date": str(msg.get("Date", "")),
        "message_id": str(msg.get("Message-ID", "")),
        "body_text": "",
        "body_html": "",
        "attachments": [],
    }

    for part in msg.walk():
        content_type = part.get_content_type()
        filename = part.get_filename()

        if filename:
            result["attachments"].append({
                "filename": filename,
                "content_type": content_type,
                "data": part.get_payload(decode=True),
            })
        elif content_type == "text/plain" and not result["body_text"]:
            payload = part.get_payload(decode=True)
            if payload:
                result["body_text"] = payload.decode("utf-8", errors="replace")
        elif content_type == "text/html" and not result["body_html"]:
            payload = part.get_payload(decode=True)
            if payload:
                result["body_html"] = payload.decode("utf-8", errors="replace")

    if not result["body_text"] and result["body_html"]:
        result["body_text"] = re.sub(r"<[^>]+>", " ", result["body_html"])
        result["body_text"] = re.sub(r"\s+", " ", result["body_text"]).strip()

    return result


# ---------------------------------------------------------------------------
# Rule matching
# ---------------------------------------------------------------------------

def _get_field(email_data: dict, field: str) -> str:
    if field == "subject":
        return email_data["subject"]
    if field == "sender":
        return email_data["sender"]
    if field == "body":
        return email_data["body_text"]
    # "any"
    return f"{email_data['subject']} {email_data['sender']} {email_data['body_text']}"


async def _match_rule(rule: dict, email_data: dict) -> bool:
    rtype = rule["rule_type"]
    pattern = rule["match_pattern"]

    if rtype == "keyword":
        text = _get_field(email_data, rule["match_field"]).lower()
        keywords = [k.strip().lower() for k in pattern.split(",")]
        return any(kw in text for kw in keywords)

    if rtype == "regex":
        text = _get_field(email_data, rule["match_field"])
        try:
            return bool(re.search(pattern, text, re.IGNORECASE))
        except re.error:
            log.warning("Invalid regex in rule %d: %s", rule["id"], pattern)
            return False

    if rtype == "attachment":
        ext = pattern.lower().lstrip(".")
        return any(
            a["filename"].lower().endswith(f".{ext}")
            for a in email_data["attachments"]
        )

    if rtype == "ai":
        try:
            from cupbots.helpers.llm import ask_llm
            result = await ask_llm(
                f"Does this email match the following criteria? Answer ONLY 'yes' or 'no'.\n\n"
                f"Criteria: {pattern}\n\n"
                f"From: {email_data['sender']}\n"
                f"Subject: {email_data['subject']}\n"
                f"Body:\n{email_data['body_text'][:2000]}",
                max_tokens=10,
            )
            return result.strip().lower().startswith("yes")
        except Exception as e:
            log.error("AI rule match failed for rule %d: %s", rule["id"], e)
            return False

    return False


# ---------------------------------------------------------------------------
# Actions
# ---------------------------------------------------------------------------

async def _action_notify(email_data: dict, rule: dict, company_id: str, mb: dict | None = None):
    chat = _get_notify_chat(mb)
    if not chat:
        log.warning("No notify chat configured")
        return
    summary = (
        f"Mailwatch [{rule['name'] or 'rule #' + str(rule['id'])}]\n"
        f"From: {email_data['sender']}\n"
        f"Subject: {email_data['subject']}\n"
        f"\n{email_data['body_text'][:500]}"
    )
    ctx = WhatsAppReplyContext(chat, WA_API_URL)
    await ctx.reply_text(summary)


async def _action_calendar(email_data: dict, rule: dict, company_id: str, mb: dict | None = None):
    try:
        from cupbots.helpers.calendar_client import get_calendar_client
    except ImportError:
        log.warning("Calendar client not available")
        return

    cal = get_calendar_client()
    imported = 0
    for att in email_data["attachments"]:
        if att["filename"].lower().endswith(".ics") or att["content_type"] == "text/calendar":
            try:
                ics_text = att["data"].decode("utf-8")
                cal.add_event_from_ics(ics_text)
                imported += 1
            except Exception as e:
                log.error("Failed to import .ics from %s: %s", email_data["subject"], e)

    if imported:
        chat = _get_notify_chat(mb)
        if chat:
            ctx = WhatsAppReplyContext(chat, WA_API_URL)
            await ctx.reply_text(
                f"Imported {imported} event(s) from: {email_data['subject']}"
            )


async def _action_crm_update(email_data: dict, rule: dict, company_id: str, mb: dict | None = None):
    try:
        from cupbots.helpers.llm import ask_llm
        from cupbots.helpers.db import get_plugin_db
    except ImportError:
        log.warning("LLM or DB not available for crm_update")
        return

    config = json.loads(rule.get("action_config", "{}"))
    prompt = config.get("prompt", "Extract the contact name and a brief summary of this email.")

    result = await ask_llm(
        f"{prompt}\n\nFrom: {email_data['sender']}\n"
        f"Subject: {email_data['subject']}\n"
        f"Body:\n{email_data['body_text'][:3000]}",
        system="Extract info from this email. Return JSON: "
               '{"contact_name": "...", "summary": "..."}',
        json_mode=True,
    )

    if not result or not isinstance(result, dict):
        await _action_notify(email_data, rule, company_id, mb)
        return

    try:
        contacts_db = get_plugin_db("contacts")
        name = result.get("contact_name", "")
        if name:
            existing = contacts_db.execute(
                "SELECT * FROM contacts WHERE name LIKE ?", (f"%{name}%",),
            ).fetchone()
            if existing:
                today = datetime.now().strftime("%Y-%m-%d")
                contacts_db.execute(
                    "INSERT INTO interactions (contact_id, channel, summary) VALUES (?, 'email', ?)",
                    (existing["id"], result.get("summary", email_data["subject"])),
                )
                contacts_db.execute(
                    "UPDATE contacts SET last_contact = ?, updated_at = datetime('now') WHERE id = ?",
                    (today, existing["id"]),
                )
                contacts_db.commit()
                log.info("Updated CRM contact %s from email", name)
    except Exception as e:
        log.warning("CRM update failed (contacts plugin may not be installed): %s", e)

    await _action_notify(email_data, rule, company_id, mb)


async def _action_draft_reply(email_data: dict, rule: dict, company_id: str, mb: dict | None = None):
    try:
        from cupbots.helpers.llm import ask_llm
    except ImportError:
        return

    config = json.loads(rule.get("action_config", "{}"))
    prompt = config.get("prompt", "Draft a professional reply to this email.")

    draft = await ask_llm(
        f"{prompt}\n\nOriginal email:\nFrom: {email_data['sender']}\n"
        f"Subject: {email_data['subject']}\n"
        f"Body:\n{email_data['body_text'][:3000]}",
        system="Draft an email reply. Be professional and concise. "
               "Return ONLY the reply body, no headers.",
        max_tokens=1024,
    )

    if not draft:
        return

    chat = _get_notify_chat(mb)
    if not chat:
        return

    # Dynamic reply-to: use the address this email was sent TO (the IMAP mailbox)
    reply_to = _extract_email_addr(email_data.get("to", ""))
    reply_subject = f"Re: {email_data['subject']}"
    sender_addr = _extract_email_addr(email_data["sender"])
    in_reply_to = email_data.get("message_id", "")

    # If AgentMail is configured, use auto-send or approval flow
    if _get_agentmail():
        # Classify email category for auto-send rules
        category = "other"
        try:
            cat_result = await ask_llm(
                f"Classify this email into exactly one category. Reply with ONLY the category name.\n"
                f"Categories: meeting_confirmation, invoice_acknowledgment, client_update, "
                f"general_inquiry, other\n\n"
                f"Subject: {email_data['subject']}\nBody: {email_data['body_text'][:1000]}",
                max_tokens=20,
            )
            if cat_result:
                category = cat_result.strip().lower().replace(" ", "_")
        except Exception:
            pass

        if _should_auto_send(category):
            # Auto-send via AgentMail
            result = await _agentmail_send(
                to=sender_addr,
                subject=reply_subject,
                body=draft,
                reply_to=reply_to,
                in_reply_to=in_reply_to,
                category=category,
                auto_sent=True,
            )
            ctx = WhatsAppReplyContext(chat, WA_API_URL)
            if result:
                await ctx.reply_text(
                    f"Auto-sent [{category}] to: {sender_addr}\n"
                    f"{reply_subject}\n---\n{draft[:500]}"
                )
            else:
                await ctx.reply_text(
                    f"Failed to auto-send to {sender_addr}\n"
                    f"{reply_subject}\n---\n{draft[:500]}\n---\n(Send failed)"
                )
        else:
            # Queue for WhatsApp approval
            ctx = WhatsAppReplyContext(chat, WA_API_URL)
            approval_text = (
                f"[Email Draft] [{category}]\n"
                f"To: {sender_addr}\n"
                f"{reply_subject}\n"
                f"Reply-To: {reply_to}\n---\n{draft}\n---\n"
                f"React \U0001f44d to send, \U0001f44e to discard"
            )
            msg_id = await ctx.send_and_get_id(approval_text)
            if msg_id:
                _pending_email_approvals[msg_id] = {
                    "to": sender_addr,
                    "subject": reply_subject,
                    "body": draft,
                    "reply_to": reply_to,
                    "in_reply_to": in_reply_to,
                    "references": in_reply_to,
                    "category": category,
                    "timestamp": time.time(),
                }
    else:
        # No AgentMail — fallback to current behavior (display-only draft)
        ctx = WhatsAppReplyContext(chat, WA_API_URL)
        await ctx.reply_text(
            f"Draft reply to: {email_data['sender']}\n"
            f"Re: {email_data['subject']}\n"
            f"---\n{draft}\n---\n"
            f"(Review only — configure AGENTMAIL_API_KEY to enable sending)"
        )


_ACTIONS = {
    "notify": _action_notify,
    "calendar": _action_calendar,
    "crm_update": _action_crm_update,
    "draft_reply": _action_draft_reply,
}


async def _ai_fallback(email_data: dict, mb: dict | None = None):
    """AI triage for emails that didn't match any rule. Uses agent loop to take action."""
    try:
        from cupbots.helpers.llm import run_agent_loop, build_tools
        from cupbots.helpers.db import load_plugin_metadata
        from cupbots.config import get_config
    except ImportError:
        log.warning("AI fallback: llm helper not available")
        return

    chat = _get_notify_chat(mb)
    if not chat:
        log.warning("AI fallback: no notify chat configured")
        return

    config = get_config()
    ai_cfg = config.get("ai", {})
    # Prefer Anthropic for agentic triage (better tool use), fall back to config default
    if ai_cfg.get("anthropic_api_key"):
        provider = "anthropic"
    else:
        provider = ai_cfg.get("api_provider", "anthropic")

    # Build tools so the agent can execute commands like /cal, /event, etc.
    from pathlib import Path
    import cupbots.helpers.wa_router as _wr
    # Framework plugins dir (cupbots/plugins/)
    plugin_dirs = [str(Path(_wr.__file__).resolve().parent.parent / "plugins")]
    plugin_dirs += config.get("external_plugin_dirs", [])
    metadata = load_plugin_metadata(plugin_dirs)
    tools = build_tools(metadata, provider=provider)

    # --- Step 1: Try to directly import .ics attachments (no LLM needed) ---
    ics_imported = []
    ics_content_raw = ""
    attachment_names = []
    for att in email_data.get("attachments", []):
        attachment_names.append(att.get("filename", ""))
        if (att.get("filename", "").lower().endswith(".ics")
                or att.get("content_type") == "text/calendar"):
            att_data = att.get("data", b"")
            if att_data:
                ics_text = att_data.decode("utf-8", errors="replace") if isinstance(att_data, bytes) else str(att_data)
                ics_content_raw = ics_text
                # Strip RECURRENCE-ID — we want standalone events, not occurrence overrides
                # (CalDAV can't store overrides without parent recurring events)
                if "RECURRENCE-ID" in ics_text:
                    log.info("AI fallback: stripping RECURRENCE-ID from .ics")
                    cleaned_lines = []
                    for line in ics_text.split("\n"):
                        if line.startswith("RECURRENCE-ID"):
                            continue
                        cleaned_lines.append(line)
                    ics_text = "\n".join(cleaned_lines)
                    # Also generate a new UID to avoid conflicts with any existing event
                    import uuid as _uuid
                    ics_text = re.sub(r'^UID:.*$', f'UID:{_uuid.uuid4()}', ics_text, count=1, flags=re.MULTILINE)

                # Direct import via calendar client — preserves original timezone
                try:
                    from cupbots.helpers.calendar_client import get_calendar_client
                    cal_client = get_calendar_client()
                    imported = cal_client.add_event_from_ics(ics_text)
                    ics_imported.extend(imported)
                    log.info("AI fallback: directly imported %d event(s) from .ics", len(imported))
                except Exception as e:
                    log.warning("AI fallback: .ics direct import failed: %s", e)
                    # Last resort: parse with icalendar directly and create event programmatically
                    try:
                        from icalendar import Calendar as _iCal
                        vcal = _iCal.from_ical(ics_content_raw)
                        cal_client = get_calendar_client()
                        for component in vcal.walk("VEVENT"):
                            dtstart = component.get("dtstart").dt
                            dtend_obj = component.get("dtend")
                            dtend = dtend_obj.dt if dtend_obj else None
                            if not dtend:
                                from datetime import timedelta as _td
                                dtend = dtstart + _td(hours=1)
                            summary = str(component.get("summary", "Meeting"))
                            location = str(component.get("location", ""))
                            uid = cal_client.add_event(summary, dtstart, dtend, location=location)
                            log.info("AI fallback: created fallback event uid=%s", uid)
                            from cupbots.helpers.calendar_client import TZ as _tz
                            ics_imported.append({
                                "summary": summary,
                                "start": dtstart.astimezone(_tz) if hasattr(dtstart, 'astimezone') and dtstart.tzinfo else dtstart,
                                "end": dtend.astimezone(_tz) if hasattr(dtend, 'astimezone') and dtend.tzinfo else dtend,
                                "location": location,
                                "uid": uid,
                            })
                    except Exception as e2:
                        log.warning("AI fallback: icalendar fallback also failed: %s", e2)

    # --- Step 2: Build event summary for imported events ---
    from cupbots.helpers.calendar_client import TZ as _cal_tz
    event_summary_lines = []
    for ev in ics_imported:
        start = ev["start"]
        end = ev["end"]
        if start.tzinfo:
            start = start.astimezone(_cal_tz)
        if end.tzinfo:
            end = end.astimezone(_cal_tz)
        tz_label = str(_cal_tz).split("/")[-1]
        line = f"- {ev['summary']}: {start.strftime('%a %d %b %H:%M')} – {end.strftime('%H:%M')} ({tz_label})"
        if ev.get("location"):
            line += f"\n  Location: {ev['location']}"
        if ev.get("url"):
            line += f"\n  Link: {ev['url']}"
        event_summary_lines.append(line)

    # --- Step 3: Build prompt for LLM (summary + any remaining actions) ---
    prompt = (
        f"An email arrived that needs triage.\n\n"
        f"From: {email_data['sender']}\n"
        f"Subject: {email_data['subject']}\n"
        f"Body:\n{email_data['body_text'][:3000]}\n\n"
        f"Attachments: {', '.join(a for a in attachment_names if a) or 'none'}\n"
    )

    if ics_imported:
        prompt += (
            f"\nCALENDAR EVENTS ALREADY IMPORTED (do NOT create these again):\n"
            + "\n".join(event_summary_lines) + "\n"
        )
        prompt += (
            f"\nInstructions:\n"
            f"- Calendar events from the .ics have already been imported (listed above)\n"
            f"- Just provide a summary of the email and confirm the events were added\n"
            f"- Include the meeting link if available\n"
        )
    else:
        # No .ics data — try to extract meeting details from email body via LLM
        # and create event programmatically (more reliable than agent tool calls)
        meeting_created = False
        body = email_data.get("body_text", "")[:3000]
        if any(kw in body.lower() for kw in ["zoom", "meet.google", "teams.microsoft",
                "meeting", "invitation", "scheduled", "calendar"]):
            try:
                from cupbots.helpers.llm import ask_llm, _extract_json
                extract_prompt = (
                    f"Extract meeting details from this email. Reply with ONLY a JSON object.\n\n"
                    f"Email subject: {email_data['subject']}\n"
                    f"Email body:\n{body}\n\n"
                    f"Return JSON: {{\"title\": \"...\", \"date\": \"YYYY-MM-DD\", \"time\": \"HH:MM\", "
                    f"\"timezone\": \"...\", \"duration_minutes\": N, \"meeting_link\": \"...\", \"location\": \"...\"}}\n\n"
                    f"Rules:\n"
                    f"- Extract the MEETING date/time, NOT the email sent/forwarded timestamp\n"
                    f"- Look for phrases like 'scheduled for', 'join at', or calendar header lines\n"
                    f"- The timezone should be the timezone mentioned in the invitation (e.g. AEST, PST, etc.)\n"
                    f"- If no explicit time is found in the body, return null\n"
                    f"- Extract Zoom/Meet/Teams links from the body\n"
                    f"- Default duration is 60 minutes unless specified"
                )
                extract_result = await ask_llm(extract_prompt, json_mode=False, timeout=30)
                meeting_data = _extract_json(extract_result)
                if meeting_data and isinstance(meeting_data, dict) and meeting_data.get("date") and meeting_data.get("time"):
                    # Build /event create command with proper flag syntax
                    from cupbots.run_command import run
                    import shlex
                    title = meeting_data["title"] or email_data["subject"]
                    start_iso = f"{meeting_data['date']}T{meeting_data['time']}"
                    duration = meeting_data.get("duration_minutes", 60) or 60
                    event_cmd = f"/event create --title {shlex.quote(title)} --start {start_iso} --duration {duration}m"
                    location = meeting_data.get("location", "")
                    link = meeting_data.get("meeting_link", "")
                    loc_parts = [p for p in (location, link) if p]
                    if loc_parts:
                        event_cmd += f" --location {shlex.quote(' | '.join(loc_parts))}"
                    log.info("AI fallback: extracted meeting, running: %s", event_cmd[:200])
                    event_output = await run(event_cmd, chat, "")
                    if event_output.startswith("Created:"):
                        event_summary_lines.append(f"Created event via email extraction:\n{event_output}")
                        meeting_created = True
                    else:
                        log.warning("AI fallback: /event create failed: %s", event_output[:200])
                        meeting_created = False
                else:
                    log.info("AI fallback: no meeting details extracted from body")
            except Exception as e:
                log.warning("AI fallback: meeting extraction failed: %s", e)

        prompt += (
            f"\nInstructions:\n"
        )
        if meeting_created:
            prompt += f"- Calendar event was already created (listed above). Just summarize the email.\n"
        else:
            prompt += (
                f"- If this is a meeting invitation, note the meeting details but do NOT try to create a calendar event.\n"
                f"  The event could not be auto-created. List the date, time, timezone, and link for the user.\n"
            )

    prompt += (
        f"- If this is informational only, just summarize it\n"
        f"- If drafting a reply, write the draft body directly (no quoting with >)\n\n"
        f"OUTPUT FORMAT (strict):\n"
        f"### Summary\n"
        f"<2-3 line summary of the email>\n\n"
        f"### Actions Taken\n"
        f"<what you did: imported events or meeting details (include date time and timezone), tool output, draft text, or 'None — informational only'>"
    )

    system = (
        "You are an email triage assistant. "
        "When calendar events were already imported or created, just confirm them. "
        "Follow the OUTPUT FORMAT exactly."
    )
    # No tools needed — event creation is handled above
    tools = []

    log.info("AI fallback: provider=%s, tools=%d, ics_imported=%d",
             provider, len(tools), len(ics_imported))

    ctx = WhatsAppReplyContext(chat, WA_API_URL)

    # Format sender as "Name (Company)" if parseable
    sender_raw = email_data["sender"]
    sender_display = sender_raw
    import re as _re_fmt
    m = _re_fmt.match(r'^"?([^"<]+?)"?\s*<([^>]+)>', sender_raw)
    if m:
        name = m.group(1).strip()
        email_addr = m.group(2).strip()
        domain = email_addr.split("@")[-1] if "@" in email_addr else ""
        company_part = domain.split(".")[0] if domain else ""
        generic = {"gmail", "yahoo", "hotmail", "outlook", "icloud", "protonmail", "aol"}
        if company_part and company_part.lower() not in generic:
            sender_display = f"{name} ({company_part.capitalize()})"
        else:
            sender_display = name

    try:
        # Summary via ask_llm (no tools needed — event creation handled above)
        from cupbots.helpers.llm import ask_llm
        response_text = await ask_llm(prompt, system=system, max_tokens=512) or ""
        log.info("AI fallback: summary generated for '%s'", email_data["subject"][:60])

        if response_text:
            await ctx.reply_text(
                f"📧 *Mailwatch AI Triage*\n"
                f"From: {sender_display}\n"
                f"Subject: {email_data['subject']}\n\n"
                f"{response_text}"
            )
    except Exception as e:
        log.error("AI fallback agent loop failed: %s", e)
        # Fall back to simple summary
        try:
            from cupbots.helpers.llm import ask_llm
            summary = await ask_llm(
                f"Briefly summarize this email (2-3 lines).\n\n"
                f"From: {email_data['sender']}\nSubject: {email_data['subject']}\n"
                f"Body:\n{email_data['body_text'][:3000]}",
                system="You are an email triage assistant. Be concise.",
                max_tokens=256,
            )
            if summary:
                await ctx.reply_text(
                    f"📧 *Mailwatch AI Triage*\n"
                    f"From: {sender_display}\n"
                    f"Subject: {email_data['subject']}\n\n"
                    f"### Summary\n{summary}\n\n"
                    f"### Actions Taken\nNone — summary only (agent error fallback)"
                )
        except Exception:
            pass


# ---------------------------------------------------------------------------
# IMAP connection
# ---------------------------------------------------------------------------

def _connect_imap(mb: dict) -> imaplib.IMAP4_SSL:
    """Connect to IMAP using mailbox credentials."""
    email_addr = mb.get("email", "")
    password = mb.get("app_password", "")
    host = mb.get("imap_host", "imap.gmail.com")
    port = mb.get("imap_port", 993)
    if not email_addr or not password:
        raise ValueError("Email credentials not configured. Use /mailwatch account add or edit config.yaml")
    conn = imaplib.IMAP4_SSL(host, port)
    conn.login(email_addr, password)
    return conn


def _test_connection(mb: dict) -> str:
    """Test IMAP connection for a mailbox. Returns status message."""
    try:
        conn = _connect_imap(mb)
        conn.select("INBOX", readonly=True)
        status, data = conn.search(None, "ALL")
        total = len(data[0].split()) if data[0] else 0
        conn.close()
        conn.logout()
        return f"Connected to {mb['email']} ({mb['imap_host']}). {total} total emails in inbox."
    except Exception as e:
        return f"Connection failed: {e}"


# ---------------------------------------------------------------------------
# Poll job handler
# ---------------------------------------------------------------------------

async def _handle_poll_job(payload: dict, bot=None):
    """Main polling job. Fetch unseen emails, match rules, execute actions."""
    company_id = payload.get("company_id", "")
    mailbox_id = payload.get("mailbox_id", 1)

    mb = _get_mailbox(company_id, mailbox_id)
    if not mb or not mb["active"]:
        log.info("Mailbox %s inactive for %s, not rescheduling", mailbox_id, company_id)
        return

    try:
        await _poll_imap(mb, mailbox_id, company_id)
        _update_mailbox_state(mb["email"], last_poll=datetime.now().isoformat(), last_error="")
    except Exception as e:
        log.error("Poll failed for %s: %s", mb["email"], e)
        _update_mailbox_state(mb["email"], last_error=str(e)[:500])
        raise  # Let jobs.py handle retry/backoff

    _reschedule(payload, mb["poll_interval"])


async def _poll_imap(mb: dict, mailbox_id: int, company_id: str):
    """Poll an IMAP mailbox for unseen mail and process via the rule pipeline."""
    conn = _connect_imap(mb)
    try:
        conn.select("INBOX")
        _, data = conn.search(None, "UNSEEN")
        uids = data[0].split() if data[0] else []
        if not uids:
            return

        rules = [r for r in _get_rules() if r["active"]]

        for uid in uids:
            uid_str = uid.decode()

            # Dedup
            existing = _db().execute(
                "SELECT 1 FROM processed_emails WHERE mailbox_id = ? AND uid = ?",
                (mailbox_id, uid_str),
            ).fetchone()
            if existing:
                continue

            _, msg_data = conn.fetch(uid, "(RFC822)")
            if not msg_data or not msg_data[0]:
                continue
            raw = msg_data[0][1]
            email_data = _parse_email(raw)

            matched_rule = None
            for rule in rules:
                try:
                    if await _match_rule(rule, email_data):
                        matched_rule = dict(rule)
                        break
                except Exception as e:
                    log.error("Rule %d match error: %s", rule["id"], e)

            if matched_rule:
                action_fn = _ACTIONS.get(matched_rule["action"])
                if action_fn:
                    try:
                        await action_fn(email_data, matched_rule, company_id, mb)
                    except Exception as e:
                        log.error("Action %s failed for rule %d: %s",
                                  matched_rule["action"], matched_rule["id"], e)

                await _emit_email_received(company_id, email_data, matched_rule["action"])

                _db().execute(
                    "INSERT OR IGNORE INTO processed_emails "
                    "(company_id, mailbox_id, uid, message_id, subject, sender, rule_id, action_taken) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (company_id, mailbox_id, uid_str, email_data["message_id"],
                     email_data["subject"][:200], email_data["sender"][:200],
                     matched_rule["id"], matched_rule["action"]),
                )
                _update_rule_stats(matched_rule["rule_key"])
                conn.store(uid, "+FLAGS", "\\Seen")
            else:
                if _ai_fallback_enabled():
                    await _ai_fallback(email_data, mb)
                    _db().execute(
                        "INSERT OR IGNORE INTO processed_emails "
                        "(company_id, mailbox_id, uid, message_id, subject, sender, rule_id, action_taken) "
                        "VALUES ('', ?, ?, ?, ?, ?, NULL, 'ai_fallback')",
                        (mailbox_id, uid_str, email_data["message_id"],
                         email_data["subject"][:200], email_data["sender"][:200]),
                    )
                    _db().commit()
                    conn.store(uid, "+FLAGS", "\\Seen")
                # else: leave UNSEEN so newer rules can catch it
        _db().commit()
    finally:
        try:
            conn.close()
            conn.logout()
        except Exception:
            pass


async def _emit_email_received(company_id: str, email_data: dict, rule_action: str):
    """Emit email.received and email.ics_received events for inter-plugin hooks."""
    try:
        from cupbots.helpers.events import emit
        await emit("email.received", {
            "company_id": company_id,
            "subject": email_data["subject"],
            "sender": email_data["sender"],
            "body_text": email_data["body_text"][:5000],
            "attachments": [
                {"filename": a["filename"], "content_type": a["content_type"]}
                for a in email_data["attachments"]
            ],
            "rule_action": rule_action,
        })
        for att in email_data["attachments"]:
            if att["filename"].lower().endswith(".ics") or att["content_type"] == "text/calendar":
                att_data = att.get("data", b"")
                await emit("email.ics_received", {
                    "company_id": company_id,
                    "subject": email_data["subject"],
                    "sender": email_data["sender"],
                    "ics_text": att_data.decode("utf-8", errors="replace") if isinstance(att_data, bytes) else str(att_data),
                })
    except Exception as e:
        log.debug("Event emission failed (non-critical): %s", e)


def _reschedule(payload: dict, interval: int):
    enqueue(
        "mailwatch_poll", payload,
        run_at=datetime.now() + timedelta(seconds=interval),
        max_attempts=3,
    )


async def _handle_watchdog_job(payload: dict, bot=None):
    """Re-enqueue polls for active mailboxes whose last_poll is stale."""
    cutoff = (datetime.now() - timedelta(minutes=3)).isoformat()
    company_id = payload.get("company_id", "")

    mailboxes = _get_mailboxes(company_id)
    for mb in mailboxes:
        if not mb["active"]:
            continue
        if mb["last_poll"] and mb["last_poll"] > cutoff:
            continue  # Not stale
        log.info("Watchdog: re-enqueuing poll for %s (last_poll: %s)",
                 mb["email"], mb["last_poll"])
        enqueue(
            "mailwatch_poll",
            {"company_id": company_id, "mailbox_id": mb["id"]},
            run_at=datetime.now() + timedelta(seconds=10),
            max_attempts=3,
        )

    # Reschedule watchdog
    enqueue(
        "mailwatch_watchdog", {"company_id": company_id},
        run_at=datetime.now() + timedelta(minutes=5),
        max_attempts=1,
    )


# ---------------------------------------------------------------------------
# AgentMail inbound webhook (via hub relay)
# ---------------------------------------------------------------------------

async def process_agentmail_webhook(body: str, headers: dict) -> None:
    """Process inbound email from AgentMail webhook (relayed via hub).

    Verifies Svix signature, converts payload to email_data format,
    and runs through the same rule-matching pipeline as IMAP polling.
    """
    # Verify Svix signature
    webhook_secret = resolve_plugin_setting(PLUGIN_NAME, "agentmail_webhook_secret")
    if webhook_secret:
        try:
            from svix.webhooks import Webhook, WebhookVerificationError
        except ImportError:
            log.warning("svix package not installed — pip install svix. Skipping signature verification.")
            WebhookVerificationError = None

        if WebhookVerificationError is not None:
            try:
                wh = Webhook(webhook_secret)
                wh.verify(body, headers)
            except WebhookVerificationError:
                log.warning("AgentMail webhook signature verification failed")
                raise ValueError("Invalid webhook signature")
    else:
        log.warning("agentmail_webhook_secret not set — skipping signature verification")

    payload = json.loads(body)
    event_type = payload.get("event_type", "")
    log.info("AgentMail webhook received: event_type=%s", event_type)

    if event_type != "message.received":
        log.info("AgentMail webhook: ignoring event type '%s'", event_type)
        return

    # AgentMail nests email data under "message", not "data"
    data = payload.get("message") or payload.get("data") or payload

    # Convert AgentMail payload to email_data format (same as _parse_email)
    # Extract attachment data — AgentMail webhooks only include attachment_id,
    # so we download content via the AgentMail API
    attachments = []
    message_id = data.get("message_id", data.get("id", ""))
    inbox_id = (resolve_plugin_setting(PLUGIN_NAME, "agentmail_address")
                or resolve_plugin_setting(PLUGIN_NAME, "agentmail_inbox_id") or "")

    for a in data.get("attachments", []):
        att = {
            "filename": a.get("filename", ""),
            "content_type": a.get("content_type", a.get("mime_type", "")),
            "data": b"",
        }
        # Try inline content first (base64 or raw)
        content = a.get("content", a.get("data", ""))
        if content:
            if isinstance(content, str):
                try:
                    import base64
                    att["data"] = base64.b64decode(content)
                except Exception:
                    att["data"] = content.encode("utf-8", errors="replace")
            elif isinstance(content, bytes):
                att["data"] = content
        # Try download URL
        if not att["data"] and a.get("url"):
            try:
                import httpx
                async with httpx.AsyncClient(timeout=10) as client:
                    r = await client.get(a["url"])
                if r.status_code == 200:
                    att["data"] = r.content
            except Exception as e:
                log.warning("AgentMail attachment download failed: %s", e)
        # Download via AgentMail SDK using attachment_id
        if not att["data"] and a.get("attachment_id") and inbox_id and message_id:
            am_client = _get_agentmail()
            if am_client:
                try:
                    file_data = await am_client.inboxes.messages.get_attachment(
                        inbox_id=inbox_id,
                        message_id=message_id,
                        attachment_id=a["attachment_id"],
                    )
                    log.info("AgentMail SDK get_attachment returned: type=%s",
                             type(file_data).__name__)
                    if file_data:
                        if isinstance(file_data, bytes):
                            att["data"] = file_data
                        elif isinstance(file_data, str):
                            att["data"] = file_data.encode("utf-8")
                        elif hasattr(file_data, 'download_url') and file_data.download_url:
                            # AttachmentResponse object — follow CDN URL
                            import httpx
                            cdn_url = file_data.download_url
                            log.info("AgentMail: following CDN URL for %s", att["filename"] or a["attachment_id"])
                            async with httpx.AsyncClient(timeout=15, follow_redirects=True) as cdn_client:
                                cdn_r = await cdn_client.get(cdn_url)
                            if cdn_r.status_code == 200:
                                att["data"] = cdn_r.content
                            else:
                                log.warning("CDN download %d: %s", cdn_r.status_code, cdn_r.text[:200])
                        elif hasattr(file_data, 'content') and file_data.content:
                            content = file_data.content
                            att["data"] = content if isinstance(content, bytes) else content.encode("utf-8")
                        elif hasattr(file_data, 'read'):
                            att["data"] = file_data.read()
                    # Update filename/content_type from SDK response if missing
                    if hasattr(file_data, 'filename') and file_data.filename and not att["filename"]:
                        att["filename"] = file_data.filename
                    if hasattr(file_data, 'content_type') and file_data.content_type and not att["content_type"]:
                        att["content_type"] = file_data.content_type
                    if att["data"]:
                        log.info("AgentMail SDK: downloaded %s (%d bytes, starts: %s)",
                                 att["filename"] or a["attachment_id"], len(att["data"]),
                                 att["data"][:50])
                except Exception as e:
                    log.warning("AgentMail SDK get_attachment failed for %s: %s", a["attachment_id"], e, exc_info=True)
            elif not am_client:
                log.warning("AgentMail: SDK client not available — cannot download attachments")
        attachments.append(att)
    if data.get("attachments"):
        log.info("AgentMail webhook: %d attachments, data present: %s",
                 len(attachments), [bool(a["data"]) for a in attachments])

    email_data = {
        "subject": data.get("subject", "(no subject)"),
        "sender": data.get("from_", data.get("from", "")),
        "to": ", ".join(data.get("to", [])) if isinstance(data.get("to"), list) else data.get("to", ""),
        "date": data.get("timestamp", data.get("created_at", "")),
        "message_id": data.get("message_id", data.get("id", "")),
        "body_text": data.get("text", data.get("extracted_text", "")),
        "body_html": data.get("html", data.get("extracted_html", "")),
        "attachments": attachments,
    }
    log.info("AgentMail webhook: from=%s subject=%s", email_data["sender"], email_data["subject"][:80])

    if not email_data["body_text"] and email_data["body_html"]:
        email_data["body_text"] = re.sub(r"<[^>]+>", " ", email_data["body_html"])
        email_data["body_text"] = re.sub(r"\s+", " ", email_data["body_text"]).strip()

    mailboxes = _get_mailboxes()
    if not mailboxes:
        log.warning("AgentMail webhook: no mailboxes configured")
        return
    mailbox_id = mailboxes[0]["id"]

    # Dedup by message_id
    uid_str = f"agentmail:{email_data['message_id']}"
    existing = _db().execute(
        "SELECT 1 FROM processed_emails WHERE mailbox_id = ? AND uid = ?",
        (mailbox_id, uid_str),
    ).fetchone()
    if existing:
        log.info("AgentMail webhook: already processed %s", uid_str)
        return

    # Load active rules from config.yaml and match
    rules = [r for r in _get_rules() if r["active"]]
    log.info("AgentMail webhook: %d active rules", len(rules))

    matched_rule = None
    for rule in rules:
        try:
            if await _match_rule(rule, email_data):
                matched_rule = rule
                break
        except Exception as e:
            log.error("Rule %d match error: %s", rule["id"], e)

    action_taken = ""
    if matched_rule:
        # Execute action (mb=None — AgentMail webhook isn't tied to a per-mailbox backend)
        action_fn = _ACTIONS.get(matched_rule["action"])
        if action_fn:
            try:
                await action_fn(email_data, matched_rule, "", None)
            except Exception as e:
                log.error("Action %s failed for rule %d: %s",
                          matched_rule["action"], matched_rule["id"], e)
        _update_rule_stats(matched_rule["rule_key"])
        action_taken = matched_rule["action"]
    elif _ai_fallback_enabled():
        log.info("AgentMail webhook: no rule matched, using AI fallback for '%s'", email_data["subject"][:80])
        await _ai_fallback(email_data)
        action_taken = "ai_fallback"
    else:
        log.info("AgentMail webhook: no rule matched for '%s'", email_data["subject"][:80])
        return

    # Emit events
    try:
        from cupbots.helpers.events import emit
        await emit("email.received", {
            "subject": email_data["subject"],
            "sender": email_data["sender"],
            "body_text": email_data["body_text"][:5000],
            "attachments": [
                {"filename": a["filename"], "content_type": a["content_type"]}
                for a in email_data["attachments"]
            ],
            "rule_action": action_taken,
            "source": "agentmail",
        })
    except Exception as e:
        log.debug("Event emission failed (non-critical): %s", e)

    # Record processed
    _db().execute(
        "INSERT OR IGNORE INTO processed_emails "
        "(company_id, mailbox_id, uid, message_id, subject, sender, rule_id, action_taken) "
        "VALUES ('', ?, ?, ?, ?, ?, NULL, ?)",
        (mailbox_id, uid_str, email_data["message_id"],
         email_data["subject"][:200], email_data["sender"][:200], action_taken),
    )
    _db().commit()
    log.info("AgentMail webhook processed: %s → %s", email_data["subject"][:60], action_taken)


# Register job handlers
register_handler("mailwatch_poll", _handle_poll_job)
register_handler("mailwatch_watchdog", _handle_watchdog_job)


# ---------------------------------------------------------------------------
# Rule parsing from chat
# ---------------------------------------------------------------------------

def _parse_rule_args(args: list[str]) -> dict | str:
    """Parse rule from args. Returns dict or error string.

    Format: <type> [field] <pattern> -> <action>
    """
    text = " ".join(args)

    if " -> " not in text:
        return "Missing ' -> ' separator. Format: /mailwatch add <type> [field] <pattern> -> <action>"

    match_part, action_part = text.split(" -> ", 1)
    action = action_part.strip().split()[0].lower()
    if action not in VALID_ACTIONS:
        return f"Invalid action '{action}'. Valid: {', '.join(VALID_ACTIONS)}"

    tokens = match_part.strip().split(None, 1)
    if not tokens:
        return "Missing rule type."

    rule_type = tokens[0].lower()
    if rule_type not in VALID_RULE_TYPES:
        return f"Invalid type '{rule_type}'. Valid: {', '.join(VALID_RULE_TYPES)}"

    rest = tokens[1] if len(tokens) > 1 else ""

    if rule_type == "attachment":
        pattern = rest.strip() or ".ics"
        return {
            "rule_type": rule_type,
            "match_field": "any",
            "match_pattern": pattern,
            "action": action,
        }

    if rule_type == "ai":
        # Everything after "ai" is the prompt
        pattern = rest.strip().strip('"')
        if not pattern:
            return "AI rules need a prompt. Example: /mailwatch add ai \"is this a client update?\" -> notify"
        return {
            "rule_type": rule_type,
            "match_field": "any",
            "match_pattern": pattern,
            "action": action,
        }

    # keyword or regex — optional field
    field = "any"
    pattern = rest
    rest_tokens = rest.split(None, 1)
    if rest_tokens and rest_tokens[0].lower() in VALID_FIELDS:
        field = rest_tokens[0].lower()
        pattern = rest_tokens[1] if len(rest_tokens) > 1 else ""

    pattern = pattern.strip().strip('"')
    if not pattern:
        return "Missing pattern."

    return {
        "rule_type": rule_type,
        "match_field": field,
        "match_pattern": pattern,
        "action": action,
    }


# ---------------------------------------------------------------------------
# Command handler
# ---------------------------------------------------------------------------

async def handle_command(msg, reply) -> bool:
    if msg.command != "mailwatch":
        return False

    args = msg.args
    company_id = ""  # Rules are global, not scoped per-group

    if not args:
        await reply.reply_text(__doc__.strip())
        return True

    sub = args[0].lower()

    if sub in ("--help", "-h", "help"):
        await reply.reply_text(__doc__.strip())
        return True

    if sub == "status":
        mailboxes = _get_mailboxes(company_id)
        if not mailboxes:
            await reply.reply_text("No mailbox configured. Add one in config.yaml under plugin_settings.mailwatch.mailboxes")
            return True
        rules = _get_rules()
        rule_count = len([r for r in rules if r["active"]])
        processed = _db().execute(
            "SELECT COUNT(*) as c FROM processed_emails",
        ).fetchone()["c"]
        lines = []
        for mb in mailboxes:
            status = "active" if mb["active"] else "stopped"
            last_poll = mb["last_poll"] or "never"
            error = f" | Error: {mb['last_error']}" if mb["last_error"] else ""
            lines.append(
                f"#{mb['id']} {mb['email']} ({mb['imap_host']})\n"
                f"  Status: {status} | Last poll: {last_poll}{error}"
            )
        header = f"*Mailwatch* — {len(mailboxes)} account(s), {rule_count} rules, {processed} processed\n"
        await reply.reply_text(header + "\n".join(lines))
        return True

    if sub == "rules":
        rules = _get_rules()
        if not rules:
            await reply.reply_text(
                "No rules configured. Add rules in config.yaml:\n"
                "plugin_settings.mailwatch.rules:\n"
                "  - type: keyword\n"
                "    field: subject\n"
                "    pattern: \"invoice\"\n"
                "    action: notify\n\n"
                "Or: /mailwatch add keyword subject \"invoice\" -> notify"
            )
            return True
        lines = ["*Mailwatch Rules:*\n"]
        ai_fb = "on" if _ai_fallback_enabled() else "off"
        lines.append(f"AI fallback: {ai_fb}\n")
        for r in rules:
            status = "" if r["active"] else " [disabled]"
            hits = f" ({r['hits']} hits)" if r["hits"] else ""
            lines.append(
                f"#{r['id']}{status} {r['rule_type']} "
                f"{r['match_field']}:{r['match_pattern'][:40]} -> {r['action']}{hits}"
            )
        await reply.reply_text("\n".join(lines))
        return True

    if sub == "add":
        parsed = _parse_rule_args(args[1:])
        if isinstance(parsed, str):
            await reply.reply_text(parsed)
            return True
        from cupbots.config import update_config_key
        settings = _get_plugin_settings()
        rules_list = list(settings.get("rules", []) or [])
        new_rule = {
            "type": parsed["rule_type"],
            "field": parsed["match_field"],
            "pattern": parsed["match_pattern"],
            "action": parsed["action"],
        }
        rules_list.append(new_rule)
        update_config_key("plugin_settings.mailwatch.rules", rules_list)
        await reply.reply_text(
            f"Rule #{len(rules_list)} added: {parsed['rule_type']} "
            f"{parsed['match_field']}:{parsed['match_pattern'][:50]} -> {parsed['action']}"
        )
        return True

    if sub == "remove":
        if len(args) < 2 or not args[1].isdigit():
            await reply.reply_text("Usage: /mailwatch remove <id>")
            return True
        rid = int(args[1])
        from cupbots.config import update_config_key
        settings = _get_plugin_settings()
        rules_list = list(settings.get("rules", []) or [])
        if rid < 1 or rid > len(rules_list):
            await reply.reply_text(f"Rule #{rid} not found. You have {len(rules_list)} rules.")
            return True
        removed = rules_list.pop(rid - 1)
        update_config_key("plugin_settings.mailwatch.rules", rules_list)
        await reply.reply_text(f"Removed rule #{rid}: {removed.get('type')} {removed.get('pattern', '')[:40]} -> {removed.get('action')}")
        return True

    if sub == "account":
        if len(args) < 2:
            await reply.reply_text(
                "Account management:\n"
                "  /mailwatch account add <email> <app_password> [imap_host]\n"
                "  /mailwatch account remove <email>\n"
                "  /mailwatch account list"
            )
            return True

        acct_sub = args[1].lower()

        if acct_sub == "add" and len(args) >= 4:
            acct_email = args[2]
            acct_password = args[3]
            acct_host = args[4] if len(args) > 4 else "imap.gmail.com"

            # Check for duplicate
            existing = _get_mailboxes(company_id)
            for mb in existing:
                if mb["email"] == acct_email:
                    await reply.reply_text(f"Account {acct_email} already exists. Remove it first.")
                    return True

            # Test connection before saving
            await reply.send_typing()
            test_mb = {"email": acct_email, "app_password": acct_password, "imap_host": acct_host, "imap_port": 993}
            result = _test_connection(test_mb)
            if "failed" in result.lower():
                await reply.reply_text(f"Connection test failed for {acct_email}:\n{result}")
                return True

            # Write to config.yaml
            from cupbots.config import update_config_key
            settings = _get_plugin_settings()
            mailboxes_list = list(settings.get("mailboxes", []) or [])
            mailboxes_list.append({
                "address": acct_email,
                "app_password": acct_password,
                "imap_host": acct_host,
            })
            update_config_key("plugin_settings.mailwatch.mailboxes", mailboxes_list)
            await reply.reply_text(f"Account added: {acct_email} ({acct_host})\n{result}\n\nUse /mailwatch start to begin polling all accounts.")
            return True

        if acct_sub == "remove" and len(args) >= 3:
            acct_email = args[2]
            settings = _get_plugin_settings()
            mailboxes_list = list(settings.get("mailboxes", []) or [])
            new_list = [mb for mb in mailboxes_list if mb.get("address") != acct_email]
            if len(new_list) == len(mailboxes_list):
                await reply.reply_text(f"Account not found: {acct_email}")
                return True
            from cupbots.config import update_config_key
            update_config_key("plugin_settings.mailwatch.mailboxes", new_list)
            await reply.reply_text(f"Account removed: {acct_email}")
            return True

        if acct_sub == "list":
            mailboxes = _get_mailboxes(company_id)
            if not mailboxes:
                await reply.reply_text("No accounts configured.\nAdd one: /mailwatch account add <email> <app_password>")
                return True
            lines = ["*Email Accounts:*\n"]
            for mb in mailboxes:
                status = "active" if mb["active"] else "stopped"
                lines.append(f"#{mb['id']} {mb['email']} ({mb['imap_host']}) — {status}")
            await reply.reply_text("\n".join(lines))
            return True

        await reply.reply_text("Usage: /mailwatch account add|remove|list")
        return True

    if sub == "start":
        mailboxes = _get_mailboxes(company_id)
        if not mailboxes:
            await reply.reply_text(
                "No accounts configured. Add one:\n"
                "/mailwatch account add your@gmail.com your-app-password\n\n"
                "Or edit config.yaml:\n"
                "plugin_settings.mailwatch.mailboxes"
            )
            return True

        await reply.send_typing()
        results = []
        for mb in mailboxes:
            result = _test_connection(mb)
            if "failed" in result.lower():
                results.append(f"{mb['email']}: {result}")
                continue

            _update_mailbox_state(mb["email"], active=1, last_error="")

            enqueue(
                "mailwatch_poll",
                {"company_id": company_id, "mailbox_id": mb["id"]},
                run_at=datetime.now() + timedelta(seconds=5),
                max_attempts=3,
            )
            results.append(f"{mb['email']}: {result} — polling every {mb['poll_interval']}s")

        # Start watchdog if not already running
        from cupbots.helpers.db import get_fw_db
        existing_wd = get_fw_db().execute(
            "SELECT 1 FROM jobs WHERE queue = 'mailwatch_watchdog' AND status = 'pending'",
        ).fetchone()
        if not existing_wd:
            enqueue(
                "mailwatch_watchdog", {"company_id": company_id},
                run_at=datetime.now() + timedelta(minutes=5),
                max_attempts=1,
            )

        await reply.reply_text("\n".join(results))
        return True

    if sub == "stop":
        mailboxes = _get_mailboxes(company_id)
        for mb in mailboxes:
            _update_mailbox_state(mb["email"], active=0)
        count = len(mailboxes)
        await reply.reply_text(f"Polling stopped for {count} account(s).")
        return True

    if sub == "test":
        if len(args) < 2 or not args[1].isdigit():
            await reply.reply_text("Usage: /mailwatch test <rule_id>")
            return True

        rid = int(args[1])
        rules = _get_rules()
        rule = next((r for r in rules if r["id"] == rid), None)
        if not rule:
            await reply.reply_text(f"Rule #{rid} not found. You have {len(rules)} rules.")
            return True

        await reply.send_typing()
        try:
            mb = _get_mailbox(company_id)
            conn = _connect_imap(mb)
            conn.select("INBOX", readonly=True)
            _, data = conn.search(None, "ALL")
            all_uids = data[0].split() if data[0] else []
            recent = all_uids[-5:] if len(all_uids) >= 5 else all_uids

            matches = []
            for uid in recent:
                _, msg_data = conn.fetch(uid, "(RFC822)")
                if not msg_data or not msg_data[0]:
                    continue
                email_data = _parse_email(msg_data[0][1])
                if await _match_rule(rule, email_data):
                    matches.append(f"  {email_data['sender']}: {email_data['subject'][:60]}")

            conn.close()
            conn.logout()

            if matches:
                await reply.reply_text(
                    f"Rule #{rid} matched {len(matches)}/5 recent emails:\n" +
                    "\n".join(matches)
                )
            else:
                await reply.reply_text(f"Rule #{rid} matched 0/5 recent emails.")
        except Exception as e:
            await reply.reply_text(f"Test failed: {e}")
        return True

    if sub == "send":
        # /mailwatch send <to> <subject> -- <body>
        rest = " ".join(args[1:])
        if " -- " not in rest or len(args) < 4:
            await reply.reply_text(
                "Usage: /mailwatch send <to> <subject> -- <body>\n"
                "Example: /mailwatch send user@example.com \"Meeting follow-up\" -- Hi, just following up..."
            )
            return True

        to_addr = args[1]
        subject_and_body = " ".join(args[2:])
        parts = subject_and_body.split(" -- ", 1)
        subject = parts[0].strip().strip('"')
        body = parts[1].strip() if len(parts) > 1 else ""

        if not body:
            await reply.reply_text("Missing email body after '--'")
            return True

        await reply.send_typing()
        result = await _agentmail_send(
            to=to_addr,
            subject=subject,
            body=body,
            category="manual",
        )
        if result:
            await reply.reply_text(f"Email sent to {to_addr}\nSubject: {subject}")
        else:
            await reply.reply_text(
                "Failed to send. Check agentmail_api_key and agentmail_address config."
            )
        return True

    if sub == "sent":
        limit = 10
        if len(args) > 1 and args[1].isdigit():
            limit = min(int(args[1]), 50)

        rows = _db().execute(
            "SELECT * FROM sent_emails ORDER BY sent_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
        if not rows:
            await reply.reply_text("No sent emails yet.")
            return True

        lines = [f"*Recent sent emails ({len(rows)}):*\n"]
        for r in rows:
            auto = "auto" if r["auto_sent"] else "approved"
            cat = f" [{r['category']}]" if r["category"] else ""
            lines.append(
                f"#{r['id']} {r['sent_at'][:16]} — {r['to_addr']}\n"
                f"  {r['subject'][:60]}{cat} ({auto})"
            )
        await reply.reply_text("\n".join(lines))
        return True

    if sub == "autosend":
        rules = _get_auto_send_rules()
        if not rules:
            await reply.reply_text(
                "No auto-send rules configured.\n"
                "Add to config.yaml under plugin_settings.mailwatch.auto_send_rules:\n"
                "  - category: meeting_confirmation\n"
                "    auto_send: true"
            )
            return True

        lines = ["*Auto-send rules:*\n"]
        for r in rules:
            status = "auto-send" if r.get("auto_send") else "approval"
            lines.append(f"  {r.get('category', '?')} → {status}")
        lines.append("\nUnlisted categories require approval.")
        await reply.reply_text("\n".join(lines))
        return True

    # Unknown subcommand
    await reply.reply_text(__doc__.strip())
    return True
