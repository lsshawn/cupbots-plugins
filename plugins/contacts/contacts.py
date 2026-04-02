"""
Contacts CRM

Commands (works in any topic):
  /crm                    — Show contacts due for a check-in
  /whois <name>           — Look up a contact (fuzzy match)
  /remember <name> — <note> — Add a note about someone
  /addcontact <name>      — Add a new contact
  /contacts [tag|tier]    — Search/filter contacts
"""

import asyncio
import re
from datetime import datetime, timedelta

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
)

from cupbots.helpers.pb import (
    pb_find_one, pb_find_many, pb_create, pb_update, pb_escape,
)
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


def _compute_next_contact(tier: str, last_contact: str | None) -> str | None:
    """Compute next contact date based on tier interval."""
    if not last_contact:
        return None
    interval = TIERS.get(tier, TIERS["C"])[1]
    last_dt = datetime.fromisoformat(last_contact)
    return (last_dt + timedelta(days=interval)).strftime("%Y-%m-%d")


async def _find_contacts(query: str) -> list[dict]:
    """Find contacts by name, tags, handles, or location (PB filter)."""
    like = pb_escape(query)
    # PocketBase uses ~ for LIKE/contains
    f = (
        f"name~'{like}' || tags~'{like}' || handles~'{like}' || location~'{like}'"
    )
    return await pb_find_many("contacts", filter_str=f, per_page=100)


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
        created = i.get("created", "")[:10]
        lines.append(f"  {created} {channel}{i.get('summary', '')}")
    return "\n".join(lines)


# --- Telegram Commands ---


async def cmd_crm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show contacts due for a check-in."""
    if not update.message:
        return

    today = datetime.now().strftime("%Y-%m-%d")
    overdue = await pb_find_many(
        "contacts",
        filter_str=f"next_contact!='' && next_contact<='{today}'",
    )
    never = await pb_find_many(
        "contacts",
        filter_str="last_contact=''",
    )

    if not overdue and not never:
        await update.message.reply_text("All caught up! No contacts due for a check-in.")
        return

    lines = []
    if overdue:
        lines.append(f"Overdue ({len(overdue)}):\n")
        for c in overdue:
            days_late = (datetime.now() - datetime.fromisoformat(c["next_contact"])).days
            emoji = TIER_EMOJI.get(c.get("tier", "C"), "")
            lines.append(f"  {emoji} {c['name']} [{c.get('tier', 'C')}] — {days_late}d overdue")
        lines.append("")

    if never:
        lines.append(f"Never contacted ({len(never)}):\n")
        for c in never[:10]:
            emoji = TIER_EMOJI.get(c.get("tier", "C"), "")
            lines.append(f"  {emoji} {c['name']} [{c.get('tier', 'C')}]")
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

    lines = []
    for c in results[:5]:
        lines.append(_format_contact(c, verbose=True))
        interactions = await pb_find_many(
            "interactions",
            filter_str=f"contact_id='{pb_escape(c['id'])}'",
            per_page=5,
        )
        if interactions:
            lines.append("  Recent:")
            lines.append(_format_interactions(interactions))
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
    safe_name = pb_escape(name)

    existing = await pb_find_many("contacts", filter_str=f"name~'{safe_name}'")

    if len(existing) == 1:
        c = existing[0]
        old_notes = c.get("notes") or ""
        today = datetime.now().strftime("%Y-%m-%d")
        new_notes = f"{old_notes}\n[{today}] {note}".strip()
        await pb_update("contacts", c["id"], {"notes": new_notes})
        await update.message.reply_text(f"Updated {c['name']}:\n  {note}")
    elif len(existing) > 1:
        names = ", ".join(c["name"] for c in existing[:5])
        await update.message.reply_text(f"Multiple matches: {names}\nBe more specific.")
    else:
        today = datetime.now().strftime("%Y-%m-%d")
        await pb_create("contacts", {
            "name": name,
            "notes": f"[{today}] {note}",
            "tier": "C",
            "location": "",
            "handles": "",
            "tags": "",
            "last_contact": "",
            "next_contact": "",
        })
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

    safe_name = pb_escape(name)
    existing = await pb_find_one("contacts", f"name='{safe_name}'")
    if existing:
        await update.message.reply_text(f"Contact '{name}' already exists. Use /whois {name}")
        return

    await pb_create("contacts", {
        "name": name,
        "tier": tier,
        "location": location,
        "tags": tags,
        "handles": handles,
        "notes": "",
        "last_contact": "",
        "next_contact": "",
    })

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
    await pb_update("contacts", contact["id"], updates)

    if "tier" in updates and contact.get("last_contact"):
        next_dt = _compute_next_contact(updates["tier"], contact["last_contact"])
        if next_dt:
            await pb_update("contacts", contact["id"], {"next_contact": next_dt})

    changes = ", ".join(f"{k}={v}" for k, v in updates.items())
    await update.message.reply_text(f"Updated {contact['name']}: {changes}")


async def cmd_contacts(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """List/search contacts."""
    if not update.message:
        return

    args = context.args or []

    if not args:
        lines = ["Contacts:\n"]
        for tier, (label, interval) in TIERS.items():
            contacts = await pb_find_many("contacts", filter_str=f"tier='{pb_escape(tier)}'")
            count = len(contacts)
            emoji = TIER_EMOJI[tier]
            lines.append(f"  {emoji} [{tier}] {label}: {count} (every {interval}d)")
        all_contacts = await pb_find_many("contacts", per_page=1)
        # PB returns totalItems in the response; approximate with a large fetch
        total_contacts = await pb_find_many("contacts", per_page=200)
        lines.append(f"\n  Total: {len(total_contacts)}")
        lines.append("\nUse /contacts <query> to search")
        await update.message.reply_text("\n".join(lines))
        return

    query = " ".join(args)
    if query.upper() in TIERS:
        results = await pb_find_many(
            "contacts", filter_str=f"tier='{pb_escape(query.upper())}'", per_page=100
        )
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
    today = datetime.now().strftime("%Y-%m-%d")
    next_dt = _compute_next_contact(contact.get("tier", "C"), today)
    await pb_update("contacts", contact["id"], {
        "last_contact": today,
        "next_contact": next_dt or "",
    })
    if note:
        await pb_create("interactions", {
            "contact_id": contact["id"],
            "channel": "manual",
            "summary": note,
        })

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

    contacts = await pb_find_many("contacts", per_page=200)
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
        await pb_update("contacts", contact["id"], {
            "last_contact": recent_date,
            "next_contact": next_dt or "",
        })
        await pb_create("interactions", {
            "contact_id": contact["id"],
            "channel": "whatsapp",
            "summary": f"Message from {recent_sender}",
        })
        updated += 1

    if updated:
        log.info("WhatsApp sync: updated %d contact(s)", updated)


async def _periodic_wa_sync(context: ContextTypes.DEFAULT_TYPE):
    """Background job to sync WhatsApp interactions via HTTP API."""
    try:
        await _sync_whatsapp_interactions()
    except Exception as e:
        log.error("WhatsApp sync error: %s", e)


async def handle_command(msg, reply) -> bool:
    """Platform-agnostic command handler for CRM."""
    if msg.command == "crm":
        today = datetime.now().strftime("%Y-%m-%d")
        overdue = await pb_find_many(
            "contacts",
            filter_str=f"next_contact!='' && next_contact<='{today}'",
        )
        if not overdue:
            await reply.reply_text("All caught up! No contacts due for a check-in.")
            return True

        lines = [f"Overdue ({len(overdue)}):\n"]
        for c in overdue:
            days_late = (datetime.now() - datetime.fromisoformat(c["next_contact"])).days
            lines.append(f"  {c['name']} [{c.get('tier', 'C')}] -- {days_late}d overdue")
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
            interactions = await pb_find_many(
                "interactions",
                filter_str=f"contact_id='{pb_escape(c['id'])}'",
                per_page=5,
            )
            if interactions:
                lines.append("  Recent:")
                lines.append(_format_interactions(interactions))
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
        safe_name = pb_escape(name)

        existing = await pb_find_many("contacts", filter_str=f"name~'{safe_name}'")

        if len(existing) == 1:
            c = existing[0]
            old_notes = c.get("notes") or ""
            today = datetime.now().strftime("%Y-%m-%d")
            new_notes = f"{old_notes}\n[{today}] {note}".strip()
            await pb_update("contacts", c["id"], {"notes": new_notes})
            await reply.reply_text(f"Updated note for {c['name']}.")
        elif len(existing) > 1:
            names = ", ".join(c["name"] for c in existing[:5])
            await reply.reply_text(f"Multiple matches: {names}. Be more specific.")
        else:
            await pb_create("contacts", {
                "name": name,
                "notes": f"[{datetime.now().strftime('%Y-%m-%d')}] {note}",
                "tier": "C",
                "location": "",
                "handles": "",
                "tags": "",
                "last_contact": "",
                "next_contact": "",
            })
            await reply.reply_text(f"Created contact {name} with note.")
        return True

    elif msg.command == "contacts":
        if not msg.args:
            lines = ["Contacts:\n"]
            for tier, (label, interval) in TIERS.items():
                contacts = await pb_find_many("contacts", filter_str=f"tier='{pb_escape(tier)}'")
                lines.append(f"  [{tier}] {label}: {len(contacts)} (every {interval}d)")
            total = await pb_find_many("contacts", per_page=200)
            lines.append(f"\n  Total: {len(total)}")
            await reply.reply_text("\n".join(lines))
            return True

        query = " ".join(msg.args)
        if query.upper() in TIERS:
            results = await pb_find_many(
                "contacts", filter_str=f"tier='{pb_escape(query.upper())}'", per_page=100
            )
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
        await pb_update("contacts", contact["id"], {
            "last_contact": today,
            "next_contact": next_dt or "",
        })
        if note:
            await pb_create("interactions", {
                "contact_id": contact["id"],
                "channel": "manual",
                "summary": note,
            })
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
