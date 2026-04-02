"""
Search

Commands (works in any topic):
  /search <query>                    — Search all platforms
  /search <query> reddit youtube     — Search specific platforms
  /searchreddit <query>              — Reddit only
  /searchyt <query>                  — YouTube only
  /searchx <query>                   — X/Twitter only
"""

import asyncio
import re
from pathlib import Path

from telegram import InlineQueryResultArticle, InputTextMessageContent, Update
from telegram.ext import Application, CommandHandler, ContextTypes, InlineQueryHandler

from cupbots.config import get_config
from cupbots.helpers.llm import add_history
from cupbots.helpers.logger import get_logger

log = get_logger("search")


def _get_search_paths():
    cfg = get_config()
    scripts_dir = Path(cfg.get("scripts_dir", ""))
    python_bin = cfg.get("python_bin", "python3")
    return {
        "script": scripts_dir / "search" / "search_all.py",
        "output_dir": scripts_dir / "search" / "output",
        "python": python_bin,
    }

ALL_PLATFORMS = {"reddit", "youtube", "x"}


def _parse_search_args(args: list[str]) -> tuple[str, list[str]]:
    """Split args into (query, platforms). Platform names at the end are extracted."""
    if not args:
        return "", []

    # Check if last args are platform names
    platforms = []
    query_parts = list(args)

    while query_parts and query_parts[-1].lower() in ALL_PLATFORMS:
        platforms.insert(0, query_parts.pop().lower())

    query = " ".join(query_parts)
    return query, platforms


async def _run_search(update, query: str, platforms: list[str]):
    """Run search_all.py and send results."""
    user_id = update.message.from_user.id if update.message.from_user else 0
    paths = _get_search_paths()
    cmd = [str(paths["python"]), str(paths["script"]), query, "--backend", "cli", "--no-telegram"]
    if platforms:
        cmd.extend(["--platforms"] + platforms)

    platform_str = ", ".join(p.capitalize() for p in (platforms or ["reddit", "youtube", "x"]))
    add_history(user_id, "user", f"/search {platform_str}: {query}")
    await update.message.reply_text(
        f"🔍 Searching *{platform_str}* for: _{query}_\n\nThis may take a few minutes...",
        parse_mode="Markdown",
    )

    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(paths["script"].parent.parent),
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=900)

        output = stdout.decode()

        # Parse stats from stdout
        telegraph_url = ""
        output_file = ""
        searches = []  # what was actually searched
        results = []   # what was found
        warnings = []  # issues
        for line in output.splitlines():
            if "telegra.ph" in line or "mdpubs.com" in line:
                match = re.search(r"https://(?:telegra\.ph|mdpubs\.com)/\S+", line)
                if match:
                    telegraph_url = match.group(0)
            elif "Saved to" in line:
                match = re.search(r"(/.+\.md)", line)
                if match:
                    output_file = match.group(1)
            elif "📡" in line:
                # e.g. "  📡 Reddit: searching 'query' (relevance, month)..."
                clean = line.strip().lstrip("📡").strip()
                if clean:
                    searches.append(clean)
            elif "Found " in line:
                match = re.search(r"Found (\d+ \w+.*)", line)
                if match:
                    results.append(match.group(1).strip())
            elif "All Invidious instances failed" in line:
                warnings.append("Invidious failed → yt-dlp fallback")
            elif "⚠️" in line:
                clean = line.strip().lstrip("⚠️").strip()
                if clean:
                    warnings.append(clean)
            elif "Rate limited" in line:
                warnings.append("Reddit rate-limited, retried")

        # Build debug summary
        debug_lines = []
        for s in searches:
            debug_lines.append(f"  📡 {s}")
        for r in results:
            debug_lines.append(f"  ✓ {r}")
        for w in warnings:
            debug_lines.append(f"  ⚠️ {w}")
        debug_str = "\n".join(debug_lines) if debug_lines else "no stats"
        stats_short = " · ".join(results) if results else "no results"

        if proc.returncode == 0 and telegraph_url:
            add_history(user_id, "assistant",
                f"Search complete for '{query}'. Found: {stats_short}. "
                f"Full analysis: {telegraph_url} "
                + (f"Output file: {output_file}" if output_file else ""))
            await update.message.reply_text(
                f"✅ *{query}*\n\n"
                f"{debug_str}\n\n"
                f"📖 [Read full analysis]({telegraph_url})",
                parse_mode="Markdown",
            )
        elif proc.returncode == 0:
            # No telegraph URL but succeeded
            last_lines = output.strip().splitlines()[-5:]
            await update.message.reply_text(
                f"✅ Search complete but no Telegraph link found.\n\n```\n{'\\n'.join(last_lines)}\n```",
                parse_mode="Markdown",
            )
        else:
            # Script prints diagnostics to stdout, errors may be in stderr or stdout
            error = stderr.decode().strip() if stderr else ""
            if not error:
                # Extract error lines from stdout (script prints errors via print())
                error_lines = [l for l in output.splitlines() if any(
                    k in l.lower() for k in ["error", "failed", "❌", "⚠️", "traceback", "exception"]
                )]
                error = "\n".join(error_lines[-10:]) if error_lines else output[-500:]
            else:
                error = error[-500:]
            await update.message.reply_text(f"❌ Search failed:\n```\n{error}\n```", parse_mode="Markdown")

    except asyncio.TimeoutError:
        await update.message.reply_text("❌ Search timed out (>15 minutes).")
    except Exception as e:
        log.error("Search error: %s", e)
        await update.message.reply_text(f"❌ Error: {type(e).__name__}. See logs.")


async def cmd_search(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not context.args:
        await update.message.reply_text(
            "Usage:\n"
            "`/search indie saas pricing`\n"
            "`/search AI agents reddit youtube`\n"
            "`/searchreddit solopreneur tools`\n"
            "`/searchyt indie hacking`\n"
            "`/searchx micro saas`",
            parse_mode="Markdown",
        )
        return

    query, platforms = _parse_search_args(context.args)
    if not query:
        await update.message.reply_text("Please provide a search query.")
        return

    await _run_search(update, query, platforms)


async def cmd_searchreddit(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not context.args:
        await update.message.reply_text("Usage: `/searchreddit <query>`", parse_mode="Markdown")
        return
    query = " ".join(context.args)
    await _run_search(update, query, ["reddit"])


async def cmd_searchyt(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not context.args:
        await update.message.reply_text("Usage: `/searchyt <query>`", parse_mode="Markdown")
        return
    query = " ".join(context.args)
    await _run_search(update, query, ["youtube"])


async def cmd_searchx(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not context.args:
        await update.message.reply_text("Usage: `/searchx <query>`", parse_mode="Markdown")
        return
    query = " ".join(context.args)
    await _run_search(update, query, ["x"])


async def _inline_search(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle @bot <query> inline mode — offer platform choices."""
    query = update.inline_query.query.strip()
    if not query or len(query) < 3:
        return

    results = [
        InlineQueryResultArticle(
            id="all",
            title=f"Search all: {query}",
            description="Reddit + YouTube + X",
            input_message_content=InputTextMessageContent(f"/search {query}"),
        ),
        InlineQueryResultArticle(
            id="reddit",
            title=f"Reddit: {query}",
            description="Search Reddit only",
            input_message_content=InputTextMessageContent(f"/searchreddit {query}"),
        ),
        InlineQueryResultArticle(
            id="youtube",
            title=f"YouTube: {query}",
            description="Search YouTube only",
            input_message_content=InputTextMessageContent(f"/searchyt {query}"),
        ),
        InlineQueryResultArticle(
            id="x",
            title=f"X/Twitter: {query}",
            description="Search X only",
            input_message_content=InputTextMessageContent(f"/searchx {query}"),
        ),
    ]
    await update.inline_query.answer(results, cache_time=0)


async def handle_command(msg, reply):
    """Platform-agnostic search handler. Supports /search, /searchreddit, /searchyt, /searchx."""
    cmd = msg.command
    if cmd not in ("search", "searchreddit", "searchyt", "searchx"):
        return False

    if not msg.args:
        await reply.reply_text("Usage: /search <query> [reddit youtube x]")
        return True

    platform_map = {"searchreddit": ["reddit"], "searchyt": ["youtube"], "searchx": ["x"]}

    if cmd in platform_map:
        query = " ".join(msg.args)
        platforms = platform_map[cmd]
    else:
        query, platforms = _parse_search_args(msg.args)

    if not query:
        await reply.reply_text("Please provide a search query.")
        return True

    platform_str = ", ".join(p.capitalize() for p in (platforms or ["reddit", "youtube", "x"]))
    await reply.reply_text(f"Searching {platform_str} for: {query}\n\nThis may take a few minutes...")

    paths = _get_search_paths()
    cmd_args = [str(paths["python"]), str(paths["script"]), query, "--backend", "cli", "--no-telegram"]
    if platforms:
        cmd_args.extend(["--platforms"] + platforms)

    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd_args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(paths["script"].parent.parent),
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=900)
        output = stdout.decode()

        # Find telegraph/mdpubs URL
        url_match = re.search(r"https://(?:telegra\.ph|mdpubs\.com)/\S+", output)
        if proc.returncode == 0 and url_match:
            await reply.reply_text(f"Search complete: {query}\n\nRead: {url_match.group(0)}")
        elif proc.returncode == 0:
            await reply.reply_text("Search complete but no output URL found.")
        else:
            error = stderr.decode().strip()[-300:] if stderr else output[-300:]
            await reply.reply_text(f"Search failed:\n{error}")

    except asyncio.TimeoutError:
        await reply.reply_text("Search timed out (>15 minutes).")
    except Exception as e:
        log.error("Search error: %s", e)
        await reply.reply_text(f"Error: {type(e).__name__}. See logs.")

    return True


def register(app: Application):
    app.add_handler(CommandHandler("search", cmd_search))
    app.add_handler(CommandHandler("searchreddit", cmd_searchreddit))
    app.add_handler(CommandHandler("searchyt", cmd_searchyt))
    app.add_handler(CommandHandler("searchx", cmd_searchx))
    app.add_handler(InlineQueryHandler(_inline_search))
