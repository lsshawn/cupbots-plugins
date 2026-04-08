"""
SEO — Agentic SEO monitoring, keyword tracking, and content generation.

Commands:
  /seo connect <domain>              — Register site with GA4 (starts OAuth)
  /seo connect <domain> --umami      — Register site with Umami backend
  /seo sites                         — List registered sites
  /seo status [domain]               — Quick health summary
  /seo report [domain]               — Full weekly intelligence report
  /seo keywords [domain]             — Keyword rankings + suggestions
  /seo decay [domain]                — Flagged decaying pages
  /seo draft <topic> [--site domain] — Generate SEO blog draft
  /seo pull [domain]                 — Manual data pull (analytics + keywords + decay)
  /seo autosend on|off [domain]      — Toggle automatic weekly reports

Sites are configured in config.yaml under plugin_settings.seo.sites.

Examples:
  /seo connect example.com
  /seo connect example.com --umami
  /seo report
  /seo keywords example.com
  /seo draft "best CRM for agencies" --site example.com
  /seo pull
  /seo autosend on
"""

import json
import secrets
from base64 import b64encode
from collections import namedtuple
from datetime import datetime, timedelta

import httpx

from cupbots.config import get_config, update_config_key
from cupbots.helpers.db import get_plugin_db, resolve_plugin_setting
from cupbots.helpers.jobs import register_handler
from cupbots.helpers.llm import ask_llm
from cupbots.helpers.logger import get_logger
from cupbots.helpers.oauth import register_provider, start_flow

log = get_logger("seo")

PLUGIN_NAME = "seo"

# ---------------------------------------------------------------------------
# Database — runtime data only (snapshots, keywords, scores, drafts)
# Site config lives in config.yaml
# ---------------------------------------------------------------------------

def create_tables(conn):
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS analytics_snapshots (
            id TEXT PRIMARY KEY,
            domain TEXT NOT NULL,
            week_start TEXT NOT NULL,
            total_sessions INTEGER DEFAULT 0,
            total_pageviews INTEGER DEFAULT 0,
            total_conversions INTEGER DEFAULT 0,
            top_pages TEXT DEFAULT '[]',
            traffic_sources TEXT DEFAULT '[]',
            anomalies TEXT DEFAULT '[]',
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        );
        CREATE INDEX IF NOT EXISTS idx_snap_domain ON analytics_snapshots (domain);

        CREATE TABLE IF NOT EXISTS keywords (
            id TEXT PRIMARY KEY,
            domain TEXT NOT NULL,
            keyword TEXT NOT NULL,
            position INTEGER,
            previous_position INTEGER,
            search_volume INTEGER DEFAULT 0,
            difficulty INTEGER DEFAULT 0,
            url TEXT,
            source TEXT DEFAULT 'manual',
            checked_at TEXT,
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        );
        CREATE INDEX IF NOT EXISTS idx_kw_domain ON keywords (domain);

        CREATE TABLE IF NOT EXISTS content_scores (
            id TEXT PRIMARY KEY,
            domain TEXT NOT NULL,
            path TEXT NOT NULL,
            current_sessions INTEGER DEFAULT 0,
            previous_sessions INTEGER DEFAULT 0,
            four_week_avg INTEGER DEFAULT 0,
            decay_pct REAL DEFAULT 0,
            flagged INTEGER DEFAULT 0,
            last_checked TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_cs_domain ON content_scores (domain);

        CREATE TABLE IF NOT EXISTS drafts (
            id TEXT PRIMARY KEY,
            domain TEXT NOT NULL,
            topic TEXT NOT NULL,
            target_keywords TEXT DEFAULT '[]',
            status TEXT NOT NULL DEFAULT 'pending',
            outline TEXT,
            content TEXT,
            seo_score INTEGER DEFAULT 0,
            requested_by TEXT,
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            completed_at TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_draft_domain ON drafts (domain);
    """)


def _db():
    return get_plugin_db(PLUGIN_NAME)


def _new_id():
    return secrets.token_urlsafe(12)


# ---------------------------------------------------------------------------
# Config-driven site management
# ---------------------------------------------------------------------------

def _get_sites() -> list[dict]:
    """Get all sites from config.yaml plugin_settings.seo.sites."""
    cfg = get_config().get("plugin_settings", {}) or {}
    seo = cfg.get("seo", {}) or {}
    return seo.get("sites", []) or []


def _get_site(domain: str) -> dict | None:
    """Get a single site config by domain."""
    for s in _get_sites():
        if s.get("domain") == domain:
            return s
    return None


def _resolve_site(args: list[str]) -> tuple[dict | None, str | None]:
    """Resolve a site from args or default to the only site.

    Returns (site_dict, error_msg). One will be None.
    """
    sites = _get_sites()

    # Check if a domain was provided
    domain = None
    for a in args:
        if "." in a and not a.startswith("-"):
            domain = a
            break

    if domain:
        site = _get_site(domain)
        if not site:
            return None, f"Site '{domain}' not found. Run `/seo sites` to see configured sites."
        return site, None

    if not sites:
        return None, "No sites configured. Run `/seo connect <domain>` or add to config.yaml."
    if len(sites) == 1:
        return sites[0], None
    domains = ", ".join(s["domain"] for s in sites)
    return None, f"Multiple sites configured. Specify one: {domains}"


def _site_index(domain: str) -> int | None:
    """Find the index of a site in the config list."""
    for i, s in enumerate(_get_sites()):
        if s.get("domain") == domain:
            return i
    return None


# ---------------------------------------------------------------------------
# Analytics backend abstraction
# ---------------------------------------------------------------------------

AnalyticsReport = namedtuple("AnalyticsReport", [
    "total_sessions", "total_pageviews", "total_conversions",
    "top_pages", "traffic_sources",
])


class GA4Backend:
    """Google Analytics 4 via OAuth + Data API v1beta."""

    def __init__(self, site: dict):
        self.domain = site["domain"]
        self.property_id = site.get("ga4_property_id")
        self.tokens = site.get("ga4_tokens", {}) or {}

    async def pull_data(self) -> AnalyticsReport:
        from google.analytics.data_v1beta import BetaAnalyticsDataClient
        from google.analytics.data_v1beta.types import (
            DateRange, Dimension, Metric, OrderBy, RunReportRequest,
        )
        from google.oauth2.credentials import Credentials

        creds = Credentials(
            token=self.tokens.get("access_token"),
            refresh_token=self.tokens.get("refresh_token"),
            token_uri="https://oauth2.googleapis.com/token",
            client_id=resolve_plugin_setting("seo", "google_client_id"),
            client_secret=resolve_plugin_setting("seo", "google_client_secret"),
        )

        client = BetaAnalyticsDataClient(credentials=creds)

        # Top pages by sessions
        page_req = RunReportRequest(
            property=self.property_id,
            date_ranges=[DateRange(start_date="7daysAgo", end_date="today")],
            dimensions=[Dimension(name="landingPagePlusQueryString")],
            metrics=[
                Metric(name="sessions"),
                Metric(name="conversions"),
                Metric(name="bounceRate"),
            ],
            limit=50,
            order_bys=[OrderBy(metric=OrderBy.MetricOrderBy(metric_name="sessions"), desc=True)],
        )
        page_resp = client.run_report(page_req)

        top_pages = []
        for row in page_resp.rows:
            top_pages.append({
                "path": row.dimension_values[0].value,
                "sessions": int(row.metric_values[0].value),
                "conversions": int(row.metric_values[1].value),
                "bounce_rate": float(row.metric_values[2].value),
            })

        # Traffic sources
        src_req = RunReportRequest(
            property=self.property_id,
            date_ranges=[DateRange(start_date="7daysAgo", end_date="today")],
            dimensions=[Dimension(name="sessionDefaultChannelGroup")],
            metrics=[Metric(name="sessions"), Metric(name="conversions")],
            limit=10,
            order_bys=[OrderBy(metric=OrderBy.MetricOrderBy(metric_name="sessions"), desc=True)],
        )
        src_resp = client.run_report(src_req)

        traffic_sources = []
        for row in src_resp.rows:
            traffic_sources.append({
                "source": row.dimension_values[0].value,
                "sessions": int(row.metric_values[0].value),
                "conversions": int(row.metric_values[1].value),
            })

        total_sessions = sum(p["sessions"] for p in top_pages)
        total_conversions = sum(p["conversions"] for p in top_pages)

        # Persist refreshed credentials back to config.yaml
        if creds.token != self.tokens.get("access_token"):
            idx = _site_index(self.domain)
            if idx is not None:
                new_tokens = {
                    "access_token": creds.token,
                    "refresh_token": creds.refresh_token,
                    "expiry": creds.expiry.isoformat() if creds.expiry else None,
                }
                update_config_key(f"plugin_settings.seo.sites.{idx}.ga4_tokens", new_tokens)

        return AnalyticsReport(
            total_sessions=total_sessions,
            total_pageviews=0,
            total_conversions=total_conversions,
            top_pages=top_pages,
            traffic_sources=traffic_sources,
        )


class UmamiBackend:
    """Umami self-hosted analytics via REST API."""

    def __init__(self, site: dict):
        self.domain = site["domain"]
        self.website_id = site.get("umami_website_id")
        self.api_url = (site.get("umami_api_url") or "").rstrip("/")
        self.api_token = site.get("umami_api_token")

    async def pull_data(self) -> AnalyticsReport:
        headers = {"Authorization": f"Bearer {self.api_token}"}
        now = datetime.now()
        start = int((now - timedelta(days=7)).timestamp() * 1000)
        end = int(now.timestamp() * 1000)

        async with httpx.AsyncClient(timeout=15) as client:
            stats_resp = await client.get(
                f"{self.api_url}/api/websites/{self.website_id}/stats",
                params={"startAt": start, "endAt": end},
                headers=headers,
            )
            stats_resp.raise_for_status()
            stats = stats_resp.json()

            pages_resp = await client.get(
                f"{self.api_url}/api/websites/{self.website_id}/metrics",
                params={"startAt": start, "endAt": end, "type": "url"},
                headers=headers,
            )
            pages_resp.raise_for_status()
            pages = pages_resp.json()

            ref_resp = await client.get(
                f"{self.api_url}/api/websites/{self.website_id}/metrics",
                params={"startAt": start, "endAt": end, "type": "referrer"},
                headers=headers,
            )
            ref_resp.raise_for_status()
            refs = ref_resp.json()

        top_pages = [
            {"path": p.get("x", "/"), "sessions": p.get("y", 0), "conversions": 0, "bounce_rate": 0}
            for p in pages[:50]
        ]
        traffic_sources = [
            {"source": r.get("x", "direct"), "sessions": r.get("y", 0), "conversions": 0}
            for r in refs[:10]
        ]

        return AnalyticsReport(
            total_sessions=stats.get("sessions", {}).get("value", 0) if isinstance(stats.get("sessions"), dict) else stats.get("sessions", 0),
            total_pageviews=stats.get("pageviews", {}).get("value", 0) if isinstance(stats.get("pageviews"), dict) else stats.get("pageviews", 0),
            total_conversions=0,
            top_pages=top_pages,
            traffic_sources=traffic_sources,
        )


def _get_backend(site: dict):
    if site.get("backend") == "umami":
        return UmamiBackend(site)
    return GA4Backend(site)


# ---------------------------------------------------------------------------
# DataForSEO client
# ---------------------------------------------------------------------------

DATAFORSEO_API = "https://api.dataforseo.com/v3"


def _dataforseo_auth() -> str:
    login = resolve_plugin_setting("seo", "dataforseo_login") or ""
    password = resolve_plugin_setting("seo", "dataforseo_password") or ""
    return "Basic " + b64encode(f"{login}:{password}".encode()).decode()


async def _dataforseo_post(endpoint: str, body: list) -> dict:
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            f"{DATAFORSEO_API}{endpoint}",
            headers={"Authorization": _dataforseo_auth(), "Content-Type": "application/json"},
            json=body,
        )
        resp.raise_for_status()
        return resp.json()


async def suggest_keywords(
    domain: str, location_code: int = 2840, language_code: str = "en",
) -> list[dict]:
    """Get keyword suggestions for a domain via DataForSEO."""
    data = await _dataforseo_post("/keywords_data/google_ads/keywords_for_site/live", [{
        "target": domain,
        "location_code": location_code,
        "language_code": language_code,
        "include_adult_keywords": False,
    }])
    results = data.get("tasks", [{}])[0].get("result", []) or []
    return [
        {
            "keyword": r["keyword"],
            "search_volume": r.get("search_volume", 0),
            "difficulty": (r.get("keyword_info") or {}).get("keyword_difficulty", 0),
            "competition": r.get("competition", 0),
        }
        for r in results
        if r.get("search_volume", 0) > 100
    ][:50]


async def check_ranking(
    keyword: str, domain: str, location_code: int = 2840, language_code: str = "en",
) -> dict:
    """Check SERP ranking for a keyword+domain via DataForSEO."""
    data = await _dataforseo_post("/serp/google/organic/live/regular", [{
        "keyword": keyword,
        "location_code": location_code,
        "language_code": language_code,
        "depth": 100,
    }])
    items = data.get("tasks", [{}])[0].get("result", [{}])[0].get("items", []) or []
    match = next(
        (i for i in items if i.get("type") == "organic" and domain in (i.get("domain") or "")),
        None,
    )
    return {
        "keyword": keyword,
        "position": match["rank_absolute"] if match else None,
        "url": match.get("url") if match else None,
    }


# ---------------------------------------------------------------------------
# Content decay detection
# ---------------------------------------------------------------------------

def _run_decay_scan(domain: str):
    """Scan a site's snapshots for content decay (>25% drop vs 4-week avg)."""
    conn = _db()
    snapshots = conn.execute(
        "SELECT * FROM analytics_snapshots WHERE domain = ? ORDER BY created_at DESC LIMIT 5",
        (domain,),
    ).fetchall()

    if len(snapshots) < 2:
        return 0

    current_pages = json.loads(snapshots[0]["top_pages"] or "[]")
    older = snapshots[1:5]

    page_avgs: dict[str, list[int]] = {}
    for snap in older:
        for p in json.loads(snap["top_pages"] or "[]"):
            page_avgs.setdefault(p["path"], []).append(p.get("sessions", 0))

    now = datetime.now().isoformat()
    flagged_count = 0

    for page in current_pages:
        hist = page_avgs.get(page["path"])
        if not hist:
            continue
        avg = round(sum(hist) / len(hist))
        if avg == 0:
            continue

        decay_pct = round(((avg - page["sessions"]) / avg) * 100, 1)
        flagged = 1 if decay_pct > 25 else 0
        if flagged:
            flagged_count += 1

        existing = conn.execute(
            "SELECT id FROM content_scores WHERE domain = ? AND path = ?",
            (domain, page["path"]),
        ).fetchone()

        if existing:
            conn.execute(
                "UPDATE content_scores SET current_sessions=?, previous_sessions=?, "
                "four_week_avg=?, decay_pct=?, flagged=?, last_checked=? WHERE id=?",
                (page["sessions"], hist[0] if hist else 0, avg, decay_pct, flagged, now, existing["id"]),
            )
        else:
            conn.execute(
                "INSERT INTO content_scores (id, domain, path, current_sessions, previous_sessions, "
                "four_week_avg, decay_pct, flagged, last_checked) VALUES (?,?,?,?,?,?,?,?,?)",
                (_new_id(), domain, page["path"], page["sessions"],
                 hist[0] if hist else 0, avg, decay_pct, flagged, now),
            )

    conn.commit()
    return flagged_count


# ---------------------------------------------------------------------------
# Blog draft generation
# ---------------------------------------------------------------------------

async def _generate_draft(draft_id: str):
    """Generate a blog draft (two-stage: outline then content)."""
    conn = _db()
    draft = conn.execute("SELECT * FROM drafts WHERE id = ?", (draft_id,)).fetchone()
    if not draft or draft["status"] != "pending":
        return

    conn.execute("UPDATE drafts SET status = 'generating' WHERE id = ?", (draft_id,))
    conn.commit()

    try:
        keywords = json.loads(draft["target_keywords"] or "[]")
        primary = keywords[0] if keywords else draft["topic"]
        secondary = keywords[1:6] if len(keywords) > 1 else []

        outline_prompt = f"""Create an outline for a blog post about: "{draft['topic']}"

Primary keyword: {primary}
Secondary keywords: {', '.join(secondary) if secondary else 'none'}

Requirements:
- H2/H3 structure optimized for featured snippets
- Include an FAQ section with 3-5 questions (for AI overview optimization)
- Target 1500-2500 words
- Each section should note the target keyword to include

Return the outline in markdown format."""

        outline = await ask_llm(outline_prompt, system="You are an SEO content strategist. Create a detailed blog post outline.", max_tokens=1024) or ""

        draft_prompt = f"""Write a complete blog post following this outline:

{outline}

Primary keyword: {primary}
Secondary keywords: {', '.join(secondary) if secondary else 'none'}

Requirements:
- SEO title (with primary keyword near the start) + meta description (150-160 chars)
- Short paragraphs (2-3 sentences max)
- Clear H2/H3 headers
- Include FAQ section with schema-friendly Q&A format
- Internal linking suggestions marked as [LINK: suggested anchor text -> /suggested-path]
- 1500-2500 words

Format as markdown with the title as H1."""

        content = await ask_llm(draft_prompt, system="You are an SEO content writer. Write for humans first, search engines second. Write in a professional, approachable voice.", max_tokens=4096) or ""

        # SEO scoring
        lower = content.lower()
        primary_count = lower.count(primary.lower())
        has_h2 = "## " in content
        has_faq = "faq" in lower or "frequently asked" in lower
        word_count = len(content.split())

        score = 0
        if primary_count >= 3:
            score += 25
        if primary_count >= 6:
            score += 10
        if has_h2:
            score += 20
        if has_faq:
            score += 20
        if 1500 <= word_count <= 2500:
            score += 25
        elif word_count >= 1000:
            score += 15
        score = min(score, 100)

        conn.execute(
            "UPDATE drafts SET status='done', outline=?, content=?, seo_score=?, completed_at=? WHERE id=?",
            (outline, content, score, datetime.now().isoformat(), draft_id),
        )
        conn.commit()
        log.info("Draft %s completed (score=%d, words=%d)", draft_id, score, word_count)

    except Exception as e:
        log.error("Draft generation failed for %s: %s", draft_id, e, exc_info=True)
        conn.execute("UPDATE drafts SET status='failed' WHERE id=?", (draft_id,))
        conn.commit()


# ---------------------------------------------------------------------------
# OAuth callback
# ---------------------------------------------------------------------------

async def _on_ga4_connected(tokens: dict, metadata: dict):
    """Called by oauth.py when GA4 OAuth completes."""
    domain = metadata.get("domain")
    if not domain:
        log.error("OAuth callback missing domain in metadata")
        return

    idx = _site_index(domain)
    if idx is None:
        log.error("OAuth callback: site %s not found in config", domain)
        return

    token_data = {
        "access_token": tokens.get("access_token"),
        "refresh_token": tokens.get("refresh_token"),
        "expiry": tokens.get("expires_in"),
    }
    update_config_key(f"plugin_settings.seo.sites.{idx}.ga4_tokens", token_data)

    chat_id = metadata.get("chat_id")
    if chat_id:
        from cupbots.helpers.channel import WhatsAppReplyContext
        reply = WhatsAppReplyContext(chat_id)
        await reply.reply_text(
            f"GA4 connected for *{domain}*!\n\n"
            f"Now set the GA4 property ID:\n"
            f"`/seo connect {domain} --property properties/XXXXXXXX`\n\n"
            "Find your property ID in GA4 Admin > Property Settings."
        )

    log.info("GA4 OAuth completed for %s", domain)


# Register Google GA4 as OAuth provider
register_provider("google_ga4", {
    "auth_url": "https://accounts.google.com/o/oauth2/v2/auth",
    "token_url": "https://oauth2.googleapis.com/token",
    "default_scopes": ["https://www.googleapis.com/auth/analytics.readonly"],
    "on_success": _on_ga4_connected,
})


# ---------------------------------------------------------------------------
# Job handlers
# ---------------------------------------------------------------------------

async def _job_analytics_pull(payload: dict):
    """Scheduled job: pull analytics for all configured sites."""
    for site in _get_sites():
        try:
            backend = _get_backend(site)
            report = await backend.pull_data()
            week_start = (datetime.now() - timedelta(days=datetime.now().weekday())).strftime("%Y-%m-%d")
            conn = _db()
            conn.execute(
                "INSERT INTO analytics_snapshots (id, domain, week_start, total_sessions, "
                "total_pageviews, total_conversions, top_pages, traffic_sources) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (_new_id(), site["domain"], week_start, report.total_sessions,
                 report.total_pageviews, report.total_conversions,
                 json.dumps(report.top_pages), json.dumps(report.traffic_sources)),
            )
            conn.commit()
            log.info("Analytics snapshot saved for %s", site["domain"])
        except Exception as e:
            log.error("Analytics pull failed for %s: %s", site["domain"], e, exc_info=True)


async def _job_keyword_check(payload: dict):
    """Scheduled job: check SERP rankings for tracked keywords."""
    conn = _db()
    for site in _get_sites():
        kws = conn.execute("SELECT * FROM keywords WHERE domain = ?", (site["domain"],)).fetchall()
        for kw in kws:
            try:
                result = await check_ranking(kw["keyword"], site["domain"])
                conn.execute(
                    "UPDATE keywords SET previous_position=position, position=?, url=?, checked_at=? WHERE id=?",
                    (result["position"], result["url"], datetime.now().isoformat(), kw["id"]),
                )
                conn.commit()
            except Exception as e:
                log.error("Keyword check failed for '%s': %s", kw["keyword"], e)


async def _job_decay_scan(payload: dict):
    """Scheduled job: scan all sites for content decay."""
    total_flagged = 0
    for site in _get_sites():
        total_flagged += _run_decay_scan(site["domain"])
    if total_flagged:
        log.info("Decay scan found %d flagged pages", total_flagged)


async def _job_draft_process(payload: dict):
    """Scheduled job: process pending blog drafts."""
    conn = _db()
    pending = conn.execute(
        "SELECT id FROM drafts WHERE status = 'pending' ORDER BY created_at LIMIT 1",
    ).fetchone()
    if pending:
        await _generate_draft(pending["id"])


async def _job_weekly_report(payload: dict):
    """Scheduled job: send weekly reports for sites with autosend + notify_chat."""
    from cupbots.helpers.channel import WhatsAppReplyContext
    for site in _get_sites():
        if not site.get("autosend"):
            continue
        notify_chat = site.get("notify_chat")
        if not notify_chat:
            continue
        report_text = _format_report(site)
        if report_text:
            reply = WhatsAppReplyContext(notify_chat)
            await reply.reply_text(report_text)


register_handler("seo_analytics_pull", _job_analytics_pull)
register_handler("seo_keyword_check", _job_keyword_check)
register_handler("seo_decay_scan", _job_decay_scan)
register_handler("seo_draft_process", _job_draft_process)
register_handler("seo_weekly_report", _job_weekly_report)


# ---------------------------------------------------------------------------
# Report formatting
# ---------------------------------------------------------------------------

def _format_report(site: dict) -> str:
    """Format a full weekly report for a site."""
    conn = _db()
    domain = site["domain"]

    snap = conn.execute(
        "SELECT * FROM analytics_snapshots WHERE domain = ? ORDER BY created_at DESC LIMIT 1",
        (domain,),
    ).fetchone()

    prev_snap = conn.execute(
        "SELECT * FROM analytics_snapshots WHERE domain = ? ORDER BY created_at DESC LIMIT 1 OFFSET 1",
        (domain,),
    ).fetchone()

    kws = conn.execute(
        "SELECT * FROM keywords WHERE domain = ? ORDER BY position NULLS LAST LIMIT 10",
        (domain,),
    ).fetchall()

    decayed = conn.execute(
        "SELECT * FROM content_scores WHERE domain = ? AND flagged = 1 ORDER BY decay_pct DESC LIMIT 5",
        (domain,),
    ).fetchall()

    lines = [f"*SEO Report: {domain}*", f"_{datetime.now().strftime('%Y-%m-%d')}_", ""]

    if snap:
        sessions = snap["total_sessions"]
        conversions = snap["total_conversions"]
        lines.append(f"*Traffic (7d):* {sessions:,} sessions, {conversions:,} conversions")

        if prev_snap:
            prev_sessions = prev_snap["total_sessions"]
            if prev_sessions:
                change = ((sessions - prev_sessions) / prev_sessions) * 100
                arrow = "+" if change >= 0 else ""
                lines.append(f"*vs last week:* {arrow}{change:.1f}%")

        top = json.loads(snap["top_pages"] or "[]")[:5]
        if top:
            lines.append("")
            lines.append("*Top Pages:*")
            for i, p in enumerate(top, 1):
                lines.append(f"  {i}. {p['path']} — {p['sessions']:,} sessions")

        sources = json.loads(snap["traffic_sources"] or "[]")[:5]
        if sources:
            lines.append("")
            lines.append("*Traffic Sources:*")
            for s in sources:
                lines.append(f"  • {s['source']}: {s['sessions']:,}")
    else:
        lines.append("_No analytics data yet. Run `/seo pull` to fetch._")

    if kws:
        lines.append("")
        lines.append("*Keyword Rankings:*")
        for kw in kws:
            pos = kw["position"]
            prev = kw["previous_position"]
            pos_str = f"#{pos}" if pos else "—"
            delta = ""
            if pos and prev:
                d = prev - pos
                if d > 0:
                    delta = f" (+{d})"
                elif d < 0:
                    delta = f" ({d})"
            lines.append(f"  • {kw['keyword']}: {pos_str}{delta} (vol: {kw['search_volume']:,})")

    if decayed:
        lines.append("")
        lines.append("*Decaying Pages:*")
        for d in decayed:
            lines.append(f"  • {d['path']} — down {d['decay_pct']:.0f}% ({d['current_sessions']} vs avg {d['four_week_avg']})")

    return "\n".join(lines)


def _format_status(site: dict) -> str:
    """Format a quick status summary for a site."""
    conn = _db()
    domain = site["domain"]
    backend = site.get("backend", "ga4").upper()

    snap = conn.execute(
        "SELECT * FROM analytics_snapshots WHERE domain = ? ORDER BY created_at DESC LIMIT 1",
        (domain,),
    ).fetchone()

    kw_count = conn.execute(
        "SELECT COUNT(*) as c FROM keywords WHERE domain = ?", (domain,),
    ).fetchone()["c"]

    flagged_count = conn.execute(
        "SELECT COUNT(*) as c FROM content_scores WHERE domain = ? AND flagged = 1",
        (domain,),
    ).fetchone()["c"]

    pending_drafts = conn.execute(
        "SELECT COUNT(*) as c FROM drafts WHERE domain = ? AND status IN ('pending', 'generating')",
        (domain,),
    ).fetchone()["c"]

    connected = bool(site.get("ga4_tokens") or site.get("umami_api_token"))

    lines = [f"*{domain}* ({backend})"]
    lines.append(f"Connected: {'yes' if connected else 'no'}")

    if snap:
        lines.append(f"Last pull: {snap['created_at'][:10]}")
        lines.append(f"Sessions (7d): {snap['total_sessions']:,}")
    else:
        lines.append("Last pull: never")

    lines.append(f"Tracked keywords: {kw_count}")
    lines.append(f"Decaying pages: {flagged_count}")
    if pending_drafts:
        lines.append(f"Pending drafts: {pending_drafts}")
    if site.get("notify_chat"):
        lines.append(f"Reports to: {site['notify_chat']}")
    lines.append(f"Autosend: {'on' if site.get('autosend') else 'off'}")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Command handler
# ---------------------------------------------------------------------------

async def handle_command(msg, reply) -> bool:
    if msg.command != "seo":
        return False

    args = msg.args
    if not args or args[0] == "--help":
        await reply.reply_text(__doc__.strip())
        return True

    sub = args[0].lower()
    rest = args[1:]

    if sub == "connect":
        return await _cmd_connect(rest, msg, reply)
    elif sub == "sites":
        return await _cmd_sites(msg, reply)
    elif sub == "status":
        return await _cmd_status(rest, msg, reply)
    elif sub == "report":
        return await _cmd_report(rest, msg, reply)
    elif sub == "keywords":
        return await _cmd_keywords(rest, msg, reply)
    elif sub == "decay":
        return await _cmd_decay(rest, msg, reply)
    elif sub == "draft":
        return await _cmd_draft(rest, msg, reply)
    elif sub == "pull":
        return await _cmd_pull(rest, msg, reply)
    elif sub == "autosend":
        return await _cmd_autosend(rest, msg, reply)
    else:
        await reply.reply_text(f"Unknown subcommand: {sub}\n\nRun `/seo --help` for usage.")
        return True


# ---------------------------------------------------------------------------
# Subcommand implementations
# ---------------------------------------------------------------------------

async def _cmd_connect(args, msg, reply) -> bool:
    """Register a site and connect analytics."""
    if not args or args[0] == "--help":
        await reply.reply_text(
            "*Usage:* `/seo connect <domain> [options]`\n\n"
            "*Options:*\n"
            "  `--umami`  — Use Umami analytics backend\n"
            "  `--property <id>` — Set GA4 property ID (e.g. properties/123456)\n"
            "  `--notify <jid>` — Set WhatsApp JID for reports\n\n"
            "*Examples:*\n"
            "  `/seo connect example.com` — Register with GA4\n"
            "  `/seo connect example.com --umami` — Register with Umami\n"
            "  `/seo connect example.com --property properties/123456`"
        )
        return True

    domain = args[0].lower().replace("https://", "").replace("http://", "").strip("/")
    is_umami = "--umami" in args
    property_id = None
    notify_chat = None

    if "--property" in args:
        idx = args.index("--property")
        if idx + 1 < len(args):
            property_id = args[idx + 1]

    if "--notify" in args:
        idx = args.index("--notify")
        if idx + 1 < len(args):
            notify_chat = args[idx + 1]

    existing = _get_site(domain)
    site_idx = _site_index(domain)

    if existing and property_id:
        update_config_key(f"plugin_settings.seo.sites.{site_idx}.ga4_property_id", property_id)
        await reply.reply_text(f"GA4 property ID updated for *{domain}*: `{property_id}`")
        return True

    if existing and notify_chat:
        update_config_key(f"plugin_settings.seo.sites.{site_idx}.notify_chat", notify_chat)
        await reply.reply_text(f"Notify chat updated for *{domain}*: `{notify_chat}`")
        return True

    if existing and not is_umami:
        # Site exists — maybe re-trigger OAuth
        if existing.get("backend", "ga4") == "ga4" and not existing.get("ga4_tokens"):
            client_id = resolve_plugin_setting("seo", "google_client_id")
            client_secret = resolve_plugin_setting("seo", "google_client_secret")
            if not client_id or not client_secret:
                await reply.reply_text("Missing Google OAuth credentials. Set `google_client_id` and `google_client_secret` in plugin_settings.seo in config.yaml.")
                return True
            await start_flow(
                provider="google_ga4",
                client_id=client_id,
                client_secret=client_secret,
                scopes=None,
                metadata={"domain": domain, "chat_id": msg.chat_id},
                reply=reply,
                extra_params={"access_type": "offline", "prompt": "consent"},
            )
            return True
        await reply.reply_text(f"Site *{domain}* already configured ({existing.get('backend', 'ga4')}).")
        return True

    # Register new site — append to config.yaml sites list
    new_site = {"domain": domain, "backend": "umami" if is_umami else "ga4"}
    if notify_chat:
        new_site["notify_chat"] = notify_chat

    sites = _get_sites()
    sites.append(new_site)
    update_config_key("plugin_settings.seo.sites", sites)

    if is_umami:
        site_idx = len(sites) - 1
        await reply.reply_text(
            f"Site *{domain}* added with Umami backend.\n\n"
            "Configure the connection in config.yaml under `plugin_settings.seo.sites`:\n"
            "  `umami_api_url`, `umami_website_id`, `umami_api_token`"
        )
    else:
        client_id = resolve_plugin_setting("seo", "google_client_id")
        client_secret = resolve_plugin_setting("seo", "google_client_secret")
        if not client_id or not client_secret:
            await reply.reply_text(
                f"Site *{domain}* added, but Google OAuth is not configured.\n"
                "Set `google_client_id` and `google_client_secret` in plugin_settings.seo in config.yaml."
            )
            return True

        await start_flow(
            provider="google_ga4",
            client_id=client_id,
            client_secret=client_secret,
            scopes=None,
            metadata={"domain": domain, "chat_id": msg.chat_id},
            reply=reply,
            extra_params={"access_type": "offline", "prompt": "consent"},
        )

    return True


async def _cmd_sites(msg, reply) -> bool:
    """List registered sites."""
    sites = _get_sites()

    if not sites:
        await reply.reply_text("No sites configured. Run `/seo connect <domain>` to add one.")
        return True

    lines = ["*Configured Sites:*", ""]
    for s in sites:
        backend = s.get("backend", "ga4").upper()
        connected = bool(s.get("ga4_tokens") or s.get("umami_api_token"))
        status = "connected" if connected else "not connected"
        notify = f" → {s['notify_chat']}" if s.get("notify_chat") else ""
        lines.append(f"• *{s['domain']}* ({backend}) — {status}{notify}")

    await reply.reply_text("\n".join(lines))
    return True


async def _cmd_status(args, msg, reply) -> bool:
    """Quick health summary for a site."""
    if args and args[0] == "--help":
        await reply.reply_text("*Usage:* `/seo status [domain]`\nShows quick health summary for a site.")
        return True

    site, err = _resolve_site(args)
    if err:
        await reply.reply_text(err)
        return True

    await reply.reply_text(_format_status(site))
    return True


async def _cmd_report(args, msg, reply) -> bool:
    """Full weekly report."""
    if args and args[0] == "--help":
        await reply.reply_text("*Usage:* `/seo report [domain]`\nShows full weekly intelligence report.")
        return True

    site, err = _resolve_site(args)
    if err:
        await reply.reply_text(err)
        return True

    report = _format_report(site)
    await reply.reply_text(report)
    return True


async def _cmd_keywords(args, msg, reply) -> bool:
    """Show keyword rankings and optionally suggest new ones."""
    if args and args[0] == "--help":
        await reply.reply_text(
            "*Usage:* `/seo keywords [domain]`\n"
            "Shows tracked keyword rankings.\n\n"
            "Add `suggest` to discover new keywords:\n"
            "  `/seo keywords suggest example.com`"
        )
        return True

    do_suggest = "suggest" in [a.lower() for a in args]
    filtered_args = [a for a in args if a.lower() != "suggest"]

    site, err = _resolve_site(filtered_args)
    if err:
        await reply.reply_text(err)
        return True

    domain = site["domain"]
    conn = _db()

    if do_suggest:
        if not resolve_plugin_setting("seo", "dataforseo_login"):
            await reply.reply_text("DataForSEO not configured. Set `dataforseo_login` and `dataforseo_password` in plugin_settings.seo.")
            return True

        await reply.reply_text(f"Fetching keyword suggestions for {domain}...")
        try:
            suggestions = await suggest_keywords(domain)
            if not suggestions:
                await reply.reply_text("No keyword suggestions found.")
                return True

            for s in suggestions[:20]:
                existing = conn.execute(
                    "SELECT id FROM keywords WHERE domain = ? AND keyword = ?",
                    (domain, s["keyword"]),
                ).fetchone()
                if not existing:
                    conn.execute(
                        "INSERT INTO keywords (id, domain, keyword, search_volume, difficulty, source) "
                        "VALUES (?,?,?,?,?,?)",
                        (_new_id(), domain, s["keyword"], s["search_volume"],
                         s["difficulty"], "dataforseo_suggest"),
                    )
            conn.commit()

            lines = [f"*Keyword Suggestions for {domain}:*", ""]
            for s in suggestions[:15]:
                lines.append(f"  • {s['keyword']} (vol: {s['search_volume']:,}, diff: {s['difficulty']})")
            lines.append(f"\n_{len(suggestions)} keywords saved. Run `/seo keywords` to see rankings._")
            await reply.reply_text("\n".join(lines))
        except Exception as e:
            await reply.reply_text(f"DataForSEO error: {e}")
        return True

    kws = conn.execute(
        "SELECT * FROM keywords WHERE domain = ? ORDER BY position NULLS LAST, search_volume DESC LIMIT 20",
        (domain,),
    ).fetchall()

    if not kws:
        await reply.reply_text(
            f"No keywords tracked for *{domain}*.\n"
            f"Run `/seo keywords suggest {domain}` to discover keywords."
        )
        return True

    lines = [f"*Keywords for {domain}:*", ""]
    for kw in kws:
        pos = kw["position"]
        prev = kw["previous_position"]
        pos_str = f"#{pos}" if pos else "—"
        delta = ""
        if pos and prev:
            d = prev - pos
            if d > 0:
                delta = f" (+{d})"
            elif d < 0:
                delta = f" ({d})"
        checked = f" ({kw['checked_at'][:10]})" if kw["checked_at"] else ""
        lines.append(f"  • {kw['keyword']}: {pos_str}{delta} — vol: {kw['search_volume']:,}{checked}")

    await reply.reply_text("\n".join(lines))
    return True


async def _cmd_decay(args, msg, reply) -> bool:
    """Show flagged decaying pages."""
    if args and args[0] == "--help":
        await reply.reply_text("*Usage:* `/seo decay [domain]`\nShows pages with >25% traffic decline (4-week avg).")
        return True

    site, err = _resolve_site(args)
    if err:
        await reply.reply_text(err)
        return True

    domain = site["domain"]
    conn = _db()
    decayed = conn.execute(
        "SELECT * FROM content_scores WHERE domain = ? AND flagged = 1 ORDER BY decay_pct DESC",
        (domain,),
    ).fetchall()

    if not decayed:
        await reply.reply_text(f"No decaying pages detected for *{domain}*.")
        return True

    lines = [f"*Decaying Pages for {domain}:*", ""]
    for d in decayed:
        lines.append(
            f"  • *{d['path']}*\n"
            f"    Current: {d['current_sessions']} | Avg: {d['four_week_avg']} | "
            f"Drop: {d['decay_pct']:.0f}%"
        )
    lines.append(f"\n_{len(decayed)} page(s) flagged (>25% decline vs 4-week average)_")
    await reply.reply_text("\n".join(lines))
    return True


async def _cmd_draft(args, msg, reply) -> bool:
    """Request a blog draft."""
    if not args or args[0] == "--help":
        await reply.reply_text(
            "*Usage:* `/seo draft <topic> [--site domain]`\n\n"
            "*Examples:*\n"
            '  `/seo draft "best CRM for small agencies"`\n'
            '  `/seo draft "SEO tips for 2026" --site example.com`'
        )
        return True

    site_domain = None
    topic_parts = []
    i = 0
    while i < len(args):
        if args[i] == "--site" and i + 1 < len(args):
            site_domain = args[i + 1]
            i += 2
        else:
            topic_parts.append(args[i])
            i += 1

    topic = " ".join(topic_parts).strip('"').strip("'")
    if not topic:
        await reply.reply_text("Please provide a topic. Run `/seo draft --help` for usage.")
        return True

    site_args = [site_domain] if site_domain else []
    site, err = _resolve_site(site_args)
    if err:
        await reply.reply_text(err)
        return True

    domain = site["domain"]
    conn = _db()
    kws = conn.execute(
        "SELECT keyword FROM keywords WHERE domain = ? ORDER BY search_volume DESC LIMIT 5",
        (domain,),
    ).fetchall()
    target_keywords = [kw["keyword"] for kw in kws]

    draft_id = _new_id()
    conn.execute(
        "INSERT INTO drafts (id, domain, topic, target_keywords, requested_by) VALUES (?,?,?,?,?)",
        (draft_id, domain, topic, json.dumps(target_keywords), msg.sender_id),
    )
    conn.commit()

    await reply.reply_text(
        f"Draft queued: *{topic}*\n"
        f"Keywords: {', '.join(target_keywords) if target_keywords else 'none (add keywords first)'}\n\n"
        "Generating... I'll update this chat when it's ready."
    )

    await _generate_draft(draft_id)

    draft = conn.execute("SELECT * FROM drafts WHERE id = ?", (draft_id,)).fetchone()
    if draft and draft["status"] == "done":
        content = draft["content"] or ""
        score = draft["seo_score"]
        words = len(content.split())

        if len(content) > 4000:
            content = content[:4000] + "\n\n... _(truncated — full draft saved)_"

        await reply.reply_text(
            f"*Draft: {topic}*\n"
            f"SEO Score: {score}/100 | Words: {words:,}\n\n"
            f"{content}"
        )
    elif draft and draft["status"] == "failed":
        await reply.reply_text("Draft generation failed. Check logs for details.")

    return True


async def _cmd_pull(args, msg, reply) -> bool:
    """Manual data pull — analytics + keywords + decay scan."""
    if args and args[0] == "--help":
        await reply.reply_text("*Usage:* `/seo pull [domain]`\nManually pull analytics, check keywords, and scan for decay.")
        return True

    site, err = _resolve_site(args)
    if err:
        await reply.reply_text(err)
        return True

    domain = site["domain"]
    await reply.reply_text(f"Pulling data for *{domain}*...")

    results = []

    # Analytics pull
    try:
        backend = _get_backend(site)
        report = await backend.pull_data()
        week_start = (datetime.now() - timedelta(days=datetime.now().weekday())).strftime("%Y-%m-%d")
        conn = _db()
        conn.execute(
            "INSERT INTO analytics_snapshots (id, domain, week_start, total_sessions, "
            "total_pageviews, total_conversions, top_pages, traffic_sources) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (_new_id(), domain, week_start, report.total_sessions,
             report.total_pageviews, report.total_conversions,
             json.dumps(report.top_pages), json.dumps(report.traffic_sources)),
        )
        conn.commit()
        results.append(f"Analytics: {report.total_sessions:,} sessions, {report.total_conversions:,} conversions")
    except Exception as e:
        results.append(f"Analytics: failed — {e}")

    # Keyword check
    conn = _db()
    kws = conn.execute("SELECT * FROM keywords WHERE domain = ?", (domain,)).fetchall()
    if kws and resolve_plugin_setting("seo", "dataforseo_login"):
        checked = 0
        for kw in kws:
            try:
                result = await check_ranking(kw["keyword"], domain)
                conn.execute(
                    "UPDATE keywords SET previous_position=position, position=?, url=?, checked_at=? WHERE id=?",
                    (result["position"], result["url"], datetime.now().isoformat(), kw["id"]),
                )
                checked += 1
            except Exception:
                pass
        conn.commit()
        results.append(f"Keywords: {checked}/{len(kws)} checked")
    elif kws:
        results.append("Keywords: skipped (DataForSEO not configured)")
    else:
        results.append("Keywords: none tracked")

    # Decay scan
    flagged = _run_decay_scan(domain)
    results.append(f"Decay scan: {flagged} page(s) flagged")

    await reply.reply_text(
        f"*Pull complete for {domain}:*\n" + "\n".join(f"  • {r}" for r in results)
    )
    return True


async def _cmd_autosend(args, msg, reply) -> bool:
    """Toggle automatic weekly reports for a site."""
    if not args or args[0] == "--help":
        await reply.reply_text(
            "*Usage:* `/seo autosend on|off [domain]`\n"
            "Toggle automatic weekly SEO reports.\n"
            "Reports go to the site's `notify_chat` (set via `/seo connect --notify`)."
        )
        return True

    toggle = args[0].lower()
    if toggle not in ("on", "off"):
        await reply.reply_text("Usage: `/seo autosend on|off [domain]`")
        return True

    site, err = _resolve_site(args[1:])
    if err:
        await reply.reply_text(err)
        return True

    domain = site["domain"]
    idx = _site_index(domain)
    if idx is None:
        await reply.reply_text(f"Site '{domain}' not found in config.")
        return True

    enabled = toggle == "on"
    update_config_key(f"plugin_settings.seo.sites.{idx}.autosend", enabled)

    if enabled and not site.get("notify_chat"):
        await reply.reply_text(
            f"Autosend *enabled* for *{domain}*, but no `notify_chat` is set.\n"
            f"Set it with: `/seo connect {domain} --notify <chat_jid>`"
        )
    else:
        status = "enabled" if enabled else "disabled"
        await reply.reply_text(f"Weekly auto-reports *{status}* for *{domain}*.")
    return True
