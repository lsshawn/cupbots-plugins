"""
YouTube Analyzer

Commands (works in any topic):
  /yt <url>  — Analyze a YouTube video (transcript → Claude → Telegraph)

Reply to any analysis (YouTube, Reddit, X) to ask follow-up questions.
Raw context (transcripts, scraped posts) is used for follow-ups, not just the Telegraph summary.
"""

import asyncio
import importlib.util
import os
import re
import tempfile
import yaml
from datetime import datetime
from pathlib import Path

import httpx
from telegram import Update
from telegram.constants import ChatAction
from telegram.ext import Application, ApplicationHandlerStop, CommandHandler, ContextTypes

from cupbots.helpers.llm import run_claude_cli
from cupbots.helpers.logger import get_logger

log = get_logger("youtube")

from cupbots.config import get_config, get_data_dir


_paths_initialized = False


def _init_yt_paths():
    global YT_SCRIPT_DIR, YT_OUTPUT_DIR, CONTEXT_DIR, PIPELINE_DIRS, _paths_initialized
    if _paths_initialized:
        return
    cfg = get_config()
    scripts_dir = Path(cfg.get("scripts_dir", ""))
    yt_analyses = Path(cfg.get("allowed_paths", {}).get("yt_analyses", str(get_data_dir() / "yt-analyses")))
    YT_SCRIPT_DIR = scripts_dir / "youtube-analyzer"
    YT_OUTPUT_DIR = yt_analyses
    CONTEXT_DIR = get_data_dir() / "analysis-context"
    PIPELINE_DIRS = {
        "reddit": scripts_dir / "reddit-digest" / "output",
        "x": scripts_dir / "x-scrapper" / "output",
        "youtube": yt_analyses,
    }
    _paths_initialized = True


# Lazy-initialized path globals (set by _init_yt_paths on first use)
YT_SCRIPT_DIR = None
YT_OUTPUT_DIR = None
CONTEXT_DIR = None
PIPELINE_DIRS = {}


def _load_module(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, YT_SCRIPT_DIR / filename)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_analyzer = None
_tg_post = None


def _get_analyzer():
    global _analyzer
    if _analyzer is None:
        _analyzer = _load_module("analyzer", "youtube-transcript-analyzer.py")
    return _analyzer


def _get_tg_post():
    global _tg_post
    if _tg_post is None:
        _tg_post = _load_module("tg_post", "telegram-telegraph-post.py")
    return _tg_post


def _is_youtube_url(text: str) -> bool:
    return bool(re.search(r"(youtube\.com/watch|youtu\.be/|youtube\.com/shorts/)", text))


def _extract_video_id(url: str) -> str | None:
    patterns = [
        r'(?:v=|/v/)([a-zA-Z0-9_-]{11})',
        r'(?:youtu\.be/)([a-zA-Z0-9_-]{11})',
        r'(?:embed/)([a-zA-Z0-9_-]{11})',
        r'(?:shorts/)([a-zA-Z0-9_-]{11})',
    ]
    for pattern in patterns:
        match = re.search(pattern, url)
        if match:
            return match.group(1)
    return None


def _extract_telegraph_url(text: str) -> str | None:
    match = re.search(r'https?://(?:telegra\.ph|mdpubs\.com)/[^\s\)]+', text or "")
    return match.group(0) if match else None


def _try_mark_miniflux_read(url: str) -> str | None:
    """Try to find this URL in Miniflux and mark it as read. Returns feed name if found."""
    try:
        import miniflux
    except ImportError:
        return None

    from cupbots.helpers.db import resolve_plugin_setting
    miniflux_url = resolve_plugin_setting("youtube", "miniflux_url")
    miniflux_api_key = resolve_plugin_setting("youtube", "miniflux_api_key")
    if not miniflux_url or not miniflux_api_key:
        return None

    try:
        client = miniflux.Client(miniflux_url, api_key=miniflux_api_key)
        video_id = _extract_video_id(url)
        if not video_id:
            return None

        # Paginate through all unread entries to find the video
        offset = 0
        batch = 250
        while True:
            response = client.get_entries(status=["unread"], limit=batch, offset=offset)
            entries = response.get("entries", [])
            for entry in entries:
                entry_url = entry.get("url", "")
                if video_id in entry_url:
                    client.update_entries(entry_ids=[entry["id"]], status="read")
                    feed_name = entry.get("feed", {}).get("title", "")
                    log.info("Marked Miniflux entry %d as read (%s)", entry["id"], feed_name)
                    return feed_name
            if len(entries) < batch:
                break
            offset += batch
    except Exception as e:
        log.warning("Miniflux lookup failed: %s", e)

    return None


# ── Context storage ──────────────────────────────────────────────

def _save_context(message_id: int, content: str):
    """Save raw context for an analysis message so follow-ups have full data."""
    CONTEXT_DIR.mkdir(parents=True, exist_ok=True)
    path = CONTEXT_DIR / f"{message_id}.txt"
    path.write_text(content, encoding="utf-8")
    log.info("Saved analysis context: %s (%d chars)", path.name, len(content))


def _load_context(message_id: int) -> str | None:
    """Load saved raw context for a message."""
    path = CONTEXT_DIR / f"{message_id}.txt"
    if path.exists():
        return path.read_text(encoding="utf-8")
    return None


def _find_pipeline_context(reply_text: str) -> str | None:
    """Try to find raw context for a Reddit/X pipeline analysis by matching the message.

    Looks at the message text to determine pipeline type and date, then reads
    the analysis frontmatter to find batch files with raw data.
    """
    # Determine pipeline type from message content
    pipeline = None
    if "Reddit" in reply_text or "reddit" in reply_text:
        pipeline = "reddit"
    elif "X List" in reply_text or "x-list" in reply_text.lower():
        pipeline = "x"
    elif "🎥" in reply_text or "youtube" in reply_text.lower():
        pipeline = "youtube"

    if not pipeline:
        return None

    if pipeline == "youtube":
        return _find_yt_context(reply_text)

    # For Reddit/X: find the most recent analysis file and its batch files
    analyses_dir = PIPELINE_DIRS[pipeline] / "analyses"
    batches_dir = PIPELINE_DIRS[pipeline] / "batches"

    if not analyses_dir.exists():
        return None

    # Find the most recent analysis file (the one this message is about)
    analysis_files = sorted(analyses_dir.glob("*.md"), reverse=True)
    if not analysis_files:
        return None

    # Try to match by date in the message or just use most recent
    today = datetime.now().strftime("%Y%m%d")
    matched = None
    for f in analysis_files:
        if today in f.name:
            matched = f
            break
    if not matched:
        matched = analysis_files[0]  # fallback to most recent

    # Read analysis and extract batch filenames from frontmatter
    analysis_text = matched.read_text(encoding="utf-8")
    batch_files = []

    if analysis_text.startswith("---"):
        end = analysis_text.find("---", 3)
        if end != -1:
            try:
                fm = yaml.safe_load(analysis_text[3:end])
                batch_files = fm.get("batches", [])
            except Exception:
                pass

    # Read raw batch files
    parts = [f"=== ANALYSIS ({matched.name}) ===\n{analysis_text}\n"]

    for batch_name in batch_files:
        batch_path = batches_dir / batch_name
        if batch_path.exists():
            raw = batch_path.read_text(encoding="utf-8")
            parts.append(f"\n=== RAW DATA ({batch_name}) ===\n{raw}\n")

    return "\n".join(parts)


def _find_yt_context(reply_text: str) -> str | None:
    """Find YT analysis file and return its content."""
    if not YT_OUTPUT_DIR.exists():
        return None

    today = datetime.now().strftime("%Y%m%d")
    files = sorted(YT_OUTPUT_DIR.glob(f"{today}*.md"), reverse=True)
    if not files:
        files = sorted(YT_OUTPUT_DIR.glob("*.md"), reverse=True)[:5]

    for f in files:
        return f.read_text(encoding="utf-8")

    return None


# ── Session tracking ─────────────────────────────────────────────

# Maps message_id -> (session_id, timestamp) for follow-up replies
_analysis_sessions: dict[int, tuple[str, float]] = {}
_sessions_lock = asyncio.Lock()
_SESSION_TTL = 3600  # 1 hour


async def _cleanup_old_sessions():
    """Remove sessions older than _SESSION_TTL."""
    import time
    now = time.time()
    async with _sessions_lock:
        expired = [k for k, (_, ts) in _analysis_sessions.items() if now - ts > _SESSION_TTL]
        for k in expired:
            del _analysis_sessions[k]
        if expired:
            log.info("Cleaned up %d expired analysis session(s)", len(expired))


# ── /yt command ──────────────────────────────────────────────────

async def cmd_yt(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message:
        return

    args = context.args or []
    url = args[0] if args else ""

    if not url or not _is_youtube_url(url):
        await update.message.reply_text(
            "Usage: `/yt <youtube-url>`\n\n"
            "Fetches transcript, analyzes with Claude, posts to Telegraph.\n"
            "Reply to the analysis to ask follow-up questions.",
            parse_mode="Markdown",
        )
        return

    msg = await update.message.reply_text("🎥 Fetching transcript...")

    try:
        analyzer = _get_analyzer()
        video_id = _extract_video_id(url)
        transcript_text, duration = await _in_thread(analyzer.get_transcript, video_id)
        duration_min = duration / 60

        video_title = await _in_thread(analyzer.get_video_title, video_id)
        await msg.edit_text(
            f"🎥 *{video_title}* ({duration_min:.0f}min)\n\n🧠 Analyzing with Claude...",
            parse_mode="Markdown",
        )

        # Include transcript directly in prompt (avoids multi-turn Read tool issue
        # where Claude burns all max_turns reading chunks and never produces output)
        prompt = (
            f"Video Title: {video_title}\n"
            f"Video URL: {url}\n\n"
            f"TRANSCRIPT:\n{transcript_text}\n\n"
            f"---\n\n"
            f"{analyzer.ANALYSIS_PROMPT}"
        )

        result = await run_claude_cli(
            prompt,
            model="sonnet",
            max_turns=2,
            max_budget_usd="1.00",
            timeout=300,
        )
        analysis = result["text"]
        session_id = result["session_id"]

        # Save markdown
        YT_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        feed_name = await _in_thread(_try_mark_miniflux_read, url) or ""
        filepath = await _save_yt_analysis(
            url, video_id, video_title, feed_name, duration_min, analysis
        )

        # Publish to Telegraph
        from cupbots.helpers.telegraph_telegram import md_to_telegraph_html, publish_to_telegraph

        tg_post = _get_tg_post()
        title, video_url, feed, body = tg_post.read_md_file(str(filepath))
        tldr = tg_post.extract_tldr(body)
        html = md_to_telegraph_html(body)
        telegraph_url = publish_to_telegraph(title, html, author_name="YT Analyzer")

        # Send response
        parts = []
        if feed_name:
            parts.append(f"📡 _{feed_name}_")
        parts.append(f"📊 *{video_title}*")
        if tldr:
            parts.append(f"\n{tldr}")
        parts.append(f"\n🎥 [Watch video]({url})")
        parts.append(f"📖 [Read analysis]({telegraph_url})")
        if feed_name:
            parts.append(f"\n✅ Marked as read in Miniflux")

        response = "\n".join(parts)
        if len(response) > 4000:
            response = response[:4000]

        sent = await msg.edit_text(response, parse_mode="Markdown", disable_web_page_preview=True)

        # Store session + raw context for follow-ups
        if sent:
            if session_id:
                import time as _time
                async with _sessions_lock:
                    _analysis_sessions[sent.message_id] = (session_id, _time.time())
            # Save raw transcript so follow-ups have full context even without session
            raw_context = f"Video Title: {video_title}\nVideo URL: {url}\nDuration: {duration_min:.0f}min\n\nTRANSCRIPT:\n{transcript_text}\n\n=== ANALYSIS ===\n{analysis}"
            _save_context(sent.message_id, raw_context)
            log.info("YT analysis done for msg %d", sent.message_id)

    except Exception as e:
        log.error("YouTube analysis failed for %s: %s", url, e)
        await msg.edit_text(f"❌ Analysis failed: {type(e).__name__}. See logs.")


# ── Reply handler ────────────────────────────────────────────────

async def _handle_analysis_reply(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle replies to any analysis message (YT, Reddit, X).

    Priority:
    1. Existing Claude session → --resume (has full context already)
    2. Saved raw context file → start new session with raw data
    3. Pipeline file match → find analysis + batch files on disk
    4. Telegraph link → fetch Telegraph content as fallback
    5. None of the above → ignore, let claude_chat handle it
    """
    msg = update.message
    if not msg or not msg.reply_to_message:
        return

    reply_to = msg.reply_to_message
    reply_to_id = reply_to.message_id
    question = msg.text or ""
    if not question:
        return

    # Case 1: Existing session
    async with _sessions_lock:
        entry = _analysis_sessions.get(reply_to_id)
    session_id = entry[0] if entry else None
    if session_id:
        await _do_followup(msg, question, session_id)
        raise ApplicationHandlerStop

    # Case 2: Saved raw context
    raw_context = _load_context(reply_to_id)
    if raw_context:
        await _start_session_with_context(msg, question, raw_context)
        raise ApplicationHandlerStop

    # Case 3: Try to find pipeline files on disk
    reply_text = reply_to.text or reply_to.caption or ""
    telegraph_url = _extract_telegraph_url(reply_text)
    if not telegraph_url:
        return  # Not an analysis message

    pipeline_context = await _in_thread(_find_pipeline_context, reply_text)
    if pipeline_context:
        await _start_session_with_context(msg, question, pipeline_context)
        raise ApplicationHandlerStop

    # Case 4: Fallback — fetch Telegraph content
    telegraph_content = await _in_thread(_fetch_telegraph_content, telegraph_url)
    if telegraph_content:
        await _start_session_with_context(msg, question, telegraph_content)
        raise ApplicationHandlerStop

    # Not an analysis we can handle — let other handlers deal with it


async def _start_session_with_context(msg, question: str, context_text: str):
    """Start a new Claude session with raw context and the user's question."""
    thinking = await msg.reply_text("📄 Loading context & thinking...")

    try:
        # Write context to temp file for Claude to read
        tmp = tempfile.NamedTemporaryFile(
            mode="w", suffix=".txt", prefix="analysis-ctx-", delete=False
        )
        tmp.write(context_text)
        tmp.close()

        prompt = (
            f"Read the file at {tmp.name}. It contains raw data and/or analysis from a content pipeline. "
            f"The user wants to discuss it.\n\n"
            f"User question: {question}"
        )

        result = await run_claude_cli(
            prompt,
            model="sonnet",
            tools="Read",
            max_turns=5,
            max_budget_usd="0.30",
            timeout=120,
        )

        try:
            os.unlink(tmp.name)
        except OSError:
            pass

        response = result["text"]
        new_session_id = result["session_id"]

        if len(response) > 4000:
            response = response[:4000] + "\n\n... (truncated)"

        sent = await thinking.edit_text(response)

        if new_session_id and sent:
            import time as _time
            async with _sessions_lock:
                _analysis_sessions[sent.message_id] = (new_session_id, _time.time())

    except Exception as e:
        log.error("Analysis follow-up failed: %s", e)
        await thinking.edit_text(f"❌ Error: {type(e).__name__}. See logs.")


async def _do_followup(msg, question: str, session_id: str):
    """Resume an existing Claude session with a follow-up question."""
    await msg.chat.send_action(action=ChatAction.TYPING)
    thinking = await msg.reply_text("🧠 Thinking...")

    try:
        result = await run_claude_cli(
            question,
            model="sonnet",
            tools="Read",
            max_turns=5,
            max_budget_usd="0.30",
            session_id=session_id,
            timeout=120,
        )

        response = result["text"]
        new_session_id = result["session_id"]

        if len(response) > 4000:
            response = response[:4000] + "\n\n... (truncated)"

        sent = await thinking.edit_text(response)

        if new_session_id and sent:
            import time as _time
            async with _sessions_lock:
                _analysis_sessions[sent.message_id] = (new_session_id, _time.time())

    except Exception as e:
        log.error("Analysis follow-up failed: %s", e)
        await thinking.edit_text(f"❌ Error: {type(e).__name__}. See logs.")


# ── Telegraph fallback ───────────────────────────────────────────

def _fetch_telegraph_content(url: str) -> str | None:
    path = url.replace("https://telegra.ph/", "").replace("http://telegra.ph/", "")
    try:
        resp = httpx.get(
            f"https://api.telegra.ph/getPage/{path}",
            params={"return_content": "true"},
            timeout=10,
        )
        data = resp.json()
        if not data.get("ok"):
            return None
        page = data["result"]
        title = page.get("title", "")
        content = _nodes_to_text(page.get("content", []))
        return f"Title: {title}\n\n{content}"
    except Exception as e:
        log.warning("Failed to fetch Telegraph page %s: %s", url, e)
        return None


def _nodes_to_text(nodes: list) -> str:
    parts = []
    for node in nodes:
        if isinstance(node, str):
            parts.append(node)
        elif isinstance(node, dict):
            tag = node.get("tag", "")
            children = node.get("children", [])
            child_text = _nodes_to_text(children)

            if tag in ("h3", "h4"):
                parts.append(f"\n## {child_text}\n")
            elif tag == "blockquote":
                parts.append(f"> {child_text}\n")
            elif tag == "p":
                parts.append(f"{child_text}\n")
            elif tag == "li":
                parts.append(f"- {child_text}\n")
            elif tag == "a":
                href = node.get("attrs", {}).get("href", "")
                parts.append(f"[{child_text}]({href})")
            elif tag in ("strong", "b"):
                parts.append(f"**{child_text}**")
            elif tag in ("em", "i"):
                parts.append(f"*{child_text}*")
            elif tag == "br":
                parts.append("\n")
            elif tag == "hr":
                parts.append("\n---\n")
            else:
                parts.append(child_text)
    return "".join(parts)


# ── Helpers ──────────────────────────────────────────────────────

async def _save_yt_analysis(
    url: str, video_id: str, title: str, feed_name: str,
    duration_min: float, analysis: str,
) -> Path:
    analyzer = _get_analyzer()
    date_str = datetime.now().strftime("%Y%m%d")
    feed_slug = analyzer.slugify(feed_name) if feed_name else ""
    title_slug = analyzer.slugify(title)
    filename = f"{date_str}-{feed_slug}-{title_slug}.md" if feed_slug else f"{date_str}-{title_slug}.md"
    filepath = YT_OUTPUT_DIR / filename

    frontmatter = f"""---
title: "{title}"
feed: "{feed_name}"
url: {url}
video_id: {video_id}
duration_minutes: {duration_min:.1f}
analyzed: {datetime.now().isoformat()}
---

"""
    filepath.write_text(frontmatter + analysis, encoding="utf-8")
    log.info("Saved analysis: %s", filepath)
    return filepath


async def _in_thread(fn, *args):
    return await asyncio.get_event_loop().run_in_executor(None, fn, *args)


async def handle_command(msg, reply) -> bool:
    """Platform-agnostic YouTube analysis."""
    if msg.command != "yt":
        return False

    _init_yt_paths()

    url = msg.args[0] if msg.args else ""
    if not url or not _is_youtube_url(url):
        await reply.reply_text("Usage: /yt <youtube-url>")
        return True

    await reply.reply_text("Fetching transcript...")

    try:
        analyzer = _get_analyzer()
        video_id = _extract_video_id(url)
        transcript_text, duration = await _in_thread(analyzer.get_transcript, video_id)
        duration_min = duration / 60
        video_title = await _in_thread(analyzer.get_video_title, video_id)

        await reply.reply_text(f"{video_title} ({duration_min:.0f}min)\nAnalyzing...")

        # Mark as read in RSS if found
        feed_name = await _in_thread(_try_mark_miniflux_read, url) or ""

        prompt = (
            f"Video Title: {video_title}\n"
            f"Video URL: {url}\n\n"
            f"TRANSCRIPT:\n{transcript_text}\n\n"
            f"---\n\n"
            f"{analyzer.ANALYSIS_PROMPT}"
        )

        result = await run_claude_cli(
            prompt, model="sonnet", max_turns=2,
            max_budget_usd="1.00", timeout=300,
        )
        analysis = result["text"]

        # Save and publish via mdpubs
        YT_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        filepath = await _save_yt_analysis(url, video_id, video_title, feed_name, duration_min, analysis)

        from plugins.mdpubs.mdpubs import publish_or_fallback
        tg_post = _get_tg_post()
        title, video_url, feed, body = tg_post.read_md_file(str(filepath))
        pub_url, _ = await publish_or_fallback(
            f"yt-{video_id}", title, body, company_id=msg.company_id, tags=["youtube"]
        )

        parts = [f"*{video_title}*"]
        if pub_url:
            parts.append(pub_url)
        else:
            parts.append("(mdpubs not configured — analysis saved locally)")
        if feed_name:
            parts.append(f"_RSS: {feed_name} — marked read_")
        await reply.reply_text("\n".join(parts))

    except Exception as e:
        log.error("YouTube analysis failed for %s: %s", url, e)
        await reply.reply_error(f"Analysis failed: {type(e).__name__}. See logs.")

    return True


async def _periodic_session_cleanup(context):
    await _cleanup_old_sessions()


def register(app: Application):
    _init_yt_paths()
    from telegram.ext import MessageHandler, filters
    app.add_handler(CommandHandler("yt", cmd_yt))
    app.add_handler(MessageHandler(
        filters.REPLY & filters.TEXT & ~filters.COMMAND,
        _handle_analysis_reply,
    ), group=50)
    # Clean up expired analysis sessions every 30 minutes
    app.job_queue.run_repeating(_periodic_session_cleanup, interval=1800, first=1800)
