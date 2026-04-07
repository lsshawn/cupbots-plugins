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
  /mailwatch account add <email> <app_password> [host]  — Add a Gmail account
  /mailwatch account remove <email>  — Remove an account
  /mailwatch account list     — List all accounts
  /mailwatch send <to> <subject> -- <body>  — Send email via AgentMail
  /mailwatch sent [N]         — Show N most recent sent emails (default 10)
  /mailwatch autosend         — Show auto-send rules

Rule format: /mailwatch add <type> [field] <pattern> -> <action>
  Types: keyword, regex, attachment, ai
  Fields: subject, sender, body, any (default: any)
  Actions: notify, calendar, crm_update, draft_reply, custom

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
VALID_ACTIONS = ("notify", "calendar", "crm_update", "draft_reply", "custom")
MVP_ACTIONS = ("notify", "calendar")
_APPROVAL_TTL = 3600  # 1 hour, matches wa_router


# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------

def create_tables(conn):
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS mailboxes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            company_id TEXT NOT NULL DEFAULT '',
            email TEXT NOT NULL,
            imap_host TEXT NOT NULL DEFAULT 'imap.gmail.com',
            imap_port INTEGER NOT NULL DEFAULT 993,
            imap_password TEXT NOT NULL DEFAULT '',
            active INTEGER NOT NULL DEFAULT 1,
            poll_interval INTEGER NOT NULL DEFAULT 60,
            last_poll TEXT NOT NULL DEFAULT '',
            last_error TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            updated_at TEXT NOT NULL DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS rules (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            company_id TEXT NOT NULL DEFAULT '',
            mailbox_id INTEGER NOT NULL DEFAULT 1,
            name TEXT NOT NULL DEFAULT '',
            rule_type TEXT NOT NULL CHECK (rule_type IN ('keyword', 'regex', 'attachment', 'ai')),
            match_field TEXT NOT NULL DEFAULT 'any',
            match_pattern TEXT NOT NULL DEFAULT '',
            action TEXT NOT NULL CHECK (action IN ('notify', 'calendar', 'crm_update', 'draft_reply', 'custom')),
            action_config TEXT NOT NULL DEFAULT '{}',
            active INTEGER NOT NULL DEFAULT 1,
            priority INTEGER NOT NULL DEFAULT 0,
            hits INTEGER NOT NULL DEFAULT 0,
            last_hit TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            updated_at TEXT NOT NULL DEFAULT (datetime('now'))
        );
        CREATE INDEX IF NOT EXISTS idx_rules_company ON rules (company_id, active);

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


_migrated = False


def _migrate_db():
    """Add imap_password column if missing (for existing installs)."""
    global _migrated
    if _migrated:
        return
    try:
        conn = _db()
        cols = [r[1] for r in conn.execute("PRAGMA table_info(mailboxes)").fetchall()]
        if "imap_password" not in cols:
            conn.execute("ALTER TABLE mailboxes ADD COLUMN imap_password TEXT NOT NULL DEFAULT ''")
            conn.commit()
            log.info("Migrated mailboxes table: added imap_password column")
    except Exception as e:
        log.warning("Migration check failed: %s", e)
    _migrated = True


# ---------------------------------------------------------------------------
# Config helpers
# ---------------------------------------------------------------------------

def _get_default_credentials():
    """Get shared credentials from plugin config (backward compat)."""
    email_addr = resolve_plugin_setting(PLUGIN_NAME, "mailwatch_email") or ""
    password = resolve_plugin_setting(PLUGIN_NAME, "mailwatch_app_password") or ""
    host = resolve_plugin_setting(PLUGIN_NAME, "mailwatch_imap_host") or "imap.gmail.com"
    return email_addr, password, host, 993


def _get_mailbox_credentials(mb: dict) -> tuple[str, str, str, int]:
    """Get credentials for a specific mailbox. Falls back to shared config if no per-mailbox password."""
    email_addr = mb.get("email", "")
    password = mb.get("imap_password", "")
    host = mb.get("imap_host", "imap.gmail.com")
    port = mb.get("imap_port", 993)

    if not password:
        # Fallback to shared plugin config
        _, default_pw, default_host, default_port = _get_default_credentials()
        password = default_pw
        if not host or host == "imap.gmail.com":
            host = default_host or "imap.gmail.com"
        if not port:
            port = default_port

    return email_addr, password, host, port


def _get_notify_chat():
    return resolve_plugin_setting(PLUGIN_NAME, "mailwatch_notify_chat") or ""


# ---------------------------------------------------------------------------
# AgentMail outbound
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

    inbox_id = resolve_plugin_setting(PLUGIN_NAME, "agentmail_inbox_id")
    if not inbox_id:
        log.warning("agentmail_inbox_id not configured")
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

    if emoji_val in ("\U0001f44d", "\u2705", "\U0001f44c"):  # 👍 ✅ 👌
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


def _get_mailboxes(company_id: str) -> list[dict]:
    """Get all mailboxes for a company."""
    _migrate_db()
    rows = _db().execute(
        "SELECT * FROM mailboxes WHERE company_id = ? ORDER BY id",
        (company_id,),
    ).fetchall()
    return [dict(r) for r in rows]


def _get_mailbox(company_id: str, mailbox_id: int | None = None) -> dict | None:
    _migrate_db()
    if mailbox_id:
        row = _db().execute(
            "SELECT * FROM mailboxes WHERE company_id = ? AND id = ?",
            (company_id, mailbox_id),
        ).fetchone()
    else:
        row = _db().execute(
            "SELECT * FROM mailboxes WHERE company_id = ? ORDER BY id LIMIT 1",
            (company_id,),
        ).fetchone()
    return dict(row) if row else None


def _get_or_create_mailbox(company_id: str) -> dict:
    mb = _get_mailbox(company_id)
    if mb:
        return mb
    email_addr, password, host, port = _get_default_credentials()
    _migrate_db()
    _db().execute(
        "INSERT INTO mailboxes (company_id, email, imap_host, imap_port, imap_password) VALUES (?, ?, ?, ?, ?)",
        (company_id, email_addr, host, port, password),
    )
    _db().commit()
    return dict(_db().execute(
        "SELECT * FROM mailboxes WHERE company_id = ? ORDER BY id DESC LIMIT 1",
        (company_id,),
    ).fetchone())


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

async def _action_notify(email_data: dict, rule: dict, company_id: str):
    chat = _get_notify_chat()
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


async def _action_calendar(email_data: dict, rule: dict, company_id: str):
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
        chat = _get_notify_chat()
        if chat:
            ctx = WhatsAppReplyContext(chat, WA_API_URL)
            await ctx.reply_text(
                f"Imported {imported} event(s) from: {email_data['subject']}"
            )


async def _action_crm_update(email_data: dict, rule: dict, company_id: str):
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
        await _action_notify(email_data, rule, company_id)
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

    await _action_notify(email_data, rule, company_id)


async def _action_draft_reply(email_data: dict, rule: dict, company_id: str):
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

    chat = _get_notify_chat()
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


async def _action_custom(email_data: dict, rule: dict, company_id: str):
    try:
        from cupbots.helpers.llm import ask_llm
    except ImportError:
        return

    config = json.loads(rule.get("action_config", "{}"))
    prompt = config.get("prompt", "Analyze this email.")

    result = await ask_llm(
        f"{prompt}\n\nFrom: {email_data['sender']}\n"
        f"Subject: {email_data['subject']}\n"
        f"Body:\n{email_data['body_text'][:3000]}",
        max_tokens=1024,
    )

    chat = _get_notify_chat()
    if chat and result:
        ctx = WhatsAppReplyContext(chat, WA_API_URL)
        await ctx.reply_text(f"Mailwatch AI [{rule['name'] or rule['id']}]:\n\n{result}")


_ACTIONS = {
    "notify": _action_notify,
    "calendar": _action_calendar,
    "crm_update": _action_crm_update,
    "draft_reply": _action_draft_reply,
    "custom": _action_custom,
}


# ---------------------------------------------------------------------------
# IMAP connection
# ---------------------------------------------------------------------------

def _connect_imap(mb: dict | None = None) -> imaplib.IMAP4_SSL:
    """Connect to IMAP. Uses per-mailbox credentials if mb is provided, else shared config."""
    if mb:
        email_addr, password, host, port = _get_mailbox_credentials(mb)
    else:
        email_addr, password, host, port = _get_default_credentials()
    if not email_addr or not password:
        raise ValueError("Email credentials not configured. Use /mailwatch account add or /config mailwatch")
    conn = imaplib.IMAP4_SSL(host, port)
    conn.login(email_addr, password)
    return conn


def _test_connection(mb: dict | None = None) -> str:
    """Test IMAP connection for a specific mailbox or default. Returns status message."""
    try:
        conn = _connect_imap(mb)
        conn.select("INBOX", readonly=True)
        status, data = conn.search(None, "ALL")
        total = len(data[0].split()) if data[0] else 0
        conn.close()
        conn.logout()
        if mb:
            email_addr = mb.get("email", "")
            host = mb.get("imap_host", "")
        else:
            email_addr, _, host, _ = _get_default_credentials()
        return f"Connected to {email_addr} ({host}). {total} total emails in inbox."
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
        conn = _connect_imap(mb)
        conn.select("INBOX")

        _, data = conn.search(None, "UNSEEN")
        uids = data[0].split() if data[0] else []

        if not uids:
            conn.close()
            conn.logout()
            _db().execute(
                "UPDATE mailboxes SET last_poll = datetime('now'), last_error = '' WHERE id = ?",
                (mb["id"],),
            )
            _db().commit()
            _reschedule(payload, mb["poll_interval"])
            return

        # Load active rules
        rules = _db().execute(
            "SELECT * FROM rules WHERE company_id = ? AND active = 1 ORDER BY priority DESC",
            (company_id,),
        ).fetchall()

        for uid in uids:
            uid_str = uid.decode()

            # Dedup check
            existing = _db().execute(
                "SELECT 1 FROM processed_emails WHERE company_id = ? AND mailbox_id = ? AND uid = ?",
                (company_id, mailbox_id, uid_str),
            ).fetchone()
            if existing:
                continue

            # Fetch email
            _, msg_data = conn.fetch(uid, "(RFC822)")
            if not msg_data or not msg_data[0]:
                continue
            raw = msg_data[0][1]
            email_data = _parse_email(raw)

            # Match rules (first match wins)
            matched_rule = None
            for rule in rules:
                try:
                    if await _match_rule(dict(rule), email_data):
                        matched_rule = dict(rule)
                        break
                except Exception as e:
                    log.error("Rule %d match error: %s", rule["id"], e)

            if matched_rule:
                # Execute action
                action_fn = _ACTIONS.get(matched_rule["action"])
                if action_fn:
                    try:
                        await action_fn(email_data, matched_rule, company_id)
                    except Exception as e:
                        log.error("Action %s failed for rule %d: %s",
                                  matched_rule["action"], matched_rule["id"], e)

                # Emit events for inter-plugin hooks (wiki, etc.)
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
                        "rule_action": matched_rule["action"],
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

                # Record processed
                _db().execute(
                    "INSERT OR IGNORE INTO processed_emails "
                    "(company_id, mailbox_id, uid, message_id, subject, sender, rule_id, action_taken) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (company_id, mailbox_id, uid_str, email_data["message_id"],
                     email_data["subject"][:200], email_data["sender"][:200],
                     matched_rule["id"], matched_rule["action"]),
                )

                # Update rule stats
                _db().execute(
                    "UPDATE rules SET hits = hits + 1, last_hit = datetime('now') WHERE id = ?",
                    (matched_rule["id"],),
                )
                _db().commit()

                # Mark as seen in IMAP
                conn.store(uid, "+FLAGS", "\\Seen")
            # No match = email stays UNSEEN, will be rechecked on next poll.
            # This way, adding a new rule catches previously unmatched emails.

        conn.close()
        conn.logout()

        _db().execute(
            "UPDATE mailboxes SET last_poll = datetime('now'), last_error = '' WHERE id = ?",
            (mb["id"],),
        )
        _db().commit()

    except Exception as e:
        log.error("Poll failed for %s: %s", company_id, e)
        _db().execute(
            "UPDATE mailboxes SET last_error = ? WHERE id = ?",
            (str(e)[:500], mb["id"]),
        )
        _db().commit()
        raise  # Let jobs.py handle retry/backoff

    _reschedule(payload, mb["poll_interval"])


def _reschedule(payload: dict, interval: int):
    enqueue(
        "mailwatch_poll", payload,
        run_at=datetime.now() + timedelta(seconds=interval),
        max_attempts=3,
    )


async def _handle_watchdog_job(payload: dict, bot=None):
    """Re-enqueue polls for active mailboxes whose last_poll is stale."""
    cutoff = (datetime.now() - timedelta(minutes=3)).isoformat()

    stale = _db().execute(
        "SELECT * FROM mailboxes WHERE active = 1 AND (last_poll < ? OR last_poll = '')",
        (cutoff,),
    ).fetchall()

    for mb in stale:
        log.info("Watchdog: re-enqueuing poll for %s (last_poll: %s)",
                 mb["company_id"], mb["last_poll"])
        enqueue(
            "mailwatch_poll",
            {"company_id": mb["company_id"], "mailbox_id": mb["id"]},
            run_at=datetime.now() + timedelta(seconds=10),
            max_attempts=3,
        )

    # Reschedule watchdog
    enqueue(
        "mailwatch_watchdog", {},
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
            wh = Webhook(webhook_secret)
            wh.verify(body, headers)
        except WebhookVerificationError:
            log.warning("AgentMail webhook signature verification failed")
            raise ValueError("Invalid webhook signature")
    else:
        log.warning("AGENTMAIL_WEBHOOK_SECRET not set — skipping signature verification")

    payload = json.loads(body)
    event_type = payload.get("event_type", "")

    if event_type != "message.received":
        log.debug("Ignoring AgentMail event: %s", event_type)
        return

    data = payload.get("data", payload)

    # Convert AgentMail payload to email_data format (same as _parse_email)
    email_data = {
        "subject": data.get("subject", "(no subject)"),
        "sender": data.get("from_", data.get("from", "")),
        "to": ", ".join(data.get("to", [])) if isinstance(data.get("to"), list) else data.get("to", ""),
        "date": data.get("created_at", ""),
        "message_id": data.get("message_id", data.get("id", "")),
        "body_text": data.get("text", ""),
        "body_html": data.get("html", ""),
        "attachments": [
            {"filename": a.get("filename", ""), "content_type": a.get("content_type", ""), "data": b""}
            for a in data.get("attachments", [])
        ],
    }

    if not email_data["body_text"] and email_data["body_html"]:
        email_data["body_text"] = re.sub(r"<[^>]+>", " ", email_data["body_html"])
        email_data["body_text"] = re.sub(r"\s+", " ", email_data["body_text"]).strip()

    # Resolve company_id from inbox_id → mailbox mapping
    inbox_id = data.get("inbox_id", "")
    configured_inbox = resolve_plugin_setting(PLUGIN_NAME, "agentmail_inbox_id")

    # Find company_id: match by configured inbox, or fall back to first active mailbox
    company_id = ""
    mailbox_id = 0
    if inbox_id and inbox_id == configured_inbox:
        # Use the first active mailbox's company_id (single-tenant typical case)
        mb = _db().execute(
            "SELECT id, company_id FROM mailboxes WHERE active = 1 ORDER BY id LIMIT 1"
        ).fetchone()
        if mb:
            company_id = mb["company_id"]
            mailbox_id = mb["id"]

    if not company_id:
        log.warning("AgentMail webhook: could not resolve company_id for inbox %s", inbox_id)
        return

    # Dedup by message_id
    uid_str = f"agentmail:{email_data['message_id']}"
    existing = _db().execute(
        "SELECT 1 FROM processed_emails WHERE company_id = ? AND mailbox_id = ? AND uid = ?",
        (company_id, mailbox_id, uid_str),
    ).fetchone()
    if existing:
        log.debug("AgentMail webhook: already processed %s", uid_str)
        return

    # Load active rules and match
    rules = _db().execute(
        "SELECT * FROM rules WHERE company_id = ? AND active = 1 ORDER BY priority DESC",
        (company_id,),
    ).fetchall()

    matched_rule = None
    for rule in rules:
        try:
            if await _match_rule(dict(rule), email_data):
                matched_rule = dict(rule)
                break
        except Exception as e:
            log.error("Rule %d match error: %s", rule["id"], e)

    if not matched_rule:
        log.debug("AgentMail webhook: no rule matched for %s", email_data["subject"][:80])
        return

    # Execute action
    action_fn = _ACTIONS.get(matched_rule["action"])
    if action_fn:
        try:
            await action_fn(email_data, matched_rule, company_id)
        except Exception as e:
            log.error("Action %s failed for rule %d: %s",
                      matched_rule["action"], matched_rule["id"], e)

    # Emit events
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
            "rule_action": matched_rule["action"],
            "source": "agentmail",
        })
    except Exception as e:
        log.debug("Event emission failed (non-critical): %s", e)

    # Record processed
    _db().execute(
        "INSERT OR IGNORE INTO processed_emails "
        "(company_id, mailbox_id, uid, message_id, subject, sender, rule_id, action_taken) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (company_id, mailbox_id, uid_str, email_data["message_id"],
         email_data["subject"][:200], email_data["sender"][:200],
         matched_rule["id"], matched_rule["action"]),
    )
    _db().execute(
        "UPDATE rules SET hits = hits + 1, last_hit = datetime('now') WHERE id = ?",
        (matched_rule["id"],),
    )
    _db().commit()
    log.info("AgentMail webhook processed: %s → %s", email_data["subject"][:60], matched_rule["action"])


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
    company_id = msg.company_id or ""

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
            await reply.reply_text("No mailbox configured. Use /mailwatch account add or /mailwatch start")
            return True
        rule_count = _db().execute(
            "SELECT COUNT(*) as c FROM rules WHERE company_id = ? AND active = 1",
            (company_id,),
        ).fetchone()["c"]
        processed = _db().execute(
            "SELECT COUNT(*) as c FROM processed_emails WHERE company_id = ?",
            (company_id,),
        ).fetchone()["c"]
        lines = []
        for mb in mailboxes:
            status = "active" if mb["active"] else "stopped"
            last_poll = mb["last_poll"] or "never"
            error = f" | Error: {mb['last_error']}" if mb["last_error"] else ""
            has_pw = "yes" if mb.get("imap_password") else "shared"
            lines.append(
                f"#{mb['id']} {mb['email']} ({mb['imap_host']})\n"
                f"  Status: {status} | Password: {has_pw} | Last poll: {last_poll}{error}"
            )
        header = f"*Mailwatch* — {len(mailboxes)} account(s), {rule_count} rules, {processed} processed\n"
        await reply.reply_text(header + "\n".join(lines))
        return True

    if sub == "rules":
        rules = _db().execute(
            "SELECT * FROM rules WHERE company_id = ? ORDER BY priority DESC, id",
            (company_id,),
        ).fetchall()
        if not rules:
            await reply.reply_text("No rules. Add one:\n/mailwatch add keyword subject \"invoice\" -> notify")
            return True
        lines = ["Mailwatch Rules:\n"]
        for r in rules:
            status = "" if r["active"] else " [disabled]"
            hits = f" ({r['hits']} hits)" if r["hits"] else ""
            name = f" {r['name']}" if r["name"] else ""
            lines.append(
                f"#{r['id']}{name}{status} {r['rule_type']} "
                f"{r['match_field']}:{r['match_pattern'][:40]} -> {r['action']}{hits}"
            )
        await reply.reply_text("\n".join(lines))
        return True

    if sub == "add":
        parsed = _parse_rule_args(args[1:])
        if isinstance(parsed, str):
            await reply.reply_text(parsed)
            return True
        _db().execute(
            "INSERT INTO rules (company_id, rule_type, match_field, match_pattern, action, name) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (company_id, parsed["rule_type"], parsed["match_field"],
             parsed["match_pattern"], parsed["action"],
             f"{parsed['rule_type']}_{parsed['action']}"),
        )
        _db().commit()
        rid = _db().execute("SELECT last_insert_rowid() as id").fetchone()["id"]
        await reply.reply_text(
            f"Rule #{rid} added: {parsed['rule_type']} "
            f"{parsed['match_field']}:{parsed['match_pattern'][:50]} -> {parsed['action']}"
        )
        return True

    if sub == "remove":
        if len(args) < 2 or not args[1].isdigit():
            await reply.reply_text("Usage: /mailwatch remove <id>")
            return True
        rid = int(args[1])
        deleted = _db().execute(
            "DELETE FROM rules WHERE id = ? AND company_id = ?",
            (rid, company_id),
        ).rowcount
        _db().commit()
        await reply.reply_text(f"Removed rule #{rid}" if deleted else f"Rule #{rid} not found.")
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
            existing = _db().execute(
                "SELECT id FROM mailboxes WHERE company_id = ? AND email = ?",
                (company_id, acct_email),
            ).fetchone()
            if existing:
                await reply.reply_text(f"Account {acct_email} already exists (#{existing['id']}). Remove it first.")
                return True

            # Test connection before saving
            await reply.send_typing()
            test_mb = {"email": acct_email, "imap_password": acct_password, "imap_host": acct_host, "imap_port": 993}
            result = _test_connection(test_mb)
            if "failed" in result.lower():
                await reply.reply_text(f"Connection test failed for {acct_email}:\n{result}")
                return True

            _migrate_db()
            _db().execute(
                "INSERT INTO mailboxes (company_id, email, imap_host, imap_port, imap_password) VALUES (?, ?, ?, ?, ?)",
                (company_id, acct_email, acct_host, 993, acct_password),
            )
            _db().commit()
            await reply.reply_text(f"Account added: {acct_email} ({acct_host})\n{result}\n\nUse /mailwatch start to begin polling all accounts.")
            return True

        if acct_sub == "remove" and len(args) >= 3:
            acct_email = args[2]
            row = _db().execute(
                "SELECT id FROM mailboxes WHERE company_id = ? AND email = ?",
                (company_id, acct_email),
            ).fetchone()
            if not row:
                await reply.reply_text(f"Account not found: {acct_email}")
                return True
            _db().execute("DELETE FROM mailboxes WHERE id = ?", (row["id"],))
            _db().commit()
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
                has_pw = "per-account" if mb.get("imap_password") else "shared config"
                lines.append(f"#{mb['id']} {mb['email']} ({mb['imap_host']}) — {status}, creds: {has_pw}")
            await reply.reply_text("\n".join(lines))
            return True

        await reply.reply_text("Usage: /mailwatch account add|remove|list")
        return True

    if sub == "start":
        mailboxes = _get_mailboxes(company_id)
        if not mailboxes:
            # Try legacy shared config
            email_addr, password, _, _ = _get_default_credentials()
            if not email_addr or not password:
                await reply.reply_text(
                    "No accounts configured. Add one:\n"
                    "/mailwatch account add your@gmail.com your-app-password\n\n"
                    "Or use shared config:\n"
                    "/config mailwatch MAILWATCH_EMAIL your@gmail.com\n"
                    "/config mailwatch MAILWATCH_APP_PASSWORD your-app-password"
                )
                return True
            # Create from shared config (backward compat)
            mailboxes = [_get_or_create_mailbox(company_id)]

        await reply.send_typing()
        results = []
        for mb in mailboxes:
            result = _test_connection(mb)
            if "failed" in result.lower():
                results.append(f"{mb['email']}: {result}")
                continue

            _db().execute(
                "UPDATE mailboxes SET active = 1, last_error = '' WHERE id = ?",
                (mb["id"],),
            )
            _db().commit()

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
                "mailwatch_watchdog", {},
                run_at=datetime.now() + timedelta(minutes=5),
                max_attempts=1,
            )

        await reply.reply_text("\n".join(results))
        return True

    if sub == "stop":
        mailboxes = _get_mailboxes(company_id)
        for mb in mailboxes:
            _db().execute("UPDATE mailboxes SET active = 0 WHERE id = ?", (mb["id"],))
        _db().commit()
        count = len(mailboxes)
        await reply.reply_text(f"Polling stopped for {count} account(s).")
        return True

    if sub == "test":
        if len(args) < 2 or not args[1].isdigit():
            await reply.reply_text("Usage: /mailwatch test <rule_id>")
            return True

        rid = int(args[1])
        rule = _db().execute(
            "SELECT * FROM rules WHERE id = ? AND company_id = ?",
            (rid, company_id),
        ).fetchone()
        if not rule:
            await reply.reply_text(f"Rule #{rid} not found.")
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
                if await _match_rule(dict(rule), email_data):
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
                "Failed to send. Check AGENTMAIL_API_KEY and AGENTMAIL_INBOX_ID config."
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
