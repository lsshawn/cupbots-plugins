"""
MdPubs — Publish & Manage Markdown Notes

Commands (works in any topic):
  /publish <title> -- <body>   — Publish a markdown note to mdpubs.com
  /publish <title>             — Publish (body via reply or empty)
  /published                   — List recent published notes

Examples:
  /publish My Report -- # Heading\nBody text here
  /publish My Report #finance -- body text
  /published
"""

import json
import os
import re

import requests

from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

from cupbots.helpers.db import get_plugin_db
from cupbots.helpers.logger import get_logger

log = get_logger("mdpubs")

API_BASE = "https://api.mdpubs.com"
PLUGIN_NAME = "mdpubs"


def create_tables(conn):
    """Create mdpubs_notes table. Called by get_plugin_db on first access."""
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS mdpubs_notes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            key TEXT NOT NULL,
            company_id TEXT NOT NULL DEFAULT '_default',
            note_id INTEGER,
            title TEXT NOT NULL DEFAULT '',
            url TEXT NOT NULL DEFAULT '',
            tags TEXT NOT NULL DEFAULT '[]',
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            updated_at TEXT NOT NULL DEFAULT (datetime('now')),
            UNIQUE(key, company_id)
        );
    """)


def _db():
    """Get the mdpubs plugin database connection."""
    return get_plugin_db(PLUGIN_NAME)


# ---------------------------------------------------------------------------
# Core mdpubs API (self-contained — no external helper dependency)
# ---------------------------------------------------------------------------

def _get_api_key() -> str:
    try:
        from cupbots.helpers.db import resolve_plugin_setting
        key = resolve_plugin_setting("mdpubs", "mdpubs_api_key") or ""
    except Exception:
        key = ""
    if not key:
        raise RuntimeError("Set MDPUBS_API_KEY in plugin_settings.mdpubs in config.yaml")
    return key


def _api_publish(title: str, content: str, tags: list[str] | None = None,
                 is_private: bool = False) -> str:
    """Create a new note on mdpubs.com. Returns the public URL."""
    resp = requests.post(
        f"{API_BASE}/notes",
        headers={"X-API-Key": _get_api_key(), "Content-Type": "application/json"},
        json={
            "title": title,
            "content": content,
            "file_extension": "md",
            "tags": tags or [],
            "isPrivate": is_private,
        },
        timeout=30,
    )
    resp.raise_for_status()
    note_id = resp.json()["id"]
    return f"https://mdpubs.com/{note_id}"


def _api_update(note_id: int, title: str | None = None, content: str | None = None,
                tags: list[str] | None = None) -> str:
    """Update an existing note. Returns the public URL."""
    payload = {}
    if title is not None:
        payload["title"] = title
    if content is not None:
        payload["content"] = content
    if tags is not None:
        payload["tags"] = tags

    resp = requests.put(
        f"{API_BASE}/notes/{note_id}",
        headers={"X-API-Key": _get_api_key(), "Content-Type": "application/json"},
        json=payload,
        timeout=30,
    )
    resp.raise_for_status()
    return f"https://mdpubs.com/{note_id}"


# ---------------------------------------------------------------------------
# SQLite-tracked publish (tracks note IDs for update-in-place)
# ---------------------------------------------------------------------------

async def publish_or_update(key: str, title: str, content: str,
                             company_id: str | None = None,
                             tags: list[str] | None = None) -> str:
    """Publish a new note or update existing by key. Tracks in plugin DB."""
    conn = _db()
    cid = company_id or "_default"

    row = conn.execute(
        "SELECT * FROM mdpubs_notes WHERE key = ? AND company_id = ?",
        (key, cid),
    ).fetchone()

    note_id = row["note_id"] if row else None

    if note_id:
        url = _api_update(note_id, title=title, content=content)
    else:
        url = _api_publish(title, content, tags=tags)
        note_id = int(url.rstrip("/").split("/")[-1])

    if row:
        conn.execute(
            "UPDATE mdpubs_notes SET note_id = ?, title = ?, url = ?, tags = ?, "
            "updated_at = datetime('now') WHERE id = ?",
            (note_id, title, url, json.dumps(tags or []), row["id"]),
        )
    else:
        conn.execute(
            "INSERT INTO mdpubs_notes (key, company_id, note_id, title, url, tags) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (key, cid, note_id, title, url, json.dumps(tags or [])),
        )
    conn.commit()

    return url


async def list_notes(company_id: str | None = None, limit: int = 20) -> list[dict]:
    """List published notes tracked in plugin DB."""
    conn = _db()
    cid = company_id or "_default"
    rows = conn.execute(
        "SELECT * FROM mdpubs_notes WHERE company_id = ? ORDER BY id DESC LIMIT ?",
        (cid, limit),
    ).fetchall()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Public API for other plugins — graceful fallback if mdpubs not configured
# ---------------------------------------------------------------------------

async def publish_or_fallback(key: str, title: str, content: str,
                               company_id: str | None = None,
                               tags: list[str] | None = None) -> tuple[str | None, str]:
    """Publish to mdpubs if configured, otherwise return raw content.

    Returns (url_or_none, content).
    """
    try:
        _get_api_key()
        _has_key = True
    except Exception:
        _has_key = False
    if not _has_key:
        return None, content

    try:
        url = await publish_or_update(key, title, content, company_id=company_id, tags=tags)
        return url, content
    except Exception as e:
        log.warning("mdpubs publish failed, falling back to inline: %s", e)
        return None, content


# ---------------------------------------------------------------------------
# Parse helpers
# ---------------------------------------------------------------------------

def _parse_publish_args(raw: str) -> tuple[str, str, list[str]]:
    """Parse title, body, and tags from raw input."""
    tags = re.findall(r"#(\w+)", raw)
    raw_clean = re.sub(r"\s*#\w+", "", raw).strip()

    if " — " in raw_clean:
        title, body = raw_clean.split(" — ", 1)
    elif " -- " in raw_clean:
        title, body = raw_clean.split(" -- ", 1)
    else:
        title = raw_clean
        body = ""

    return title.strip(), body.strip(), tags


# ---------------------------------------------------------------------------
# Telegram command handlers
# ---------------------------------------------------------------------------

async def cmd_publish(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not context.args:
        await update.message.reply_text(
            "Usage:\n"
            "`/publish My Report -- markdown body here`\n"
            "`/publish My Report #finance #2026 -- body`\n"
            "\nReply to a message with `/publish Title` to publish it.",
            parse_mode="Markdown",
        )
        return

    raw = " ".join(context.args)
    title, body, tags = _parse_publish_args(raw)

    if update.message.reply_to_message and not body:
        reply = update.message.reply_to_message
        body = reply.text or reply.caption or ""

    if not body:
        await update.message.reply_text("No body content. Add text after `--` or reply to a message.", parse_mode="Markdown")
        return

    await update.message.reply_text("Publishing...")

    try:
        key = re.sub(r"[^a-zA-Z0-9]+", "-", title).strip("-").lower()
        url = await publish_or_update(key, title, body, tags=tags)
        await update.message.reply_text(f"Published: {url}")
    except Exception as e:
        log.error("Publish failed: %s", e)
        await update.message.reply_text(f"Failed: {type(e).__name__}. See logs.")


async def cmd_notes_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message:
        return

    notes = await list_notes()
    if not notes:
        await update.message.reply_text("No published notes.")
        return

    lines = [f"Published notes ({len(notes)}):\n"]
    for n in notes:
        title = n.get("title", n.get("key", "?"))
        url = n.get("url", "")
        lines.append(f"- [{title}]({url})")

    await update.message.reply_text("\n".join(lines), parse_mode="Markdown", disable_web_page_preview=True)


# ---------------------------------------------------------------------------
# Cross-platform handler
# ---------------------------------------------------------------------------

async def handle_command(msg, reply):
    """Platform-agnostic command handler."""
    if msg.command == "publish":
        if not msg.args:
            await reply.reply_text("Usage: /publish My Title -- body content here")
            return True

        raw = " ".join(msg.args)
        title, body, tags = _parse_publish_args(raw)

        if msg.reply_to_text and not body:
            body = msg.reply_to_text

        if not body:
            await reply.reply_text("No body content. Add text after -- or reply to a message.")
            return True

        await reply.reply_text("Publishing...")
        try:
            key = re.sub(r"[^a-zA-Z0-9]+", "-", title).strip("-").lower()
            url = await publish_or_update(key, title, body, company_id=msg.company_id, tags=tags)
            await reply.reply_text(f"Published: {url}")
        except Exception as e:
            log.error("Publish failed: %s", e)
            await reply.reply_error(f"{type(e).__name__}. See logs.")
        return True

    elif msg.command == "published":
        notes = await list_notes(company_id=msg.company_id)
        if not notes:
            await reply.reply_text("No published notes.")
            return True

        lines = [f"Published notes ({len(notes)}):\n"]
        for n in notes:
            title = n.get("title", n.get("key", "?"))
            url = n.get("url", "")
            lines.append(f"- {title}: {url}")

        await reply.reply_text("\n".join(lines))
        return True

    return False


# ---------------------------------------------------------------------------
# Plugin registration
# ---------------------------------------------------------------------------

def register(app: Application):
    app.add_handler(CommandHandler("publish", cmd_publish))
    app.add_handler(CommandHandler("published", cmd_notes_list))
