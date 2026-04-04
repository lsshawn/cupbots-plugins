"""
X Digest — AI market intelligence from your X/Twitter list.

Commands:
  /xdigest                         — Show help
  /xdigest run                     — Run digest now
  /xdigest history                 — Show recent digests

Setup: Configure via /config xdigest (requires X list ID and browser cookies).
Schedule: /schedule add "daily 6am" /xdigest run

Examples:
  /xdigest run
  /xdigest history
"""

import json
import os
import re
from datetime import datetime
from pathlib import Path

from cupbots.helpers.db import get_plugin_db, resolve_plugin_setting
from cupbots.helpers.logger import get_logger

log = get_logger("xdigest")

PLUGIN_NAME = "xdigest"
WA_API_URL = os.environ.get("WA_API_URL", "http://127.0.0.1:3100")

ANALYSIS_PROMPT = """You are a Senior Venture Architect and Indie Product Strategist specializing in "Signal Extraction." Your task is to transform raw social data from an X/Twitter list into a Market Opportunity Map that identifies patterns, validated pain points, and distribution levers.

# Objective

Synthesize—don't summarize—the input tweets to reveal market gaps and provide a clear "So What?" for each finding.

# Analysis Framework

Evaluate every insight through these five lenses:

1. **Product Intelligence & Trends:** Identify new product launches, updates, or announcements. Analyze the "job to be done" and market sentiment (Hype vs. Utility). Note which products are getting organic traction vs. paid hype.

2. **Business Ideas:** Identify Problem/Solution pairs from complaints, workarounds, and gaps. Tag each as:
   - "Quick Win" (can ship in weeks, solo founder)
   - "Long Game" (requires sustained effort, team, or capital)

3. **Distribution & Growth Levers:** Name specific growth channels mentioned or implied. How does a solo founder scale this?

4. **Product Friction & Complaints:** What are users/builders complaining about? Categorize as:
   - UX friction / Pricing friction / Missing features / Migration pain

5. **Mental Models & Contrarian Takes:** Surface contrarian beliefs, worldview shifts, or unconventional strategies among successful builders.

# Output Structure

## Executive Summary
[3-4 sentences: (a) overall sentiment, (b) biggest opportunity, (c) key risk founders are ignoring, (d) one contrarian signal worth watching]

---

## Product Intelligence (The Newsroom)

### [Product Name]
- **What:** [1-sentence summary]
- **Job to be Done:** [What problem does this solve?]
- **Trend Signal:** [Isolated feature or industry direction?]
- **Community Sentiment:** [Skeptical / Optimistic / High Utility / Overhyped]
- **Evidence:** [@handle quotes]
- **Indie Angle:** [How can a solo founder leverage or compete?]

---

## Business Ideas & Opportunities

### Idea [N]: [Name]
- **Problem:** [Pain point from tweets]
- **Solution:** [Proposed product/service]
- **Type:** [Quick Win / Long Game]
- **Evidence:** [@handle quotes]
- **Distribution:** [How to get first 100 users]
- **Moat:** [What prevents copying?]

---

## Distribution Playbook

### Channel [N]: [Name]
- **Insight:** [Growth lever sentence]
- **Evidence:** [Tweet references]
- **Playbook:** [Step-by-step for a solo founder]
- **Actionability:** [High / Med / Low]

---

## Product Friction Map

### Friction [N]: [Tool/Product Name]
- **Type:** [UX / Pricing / Missing Feature / Migration]
- **Pain Level:** [Annoying / Blocking / Deal-breaker]
- **Evidence:** [@handle complaints]
- **Opportunity:** [What could you build to fix this?]

---

## Mental Models & Alpha

### Model [N]: [Name]
- **Insight:** [The contrarian or non-obvious belief]
- **Who said it:** [@handle]
- **Why it matters:** [How this changes your strategy]
- **Counter-argument:** [The mainstream view it challenges]

---

## Strategic Blind Spots

- **[Category]:** [Overlooked risk or limitation]
- **Why it matters:** [Potential downside]

---

## Weekly Narrative

[2-3 sentences: What story is the indie/tech community telling itself this week?]

---

STRICT RULES:
1. Every insight MUST cite at least one @handle from the input.
2. Do NOT invent tweets or attribute quotes to handles not in the input.
3. If a section has no supporting evidence, write: "No signal detected in this batch."
4. Prioritize actionability — every insight should have a "So What?" for a solo founder.
5. Be opinionated — weak analysis is worse than wrong analysis.
6. Work through ALL tweets — do not stop at the first 20%."""


# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------

def create_tables(conn):
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS digests (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            company_id TEXT NOT NULL DEFAULT '',
            tweet_count INTEGER NOT NULL DEFAULT 0,
            last_tweet_id TEXT NOT NULL DEFAULT '',
            analysis TEXT NOT NULL DEFAULT '',
            published_url TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        );
    """)


def _db():
    return get_plugin_db(PLUGIN_NAME)


# ---------------------------------------------------------------------------
# X scraping via Playwright
# ---------------------------------------------------------------------------

async def _scrape_list(list_id: str, cookies_json: str, last_tweet_id: str = "") -> list[dict]:
    """Scrape tweets from an X list using Playwright. Returns list of tweet dicts."""
    import asyncio
    from playwright.async_api import async_playwright

    tweets = {}

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(
            storage_state=json.loads(cookies_json),
            user_agent="Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
        )
        page = await context.new_page()

        list_url = f"https://x.com/i/lists/{list_id}"
        log.info("Navigating to %s", list_url)
        await page.goto(list_url, wait_until="domcontentloaded")

        try:
            await page.wait_for_selector('article[data-testid="tweet"]', timeout=15000)
        except Exception:
            if "/login" in page.url or "/i/flow/login" in page.url:
                await browser.close()
                raise RuntimeError("X session expired — update cookies via /config xdigest XDIGEST_COOKIES")
            await browser.close()
            raise RuntimeError(f"No tweets loaded. URL: {page.url}")

        capture_js = """
        () => {
            const results = [];
            for (const x of document.querySelectorAll('article[data-testid="tweet"]')) {
                const k = x.querySelector('a[href*="/status/"]');
                if (!k) continue;
                const id = k.href.split("/").pop();
                const author = x.querySelector('[data-testid="User-Name"] a')?.href?.split("/").pop() || "unknown";
                results.push({
                    id,
                    author,
                    text: x.querySelector('[data-testid="tweetText"]')?.innerText || null,
                    url: k.href
                });
            }
            return results;
        }
        """

        stall_count = 0
        last_height = 0
        found_stop = False

        while not found_stop:
            batch = await page.evaluate(capture_js)
            for tweet in batch:
                tid = tweet["id"]
                if last_tweet_id and tid == last_tweet_id:
                    found_stop = True
                    break
                if tid not in tweets:
                    tweets[tid] = tweet

            if len(tweets) >= 5000:
                break

            await page.evaluate("window.scrollBy(0, 1800)")
            await page.wait_for_timeout(1500)

            curr_height = await page.evaluate("document.body.scrollHeight")
            if curr_height == last_height:
                stall_count += 1
                if stall_count >= 15:
                    break
                await page.evaluate("window.scrollBy(0, -500)")
                await page.wait_for_timeout(500)
            else:
                stall_count = 0
            last_height = curr_height

        await browser.close()

    return list(tweets.values())


def _format_tweets(tweets: list[dict]) -> str:
    lines = []
    for t in tweets:
        text = (t.get("text") or "[No text]").replace("\n", " ")
        lines.append(f"- @{t['author']}: {text} ({t['id']})")
    return "\n".join(lines)


def _extract_summary(analysis: str) -> str:
    lines = analysis.strip().splitlines()
    in_summary = False
    parts = []
    for line in lines:
        if "executive summary" in line.lower():
            in_summary = True
            continue
        if in_summary:
            if line.startswith("##") or line.startswith("---"):
                break
            if line.strip():
                parts.append(line.strip())
    return " ".join(parts) if parts else ""


# ---------------------------------------------------------------------------
# Core logic
# ---------------------------------------------------------------------------

async def _run_digest(company_id: str) -> str:
    list_id = resolve_plugin_setting(PLUGIN_NAME, "XDIGEST_LIST_ID") or ""
    cookies = resolve_plugin_setting(PLUGIN_NAME, "XDIGEST_COOKIES") or ""
    if not list_id or not cookies:
        return ("Configure X digest first:\n"
                "/config xdigest XDIGEST_LIST_ID <list_id>\n"
                "/config xdigest XDIGEST_COOKIES <json>")

    # Get last tweet ID for incremental scraping
    last = _db().execute(
        "SELECT last_tweet_id FROM digests WHERE company_id = ? ORDER BY id DESC LIMIT 1",
        (company_id,),
    ).fetchone()
    last_tweet_id = last["last_tweet_id"] if last else ""

    try:
        tweets = await _scrape_list(list_id, cookies, last_tweet_id)
    except Exception as e:
        return f"Scraping failed: {e}"

    if not tweets:
        return "No new tweets since last scrape."

    tweets_text = _format_tweets(tweets)

    # Analyze
    try:
        from cupbots.helpers.llm import ask_llm
        user_msg = (f"X List Analysis\nDate: {datetime.now().strftime('%Y-%m-%d')}\n"
                    f"Tweet count: {len(tweets)}\n\nTWEETS:\n{tweets_text}")
        analysis = await ask_llm(ANALYSIS_PROMPT + "\n\n" + user_msg, max_tokens=12000)
    except Exception as e:
        return f"Analysis failed: {e}"

    if not analysis:
        return "Analysis returned empty."

    # Publish
    published_url = ""
    try:
        from cupbots.helpers.telegraph_telegram import publish_to_telegraph
        title = f"X List Analysis — {datetime.now().strftime('%b %d, %Y')}"
        published_url = publish_to_telegraph(title, analysis, author_name="X Digest")
    except Exception as e:
        log.warning("Publish failed: %s", e)

    # Save
    newest_id = tweets[0]["id"] if tweets else last_tweet_id
    _db().execute(
        "INSERT INTO digests (company_id, tweet_count, last_tweet_id, analysis, published_url) "
        "VALUES (?, ?, ?, ?, ?)",
        (company_id, len(tweets), newest_id, analysis[:10000], published_url),
    )
    _db().commit()

    summary = _extract_summary(analysis)
    parts = [f"X Digest — {len(tweets)} tweets analyzed"]
    if summary:
        parts.append(f"\n{summary[:500]}")
    if published_url:
        parts.append(f"\nFull analysis: {published_url}")
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Command handler
# ---------------------------------------------------------------------------

async def handle_command(msg, reply) -> bool:
    if msg.command != "xdigest":
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
        await reply.reply_text("Scraping X list... this may take a few minutes.")
        result = await _run_digest(company_id)
        await reply.reply_text(result)
        return True

    if sub == "history":
        rows = _db().execute(
            "SELECT id, tweet_count, published_url, created_at FROM digests "
            "WHERE company_id = ? ORDER BY created_at DESC LIMIT 10",
            (company_id,),
        ).fetchall()
        if not rows:
            await reply.reply_text("No digests yet. Run /xdigest run")
            return True
        lines = ["Recent X digests:\n"]
        for r in rows:
            url = f" — {r['published_url']}" if r["published_url"] else ""
            lines.append(f"#{r['id']} {r['created_at'][:16]} ({r['tweet_count']} tweets){url}")
        await reply.reply_text("\n".join(lines))
        return True

    await reply.reply_text(__doc__.strip())
    return True
