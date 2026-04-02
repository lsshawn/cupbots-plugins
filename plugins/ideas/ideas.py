"""
Ideas

Commands (works in any topic):
  /idea <description>   — Capture an idea
  /ideas                — Show this year's ideas
"""

import asyncio
import re
from datetime import datetime
from pathlib import Path

from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

from cupbots.paths import get_allowed_path


def _get_ideas_file() -> Path:
    return get_allowed_path("ideas")
_file_lock = asyncio.Lock()


async def cmd_idea(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not context.args:
        await update.message.reply_text("Usage: `/idea AI tool that does X`", parse_mode="Markdown")
        return

    idea = " ".join(context.args)
    year = datetime.now().strftime("%Y")
    line = f"- {idea}\n"

    async with _file_lock:
        text = _get_ideas_file().read_text(encoding="utf-8")

        # Find the current year section and append after it
        year_header = f"## {year}"
        if year_header in text:
            # Find the end of the year section (next ## or end of file)
            header_pos = text.index(year_header)
            after_header = text[header_pos + len(year_header):]

            # Find the next year header
            next_header = re.search(r"\n## \d{4}", after_header)
            if next_header:
                insert_pos = header_pos + len(year_header) + next_header.start()
                text = text[:insert_pos] + "\n" + line + text[insert_pos:]
            else:
                # Append at end of file
                text = text.rstrip() + "\n" + line
        else:
            # Add new year section after frontmatter
            fm_end = text.find("---", 3)
            if fm_end != -1:
                insert_pos = fm_end + 3
                text = text[:insert_pos] + f"\n\n{year_header}\n\n{line}" + text[insert_pos:]
            else:
                text += f"\n{year_header}\n\n{line}"

        _get_ideas_file().write_text(text, encoding="utf-8")

    display = idea if len(idea) <= 60 else idea[:57] + "..."
    await update.message.reply_text(f"💡 Idea saved: {display}")


async def cmd_ideas(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message:
        return

    if not _get_ideas_file().exists():
        await update.message.reply_text("No ideas file found.")
        return

    text = _get_ideas_file().read_text(encoding="utf-8")
    year = datetime.now().strftime("%Y")
    year_header = f"## {year}"

    if year_header not in text:
        await update.message.reply_text(f"No ideas for {year} yet.")
        return

    # Extract this year's section
    header_pos = text.index(year_header)
    after_header = text[header_pos + len(year_header):]

    next_header = re.search(r"\n## \d{4}", after_header)
    if next_header:
        section = after_header[:next_header.start()]
    else:
        section = after_header

    ideas = [line.strip() for line in section.splitlines() if line.strip().startswith("- ")]

    if not ideas:
        await update.message.reply_text(f"No ideas for {year} yet.")
        return

    lines = [f"💡 *Ideas {year}* ({len(ideas)} total)\n"]
    for item in ideas:
        lines.append(f"• {item[2:]}")  # strip "- " prefix

    await update.message.reply_text("\n".join(lines), parse_mode="Markdown", disable_web_page_preview=True)


async def handle_command(msg, reply):
    """Platform-agnostic command handler."""
    if msg.command == "idea":
        if not msg.args:
            await reply.reply_text("Usage: /idea AI tool that does X")
            return True

        idea = " ".join(msg.args)
        year = datetime.now().strftime("%Y")
        line = f"- {idea}\n"

        async with _file_lock:
            text = _get_ideas_file().read_text(encoding="utf-8")
            year_header = f"## {year}"
            if year_header in text:
                header_pos = text.index(year_header)
                after_header = text[header_pos + len(year_header):]
                next_header = re.search(r"\n## \d{4}", after_header)
                if next_header:
                    insert_pos = header_pos + len(year_header) + next_header.start()
                    text = text[:insert_pos] + "\n" + line + text[insert_pos:]
                else:
                    text = text.rstrip() + "\n" + line
            else:
                fm_end = text.find("---", 3)
                if fm_end != -1:
                    insert_pos = fm_end + 3
                    text = text[:insert_pos] + f"\n\n{year_header}\n\n{line}" + text[insert_pos:]
                else:
                    text += f"\n{year_header}\n\n{line}"
            _get_ideas_file().write_text(text, encoding="utf-8")

        display = idea if len(idea) <= 60 else idea[:57] + "..."
        await reply.reply_text(f"Idea saved: {display}")
        return True

    elif msg.command == "ideas":
        if not _get_ideas_file().exists():
            await reply.reply_text("No ideas file found.")
            return True

        text = _get_ideas_file().read_text(encoding="utf-8")
        year = datetime.now().strftime("%Y")
        year_header = f"## {year}"

        if year_header not in text:
            await reply.reply_text(f"No ideas for {year} yet.")
            return True

        header_pos = text.index(year_header)
        after_header = text[header_pos + len(year_header):]
        next_header = re.search(r"\n## \d{4}", after_header)
        section = after_header[:next_header.start()] if next_header else after_header

        ideas = [line.strip() for line in section.splitlines() if line.strip().startswith("- ")]
        if not ideas:
            await reply.reply_text(f"No ideas for {year} yet.")
            return True

        lines = [f"Ideas {year} ({len(ideas)} total)\n"]
        for item in ideas:
            lines.append(f"- {item[2:]}")
        await reply.reply_text("\n".join(lines))
        return True

    return False


def register(app: Application):
    app.add_handler(CommandHandler("idea", cmd_idea))
    app.add_handler(CommandHandler("ideas", cmd_ideas))
