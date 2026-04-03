"""
Contacts CRM

Commands (works in any topic):
  /crm                    — Show contacts due for a check-in
  /whois <name>           — Look up a contact (fuzzy match)
  /remember <name> — <note> — Add a note about someone
  /addcontact <name>      — Add a new contact
  /contacts [tag|tier]    — Search/filter contacts
"""

import re
from datetime import datetime, timedelta

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
)

from cupbots.helpers.db import get_plugin_db
from cupbots.helpers.logger import get_logger

log = get_logger("contacts")

# Tier definitions: name -> (label, contact interval in days)
TIERS = {
    "A": ("Very important", 21),
    "B": ("Important", 60),
    "C": ("Regular", 180),
    "D": ("Keep warm", 365),
}

TIER_EMOJI = {"A": "\U0001f525", "B": "\u2b50", "C": "\U0001f465", "D": "\U0001f4c1"}

PLUGIN_NAME = "contacts"


def create_tables(conn):
    """Create contacts and interactions tables. Called by get_plugin_db on first access."""
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS contacts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            tier TEXT NOT NULL DEFAULT 'C' CHECK (tier IN ('A','B','C','D')),
            location TEXT NOT NULL DEFAULT '',
            handles TEXT NOT NULL DEFAULT '',
            tags TEXT NOT NULL DEFAULT '',
            notes TEXT NOT NULL DEFAULT '',
            last_contact TEXT NOT NULL DEFAULT '',
            next_contact TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            updated_at TEXT NOT NULL DEFAULT (datetime('now'))
        );
        CREATE INDEX IF NOT EXISTS idx_contacts_name ON contacts (name);
        CREATE INDEX IF NOT EXISTS idx_contacts_tier ON contacts (tier);

        CREATE TABLE IF NOT EXISTS interactions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            contact_id INTEGER NOT NULL REFERENCES contacts(id) ON DELETE CASCADE,
            channel TEXT NOT NULL DEFAULT '',
            summary TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        );
    """)


def _db():
    """Get the contacts plugin database connection."""
    return get_plugin_db(PLUGIN_NAME)


def _compute_next_contact(tier: str, last_contact: str | None) -> str | None:
    """Compute next contact date based on tier interval."""
    if not last_contact:
        return None
    interval = TIERS.get(tier, TIERS["C"])[1]
    last_dt = datetime.fromisoformat(last_contact)
    return (last_dt + timedelta(days=interval)).strftime("%Y-%m-%d")


async def _find_contacts(query: str) -> list[dict]:
    """Find contacts by name, tags, handles, or location."""
    conn = _db()
    pattern = f"%{query}%"
    rows = conn.execute(
        "SELECT * FROM contacts WHERE name LIKE ? OR tags LIKE ? OR handles LIKE ? OR location LIKE ?",
        (pattern, pattern, pattern, pattern),
    ).fetchall()
    return [dict(r) for r in rows]


def _format_contact(c: dict, verbose: bool = False) -> str:
    """Format a contact for display."""
    tier = c.get("tier", "C")
    emoji = TIER_EMOJI.get(tier, "")
    lines = [f"{emoji} {c.get('name', '?')} [{tier}]"]

    if c.get("location"):
        lines[0] += f" - {c['location']}"

    if verbose:
        if c.get("handles"):
            lines.append(f"  Handles: {c['handles']}")
        if c.get("tags"):
            lines.append(f"  Tags: {c['tags']}")
        if c.get("notes"):
            lines.append(f"  Notes: {c['notes']}")
        if c.get("last_contact"):
            lines.append(f"  Last contact: {c['last_contact']}")
        if c.get("next_contact"):
            overdue = ""
            try:
                nxt = datetime.fromisoformat(c["next_contact"])
                if nxt < datetime.now():
                    days_late = (datetime.now() - nxt).days
                    overdue = f" (overdue by {days_late}d)"
            except ValueError:
                pass
            lines.append(f"  Next contact: {c['next_contact']}{overdue}")

    return "\n".join(lines)


def _format_interactions(interactions: list[dict], limit: int = 5) -> str:
    """Format recent interactions."""
    if not interactions:
        return "  No interactions logged."
    lines = []
    for i in interactions[:limit]:
        channel = f"[{i['channel']}] " if i.get("channel") else ""
        created = (i.get("created_at") or "")[:10]
        lines.append(f"  {created} {channel}{i.get('summary', '')}")
    return "\n".join(lines)


# --- Telegram Commands ---


async def cmd_crm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show contacts due for a check-in."""
    if not update.message:
        return

    conn = _db()
    today = datetime.now().strftime("%Y-%m-%d")
    overdue = conn.execute(
        "SELECT * FROM contacts WHERE next_contact != '' AND next_contact <= ?",
        (today,),
    ).fetchall()
    never = conn.execute(
        "SELECT * FROM contacts WHERE last_contact = ''"
    ).fetchall()

    if not overdue and not never:
        await update.message.reply_text("All caught up! No contacts due for a check-in.")
        return

    lines = []
    if overdue:
        lines.append(f"Overdue ({len(overdue)}):\n")
        for c in overdue:
            days_late = (datetime.now() - datetime.fromisoformat(c["next_contact"])).days
            emoji = TIER_EMOJI.get(c["tier"], "")
            lines.append(f"  {emoji} {c['name']} [{c['tier']}] — {days_late}d overdue")
        lines.append("")

    if never:
        lines.append(f"Never contacted ({len(never)}):\n")
        for c in list(never)[:10]:
            emoji = TIER_EMOJI.get(c["tier"], "")
            lines.append(f"  {emoji} {c['name']} [{c['tier']}]")
        if len(never) > 10:
            lines.append(f"  ... and {len(never) - 10} more")

    await update.message.reply_text("\n".join(lines))


async def cmd_whois(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Look up a contact with full details + recent interactions."""
    if not update.message or not context.args:
        await update.message.reply_text("Usage: /whois <name>")
        return

    query = " ".join(context.args)
    results = await _find_contacts(query)

    if not results:
        await update.message.reply_text(f"No contacts matching '{query}'.")
        return

    conn = _db()
    lines = []
    for c in results[:5]:
        lines.append(_format_contact(c, verbose=True))
        interactions = conn.execute(
            "SELECT * FROM interactions WHERE contact_id = ? ORDER BY id DESC LIMIT 5",
            (c["id"],),
        ).fetchall()
        if interactions:
            lines.append("  Recent:")
            lines.append(_format_interactions([dict(i) for i in interactions]))
        lines.append("")

    text = "\n".join(lines)
    if len(text) > 4000:
        text = text[:4000] + "\n\n... (truncated)"
    await update.message.reply_text(text)


async def cmd_remember(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Add a note about someone. Creates the contact if they don't exist."""
    if not update.message or not context.args:
        await update.message.reply_text(
            "Usage: /remember <name> \u2014 <note>\n"
            "Example: /remember John \u2014 loves hiking, works at Google"
        )
        return

    raw = " ".join(context.args)

    # Split on — or --
    if " \u2014 " in raw:
        name, note = raw.split(" \u2014 ", 1)
    elif " -- " in raw:
        name, note = raw.split(" -- ", 1)
    else:
        parts = raw.split(None, 1)
        if len(parts) < 2:
            await update.message.reply_text("Usage: /remember <name> \u2014 <note>")
            return
        name, note = parts

    name = name.strip()
    note = note.strip()

    conn = _db()
    existing = conn.execute(
        "SELECT * FROM contacts WHERE name LIKE ?", (f"%{name}%",)
    ).fetchall()

    if len(existing) == 1:
        c = dict(existing[0])
        old_notes = c.get("notes") or ""
        today = datetime.now().strftime("%Y-%m-%d")
        new_notes = f"{old_notes}\n[{today}] {note}".strip()
        conn.execute(
            "UPDATE contacts SET notes = ?, updated_at = datetime('now') WHERE id = ?",
            (new_notes, c["id"]),
        )
        conn.commit()
        await update.message.reply_text(f"Updated {c['name']}:\n  {note}")
    elif len(existing) > 1:
        names = ", ".join(dict(c)["name"] for c in existing[:5])
        await update.message.reply_text(f"Multiple matches: {names}\nBe more specific.")
    else:
        today = datetime.now().strftime("%Y-%m-%d")
        conn.execute(
            "INSERT INTO contacts (name, notes, tier) VALUES (?, ?, 'C')",
            (name, f"[{today}] {note}"),
        )
        conn.commit()
        await update.message.reply_text(
            f"Created new contact: {name} [C]\n  {note}\n\nUse /editcontact to set tier, tags, location."
        )


async def cmd_addcontact(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Add a new contact."""
    if not update.message or not context.args:
        await update.message.reply_text(
            "Usage: /addcontact <name> [tier=C] [location=...] [tags=...] [handle=...]\n"
            "Example: /addcontact John Doe tier=A location=London tags=dev,founder handle=@john"
        )
        return

    raw = " ".join(context.args)

    tier = "C"
    location = ""
    tags = ""
    handles = ""
    name_parts = []

    for token in raw.split():
        if token.lower().startswith("tier="):
            tier = token.split("=", 1)[1].upper()
            if tier not in TIERS:
                await update.message.reply_text(f"Invalid tier '{tier}'. Use A, B, C, or D.")
                return
        elif token.lower().startswith("location="):
            location = token.split("=", 1)[1]
        elif token.lower().startswith("tags="):
            tags = token.split("=", 1)[1]
        elif token.lower().startswith("handle="):
            handles = token.split("=", 1)[1]
        else:
            name_parts.append(token)

    name = " ".join(name_parts).strip()
    if not name:
        await update.message.reply_text("Please provide a name.")
        return

    conn = _db()
    existing = conn.execute(
        "SELECT id FROM contacts WHERE name = ?", (name,)
    ).fetchone()
    if existing:
        await update.message.reply_text(f"Contact '{name}' already exists. Use /whois {name}")
        return

    conn.execute(
        "INSERT INTO contacts (name, tier, location, tags, handles) VALUES (?, ?, ?, ?, ?)",
        (name, tier, location, tags, handles),
    )
    conn.commit()

    emoji = TIER_EMOJI.get(tier, "")
    tier_label = TIERS[tier][0]
    await update.message.reply_text(
        f"Added: {emoji} {name} [{tier}] ({tier_label})"
        + (f"\n  Location: {location}" if location else "")
        + (f"\n  Tags: {tags}" if tags else "")
        + (f"\n  Handle: {handles}" if handles else "")
    )


async def cmd_editcontact(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Edit a contact's fields."""
    if not update.message or not context.args:
        await update.message.reply_text(
            "Usage: /editcontact <name> <field>=<value>\n"
            "Fields: tier, location, tags, handles, name\n"
            "Example: /editcontact John tier=A location=NYC tags=dev,investor"
        )
        return

    raw = " ".join(context.args)

    match = re.search(r"\s+(tier|location|tags|handles|name)=", raw)
    if not match:
        await update.message.reply_text("No field=value found. Use: tier=, location=, tags=, handles=, name=")
        return

    search_name = raw[: match.start()].strip()
    fields_str = raw[match.start() :].strip()

    updates = {}
    for m in re.finditer(r"(tier|location|tags|handles|name)=(\S+)", fields_str):
        field, value = m.group(1), m.group(2)
        if field == "tier":
            value = value.upper()
            if value not in TIERS:
                await update.message.reply_text(f"Invalid tier '{value}'. Use A, B, C, or D.")
                return
        updates[field] = value

    if not updates:
        await update.message.reply_text("No valid updates.")
        return

    results = await _find_contacts(search_name)
    if not results:
        await update.message.reply_text(f"No contact matching '{search_name}'.")
        return
    if len(results) > 1:
        names = ", ".join(c["name"] for c in results[:5])
        await update.message.reply_text(f"Multiple matches: {names}\nBe more specific.")
        return

    contact = results[0]
    conn = _db()

    set_clause = ", ".join(f"{k} = ?" for k in updates)
    values = list(updates.values()) + [contact["id"]]
    conn.execute(
        f"UPDATE contacts SET {set_clause}, updated_at = datetime('now') WHERE id = ?",
        values,
    )
    conn.commit()

    if "tier" in updates and contact.get("last_contact"):
        next_dt = _compute_next_contact(updates["tier"], contact["last_contact"])
        if next_dt:
            conn.execute(
                "UPDATE contacts SET next_contact = ?, updated_at = datetime('now') WHERE id = ?",
                (next_dt, contact["id"]),
            )
            conn.commit()

    changes = ", ".join(f"{k}={v}" for k, v in updates.items())
    await update.message.reply_text(f"Updated {contact['name']}: {changes}")


async def cmd_contacts(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """List/search contacts."""
    if not update.message:
        return

    conn = _db()
    args = context.args or []

    if not args:
        lines = ["Contacts:\n"]
        for tier, (label, interval) in TIERS.items():
            count = conn.execute(
                "SELECT COUNT(*) as cnt FROM contacts WHERE tier = ?", (tier,)
            ).fetchone()["cnt"]
            emoji = TIER_EMOJI[tier]
            lines.append(f"  {emoji} [{tier}] {label}: {count} (every {interval}d)")
        total = conn.execute("SELECT COUNT(*) as cnt FROM contacts").fetchone()["cnt"]
        lines.append(f"\n  Total: {total}")
        lines.append("\nUse /contacts <query> to search")
        await update.message.reply_text("\n".join(lines))
        return

    query = " ".join(args)
    if query.upper() in TIERS:
        rows = conn.execute(
            "SELECT * FROM contacts WHERE tier = ?", (query.upper(),)
        ).fetchall()
        results = [dict(r) for r in rows]
    else:
        results = await _find_contacts(query)

    if not results:
        await update.message.reply_text(f"No contacts matching '{query}'.")
        return

    lines = [f"Found {len(results)} contact(s):\n"]
    for c in results[:20]:
        lines.append(_format_contact(c))

    if len(results) > 20:
        lines.append(f"\n... and {len(results) - 20} more")

    await update.message.reply_text("\n".join(lines))


async def cmd_touched(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Log that you contacted someone. Updates last_contact and recomputes next_contact."""
    if not update.message or not context.args:
        await update.message.reply_text(
            "Usage: /touched <name> [note]\n"
            "Example: /touched John caught up over coffee"
        )
        return

    raw = " ".join(context.args)
    parts = raw.split(None, 1)
    search_name = parts[0]
    note = parts[1] if len(parts) > 1 else None

    results = await _find_contacts(search_name)
    if not results:
        await update.message.reply_text(f"No contact matching '{search_name}'.")
        return
    if len(results) > 1:
        exact = [c for c in results if c["name"].lower() == search_name.lower()]
        if len(exact) == 1:
            results = exact
        else:
            names = ", ".join(c["name"] for c in results[:5])
            await update.message.reply_text(f"Multiple matches: {names}\nBe more specific.")
            return

    contact = results[0]
    conn = _db()
    today = datetime.now().strftime("%Y-%m-%d")
    next_dt = _compute_next_contact(contact.get("tier", "C"), today)
    conn.execute(
        "UPDATE contacts SET last_contact = ?, next_contact = ?, updated_at = datetime('now') WHERE id = ?",
        (today, next_dt or "", contact["id"]),
    )
    if note:
        conn.execute(
            "INSERT INTO interactions (contact_id, channel, summary) VALUES (?, 'manual', ?)",
            (contact["id"], note),
        )
    conn.commit()

    emoji = TIER_EMOJI.get(contact.get("tier", "C"), "")
    msg = f"Logged contact with {emoji} {contact['name']}"
    if next_dt:
        msg += f"\n  Next check-in: {next_dt}"
    if note:
        msg += f"\n  Note: {note}"
    await update.message.reply_text(msg)


# --- Auto-sync from WhatsApp ---


def _extract_phones(handles: str | None) -> list[str]:
    """Extract phone numbers from handles field."""
    if not handles:
        return []
    phones = []
    for part in handles.split(","):
        part = part.strip()
        if part.startswith("wa:"):
            phones.append(re.sub(r"[^\d]", "", part[3:]))
        elif part.startswith("+") or (part and part[0].isdigit()):
            phones.append(re.sub(r"[^\d]", "", part))
    return [p for p in phones if p]


def _wa_api_get(path: str) -> list | dict | None:
    """Query the WhatsApp bot HTTP API (localhost:3100)."""
    import urllib.request
    import json as _json
    try:
        url = f"http://127.0.0.1:3100{path}"
        with urllib.request.urlopen(url, timeout=10) as resp:
            return _json.loads(resp.read())
    except Exception:
        return None


async def _sync_whatsapp_interactions():
    """Sync recent WhatsApp messages to update last_contact dates."""
    status = _wa_api_get("/status")
    if status is None:
        log.debug("WhatsApp API not reachable, skipping sync")
        return

    conn = _db()
    contacts = conn.execute("SELECT * FROM contacts").fetchall()
    contacts = [dict(r) for r in contacts]
    if not contacts:
        return

    chats = _wa_api_get("/chats?limit=200")
    if not chats:
        return

    updated = 0
    for contact in contacts:
        phones = _extract_phones(contact.get("handles"))
        name = contact.get("name", "")
        recent_date = None
        recent_sender = None

        for chat in chats:
            chat_id = chat.get("id", "")
            chat_name = chat.get("name", "")

            matched = False
            for phone in phones:
                if phone in chat_id:
                    matched = True
                    break
            if not matched and name and chat_name and name.lower() in chat_name.lower():
                matched = True

            if not matched:
                continue

            messages = _wa_api_get(f"/messages/{chat_id}?limit=5")
            if not messages:
                continue

            for msg in messages:
                if msg.get("is_from_me"):
                    continue
                ts = msg.get("timestamp")
                if ts:
                    msg_date = datetime.fromtimestamp(ts).strftime("%Y-%m-%d")
                    recent_date = msg_date
                    recent_sender = msg.get("sender_name") or chat_name
                    break

            if recent_date:
                break

        if not recent_date:
            continue

        if contact.get("last_contact") and recent_date <= contact["last_contact"]:
            continue

        next_dt = _compute_next_contact(contact.get("tier", "C"), recent_date)
        conn.execute(
            "UPDATE contacts SET last_contact = ?, next_contact = ?, updated_at = datetime('now') WHERE id = ?",
            (recent_date, next_dt or "", contact["id"]),
        )
        conn.execute(
            "INSERT INTO interactions (contact_id, channel, summary) VALUES (?, 'whatsapp', ?)",
            (contact["id"], f"Message from {recent_sender}"),
        )
        updated += 1

    if updated:
        conn.commit()
        log.info("WhatsApp sync: updated %d contact(s)", updated)


async def _periodic_wa_sync(context: ContextTypes.DEFAULT_TYPE):
    """Background job to sync WhatsApp interactions via HTTP API."""
    try:
        await _sync_whatsapp_interactions()
    except Exception as e:
        log.error("WhatsApp sync error: %s", e)


async def handle_command(msg, reply) -> bool:
    """Platform-agnostic command handler for CRM."""
    conn = _db()

    if msg.command == "crm":
        today = datetime.now().strftime("%Y-%m-%d")
        overdue = conn.execute(
            "SELECT * FROM contacts WHERE next_contact != '' AND next_contact <= ?",
            (today,),
        ).fetchall()
        if not overdue:
            await reply.reply_text("All caught up! No contacts due for a check-in.")
            return True

        lines = [f"Overdue ({len(overdue)}):\n"]
        for c in overdue:
            days_late = (datetime.now() - datetime.fromisoformat(c["next_contact"])).days
            lines.append(f"  {c['name']} [{c['tier']}] -- {days_late}d overdue")
        await reply.reply_text("\n".join(lines))
        return True

    elif msg.command == "whois":
        if not msg.args:
            await reply.reply_text("Usage: /whois <name>")
            return True

        query = " ".join(msg.args)
        results = await _find_contacts(query)
        if not results:
            await reply.reply_text(f"No contacts matching '{query}'.")
            return True
        lines = []
        for c in results[:5]:
            lines.append(_format_contact(c, verbose=True))
            interactions = conn.execute(
                "SELECT * FROM interactions WHERE contact_id = ? ORDER BY id DESC LIMIT 5",
                (c["id"],),
            ).fetchall()
            if interactions:
                lines.append("  Recent:")
                lines.append(_format_interactions([dict(i) for i in interactions]))
            lines.append("")
        text = "\n".join(lines)
        if len(text) > 4000:
            text = text[:4000] + "\n\n... (truncated)"
        await reply.reply_text(text)
        return True

    elif msg.command == "remember":
        if not msg.args:
            await reply.reply_text("Usage: /remember <name> -- <note>")
            return True

        raw = " ".join(msg.args)
        if " \u2014 " in raw:
            name, note = raw.split(" \u2014 ", 1)
        elif " -- " in raw:
            name, note = raw.split(" -- ", 1)
        else:
            parts = raw.split(None, 1)
            if len(parts) < 2:
                await reply.reply_text("Usage: /remember <name> -- <note>")
                return True
            name, note = parts

        name = name.strip()
        note = note.strip()

        existing = conn.execute(
            "SELECT * FROM contacts WHERE name LIKE ?", (f"%{name}%",)
        ).fetchall()

        if len(existing) == 1:
            c = dict(existing[0])
            old_notes = c.get("notes") or ""
            today = datetime.now().strftime("%Y-%m-%d")
            new_notes = f"{old_notes}\n[{today}] {note}".strip()
            conn.execute(
                "UPDATE contacts SET notes = ?, updated_at = datetime('now') WHERE id = ?",
                (new_notes, c["id"]),
            )
            conn.commit()
            await reply.reply_text(f"Updated note for {c['name']}.")
        elif len(existing) > 1:
            names = ", ".join(dict(c)["name"] for c in existing[:5])
            await reply.reply_text(f"Multiple matches: {names}. Be more specific.")
        else:
            today = datetime.now().strftime("%Y-%m-%d")
            conn.execute(
                "INSERT INTO contacts (name, notes, tier) VALUES (?, ?, 'C')",
                (name, f"[{today}] {note}"),
            )
            conn.commit()
            await reply.reply_text(f"Created contact {name} with note.")
        return True

    elif msg.command == "contacts":
        if not msg.args:
            lines = ["Contacts:\n"]
            for tier, (label, interval) in TIERS.items():
                count = conn.execute(
                    "SELECT COUNT(*) as cnt FROM contacts WHERE tier = ?", (tier,)
                ).fetchone()["cnt"]
                lines.append(f"  [{tier}] {label}: {count} (every {interval}d)")
            total = conn.execute("SELECT COUNT(*) as cnt FROM contacts").fetchone()["cnt"]
            lines.append(f"\n  Total: {total}")
            await reply.reply_text("\n".join(lines))
            return True

        query = " ".join(msg.args)
        if query.upper() in TIERS:
            rows = conn.execute(
                "SELECT * FROM contacts WHERE tier = ?", (query.upper(),)
            ).fetchall()
            results = [dict(r) for r in rows]
        else:
            results = await _find_contacts(query)
        if not results:
            await reply.reply_text(f"No contacts matching '{query}'.")
            return True
        lines = [f"Found {len(results)} contact(s):\n"]
        for c in results[:20]:
            lines.append(_format_contact(c))
        if len(results) > 20:
            lines.append(f"\n... and {len(results) - 20} more")
        await reply.reply_text("\n".join(lines))
        return True

    elif msg.command == "touched":
        if not msg.args:
            await reply.reply_text("Usage: /touched <name> [note]")
            return True

        raw = " ".join(msg.args)
        parts = raw.split(None, 1)
        search_name = parts[0]
        note = parts[1] if len(parts) > 1 else None

        results = await _find_contacts(search_name)
        if not results:
            await reply.reply_text(f"No contact matching '{search_name}'.")
            return True
        if len(results) > 1:
            exact = [c for c in results if c["name"].lower() == search_name.lower()]
            if len(exact) == 1:
                results = exact
            else:
                names = ", ".join(c["name"] for c in results[:5])
                await reply.reply_text(f"Multiple matches: {names}. Be more specific.")
                return True
        contact = results[0]
        today = datetime.now().strftime("%Y-%m-%d")
        next_dt = _compute_next_contact(contact.get("tier", "C"), today)
        conn.execute(
            "UPDATE contacts SET last_contact = ?, next_contact = ?, updated_at = datetime('now') WHERE id = ?",
            (today, next_dt or "", contact["id"]),
        )
        if note:
            conn.execute(
                "INSERT INTO interactions (contact_id, channel, summary) VALUES (?, 'manual', ?)",
                (contact["id"], note),
            )
        conn.commit()
        text = f"Logged contact with {contact['name']}"
        if next_dt:
            text += f"\n  Next check-in: {next_dt}"
        if note:
            text += f"\n  Note: {note}"
        await reply.reply_text(text)
        return True

    return False


def register(app: Application):
    app.add_handler(CommandHandler("crm", cmd_crm))
    app.add_handler(CommandHandler("whois", cmd_whois))
    app.add_handler(CommandHandler("remember", cmd_remember))
    app.add_handler(CommandHandler("addcontact", cmd_addcontact))
    app.add_handler(CommandHandler("editcontact", cmd_editcontact))
    app.add_handler(CommandHandler("contacts", cmd_contacts))
    app.add_handler(CommandHandler("touched", cmd_touched))

    # Sync WhatsApp interactions every 10 minutes
    app.job_queue.run_repeating(_periodic_wa_sync, interval=600, first=30)
