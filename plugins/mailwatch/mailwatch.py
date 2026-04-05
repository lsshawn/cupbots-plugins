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
"""

import email as email_lib
import imaplib
import json
import os
import re
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
    """)


def _db():
    return get_plugin_db(PLUGIN_NAME)


# ---------------------------------------------------------------------------
# Config helpers
# ---------------------------------------------------------------------------

def _get_credentials():
    email_addr = resolve_plugin_setting(PLUGIN_NAME, "MAILWATCH_EMAIL") or ""
    password = resolve_plugin_setting(PLUGIN_NAME, "MAILWATCH_APP_PASSWORD") or ""
    host = resolve_plugin_setting(PLUGIN_NAME, "MAILWATCH_IMAP_HOST") or "imap.gmail.com"
    return email_addr, password, host, 993


def _get_notify_chat():
    return resolve_plugin_setting(PLUGIN_NAME, "MAILWATCH_NOTIFY_CHAT") or ""


def _get_mailbox(company_id: str) -> dict | None:
    row = _db().execute(
        "SELECT * FROM mailboxes WHERE company_id = ? ORDER BY id LIMIT 1",
        (company_id,),
    ).fetchone()
    return dict(row) if row else None


def _get_or_create_mailbox(company_id: str) -> dict:
    mb = _get_mailbox(company_id)
    if mb:
        return mb
    email_addr, _, host, port = _get_credentials()
    _db().execute(
        "INSERT INTO mailboxes (company_id, email, imap_host, imap_port) VALUES (?, ?, ?, ?)",
        (company_id, email_addr, host, port),
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

    chat = _get_notify_chat()
    if chat and draft:
        ctx = WhatsAppReplyContext(chat, WA_API_URL)
        await ctx.reply_text(
            f"Draft reply to: {email_data['sender']}\n"
            f"Re: {email_data['subject']}\n"
            f"---\n{draft}\n---\n"
            f"(Review only)"
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

def _connect_imap() -> imaplib.IMAP4_SSL:
    email_addr, password, host, port = _get_credentials()
    if not email_addr or not password:
        raise ValueError("Email credentials not configured. Use /config mailwatch")
    conn = imaplib.IMAP4_SSL(host, port)
    conn.login(email_addr, password)
    return conn


def _test_connection() -> str:
    """Test IMAP connection. Returns status message."""
    try:
        conn = _connect_imap()
        conn.select("INBOX", readonly=True)
        status, data = conn.search(None, "ALL")
        total = len(data[0].split()) if data[0] else 0
        conn.close()
        conn.logout()
        email_addr, _, host, _ = _get_credentials()
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

    mb = _get_mailbox(company_id)
    if not mb or not mb["active"]:
        log.info("Mailbox inactive for %s, not rescheduling", company_id)
        return

    try:
        conn = _connect_imap()
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
        mb = _get_mailbox(company_id)
        if not mb:
            await reply.reply_text("No mailbox configured. Use /mailwatch start")
            return True
        rule_count = _db().execute(
            "SELECT COUNT(*) as c FROM rules WHERE company_id = ? AND active = 1",
            (company_id,),
        ).fetchone()["c"]
        processed = _db().execute(
            "SELECT COUNT(*) as c FROM processed_emails WHERE company_id = ?",
            (company_id,),
        ).fetchone()["c"]
        status = "active" if mb["active"] else "stopped"
        last_poll = mb["last_poll"] or "never"
        error = f"\nLast error: {mb['last_error']}" if mb["last_error"] else ""
        await reply.reply_text(
            f"Mailwatch: {status}\n"
            f"Email: {mb['email']}\n"
            f"Last poll: {last_poll}\n"
            f"Rules: {rule_count} active\n"
            f"Processed: {processed} emails{error}"
        )
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

    if sub == "start":
        email_addr, password, _, _ = _get_credentials()
        if not email_addr or not password:
            await reply.reply_text(
                "Configure credentials first:\n"
                "/config mailwatch MAILWATCH_EMAIL your@gmail.com\n"
                "/config mailwatch MAILWATCH_APP_PASSWORD your-app-password\n"
                "/config mailwatch MAILWATCH_NOTIFY_CHAT your-jid"
            )
            return True

        await reply.send_typing()
        result = _test_connection()
        if "failed" in result.lower():
            await reply.reply_text(result)
            return True

        mb = _get_or_create_mailbox(company_id)
        _db().execute(
            "UPDATE mailboxes SET active = 1, last_error = '' WHERE id = ?",
            (mb["id"],),
        )
        _db().commit()

        # Enqueue first poll
        enqueue(
            "mailwatch_poll",
            {"company_id": company_id, "mailbox_id": mb["id"]},
            run_at=datetime.now() + timedelta(seconds=5),
            max_attempts=3,
        )

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

        await reply.reply_text(f"{result}\nPolling started (every {mb['poll_interval']}s).")
        return True

    if sub == "stop":
        mb = _get_mailbox(company_id)
        if mb:
            _db().execute("UPDATE mailboxes SET active = 0 WHERE id = ?", (mb["id"],))
            _db().commit()
        await reply.reply_text("Polling stopped.")
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
            conn = _connect_imap()
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

    # Unknown subcommand
    await reply.reply_text(__doc__.strip())
    return True
