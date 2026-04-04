"""
Reddit Digest — AI-curated daily digest from your tracked subreddits.

Commands:
  /reddit                          — Show help
  /reddit run                      — Run digest now
  /reddit track <subreddit> [cat]  — Track a subreddit (category: signal, tech, finance, fun)
  /reddit untrack <subreddit>      — Stop tracking a subreddit
  /reddit list                     — Show tracked subreddits
  /reddit history                  — Show recent digests

Examples:
  /reddit track microsaas signal
  /reddit track AskReddit fun
  /reddit untrack worldnews
  /reddit run
"""

import asyncio
import os
from datetime import datetime

import httpx

from cupbots.helpers.db import get_plugin_db, resolve_plugin_setting
from cupbots.helpers.logger import get_logger

log = get_logger("reddit")

PLUGIN_NAME = "reddit"
USER_AGENT = "CupBotsRedditDigest/1.0 (bot; personal feed reader)"
RATE_LIMIT_DELAY = 2
WA_API_URL = os.environ.get("WA_API_URL", "http://127.0.0.1:3100")

ANALYSIS_PROMPT = """You are a Daily Reddit Digest curator for an indie founder who wants signal without the scroll.

You will receive posts from multiple subreddit categories. Each category has a depth level:

- **signal** categories (business/startup subs): Deep analysis. Extract actionable insights, business ideas, pain points, and opportunities.
- **tech** categories: Moderate analysis. Highlight notable launches, trends, and tools worth knowing about.
- **finance** categories: Moderate analysis. Notable discussions, opportunities, warnings.
- **fun** categories: Brief and light. Just the best/funniest/most interesting stuff in 1-2 sentences each.

# Output Structure

## TL;DR
[3-4 sentences: What's the vibe today? Biggest signal? Anything surprising?]

---

## Signal (Business & Startups)

For each notable insight from signal subreddits:

### [Insight Title]
- **Source:** r/subreddit — u/author
- **What:** [2-3 sentence summary of the post/discussion]
- **So What:** [Why this matters for an indie founder — actionable takeaway]
- **Link:** [reddit URL]

Group related posts into a single insight when they point to the same trend.

---

## Tech

For each notable item from tech subreddits:

- **[Title]** (r/subreddit, [score] upvotes) — [1-2 sentence summary + why it matters]. [Link]

---

## Finance

For each notable item from finance subreddits:

- **[Title]** (r/subreddit, [score] upvotes) — [1-2 sentence summary]. [Link]

---

## Fun & Interesting

Quick hits — the best stuff from fun subreddits:

- **[Title]** (r/subreddit) — [1 sentence, keep it fun]. [Link]

---

## Patterns & Trends

[2-3 sentences: Any recurring themes across subreddits today? What's the community mood?]

---

RULES:
1. Include reddit post URLs for every item so I can dive deeper.
2. For signal posts, focus on ACTIONABLE insights, not just summaries.
3. Skip low-quality, repetitive, or generic motivational posts.
4. Be opinionated — tell me what matters and what's noise.
5. Keep fun section genuinely fun — don't over-analyze it.
6. If a category has no interesting posts, say "Nothing notable today" and move on."""


# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------

def create_tables(conn):
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS subreddits (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            company_id TEXT NOT NULL DEFAULT '',
            name TEXT NOT NULL,
            category TEXT NOT NULL DEFAULT 'general',
            listing TEXT NOT NULL DEFAULT 'hot',
            post_limit INTEGER NOT NULL DEFAULT 25,
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            UNIQUE(company_id, name)
        );

        CREATE TABLE IF NOT EXISTS digests (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            company_id TEXT NOT NULL DEFAULT '',
            post_count INTEGER NOT NULL DEFAULT 0,
            analysis TEXT NOT NULL DEFAULT '',
            published_url TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        );
    """)


def _db():
    return get_plugin_db(PLUGIN_NAME)


# ---------------------------------------------------------------------------
# Reddit API
# ---------------------------------------------------------------------------

async def _fetch_subreddit(client: httpx.AsyncClient, subreddit: str,
                           listing: str = "hot", limit: int = 25) -> list[dict]:
    """Fetch posts from a single subreddit via public JSON API."""
    url = f"https://www.reddit.com/r/{subreddit}/{listing}.json"
    params = {"limit": limit, "raw_json": 1}
    if listing == "top":
        params["t"] = "day"

    try:
        resp = await client.get(url, params=params)
        if resp.status_code == 429:
            await asyncio.sleep(10)
            resp = await client.get(url, params=params)
        if resp.status_code != 200:
            log.warning("Failed r/%s: HTTP %d", subreddit, resp.status_code)
            return []

        data = resp.json()
        posts = []
        for item in data.get("data", {}).get("children", []):
            post = item["data"]
            if post.get("stickied"):
                continue
            posts.append({
                "subreddit": subreddit,
                "title": post["title"],
                "author": post.get("author", "[deleted]"),
                "score": post["score"],
                "num_comments": post["num_comments"],
                "url": f"https://reddit.com{post['permalink']}",
                "selftext": (post.get("selftext") or "")[:500],
            })
        return posts
    except Exception as e:
        log.error("Error fetching r/%s: %s", subreddit, e)
        return []


async def _fetch_all(company_id: str) -> tuple[str, int]:
    """Fetch all tracked subreddits. Returns (markdown_text, post_count)."""
    subs = _db().execute(
        "SELECT * FROM subreddits WHERE company_id = ? ORDER BY category, name",
        (company_id,),
    ).fetchall()

    if not subs:
        return "", 0

    categories: dict[str, list[dict]] = {}
    async with httpx.AsyncClient(timeout=15, headers={"User-Agent": USER_AGENT}) as client:
        for sub in subs:
            posts = await _fetch_subreddit(client, sub["name"], sub["listing"], sub["post_limit"])
            cat = sub["category"]
            categories.setdefault(cat, []).extend(posts)
            await asyncio.sleep(RATE_LIMIT_DELAY)

    # Format as markdown
    lines = []
    total = 0
    for cat, posts in categories.items():
        posts.sort(key=lambda p: p["score"], reverse=True)
        desc = {"signal": "Business & tech signal", "tech": "Tech trends",
                "finance": "Finance", "fun": "Fun stuff"}.get(cat, cat)
        lines.append(f"# {cat}: {desc}\n")
        for p in posts:
            text = p["selftext"].replace("\n", " ").strip()
            if text:
                text = f" — {text[:200]}"
            lines.append(
                f"- [{p['score']} upvotes, {p['num_comments']} comments] r/{p['subreddit']} "
                f"u/{p['author']}: {p['title']}{text} ({p['url']})"
            )
        lines.append("")
        total += len(posts)

    return "\n".join(lines), total


def _extract_tldr(analysis: str) -> str:
    lines = analysis.strip().splitlines()
    in_tldr = False
    summary_lines = []
    for line in lines:
        if "tl;dr" in line.lower() or "tldr" in line.lower():
            in_tldr = True
            continue
        if in_tldr:
            if line.startswith("##") or line.startswith("---"):
                break
            if line.strip():
                summary_lines.append(line.strip())
    return " ".join(summary_lines) if summary_lines else ""


# ---------------------------------------------------------------------------
# Core logic
# ---------------------------------------------------------------------------

async def _run_digest(company_id: str, chat_id: str) -> str:
    """Fetch, analyze, publish. Returns status message."""
    posts_text, post_count = await _fetch_all(company_id)
    if not posts_text:
        return "No tracked subreddits or no posts fetched. Use /reddit track <subreddit> first."

    # Analyze with Claude
    try:
        from cupbots.helpers.llm import ask_llm
        user_msg = f"Reddit Digest\nDate: {datetime.now().strftime('%Y-%m-%d')}\n\nPOSTS:\n{posts_text}"
        analysis = await ask_llm(
            ANALYSIS_PROMPT + "\n\n" + user_msg,
            max_tokens=8000,
        )
    except Exception as e:
        log.error("Analysis failed: %s", e)
        return f"Analysis failed: {e}"

    if not analysis:
        return "Analysis returned empty."

    # Publish to mdpubs
    published_url = ""
    try:
        from cupbots.helpers.telegraph_telegram import publish_to_telegraph
        title = f"Reddit Digest — {datetime.now().strftime('%b %d, %Y')}"
        published_url = publish_to_telegraph(title, analysis, author_name="Reddit Digest")
    except Exception as e:
        log.warning("Publish failed: %s", e)

    # Save digest
    _db().execute(
        "INSERT INTO digests (company_id, post_count, analysis, published_url) VALUES (?, ?, ?, ?)",
        (company_id, post_count, analysis[:10000], published_url),
    )
    _db().commit()

    # Build response
    tldr = _extract_tldr(analysis)
    parts = [f"Reddit Digest — {post_count} posts analyzed"]
    if tldr:
        parts.append(f"\n{tldr[:500]}")
    if published_url:
        parts.append(f"\nFull digest: {published_url}")

    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Command handler
# ---------------------------------------------------------------------------

async def handle_command(msg, reply) -> bool:
    if msg.command != "reddit":
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
        result = await _run_digest(company_id, msg.chat_id)
        await reply.reply_text(result)
        return True

    if sub == "track":
        if len(args) < 2:
            await reply.reply_text("Usage: /reddit track <subreddit> [signal|tech|finance|fun]")
            return True
        name = args[1].lower().lstrip("r/")
        category = args[2].lower() if len(args) > 2 else "general"
        if category not in ("signal", "tech", "finance", "fun", "general"):
            category = "general"
        try:
            _db().execute(
                "INSERT OR REPLACE INTO subreddits (company_id, name, category) VALUES (?, ?, ?)",
                (company_id, name, category),
            )
            _db().commit()
            await reply.reply_text(f"Tracking r/{name} [{category}]")
        except Exception as e:
            await reply.reply_text(f"Failed: {e}")
        return True

    if sub == "untrack":
        if len(args) < 2:
            await reply.reply_text("Usage: /reddit untrack <subreddit>")
            return True
        name = args[1].lower().lstrip("r/")
        deleted = _db().execute(
            "DELETE FROM subreddits WHERE company_id = ? AND name = ?",
            (company_id, name),
        ).rowcount
        _db().commit()
        await reply.reply_text(f"Untracked r/{name}" if deleted else f"r/{name} not found.")
        return True

    if sub == "list":
        rows = _db().execute(
            "SELECT * FROM subreddits WHERE company_id = ? ORDER BY category, name",
            (company_id,),
        ).fetchall()
        if not rows:
            await reply.reply_text("No tracked subreddits. Use /reddit track <subreddit> [category]")
            return True
        lines = ["Tracked subreddits:\n"]
        current_cat = ""
        for r in rows:
            if r["category"] != current_cat:
                current_cat = r["category"]
                lines.append(f"\n[{current_cat}]")
            lines.append(f"  r/{r['name']} ({r['listing']}, limit {r['post_limit']})")
        await reply.reply_text("\n".join(lines))
        return True

    if sub == "history":
        rows = _db().execute(
            "SELECT id, post_count, published_url, created_at FROM digests "
            "WHERE company_id = ? ORDER BY created_at DESC LIMIT 10",
            (company_id,),
        ).fetchall()
        if not rows:
            await reply.reply_text("No digests yet. Run /reddit run")
            return True
        lines = ["Recent digests:\n"]
        for r in rows:
            url = f" — {r['published_url']}" if r["published_url"] else ""
            lines.append(f"#{r['id']} {r['created_at'][:16]} ({r['post_count']} posts){url}")
        await reply.reply_text("\n".join(lines))
        return True

    await reply.reply_text(__doc__.strip())
    return True
