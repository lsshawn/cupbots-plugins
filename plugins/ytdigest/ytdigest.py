"""
YouTube Digest — AI-powered video analysis with timestamped insights.

Commands:
  /ytdigest <url>                  — Analyze a single YouTube video
  /ytdigest run                    — Process unread videos from Miniflux RSS
  /ytdigest history                — Show recent analyses

Setup: /config ytdigest for Miniflux credentials (optional, for RSS mode).
Schedule: /schedule add "daily 20:00" /ytdigest run

Examples:
  /ytdigest https://youtube.com/watch?v=abc123
  /ytdigest run
  /ytdigest history
"""

import json
import os
import re
import subprocess
import tempfile
from datetime import datetime
from pathlib import Path

import httpx

from cupbots.helpers.db import get_plugin_db, resolve_plugin_setting
from cupbots.helpers.logger import get_logger

log = get_logger("ytdigest")

PLUGIN_NAME = "ytdigest"
WA_API_URL = os.environ.get("WA_API_URL", "http://127.0.0.1:3100")
MIN_DURATION_DEFAULT = 3  # minutes

ANALYSIS_PROMPT = """You are a video analyst. Extract evidence-based, quote-supported, tactically useful insights from the transcript.

NEVER ask questions. NEVER refuse to analyze. Just analyze whatever transcript you receive.

Every insight must include a direct quote and a clickable timestamp link.

Write in grade 8 level. Keep sentences simple without diluting the facts.

Analyze the FULL transcript from start to finish. Extract every high-value insight. Cover ALL important points chronologically. Use direct quotes with clickable timestamp links.

TIMESTAMP FORMAT (CRITICAL)

The Video URL is provided below. Every quote MUST include a clickable markdown link using that exact URL with &t=SECONDS appended.

Format: ["quote text"](Video_URL&t=SECONDS)

Convert HH:MM:SS timestamps to total seconds for &t=.
00:05:40 = 340 seconds -> &t=340

OUTPUT FORMAT

Start with a 2-3 sentence TL;DR summarizing the video's core message.

Each insight requires:
- Bolded title
- 2-3 sentence explanation
- One direct quote as a clickable timestamp link

Use whichever of these sections are relevant:

---

## Core Philosophies & Mental Models

Extract mental models, frameworks, or thinking patterns discussed.

**[Title]**

Explanation (2-3 sentences).

> ["exact quote"](VIDEO_URL&t=SECONDS)

---

## Systems, Routines & Workflows

Concrete systems, workflows, habits, tools, or processes.

**[Title]**

Explanation.

> ["exact quote"](VIDEO_URL&t=SECONDS)

---

## Actionable Ideas & Opportunities

Turn frustrations, gaps, or opportunities into actionable takeaways.

**[Title]**

Mechanism: 2-3 sentence description.

> ["exact quote"](VIDEO_URL&t=SECONDS)

---

## Specific Numbers, Results & Proof Points

Every concrete metric, revenue figure, growth number, or measurable result.

**[What was measured]**

Context in 1-2 sentences.

> ["exact quote with the number"](VIDEO_URL&t=SECONDS)

---

## Mistakes, Warnings & Contrarian Takes

Explicitly stated mistakes, regrets, failures, or opinions against common advice.

**[Title]**

What happened and what they learned.

> ["exact quote"](VIDEO_URL&t=SECONDS)

---

## Trends & Industry Shifts

Industry-wide patterns or predictions (only if explicitly discussed).

- Bullet list with clickable quote + timestamp links

---

STRICT RULES:
1. Do NOT infer unspoken motivations or insert external knowledge.
2. All insights require exact quotes with clickable timestamp links.
3. Do NOT round numbers — use exact figures from the transcript.
4. Work through the ENTIRE transcript — do not stop halfway."""


# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------

def create_tables(conn):
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS analyses (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            company_id TEXT NOT NULL DEFAULT '',
            video_id TEXT NOT NULL,
            title TEXT NOT NULL DEFAULT '',
            channel TEXT NOT NULL DEFAULT '',
            duration_minutes REAL NOT NULL DEFAULT 0,
            analysis TEXT NOT NULL DEFAULT '',
            published_url TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            UNIQUE(company_id, video_id)
        );
    """)


def _db():
    return get_plugin_db(PLUGIN_NAME)


# ---------------------------------------------------------------------------
# YouTube transcript extraction
# ---------------------------------------------------------------------------

def _extract_video_id(url: str) -> str:
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
    raise ValueError(f"Could not extract video ID from: {url}")


def _format_timestamp(seconds: float) -> str:
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = int(seconds % 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


def _get_transcript_api(video_id: str) -> tuple[str, float]:
    """Fetch transcript using youtube-transcript-api."""
    from youtube_transcript_api import YouTubeTranscriptApi

    ytt = YouTubeTranscriptApi()
    result = ytt.fetch(video_id, languages=["en"])

    lines = []
    total_duration = 0.0
    for snippet in result:
        start_sec = snippet.start
        ts = _format_timestamp(start_sec)
        text = snippet.text.replace("\n", " ").strip()
        if text:
            lines.append(f"[{ts}] {text}")
            total_duration = max(total_duration, start_sec + snippet.duration)

    if not lines:
        raise RuntimeError(f"Transcript was empty for {video_id}")
    return "\n".join(lines), total_duration


def _get_transcript_ytdlp(video_id: str) -> tuple[str, float]:
    """Fetch transcript using yt-dlp as fallback."""
    import yt_dlp

    url = f"https://www.youtube.com/watch?v={video_id}"
    with tempfile.TemporaryDirectory() as tmpdir:
        opts = {
            "skip_download": True,
            "writeautomaticsub": True,
            "writesubtitles": True,
            "subtitleslangs": ["en"],
            "subtitlesformat": "json3",
            "outtmpl": os.path.join(tmpdir, "%(id)s"),
            "quiet": True,
            "no_warnings": True,
        }
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=True)

        sub_file = None
        for f in os.listdir(tmpdir):
            if f.endswith(".json3"):
                sub_file = os.path.join(tmpdir, f)
                break
        if not sub_file:
            raise RuntimeError(f"No subtitles found for {video_id}")

        with open(sub_file, "r", encoding="utf-8") as f:
            sub_data = json.load(f)

        lines = []
        total_duration = 0.0
        for event in sub_data.get("events", []):
            start_ms = event.get("tStartMs", 0)
            duration_ms = event.get("dDurationMs", 0)
            segs = event.get("segs", [])
            if not segs:
                continue
            text = "".join(seg.get("utf8", "") for seg in segs).strip()
            if not text or text == "\n":
                continue
            start_sec = start_ms / 1000.0
            lines.append(f"[{_format_timestamp(start_sec)}] {text}")
            total_duration = max(total_duration, start_sec + duration_ms / 1000.0)

        if not lines:
            raise RuntimeError(f"Subtitles file was empty for {video_id}")
        if info and info.get("duration"):
            total_duration = max(total_duration, float(info["duration"]))
        return "\n".join(lines), total_duration


def _get_transcript(video_id: str) -> tuple[str, float]:
    """Fetch transcript with fallback between backends."""
    try:
        return _get_transcript_api(video_id)
    except Exception as e:
        log.info("youtube-transcript-api failed (%s), trying yt-dlp", e)
        return _get_transcript_ytdlp(video_id)


def _get_video_title(video_id: str) -> str:
    try:
        result = subprocess.run(
            ["yt-dlp", "--get-title", "--no-download",
             f"https://youtube.com/watch?v={video_id}"],
            capture_output=True, text=True, timeout=30,
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass
    return f"YouTube Video {video_id}"


# ---------------------------------------------------------------------------
# Core logic
# ---------------------------------------------------------------------------

async def _analyze_video(video_id: str, company_id: str,
                         title: str = "", channel: str = "") -> str:
    """Analyze a single video. Returns status message."""
    # Check if already analyzed
    existing = _db().execute(
        "SELECT * FROM analyses WHERE company_id = ? AND video_id = ?",
        (company_id, video_id),
    ).fetchone()
    if existing:
        url = existing["published_url"]
        return f"Already analyzed: {existing['title']}" + (f"\n{url}" if url else "")

    url = f"https://www.youtube.com/watch?v={video_id}"

    try:
        transcript, duration = _get_transcript(video_id)
    except Exception as e:
        return f"Transcript failed: {e}"

    duration_min = duration / 60
    if not title:
        title = _get_video_title(video_id)

    # Analyze
    try:
        from cupbots.helpers.llm import ask_llm
        user_msg = f"Video Title: {title}\nVideo URL: {url}\n\nTRANSCRIPT:\n{transcript}"
        analysis = await ask_llm(ANALYSIS_PROMPT + "\n\n" + user_msg, max_tokens=8000)
    except Exception as e:
        return f"Analysis failed: {e}"

    if not analysis:
        return "Analysis returned empty."

    # Publish
    published_url = ""
    try:
        from cupbots.helpers.telegraph_telegram import publish_to_telegraph
        published_url = publish_to_telegraph(title, analysis, author_name="YT Digest")
    except Exception as e:
        log.warning("Publish failed: %s", e)

    # Save
    _db().execute(
        "INSERT OR REPLACE INTO analyses "
        "(company_id, video_id, title, channel, duration_minutes, analysis, published_url) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (company_id, video_id, title, channel, duration_min, analysis[:15000], published_url),
    )
    _db().commit()

    # Build response
    tldr = ""
    for line in analysis.splitlines():
        if line.strip() and not line.startswith("#") and not line.startswith("---"):
            tldr = line.strip()
            break

    parts = [f"{title} ({duration_min:.0f} min)"]
    if tldr:
        parts.append(f"\n{tldr[:500]}")
    if published_url:
        parts.append(f"\nFull analysis: {published_url}")
    return "\n".join(parts)


async def _run_miniflux(company_id: str) -> str:
    """Process unread YouTube entries from Miniflux."""
    miniflux_url = resolve_plugin_setting(PLUGIN_NAME, "MINIFLUX_URL") or ""
    miniflux_key = resolve_plugin_setting(PLUGIN_NAME, "MINIFLUX_API_KEY") or ""
    if not miniflux_url or not miniflux_key:
        return ("Configure Miniflux first:\n"
                "/config ytdigest MINIFLUX_URL https://your-miniflux.com\n"
                "/config ytdigest MINIFLUX_API_KEY your-key")

    min_dur = int(resolve_plugin_setting(PLUGIN_NAME, "YTDIGEST_MIN_DURATION") or MIN_DURATION_DEFAULT)

    # Fetch unread entries
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.get(
            f"{miniflux_url}/v1/entries",
            params={"status": "unread", "limit": 250},
            headers={"X-Auth-Token": miniflux_key},
        )
        r.raise_for_status()
        entries = r.json().get("entries", [])

    # Filter YouTube URLs
    yt_entries = [e for e in entries
                  if "youtube.com/watch" in e.get("url", "") or "youtu.be/" in e.get("url", "")]

    if not yt_entries:
        return f"No unread YouTube entries ({len(entries)} total unread)."

    processed = 0
    skipped = 0
    errors = 0
    results = []

    for entry in yt_entries:
        entry_url = entry["url"]
        entry_title = entry.get("title", "")
        feed_name = entry.get("feed", {}).get("title", "")

        try:
            video_id = _extract_video_id(entry_url)

            # Quick duration check
            try:
                _, duration = _get_transcript(video_id)
                if duration / 60 < min_dur:
                    skipped += 1
                    # Mark as read anyway
                    async with httpx.AsyncClient(timeout=10) as client:
                        await client.put(
                            f"{miniflux_url}/v1/entries",
                            headers={"X-Auth-Token": miniflux_key},
                            json={"entry_ids": [entry["id"]], "status": "read"},
                        )
                    continue
            except Exception:
                pass

            result = await _analyze_video(video_id, company_id,
                                          title=entry_title, channel=feed_name)
            results.append(result)
            processed += 1

            # Mark as read in Miniflux
            async with httpx.AsyncClient(timeout=10) as client:
                await client.put(
                    f"{miniflux_url}/v1/entries",
                    headers={"X-Auth-Token": miniflux_key},
                    json={"entry_ids": [entry["id"]], "status": "read"},
                )

        except Exception as e:
            log.error("Failed to process %s: %s", entry_url, e)
            errors += 1

    summary = f"YouTube Digest: {processed} analyzed, {skipped} skipped (<{min_dur}min), {errors} errors"
    if results:
        summary += "\n\n" + "\n---\n".join(results[:5])
    return summary


# ---------------------------------------------------------------------------
# Command handler
# ---------------------------------------------------------------------------

async def handle_command(msg, reply) -> bool:
    if msg.command != "ytdigest":
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

    if sub == "run":
        await reply.send_typing()
        result = await _run_miniflux(company_id)
        await reply.reply_text(result)
        return True

    if sub == "history":
        rows = _db().execute(
            "SELECT id, title, duration_minutes, published_url, created_at FROM analyses "
            "WHERE company_id = ? ORDER BY created_at DESC LIMIT 10",
            (company_id,),
        ).fetchall()
        if not rows:
            await reply.reply_text("No analyses yet.")
            return True
        lines = ["Recent analyses:\n"]
        for r in rows:
            dur = f" ({r['duration_minutes']:.0f}min)" if r["duration_minutes"] else ""
            url = f"\n  {r['published_url']}" if r["published_url"] else ""
            lines.append(f"#{r['id']} {r['title'][:50]}{dur}{url}")
        await reply.reply_text("\n".join(lines))
        return True

    # Treat as YouTube URL
    if "youtube.com" in sub or "youtu.be" in sub:
        await reply.send_typing()
        try:
            video_id = _extract_video_id(sub)
            result = await _analyze_video(video_id, company_id)
            await reply.reply_text(result)
        except ValueError as e:
            await reply.reply_text(str(e))
        return True

    await reply.reply_text(__doc__.strip())
    return True
