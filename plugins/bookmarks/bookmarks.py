"""
Bookmarks

Commands (works in any topic):
  /bookmark <url> [#tag]           — Save a link (auto-fetches title)
  /bookmark <url> <title> [#tag]   — Save with custom title
  /unbookmark <url_or_text>        — Remove a bookmark by URL or keyword
  /bookmarks                       — Show last 5 unread bookmarks
"""

import re
from html.parser import HTMLParser
from pathlib import Path
from urllib.request import Request, urlopen

from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

from cupbots.paths import get_allowed_path


def _get_bookmarks_file() -> Path:
    return get_allowed_path("bookmarks")


def _fetch_title(url: str) -> str | None:
    """Fetch the <title> tag from a URL. No AI, just HTTP + HTML parsing."""
    try:
        req = Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urlopen(req, timeout=5) as resp:
            # Read only first 16KB to find <title>
            data = resp.read(16384).decode("utf-8", errors="ignore")

        class TitleParser(HTMLParser):
            def __init__(self):
                super().__init__()
                self._in_title = False
                self.title = None

            def handle_starttag(self, tag, attrs):
                if tag.lower() == "title":
                    self._in_title = True

            def handle_data(self, data):
                if self._in_title:
                    self.title = data.strip()
                    self._in_title = False

        parser = TitleParser()
        parser.feed(data)
        return parser.title
    except Exception:
        return None


async def cmd_bookmark(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not context.args:
        await update.message.reply_text("Usage: `/bookmark <url> [title] [#tag]`", parse_mode="Markdown")
        return

    args = context.args
    url = args[0]

    # Extract tags (words starting with #)
    tags = [a for a in args[1:] if a.startswith("#")]
    non_tag_args = [a for a in args[1:] if not a.startswith("#")]

    # Title is everything between url and tags
    title = " ".join(non_tag_args) if non_tag_args else None
    tag_str = " ".join(tags)

    # Auto-fetch title if not provided and URL is a link
    if not title and url.startswith("http"):
        title = _fetch_title(url)

    # Build the bookmark line
    if title:
        line = f"- [ ] [{title}]({url})"
    else:
        line = f"- [ ] {url}"

    if tag_str:
        line += f" {tag_str}"

    # Append to bookmarks.md
    with open(_get_bookmarks_file(), "a", encoding="utf-8") as f:
        f.write(f"{line}\n")

    display = title or url
    if len(display) > 60:
        display = display[:57] + "..."

    await update.message.reply_text(
        f"🔖 Saved: {display}" + (f" {tag_str}" if tag_str else ""),
    )


async def cmd_unbookmark(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Remove a bookmark line matching the given URL or keyword."""
    if not update.message or not context.args:
        await update.message.reply_text("Usage: `/unbookmark <url or keyword>`", parse_mode="Markdown")
        return

    query = " ".join(context.args).lower()

    if not _get_bookmarks_file().exists():
        await update.message.reply_text("No bookmarks file found.")
        return

    lines = _get_bookmarks_file().read_text(encoding="utf-8").splitlines()
    new_lines = []
    removed = []

    for line in lines:
        if line.strip().startswith("- [ ]") and query in line.lower():
            removed.append(line.strip())
        else:
            new_lines.append(line)

    if not removed:
        await update.message.reply_text(f"No unread bookmark matching '{query}'.")
        return

    _get_bookmarks_file().write_text("\n".join(new_lines) + "\n", encoding="utf-8")

    display = "\n".join(f"• {r.replace('- [ ] ', '', 1)}" for r in removed)
    if len(display) > 300:
        display = display[:297] + "..."

    await update.message.reply_text(f"🗑️ Removed {len(removed)} bookmark(s):\n{display}")


async def cmd_bookmarks(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message:
        return

    if not _get_bookmarks_file().exists():
        await update.message.reply_text("No bookmarks file found.")
        return

    text = _get_bookmarks_file().read_text(encoding="utf-8")
    # Find unchecked items
    unread = [line.strip() for line in text.splitlines() if line.strip().startswith("- [ ]")]

    if not unread:
        await update.message.reply_text("📚 No unread bookmarks!")
        return

    # Show last 5
    recent = unread[-5:]
    lines = ["📚 *Last 5 unread bookmarks:*\n"]
    for item in recent:
        # Strip the checkbox prefix
        clean = item.replace("- [ ] ", "", 1)
        lines.append(f"• {clean}")

    lines.append(f"\n_{len(unread)} total unread_")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown", disable_web_page_preview=True)


async def handle_command(msg, reply):
    """Platform-agnostic command handler."""
    if msg.command == "bookmark":
        if not msg.args:
            await reply.reply_text("Usage: /bookmark <url> [title] [#tag]")
            return True

        url = msg.args[0]
        tags = [a for a in msg.args[1:] if a.startswith("#")]
        non_tag_args = [a for a in msg.args[1:] if not a.startswith("#")]
        title = " ".join(non_tag_args) if non_tag_args else None
        tag_str = " ".join(tags)

        if not title and url.startswith("http"):
            title = _fetch_title(url)

        if title:
            line = f"- [ ] [{title}]({url})"
        else:
            line = f"- [ ] {url}"
        if tag_str:
            line += f" {tag_str}"

        with open(_get_bookmarks_file(), "a", encoding="utf-8") as f:
            f.write(f"{line}\n")

        display = title or url
        if len(display) > 60:
            display = display[:57] + "..."
        await reply.reply_text(f"Saved: {display}" + (f" {tag_str}" if tag_str else ""))
        return True

    elif msg.command == "unbookmark":
        if not msg.args:
            await reply.reply_text("Usage: /unbookmark <url or keyword>")
            return True

        query = " ".join(msg.args).lower()
        if not _get_bookmarks_file().exists():
            await reply.reply_text("No bookmarks file found.")
            return True

        lines = _get_bookmarks_file().read_text(encoding="utf-8").splitlines()
        new_lines = []
        removed = []
        for line in lines:
            if line.strip().startswith("- [ ]") and query in line.lower():
                removed.append(line.strip())
            else:
                new_lines.append(line)

        if not removed:
            await reply.reply_text(f"No unread bookmark matching '{query}'.")
            return True

        _get_bookmarks_file().write_text("\n".join(new_lines) + "\n", encoding="utf-8")
        await reply.reply_text(f"Removed {len(removed)} bookmark(s).")
        return True

    elif msg.command == "bookmarks":
        if not _get_bookmarks_file().exists():
            await reply.reply_text("No bookmarks file found.")
            return True

        text = _get_bookmarks_file().read_text(encoding="utf-8")
        unread = [line.strip() for line in text.splitlines() if line.strip().startswith("- [ ]")]

        if not unread:
            await reply.reply_text("No unread bookmarks!")
            return True

        recent = unread[-5:]
        lines = ["Last 5 unread bookmarks:\n"]
        for item in recent:
            clean = item.replace("- [ ] ", "", 1)
            lines.append(f"- {clean}")
        lines.append(f"\n{len(unread)} total unread")
        await reply.reply_text("\n".join(lines))
        return True

    return False


def register(app: Application):
    app.add_handler(CommandHandler("bookmark", cmd_bookmark))
    app.add_handler(CommandHandler("unbookmark", cmd_unbookmark))
    app.add_handler(CommandHandler("bookmarks", cmd_bookmarks))
