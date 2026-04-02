"""
Notes

Commands (works in any topic):
  /note <title> <body>   — Create a zk note
  /note <title>          — Create a note (body via reply or empty)
  /notes                 — Show recent notes
"""

import re
from datetime import datetime
from pathlib import Path

from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

from cupbots.helpers.logger import get_logger
from cupbots.paths import get_allowed_path

log = get_logger("note")


def _get_note_dir() -> Path:
    return get_allowed_path("notes")


def _create_note(title: str, body: str = "", tags: list[str] | None = None) -> Path:
    """Create a zk-format note and return the file path."""
    date_prefix = datetime.now().strftime("%Y%m%d")
    formatted_date = datetime.now().strftime("%Y-%m-%d")

    # Slugify title
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", title).strip("-").lower()
    filename = f"{date_prefix}-{slug}.md"
    filepath = _get_note_dir() / filename

    tag_lines = ""
    if tags:
        tag_lines = "\n".join(f"  - {t}" for t in tags)
    else:
        tag_lines = "  -"

    content = f"""---
title: '{title}'
date: {formatted_date}
tags:
{tag_lines}
---

{body}
"""
    filepath.write_text(content)
    log.info("Created note: %s", filename)
    return filepath


async def cmd_note(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text(
            "Usage:\n"
            "/note My Title — body text here\n"
            "/note My Title #tag1 #tag2 — body text\n"
            "\nEverything before — is the title, after is the body.\n"
            "Reply to a message with /note Title to capture it.",
        )
        return

    raw = " ".join(context.args)

    # Extract tags (#word)
    tags = re.findall(r"#(\w+)", raw)
    raw_clean = re.sub(r"\s*#\w+", "", raw).strip()

    # Split on — or -- for title/body
    if " — " in raw_clean:
        title, body = raw_clean.split(" — ", 1)
    elif " -- " in raw_clean:
        title, body = raw_clean.split(" -- ", 1)
    else:
        title = raw_clean
        body = ""

    title = title.strip()
    body = body.strip()

    # If replying to a message, use that as the body
    if update.message.reply_to_message and not body:
        reply = update.message.reply_to_message
        body = reply.text or reply.caption or ""

    filepath = _create_note(title, body, tags or None)

    await update.message.reply_text(
        f"📝 Note created: {filepath.name}"
    )


async def cmd_notes(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show the 5 most recent notes."""
    notes = sorted(_get_note_dir().glob("*.md"), reverse=True)

    # Filter to zk notes (start with date prefix)
    notes = [n for n in notes if re.match(r"\d{8}-", n.name)][:5]

    if not notes:
        await update.message.reply_text("No notes found.")
        return

    lines = ["📝 Recent notes:\n"]
    for n in notes:
        # Extract title from frontmatter
        text = n.read_text(errors="ignore")
        title_match = re.search(r"title:\s*['\"]?(.+?)['\"]?\s*$", text, re.MULTILINE)
        title = title_match.group(1) if title_match else n.stem
        date = n.name[:8]
        date_fmt = f"{date[:4]}-{date[4:6]}-{date[6:]}"
        lines.append(f"  {date_fmt}  {title}")

    await update.message.reply_text("\n".join(lines))


async def handle_command(msg, reply):
    """Platform-agnostic command handler for WhatsApp and other platforms."""
    if msg.command == "note":
        if not msg.args:
            await reply.reply_text(
                "Usage:\n/note My Title -- body text here\n/note My Title #tag1 #tag2 -- body"
            )
            return True

        raw = " ".join(msg.args)
        tags = re.findall(r"#(\w+)", raw)
        raw_clean = re.sub(r"\s*#\w+", "", raw).strip()

        if " — " in raw_clean:
            title, body = raw_clean.split(" — ", 1)
        elif " -- " in raw_clean:
            title, body = raw_clean.split(" -- ", 1)
        else:
            title = raw_clean
            body = ""

        title = title.strip()
        body = body.strip()

        if msg.reply_to_text and not body:
            body = msg.reply_to_text

        filepath = _create_note(title, body, tags or None)
        await reply.reply_text(f"Note created: {filepath.name}")
        return True

    elif msg.command == "notes":
        notes = sorted(_get_note_dir().glob("*.md"), reverse=True)
        notes = [n for n in notes if re.match(r"\d{8}-", n.name)][:5]

        if not notes:
            await reply.reply_text("No notes found.")
            return True

        lines = ["Recent notes:\n"]
        for n in notes:
            text = n.read_text(errors="ignore")
            title_match = re.search(r"title:\s*['\"]?(.+?)['\"]?\s*$", text, re.MULTILINE)
            title = title_match.group(1) if title_match else n.stem
            date = n.name[:8]
            date_fmt = f"{date[:4]}-{date[4:6]}-{date[6:]}"
            lines.append(f"  {date_fmt}  {title}")

        await reply.reply_text("\n".join(lines))
        return True

    return False


def register(app: Application):
    app.add_handler(CommandHandler("note", cmd_note))
    app.add_handler(CommandHandler("notes", cmd_notes))
