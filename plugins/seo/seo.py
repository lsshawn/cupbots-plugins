"""
SEO — Agentic SEO monitoring + Execution Engine for the Impactology framework.

Multi-tenant: every table carries company_id. Sites are configured under
`plugin_settings.seo.sites:` (untenanted / single-tenant) OR
`plugin_settings.seo.companies.<id>.sites:` (per-client). Scheduled jobs
iterate all sites per company via _iter_all_sites_with_company(). Two
clients can track the same domain and see separate data.

Core commands:
  /seo connect <domain>              — Register site with GA4 (starts OAuth)
  /seo connect <domain> --umami      — Register site with Umami backend
  /seo sites                         — List registered sites
  /seo status [domain]               — Quick health summary
  /seo pull [domain]                 — Manual data pull (analytics + keywords + decay)
  /seo autosend on|off [domain]      — Toggle automatic weekly reports
  /seo schedule                      — View/edit auto-scheduling of recurring jobs

Reporting & insights:
  /seo report [domain]               — Full weekly intelligence report (with action plan)
  /seo plan [domain]                 — Generate this week's prioritized actions
  /seo actions [list|done <id>]      — Manage action items
  /seo keywords [domain]             — Keyword rankings + suggestions
  /seo decay [domain]                — Flagged decaying pages
  /seo search [domain]               — Google Search Console insights (CTR opportunities)
  /seo backlinks [domain]            — Backlink summary + new/lost
  /seo conversion [domain]           — High-traffic-low-conversion pages
  /seo health [domain]               — Web Vitals + uptime + form check status

Content & outreach:
  /seo draft <topic> [--site domain] — Generate SEO blog draft
  /seo outreach <type> [--site dom]  — Draft outreach email (partnership/linkbuilding/etc)

Health checks:
  /seo formcheck [domain] [name]     — Manually trigger a form submission check

Sites are configured in config.yaml under plugin_settings.seo.sites.

Examples:
  /seo connect example.com
  /seo plan example.com
  /seo actions done abc123
  /seo health example.com
  /seo draft "best CRM for agencies" --site example.com
"""

import importlib.util as _ilu
import json
import pathlib as _pl
import secrets
from base64 import b64encode
from collections import namedtuple
from datetime import datetime, timedelta

import httpx

from cupbots.config import get_config, update_config_key
from cupbots.helpers.db import get_plugin_db, resolve_plugin_setting


def _load_sibling(name: str):
    """Load a sibling helper module without relying on package semantics."""
    spec = _ilu.spec_from_file_location(
        f"seo.{name}", _pl.Path(__file__).parent / f"{name}.py",
    )
    mod = _ilu.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_action_planner = _load_sibling("_action_planner")
_gsc = _load_sibling("_gsc")
_psi = _load_sibling("_psi")
_uptime_kuma = _load_sibling("_uptime_kuma")
_form_check = _load_sibling("_form_check")
from cupbots.helpers.jobs import enqueue, register_handler
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
    # Multi-tenant: every row is scoped by (company_id, domain). Two clients
    # can track the same domain string and see separate data. Greenfield —
    # DEFAULT '' means existing rows land in the untenanted bucket.
    # Create tables (without indexes — indexes go after migration)
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS analytics_snapshots (
            id TEXT PRIMARY KEY,
            company_id TEXT NOT NULL DEFAULT '',
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

        CREATE TABLE IF NOT EXISTS keywords (
            id TEXT PRIMARY KEY,
            company_id TEXT NOT NULL DEFAULT '',
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

        CREATE TABLE IF NOT EXISTS content_scores (
            id TEXT PRIMARY KEY,
            company_id TEXT NOT NULL DEFAULT '',
            domain TEXT NOT NULL,
            path TEXT NOT NULL,
            current_sessions INTEGER DEFAULT 0,
            previous_sessions INTEGER DEFAULT 0,
            four_week_avg INTEGER DEFAULT 0,
            decay_pct REAL DEFAULT 0,
            flagged INTEGER DEFAULT 0,
            last_checked TEXT
        );

        CREATE TABLE IF NOT EXISTS drafts (
            id TEXT PRIMARY KEY,
            company_id TEXT NOT NULL DEFAULT '',
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

        CREATE TABLE IF NOT EXISTS actions (
            id TEXT PRIMARY KEY,
            company_id TEXT NOT NULL DEFAULT '',
            domain TEXT NOT NULL,
            title TEXT NOT NULL,
            why TEXT,
            how_ TEXT,
            expected_impact TEXT,
            target_metric TEXT DEFAULT 'none',
            target_ref TEXT,
            priority INTEGER DEFAULT 3,
            status TEXT NOT NULL DEFAULT 'pending',
            baseline_value TEXT,
            actual_impact TEXT,
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            done_at TEXT,
            measured_at TEXT
        );

        CREATE TABLE IF NOT EXISTS gsc_snapshots (
            id TEXT PRIMARY KEY,
            company_id TEXT NOT NULL DEFAULT '',
            domain TEXT NOT NULL,
            week_start TEXT NOT NULL,
            total_clicks INTEGER DEFAULT 0,
            total_impressions INTEGER DEFAULT 0,
            avg_ctr REAL DEFAULT 0,
            avg_position REAL DEFAULT 0,
            top_queries TEXT DEFAULT '[]',
            top_pages TEXT DEFAULT '[]',
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS backlinks_snapshots (
            id TEXT PRIMARY KEY,
            company_id TEXT NOT NULL DEFAULT '',
            domain TEXT NOT NULL,
            week_start TEXT NOT NULL,
            total_backlinks INTEGER DEFAULT 0,
            total_referring_domains INTEGER DEFAULT 0,
            new_backlinks TEXT DEFAULT '[]',
            lost_backlinks TEXT DEFAULT '[]',
            domain_rank INTEGER DEFAULT 0,
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS conversion_insights (
            id TEXT PRIMARY KEY,
            company_id TEXT NOT NULL DEFAULT '',
            domain TEXT NOT NULL,
            path TEXT NOT NULL,
            sessions INTEGER DEFAULT 0,
            conversions INTEGER DEFAULT 0,
            conversion_rate REAL DEFAULT 0,
            site_avg_rate REAL DEFAULT 0,
            flagged INTEGER DEFAULT 0,
            recommendation TEXT,
            updated_at TEXT NOT NULL DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS web_vitals_snapshots (
            id TEXT PRIMARY KEY,
            company_id TEXT NOT NULL DEFAULT '',
            domain TEXT NOT NULL,
            url TEXT NOT NULL,
            strategy TEXT NOT NULL,
            lcp_ms INTEGER,
            inp_ms INTEGER,
            cls REAL,
            fcp_ms INTEGER,
            performance_score INTEGER,
            audited_at TEXT NOT NULL DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS form_check_runs (
            id TEXT PRIMARY KEY,
            company_id TEXT NOT NULL DEFAULT '',
            domain TEXT NOT NULL,
            form_name TEXT NOT NULL,
            ran_at TEXT NOT NULL DEFAULT (datetime('now')),
            success INTEGER DEFAULT 0,
            status_code INTEGER,
            response_excerpt TEXT,
            error TEXT
        );

        CREATE TABLE IF NOT EXISTS outreach_drafts (
            id TEXT PRIMARY KEY,
            company_id TEXT NOT NULL DEFAULT '',
            domain TEXT NOT NULL,
            outreach_type TEXT NOT NULL,
            target TEXT,
            subject TEXT,
            body TEXT,
            status TEXT NOT NULL DEFAULT 'draft',
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            sent_at TEXT
        );
    """)

    # Migration: add company_id to tables created before tenant scoping
    for table in ("analytics_snapshots", "keywords", "content_scores", "drafts",
                  "actions", "gsc_snapshots", "backlinks_snapshots",
                  "conversion_insights", "web_vitals_snapshots",
                  "form_check_runs", "outreach_drafts"):
        try:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN company_id TEXT NOT NULL DEFAULT ''")
        except Exception:
            pass

    conn.executescript("""
        CREATE INDEX IF NOT EXISTS idx_snap_company_domain ON analytics_snapshots (company_id, domain);
        CREATE INDEX IF NOT EXISTS idx_kw_company_domain ON keywords (company_id, domain);
        CREATE INDEX IF NOT EXISTS idx_cs_company_domain ON content_scores (company_id, domain);
        CREATE INDEX IF NOT EXISTS idx_draft_company_domain ON drafts (company_id, domain);
        CREATE INDEX IF NOT EXISTS idx_actions_company_domain ON actions (company_id, domain);
        CREATE INDEX IF NOT EXISTS idx_actions_status ON actions (company_id, status);
        CREATE INDEX IF NOT EXISTS idx_gsc_company_domain ON gsc_snapshots (company_id, domain);
        CREATE INDEX IF NOT EXISTS idx_bl_company_domain ON backlinks_snapshots (company_id, domain);
        CREATE INDEX IF NOT EXISTS idx_ci_company_domain ON conversion_insights (company_id, domain);
        CREATE INDEX IF NOT EXISTS idx_wv_company_domain ON web_vitals_snapshots (company_id, domain);
        CREATE INDEX IF NOT EXISTS idx_fc_company_domain ON form_check_runs (company_id, domain);
        CREATE INDEX IF NOT EXISTS idx_od_company_domain ON outreach_drafts (company_id, domain);
    """)


def _db():
    return get_plugin_db(PLUGIN_NAME)


def _new_id():
    return secrets.token_urlsafe(12)


# ---------------------------------------------------------------------------
# Config-driven site management
# ---------------------------------------------------------------------------

# Multi-tenant site config
# ------------------------
# Two supported shapes in config.yaml:
#
#   # Untenanted (flat list — backwards compat, single-tenant):
#   plugin_settings:
#     seo:
#       sites:
#         - domain: example.com
#
#   # Per-company (multi-tenant):
#   plugin_settings:
#     seo:
#       companies:
#         acme:
#           sites:
#             - domain: acme-client.com
#         beta:
#           sites:
#             - domain: beta-client.com
#
# The two shapes coexist: flat `sites:` belongs to company_id='' (untenanted
# bucket), `companies.<id>.sites:` belongs to that company. Every command
# handler and scheduled job scopes by company_id — client A cannot see
# client B's sites, snapshots, keywords, or anything else.


def _get_sites(company_id: str = "") -> list[dict]:
    """Return the list of sites for a given company_id.

    company_id='' reads the flat `plugin_settings.seo.sites:` list (the
    untenanted bucket / single-tenant default). Any other value reads
    `plugin_settings.seo.companies.<company_id>.sites:`.
    """
    cfg = get_config().get("plugin_settings", {}) or {}
    seo = cfg.get("seo", {}) or {}
    if not company_id:
        return seo.get("sites", []) or []
    companies = seo.get("companies", {}) or {}
    company_cfg = companies.get(company_id, {}) or {}
    return company_cfg.get("sites", []) or []


def _iter_all_sites_with_company() -> list[tuple[str, dict]]:
    """Yield every (company_id, site) pair across the flat list and all
    configured companies. Used by scheduled jobs that run without an
    incoming message context."""
    cfg = get_config().get("plugin_settings", {}) or {}
    seo = cfg.get("seo", {}) or {}
    out: list[tuple[str, dict]] = []
    # Untenanted bucket (flat list)
    for s in (seo.get("sites", []) or []):
        if isinstance(s, dict):
            out.append(("", s))
    # Per-company
    for cid, ccfg in (seo.get("companies", {}) or {}).items():
        if not isinstance(ccfg, dict):
            continue
        for s in (ccfg.get("sites", []) or []):
            if isinstance(s, dict):
                out.append((str(cid), s))
    return out


def _get_site(domain: str, company_id: str = "") -> dict | None:
    """Get a single site config by domain, scoped to a company."""
    for s in _get_sites(company_id):
        if s.get("domain") == domain:
            return s
    return None


def _resolve_site(args: list[str], company_id: str = "") -> tuple[dict | None, str | None]:
    """Resolve a site from args or default to the only site, scoped to a company.

    Returns (site_dict, error_msg). One will be None.
    """
    sites = _get_sites(company_id)

    # Check if a domain was provided
    domain = None
    for a in args:
        if "." in a and not a.startswith("-"):
            domain = a
            break

    scope_hint = f" (company {company_id!r})" if company_id else ""
    if domain:
        site = _get_site(domain, company_id)
        if not site:
            return None, f"Site '{domain}' not found{scope_hint}. Run `/seo sites` to see configured sites."
        return site, None

    if not sites:
        return None, f"No sites configured{scope_hint}. Run `/seo connect <domain>` or add to config.yaml."
    if len(sites) == 1:
        return sites[0], None
    domains = ", ".join(s["domain"] for s in sites)
    return None, f"Multiple sites configured{scope_hint}. Specify one: {domains}"


def _site_index(domain: str, company_id: str = "") -> int | None:
    """Find the index of a site in its company's site list."""
    for i, s in enumerate(_get_sites(company_id)):
        if s.get("domain") == domain:
            return i
    return None


def _site_config_path(company_id: str, idx: int, *subkeys: str) -> str:
    """Build a config.yaml key path for a site's config field.

    Untenanted: plugin_settings.seo.sites.<idx>[.<subkey>...]
    Tenanted:   plugin_settings.seo.companies.<company_id>.sites.<idx>[.<subkey>...]
    """
    if company_id:
        base = f"plugin_settings.seo.companies.{company_id}.sites.{idx}"
    else:
        base = f"plugin_settings.seo.sites.{idx}"
    if subkeys:
        return base + "." + ".".join(subkeys)
    return base


def _sites_config_path(company_id: str) -> str:
    """Build the config.yaml path to the sites list itself."""
    if company_id:
        return f"plugin_settings.seo.companies.{company_id}.sites"
    return "plugin_settings.seo.sites"


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


async def get_backlinks_summary(domain: str) -> dict:
    """Get total backlinks + referring domains via DataForSEO."""
    data = await _dataforseo_post("/backlinks/summary/live", [{"target": domain}])
    result = (data.get("tasks", [{}])[0].get("result", []) or [{}])[0]
    return {
        "total_backlinks": result.get("backlinks", 0),
        "total_referring_domains": result.get("referring_domains", 0),
        "domain_rank": result.get("rank", 0),
    }


async def get_referring_domains(domain: str, limit: int = 100) -> list[dict]:
    """Get top referring domains via DataForSEO."""
    data = await _dataforseo_post("/backlinks/referring_domains/live", [{
        "target": domain,
        "limit": limit,
    }])
    result = (data.get("tasks", [{}])[0].get("result", []) or [{}])[0]
    items = result.get("items", []) or []
    return [
        {
            "domain": i.get("domain", ""),
            "backlinks": i.get("backlinks", 0),
            "rank": i.get("rank", 0),
            "first_seen": i.get("first_seen", ""),
        }
        for i in items
    ]


def _diff_backlinks(prev_domains: list[dict], current_domains: list[dict]) -> tuple[list[str], list[str]]:
    """Compare two lists of referring domains, return (new_domains, lost_domains)."""
    prev_set = {d["domain"] for d in prev_domains}
    curr_set = {d["domain"] for d in current_domains}
    new = sorted(curr_set - prev_set)
    lost = sorted(prev_set - curr_set)
    return new, lost


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

def _run_decay_scan(domain: str, company_id: str = ""):
    """Scan a site's snapshots for content decay (>25% drop vs 4-week avg)."""
    conn = _db()
    snapshots = conn.execute(
        "SELECT * FROM analytics_snapshots WHERE company_id = ? AND domain = ? "
        "ORDER BY created_at DESC LIMIT 5",
        (company_id, domain),
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
            "SELECT id FROM content_scores WHERE company_id = ? AND domain = ? AND path = ?",
            (company_id, domain, page["path"]),
        ).fetchone()

        if existing:
            conn.execute(
                "UPDATE content_scores SET current_sessions=?, previous_sessions=?, "
                "four_week_avg=?, decay_pct=?, flagged=?, last_checked=? "
                "WHERE company_id = ? AND id = ?",
                (page["sessions"], hist[0] if hist else 0, avg, decay_pct, flagged,
                 now, company_id, existing["id"]),
            )
        else:
            conn.execute(
                "INSERT INTO content_scores (id, company_id, domain, path, current_sessions, "
                "previous_sessions, four_week_avg, decay_pct, flagged, last_checked) "
                "VALUES (?,?,?,?,?,?,?,?,?,?)",
                (_new_id(), company_id, domain, page["path"], page["sessions"],
                 hist[0] if hist else 0, avg, decay_pct, flagged, now),
            )

    conn.commit()
    return flagged_count


# ---------------------------------------------------------------------------
# Blog draft generation
# ---------------------------------------------------------------------------

async def _generate_draft(draft_id: str):
    """Generate a blog draft (two-stage: outline then content).

    Reads company_id from the draft row itself — callers only need to pass
    the draft_id. All subsequent updates are scoped by (company_id, id) as
    defense-in-depth even though id is globally unique.
    """
    conn = _db()
    draft = conn.execute("SELECT * FROM drafts WHERE id = ?", (draft_id,)).fetchone()
    if not draft or draft["status"] != "pending":
        return

    draft_company = draft["company_id"] if "company_id" in draft.keys() else ""
    conn.execute(
        "UPDATE drafts SET status = 'generating' WHERE company_id = ? AND id = ?",
        (draft_company, draft_id),
    )
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
            "UPDATE drafts SET status='done', outline=?, content=?, seo_score=?, completed_at=? "
            "WHERE company_id = ? AND id = ?",
            (outline, content, score, datetime.now().isoformat(), draft_company, draft_id),
        )
        conn.commit()
        log.info("Draft %s completed (score=%d, words=%d)", draft_id, score, word_count)

    except Exception as e:
        log.error("Draft generation failed for %s: %s", draft_id, e, exc_info=True)
        conn.execute(
            "UPDATE drafts SET status='failed' WHERE company_id = ? AND id = ?",
            (draft_company, draft_id),
        )
        conn.commit()


# ---------------------------------------------------------------------------
# OAuth callback
# ---------------------------------------------------------------------------

async def _on_ga4_connected(tokens: dict, metadata: dict):
    """Called by oauth.py when GA4 OAuth completes.

    metadata must carry both `domain` and `company_id` (populated by
    start_flow() in _cmd_connect). The token payload is written to the
    correct per-company path in config.yaml.
    """
    domain = metadata.get("domain")
    if not domain:
        log.error("OAuth callback missing domain in metadata")
        return

    company_id = metadata.get("company_id", "") or ""
    idx = _site_index(domain, company_id)
    if idx is None:
        log.error("OAuth callback: site %s not found in config (company=%s)",
                  domain, company_id or "-")
        return

    token_data = {
        "access_token": tokens.get("access_token"),
        "refresh_token": tokens.get("refresh_token"),
        "expiry": tokens.get("expires_in"),
    }
    update_config_key(_site_config_path(company_id, idx, "ga4_tokens"), token_data)

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

    log.info("GA4 OAuth completed for %s (company=%s)", domain, company_id or "-")


# Register Google GA4 + Search Console as a single OAuth provider.
# Existing users with old single-scope tokens must re-run /seo connect once.
register_provider("google_ga4", {
    "auth_url": "https://accounts.google.com/o/oauth2/v2/auth",
    "token_url": "https://oauth2.googleapis.com/token",
    "default_scopes": [
        "https://www.googleapis.com/auth/analytics.readonly",
        "https://www.googleapis.com/auth/webmasters.readonly",
    ],
    "on_success": _on_ga4_connected,
})


# ---------------------------------------------------------------------------
# Job handlers
# ---------------------------------------------------------------------------

async def _job_analytics_pull(payload: dict):
    """Scheduled job: pull analytics for all configured sites across all companies."""
    for company_id, site in _iter_all_sites_with_company():
        try:
            backend = _get_backend(site)
            report = await backend.pull_data()
            week_start = (datetime.now() - timedelta(days=datetime.now().weekday())).strftime("%Y-%m-%d")
            conn = _db()
            conn.execute(
                "INSERT INTO analytics_snapshots (id, company_id, domain, week_start, total_sessions, "
                "total_pageviews, total_conversions, top_pages, traffic_sources) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                (_new_id(), company_id, site["domain"], week_start, report.total_sessions,
                 report.total_pageviews, report.total_conversions,
                 json.dumps(report.top_pages), json.dumps(report.traffic_sources)),
            )
            conn.commit()
            log.info("Analytics snapshot saved for %s (company=%s)",
                     site["domain"], company_id or "-")
        except Exception as e:
            log.error("Analytics pull failed for %s (company=%s): %s",
                      site["domain"], company_id or "-", e, exc_info=True)


async def _job_keyword_check(payload: dict):
    """Scheduled job: check SERP rankings for tracked keywords across all companies."""
    conn = _db()
    for company_id, site in _iter_all_sites_with_company():
        kws = conn.execute(
            "SELECT * FROM keywords WHERE company_id = ? AND domain = ?",
            (company_id, site["domain"]),
        ).fetchall()
        for kw in kws:
            try:
                result = await check_ranking(kw["keyword"], site["domain"])
                conn.execute(
                    "UPDATE keywords SET previous_position=position, position=?, url=?, checked_at=? "
                    "WHERE company_id = ? AND id = ?",
                    (result["position"], result["url"], datetime.now().isoformat(),
                     company_id, kw["id"]),
                )
                conn.commit()
            except Exception as e:
                log.error("Keyword check failed for '%s' (company=%s): %s",
                          kw["keyword"], company_id or "-", e)


async def _job_decay_scan(payload: dict):
    """Scheduled job: scan all sites for content decay across all companies."""
    total_flagged = 0
    for company_id, site in _iter_all_sites_with_company():
        total_flagged += _run_decay_scan(site["domain"], company_id)
    if total_flagged:
        log.info("Decay scan found %d flagged pages", total_flagged)


async def _job_draft_process(payload: dict):
    """Scheduled job: process the oldest pending blog draft (tenant-agnostic:
    _generate_draft() reads company_id from the draft row and scopes writes
    accordingly)."""
    conn = _db()
    pending = conn.execute(
        "SELECT id FROM drafts WHERE status = 'pending' ORDER BY created_at LIMIT 1",
    ).fetchone()
    if pending:
        await _generate_draft(pending["id"])


async def _job_weekly_report(payload: dict):
    """Scheduled job: send weekly reports for sites with autosend across all companies."""
    from cupbots.helpers.channel import WhatsAppReplyContext
    for company_id, site in _iter_all_sites_with_company():
        if not site.get("autosend"):
            continue
        notify_chat = site.get("notify_chat")
        if not notify_chat:
            continue
        try:
            report_text = await _build_report(site, company_id=company_id, with_action_plan=True)
        except Exception as e:
            log.error("Weekly report build failed for %s (company=%s): %s",
                      site["domain"], company_id or "-", e, exc_info=True)
            continue
        if report_text:
            reply = WhatsAppReplyContext(notify_chat)
            await reply.reply_text(report_text)


async def _job_gsc_pull(payload: dict):
    """Scheduled job: pull Google Search Console data for all sites across all companies."""
    client_id = resolve_plugin_setting("seo", "google_client_id")
    client_secret = resolve_plugin_setting("seo", "google_client_secret")
    if not client_id or not client_secret:
        log.warning("GSC pull skipped: Google OAuth not configured")
        return

    for company_id, site in _iter_all_sites_with_company():
        domain = site["domain"]
        site_url = site.get("gsc_property_id") or f"sc-domain:{domain}"
        tokens = site.get("ga4_tokens")
        if not tokens:
            continue

        try:
            summary, refreshed = await _gsc.pull_gsc_summary(
                site_url, tokens, client_id, client_secret,
            )
        except Exception as e:
            log.error("GSC pull failed for %s (company=%s): %s",
                      domain, company_id or "-", e, exc_info=True)
            continue

        if refreshed:
            idx = _site_index(domain, company_id)
            if idx is not None:
                update_config_key(_site_config_path(company_id, idx, "ga4_tokens"), refreshed)

        week_start = (datetime.now() - timedelta(days=datetime.now().weekday())).strftime("%Y-%m-%d")
        conn = _db()
        conn.execute(
            "INSERT INTO gsc_snapshots (id, company_id, domain, week_start, total_clicks, "
            "total_impressions, avg_ctr, avg_position, top_queries, top_pages) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (_new_id(), company_id, domain, week_start,
             summary["total_clicks"], summary["total_impressions"],
             summary["avg_ctr"], summary["avg_position"],
             json.dumps(summary["top_queries"]), json.dumps(summary["top_pages"])),
        )
        conn.commit()
        log.info("GSC snapshot saved for %s (company=%s)", domain, company_id or "-")


def _wrap_recurring(queue: str, handler):
    """Wrap a job handler so it self-re-enqueues for the next cron run."""
    async def _wrapped(payload: dict, **kwargs):
        try:
            await handler(payload)
        finally:
            if (payload or {}).get("_auto"):
                _reenqueue_self(queue, payload)
    return _wrapped


register_handler("seo_analytics_pull", _wrap_recurring("seo_analytics_pull", _job_analytics_pull))
register_handler("seo_keyword_check", _wrap_recurring("seo_keyword_check", _job_keyword_check))
register_handler("seo_decay_scan", _wrap_recurring("seo_decay_scan", _job_decay_scan))
register_handler("seo_draft_process", _job_draft_process)  # not auto-scheduled
register_handler("seo_weekly_report", _wrap_recurring("seo_weekly_report", _job_weekly_report))
async def _job_backlinks_pull(payload: dict):
    """Scheduled job: pull backlinks summary + referring domains for all sites across all companies."""
    if not resolve_plugin_setting("seo", "dataforseo_login"):
        log.warning("Backlinks pull skipped: DataForSEO not configured")
        return

    conn = _db()
    for company_id, site in _iter_all_sites_with_company():
        domain = site["domain"]
        try:
            summary = await get_backlinks_summary(domain)
            referring = await get_referring_domains(domain, limit=200)
        except Exception as e:
            log.error("Backlinks pull failed for %s (company=%s): %s",
                      domain, company_id or "-", e, exc_info=True)
            continue

        # Diff against last snapshot to find new/lost (scoped to this company)
        prev = conn.execute(
            "SELECT new_backlinks FROM backlinks_snapshots "
            "WHERE company_id = ? AND domain = ? "
            "ORDER BY created_at DESC LIMIT 1",
            (company_id, domain),
        ).fetchone()
        prev_domains = []
        if prev:
            try:
                prev_domains = [{"domain": d} for d in json.loads(prev["new_backlinks"] or "[]")]
            except Exception:
                pass

        new_doms, lost_doms = _diff_backlinks(prev_domains, referring)

        week_start = (datetime.now() - timedelta(days=datetime.now().weekday())).strftime("%Y-%m-%d")
        conn.execute(
            "INSERT INTO backlinks_snapshots (id, company_id, domain, week_start, total_backlinks, "
            "total_referring_domains, new_backlinks, lost_backlinks, domain_rank) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (_new_id(), company_id, domain, week_start,
             summary["total_backlinks"], summary["total_referring_domains"],
             json.dumps([d["domain"] for d in referring[:50]]),
             json.dumps(lost_doms[:20]),
             summary["domain_rank"]),
        )
        conn.commit()
        log.info("Backlinks snapshot saved for %s (company=%s, %d backlinks, %d new domains)",
                 domain, company_id or "-", summary["total_backlinks"], len(new_doms))


def _analyze_conversion(domain: str, company_id: str = "") -> int:
    """Analyze the latest analytics snapshot for high-traffic-low-conversion pages.

    Flags pages where:
      - sessions are above the median (high traffic)
      - conversion rate < 50% of site average

    Returns the number of newly flagged pages.
    """
    conn = _db()
    snap = conn.execute(
        "SELECT * FROM analytics_snapshots WHERE company_id = ? AND domain = ? "
        "ORDER BY created_at DESC LIMIT 1",
        (company_id, domain),
    ).fetchone()
    if not snap:
        return 0

    pages = json.loads(snap["top_pages"] or "[]")
    if not pages:
        return 0

    # Site average conversion rate
    total_sessions = sum(p.get("sessions", 0) for p in pages) or 1
    total_conversions = sum(p.get("conversions", 0) for p in pages)
    site_avg_rate = (total_conversions / total_sessions) * 100

    # Median sessions to filter "high traffic"
    sorted_sessions = sorted([p.get("sessions", 0) for p in pages])
    median_sessions = sorted_sessions[len(sorted_sessions) // 2] if sorted_sessions else 0

    threshold = site_avg_rate * 0.5
    flagged = 0

    # Clear previous insights for this company + domain
    conn.execute(
        "DELETE FROM conversion_insights WHERE company_id = ? AND domain = ?",
        (company_id, domain),
    )

    for p in pages:
        sessions = p.get("sessions", 0)
        conversions = p.get("conversions", 0)
        if sessions == 0:
            continue
        conv_rate = (conversions / sessions) * 100
        is_flagged = sessions >= median_sessions and conv_rate < threshold

        if is_flagged:
            flagged += 1

        conn.execute(
            "INSERT INTO conversion_insights (id, company_id, domain, path, sessions, conversions, "
            "conversion_rate, site_avg_rate, flagged) VALUES (?,?,?,?,?,?,?,?,?)",
            (_new_id(), company_id, domain, p["path"], sessions, conversions,
             round(conv_rate, 2), round(site_avg_rate, 2), 1 if is_flagged else 0),
        )

    conn.commit()
    return flagged


async def _job_conversion_analyze(payload: dict):
    """Scheduled job: analyze conversion insights for all sites across all companies."""
    for company_id, site in _iter_all_sites_with_company():
        try:
            flagged = _analyze_conversion(site["domain"], company_id)
            if flagged:
                log.info("Conversion analysis for %s (company=%s) flagged %d pages",
                         site["domain"], company_id or "-", flagged)
        except Exception as e:
            log.error("Conversion analysis failed for %s (company=%s): %s",
                      site["domain"], company_id or "-", e, exc_info=True)


async def _job_pagespeed_pull(payload: dict):
    """Scheduled job: pull PageSpeed Insights across all sites and all companies."""
    api_key = resolve_plugin_setting("seo", "pagespeed_api_key")  # optional

    for company_id, site in _iter_all_sites_with_company():
        domain = site["domain"]
        # Build URL list: homepage + top 5 pages from latest snapshot
        urls = [f"https://{domain}/"]
        conn = _db()
        snap = conn.execute(
            "SELECT top_pages FROM analytics_snapshots "
            "WHERE company_id = ? AND domain = ? "
            "ORDER BY created_at DESC LIMIT 1",
            (company_id, domain),
        ).fetchone()
        if snap:
            for p in json.loads(snap["top_pages"] or "[]")[:5]:
                path = p.get("path", "")
                if path and not path.startswith("http"):
                    if not path.startswith("/"):
                        path = "/" + path
                    urls.append(f"https://{domain}{path}")
                elif path:
                    urls.append(path)

        # Dedupe
        urls = list(dict.fromkeys(urls))

        for url in urls:
            for strategy in ("mobile", "desktop"):
                try:
                    vitals = await _psi.pull_pagespeed(url, strategy=strategy, api_key=api_key)
                except Exception as e:
                    log.warning("PSI failed for %s (%s): %s", url, strategy, e)
                    continue

                conn.execute(
                    "INSERT INTO web_vitals_snapshots (id, company_id, domain, url, strategy, lcp_ms, "
                    "inp_ms, cls, fcp_ms, performance_score) VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (_new_id(), company_id, domain, url, strategy,
                     vitals["lcp_ms"], vitals["inp_ms"], vitals["cls"],
                     vitals["fcp_ms"], vitals["performance_score"]),
                )
                conn.commit()
        log.info("PageSpeed snapshot saved for %s (company=%s, %d urls)",
                 domain, company_id or "-", len(urls))


def _is_form_check_due(form_def: dict, last_run_iso: str | None) -> bool:
    """Check if a form check is due based on its schedule and last run."""
    schedule = (form_def.get("schedule") or "weekly").lower()
    if schedule == "manual":
        return False
    if not last_run_iso:
        return True
    try:
        last = datetime.fromisoformat(last_run_iso)
    except Exception:
        return True
    delta = datetime.now() - last
    if schedule == "daily":
        return delta >= timedelta(hours=23)
    if schedule == "weekly":
        return delta >= timedelta(days=6, hours=23)
    return False


async def _job_form_check(payload: dict):
    """Scheduled job: run form checks for all sites across all companies where they're due."""
    conn = _db()
    for company_id, site in _iter_all_sites_with_company():
        domain = site["domain"]
        form_checks = site.get("form_checks") or []
        for form_def in form_checks:
            name = form_def.get("name", "unnamed")
            last_row = conn.execute(
                "SELECT ran_at FROM form_check_runs "
                "WHERE company_id = ? AND domain = ? AND form_name = ? "
                "ORDER BY ran_at DESC LIMIT 1",
                (company_id, domain, name),
            ).fetchone()
            last_run = last_row["ran_at"] if last_row else None

            if not _is_form_check_due(form_def, last_run):
                continue

            log.info("Running form check '%s' for %s (company=%s)",
                     name, domain, company_id or "-")
            result = await _form_check.run_form_check(form_def)
            conn.execute(
                "INSERT INTO form_check_runs (id, company_id, domain, form_name, success, "
                "status_code, response_excerpt, error) VALUES (?,?,?,?,?,?,?,?)",
                (_new_id(), company_id, domain, name, 1 if result["success"] else 0,
                 result["status_code"], result["response_excerpt"], result["error"]),
            )
            conn.commit()

            # Notify on failure if site has notify_chat
            if not result["success"] and site.get("notify_chat"):
                from cupbots.helpers.channel import WhatsAppReplyContext
                rep = WhatsAppReplyContext(site["notify_chat"])
                await rep.reply_text(
                    f"⚠️ *Form check failed:* {domain} / {name}\n"
                    f"Status: {result['status_code']}\nError: {result['error']}"
                )


register_handler("seo_gsc_pull", _wrap_recurring("seo_gsc_pull", _job_gsc_pull))
register_handler("seo_backlinks_pull", _wrap_recurring("seo_backlinks_pull", _job_backlinks_pull))
register_handler("seo_conversion_analyze", _wrap_recurring("seo_conversion_analyze", _job_conversion_analyze))
async def _job_action_measure(payload: dict):
    """Scheduled job: measure impact of actions marked done >= 7 days ago.

    Iterates actions across all companies — measure_action_impact() reads
    each action's own company_id from the row and scopes its writes
    accordingly, so cross-tenant measurement is safe."""
    conn = _db()
    rows = conn.execute(
        "SELECT id FROM actions WHERE status = 'done' AND done_at IS NOT NULL "
        "AND done_at <= datetime('now', '-7 days') AND target_metric != 'none' "
        "AND baseline_value != ''",
    ).fetchall()

    for r in rows:
        try:
            await _action_planner.measure_action_impact(r["id"])
        except Exception as e:
            log.warning("Action measurement failed for %s: %s", r["id"], e)

    if rows:
        log.info("Measured impact of %d action(s) across all companies", len(rows))


register_handler("seo_pagespeed_pull", _wrap_recurring("seo_pagespeed_pull", _job_pagespeed_pull))
register_handler("seo_form_check", _wrap_recurring("seo_form_check", _job_form_check))
register_handler("seo_action_measure", _wrap_recurring("seo_action_measure", _job_action_measure))


# ---------------------------------------------------------------------------
# Report formatting
# ---------------------------------------------------------------------------

async def _build_report(site: dict, company_id: str = "", *, with_action_plan: bool = True) -> str:
    """Async wrapper that optionally generates an action plan, then formats the report."""
    actions: list[dict] = []
    if with_action_plan:
        try:
            actions = await _action_planner.generate_action_plan(site["domain"], company_id)
        except Exception as e:
            log.warning("Action plan generation failed for %s (company=%s): %s",
                        site["domain"], company_id or "-", e)
    return _format_report(site, company_id=company_id, actions=actions)


def _format_report(site: dict, *, company_id: str = "", actions: list[dict] | None = None) -> str:
    """Format a full weekly report for a site. Pure sync formatting."""
    conn = _db()
    domain = site["domain"]

    snap = conn.execute(
        "SELECT * FROM analytics_snapshots WHERE company_id = ? AND domain = ? "
        "ORDER BY created_at DESC LIMIT 1",
        (company_id, domain),
    ).fetchone()

    prev_snap = conn.execute(
        "SELECT * FROM analytics_snapshots WHERE company_id = ? AND domain = ? "
        "ORDER BY created_at DESC LIMIT 1 OFFSET 1",
        (company_id, domain),
    ).fetchone()

    kws = conn.execute(
        "SELECT * FROM keywords WHERE company_id = ? AND domain = ? "
        "ORDER BY position NULLS LAST LIMIT 10",
        (company_id, domain),
    ).fetchall()

    decayed = conn.execute(
        "SELECT * FROM content_scores WHERE company_id = ? AND domain = ? AND flagged = 1 "
        "ORDER BY decay_pct DESC LIMIT 5",
        (company_id, domain),
    ).fetchall()

    # Last week's measured action results
    measured = conn.execute(
        "SELECT title, actual_impact FROM actions "
        "WHERE company_id = ? AND domain = ? AND status = 'measured' "
        "AND measured_at >= datetime('now', '-7 days') ORDER BY measured_at DESC LIMIT 5",
        (company_id, domain),
    ).fetchall()

    lines = [f"*SEO Report: {domain}*", f"_{datetime.now().strftime('%Y-%m-%d')}_", ""]

    # Lead with action plan
    if actions:
        lines.append(_action_planner.format_action_plan(actions))
        lines.append("")
        lines.append("─" * 20)
        lines.append("")

    # Last week's results
    if measured:
        lines.append("*Last Week's Results:*")
        for m in measured:
            try:
                impact = json.loads(m["actual_impact"])
                pct = impact.get("change_pct")
                if pct is not None:
                    sign = "+" if pct >= 0 else ""
                    lines.append(f"  ✓ {m['title']} → {sign}{pct}%")
                else:
                    lines.append(f"  ✓ {m['title']}")
            except Exception:
                lines.append(f"  ✓ {m['title']}")
        lines.append("")

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


def _format_status(site: dict, company_id: str = "") -> str:
    """Format a quick status summary for a site."""
    conn = _db()
    domain = site["domain"]
    backend = site.get("backend", "ga4").upper()

    snap = conn.execute(
        "SELECT * FROM analytics_snapshots WHERE company_id = ? AND domain = ? "
        "ORDER BY created_at DESC LIMIT 1",
        (company_id, domain),
    ).fetchone()

    kw_count = conn.execute(
        "SELECT COUNT(*) as c FROM keywords WHERE company_id = ? AND domain = ?",
        (company_id, domain),
    ).fetchone()["c"]

    flagged_count = conn.execute(
        "SELECT COUNT(*) as c FROM content_scores "
        "WHERE company_id = ? AND domain = ? AND flagged = 1",
        (company_id, domain),
    ).fetchone()["c"]

    pending_drafts = conn.execute(
        "SELECT COUNT(*) as c FROM drafts "
        "WHERE company_id = ? AND domain = ? AND status IN ('pending', 'generating')",
        (company_id, domain),
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
    elif sub == "plan":
        return await _cmd_plan(rest, msg, reply)
    elif sub == "actions":
        return await _cmd_actions(rest, msg, reply)
    elif sub == "search":
        return await _cmd_search(rest, msg, reply)
    elif sub == "backlinks":
        return await _cmd_backlinks(rest, msg, reply)
    elif sub == "conversion":
        return await _cmd_conversion(rest, msg, reply)
    elif sub == "health":
        return await _cmd_health(rest, msg, reply)
    elif sub == "formcheck":
        return await _cmd_formcheck(rest, msg, reply)
    elif sub == "outreach":
        return await _cmd_outreach(rest, msg, reply)
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
    elif sub == "schedule":
        return await _cmd_schedule(rest, msg, reply)
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

    company_id = msg.company_id or ""
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

    existing = _get_site(domain, company_id)
    site_idx = _site_index(domain, company_id)

    if existing and property_id:
        update_config_key(
            _site_config_path(company_id, site_idx, "ga4_property_id"), property_id,
        )
        await reply.reply_text(f"GA4 property ID updated for *{domain}*: `{property_id}`")
        return True

    if existing and notify_chat:
        update_config_key(
            _site_config_path(company_id, site_idx, "notify_chat"), notify_chat,
        )
        await reply.reply_text(f"Notify chat updated for *{domain}*: `{notify_chat}`")
        return True

    if existing and not is_umami:
        # Site exists — maybe re-trigger OAuth
        if existing.get("backend", "ga4") == "ga4" and not existing.get("ga4_tokens"):
            await start_flow(
                provider="google_ga4",
                scopes=None,
                metadata={
                    "domain": domain,
                    "company_id": company_id,
                    "chat_id": msg.chat_id,
                },
                reply=reply,
                extra_params={"access_type": "offline", "prompt": "consent"},
            )
            return True
        await reply.reply_text(f"Site *{domain}* already configured ({existing.get('backend', 'ga4')}).")
        return True

    # Register new site — append to the company's sites list in config.yaml
    new_site = {"domain": domain, "backend": "umami" if is_umami else "ga4"}
    if notify_chat:
        new_site["notify_chat"] = notify_chat

    sites = _get_sites(company_id)
    sites.append(new_site)
    update_config_key(_sites_config_path(company_id), sites)

    if is_umami:
        await reply.reply_text(
            f"Site *{domain}* added with Umami backend.\n\n"
            f"Configure the connection in config.yaml under "
            f"`{_sites_config_path(company_id)}`:\n"
            "  `umami_api_url`, `umami_website_id`, `umami_api_token`"
        )
    else:
        # OAuth client credentials live on the hub as env vars — the bot has
        # nothing to configure. start_flow() will error clearly if the hub
        # isn't connected or hasn't been set up.
        await start_flow(
            provider="google_ga4",
            scopes=None,
            metadata={
                "domain": domain,
                "company_id": company_id,
                "chat_id": msg.chat_id,
            },
            reply=reply,
            extra_params={"access_type": "offline", "prompt": "consent"},
        )

    return True


async def _cmd_sites(msg, reply) -> bool:
    """List registered sites for the caller's company."""
    company_id = msg.company_id or ""
    sites = _get_sites(company_id)

    if not sites:
        scope = f" (company {company_id!r})" if company_id else ""
        await reply.reply_text(
            f"No sites configured{scope}. Run `/seo connect <domain>` to add one."
        )
        return True

    scope_label = f" ({company_id})" if company_id else ""
    lines = [f"*Configured Sites{scope_label}:*", ""]
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

    company_id = msg.company_id or ""
    site, err = _resolve_site(args, company_id)
    if err:
        await reply.reply_text(err)
        return True

    await reply.reply_text(_format_status(site, company_id))
    return True


async def _cmd_report(args, msg, reply) -> bool:
    """Full weekly report (with action plan)."""
    if args and args[0] == "--help":
        await reply.reply_text(
            "*Usage:* `/seo report [domain] [--no-plan]`\n"
            "Shows full weekly intelligence report. Generates a fresh action plan unless `--no-plan`."
        )
        return True

    company_id = msg.company_id or ""
    with_plan = "--no-plan" not in args
    filtered = [a for a in args if a != "--no-plan"]
    site, err = _resolve_site(filtered, company_id)
    if err:
        await reply.reply_text(err)
        return True

    if with_plan:
        await reply.reply_text(f"Generating report for {site['domain']}...")
    report = await _build_report(site, company_id=company_id, with_action_plan=with_plan)
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

    company_id = msg.company_id or ""
    do_suggest = "suggest" in [a.lower() for a in args]
    filtered_args = [a for a in args if a.lower() != "suggest"]

    site, err = _resolve_site(filtered_args, company_id)
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
                    "SELECT id FROM keywords WHERE company_id = ? AND domain = ? AND keyword = ?",
                    (company_id, domain, s["keyword"]),
                ).fetchone()
                if not existing:
                    conn.execute(
                        "INSERT INTO keywords (id, company_id, domain, keyword, search_volume, "
                        "difficulty, source) VALUES (?,?,?,?,?,?,?)",
                        (_new_id(), company_id, domain, s["keyword"], s["search_volume"],
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
        "SELECT * FROM keywords WHERE company_id = ? AND domain = ? "
        "ORDER BY position NULLS LAST, search_volume DESC LIMIT 20",
        (company_id, domain),
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

    company_id = msg.company_id or ""
    site, err = _resolve_site(args, company_id)
    if err:
        await reply.reply_text(err)
        return True

    domain = site["domain"]
    conn = _db()
    decayed = conn.execute(
        "SELECT * FROM content_scores WHERE company_id = ? AND domain = ? AND flagged = 1 "
        "ORDER BY decay_pct DESC",
        (company_id, domain),
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

    company_id = msg.company_id or ""
    site_args = [site_domain] if site_domain else []
    site, err = _resolve_site(site_args, company_id)
    if err:
        await reply.reply_text(err)
        return True

    domain = site["domain"]
    conn = _db()
    kws = conn.execute(
        "SELECT keyword FROM keywords WHERE company_id = ? AND domain = ? "
        "ORDER BY search_volume DESC LIMIT 5",
        (company_id, domain),
    ).fetchall()
    target_keywords = [kw["keyword"] for kw in kws]

    draft_id = _new_id()
    conn.execute(
        "INSERT INTO drafts (id, company_id, domain, topic, target_keywords, requested_by) "
        "VALUES (?,?,?,?,?,?)",
        (draft_id, company_id, domain, topic, json.dumps(target_keywords), msg.sender_id),
    )
    conn.commit()

    await reply.reply_text(
        f"Draft queued: *{topic}*\n"
        f"Keywords: {', '.join(target_keywords) if target_keywords else 'none (add keywords first)'}\n\n"
        "Generating... I'll update this chat when it's ready."
    )

    await _generate_draft(draft_id)

    draft = conn.execute(
        "SELECT * FROM drafts WHERE company_id = ? AND id = ?",
        (company_id, draft_id),
    ).fetchone()
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

    company_id = msg.company_id or ""
    site, err = _resolve_site(args, company_id)
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
            "INSERT INTO analytics_snapshots (id, company_id, domain, week_start, total_sessions, "
            "total_pageviews, total_conversions, top_pages, traffic_sources) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (_new_id(), company_id, domain, week_start, report.total_sessions,
             report.total_pageviews, report.total_conversions,
             json.dumps(report.top_pages), json.dumps(report.traffic_sources)),
        )
        conn.commit()
        results.append(f"Analytics: {report.total_sessions:,} sessions, {report.total_conversions:,} conversions")
    except Exception as e:
        results.append(f"Analytics: failed — {e}")

    # Keyword check
    conn = _db()
    kws = conn.execute(
        "SELECT * FROM keywords WHERE company_id = ? AND domain = ?",
        (company_id, domain),
    ).fetchall()
    if kws and resolve_plugin_setting("seo", "dataforseo_login"):
        checked = 0
        for kw in kws:
            try:
                result = await check_ranking(kw["keyword"], domain)
                conn.execute(
                    "UPDATE keywords SET previous_position=position, position=?, url=?, checked_at=? "
                    "WHERE company_id = ? AND id = ?",
                    (result["position"], result["url"], datetime.now().isoformat(),
                     company_id, kw["id"]),
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
    flagged = _run_decay_scan(domain, company_id)
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

    company_id = msg.company_id or ""
    site, err = _resolve_site(args[1:], company_id)
    if err:
        await reply.reply_text(err)
        return True

    domain = site["domain"]
    idx = _site_index(domain, company_id)
    if idx is None:
        await reply.reply_text(f"Site '{domain}' not found in config.")
        return True

    enabled = toggle == "on"
    update_config_key(_site_config_path(company_id, idx, "autosend"), enabled)

    if enabled and not site.get("notify_chat"):
        await reply.reply_text(
            f"Autosend *enabled* for *{domain}*, but no `notify_chat` is set.\n"
            f"Set it with: `/seo connect {domain} --notify <chat_jid>`"
        )
    else:
        status = "enabled" if enabled else "disabled"
        await reply.reply_text(f"Weekly auto-reports *{status}* for *{domain}*.")
    return True


async def _cmd_plan(args, msg, reply) -> bool:
    """Generate this week's action plan via LLM."""
    if args and args[0] == "--help":
        await reply.reply_text(
            "*Usage:* `/seo plan [domain]`\n"
            "Generates 3-5 prioritized actions for this week based on all SEO data."
        )
        return True

    company_id = msg.company_id or ""
    site, err = _resolve_site(args, company_id)
    if err:
        await reply.reply_text(err)
        return True

    domain = site["domain"]
    await reply.reply_text(f"Analyzing {domain} and generating action plan...")

    try:
        actions = await _action_planner.generate_action_plan(domain, company_id)
    except Exception as e:
        log.error("Action plan generation failed for %s (company=%s): %s",
                  domain, company_id or "-", e, exc_info=True)
        await reply.reply_text(f"Failed to generate action plan: {e}")
        return True

    text = _action_planner.format_action_plan(actions)
    await reply.reply_text(text)
    return True


async def _cmd_actions(args, msg, reply) -> bool:
    """Manage action items."""
    if not args or args[0] == "--help":
        await reply.reply_text(
            "*Usage:*\n"
            "  `/seo actions list [domain]` — Show pending and recent actions\n"
            "  `/seo actions done <id>` — Mark an action as completed (snapshots baseline)\n\n"
            "Actions are generated by `/seo plan` and surfaced in `/seo report`."
        )
        return True

    sub = args[0].lower()
    company_id = msg.company_id or ""

    if sub == "done":
        if len(args) < 2:
            await reply.reply_text("Usage: `/seo actions done <id>`")
            return True
        action_id = args[1]
        conn = _db()
        action = conn.execute(
            "SELECT * FROM actions WHERE company_id = ? AND id = ?",
            (company_id, action_id),
        ).fetchone()
        if not action:
            await reply.reply_text(
                f"Action `{action_id}` not found in company '{company_id or '-'}'."
            )
            return True
        # Re-capture baseline at completion (data may have moved since creation)
        new_baseline = _action_planner._capture_baseline(
            action["domain"], action["target_metric"], action["target_ref"], company_id,
        )
        conn.execute(
            "UPDATE actions SET status='done', done_at=datetime('now'), baseline_value=? "
            "WHERE company_id = ? AND id = ?",
            (new_baseline or action["baseline_value"], company_id, action_id),
        )
        conn.commit()
        await reply.reply_text(
            f"Marked action *{action['title']}* as done.\n"
            f"_Impact will be measured in next week's report._"
        )
        return True

    if sub == "list":
        site, err = _resolve_site(args[1:], company_id)
        if err:
            await reply.reply_text(err)
            return True
        domain = site["domain"]

        conn = _db()
        rows = conn.execute(
            "SELECT * FROM actions WHERE company_id = ? AND domain = ? "
            "ORDER BY created_at DESC LIMIT 15",
            (company_id, domain),
        ).fetchall()

        if not rows:
            await reply.reply_text(
                f"No actions for *{domain}*. Run `/seo plan {domain}` to generate some."
            )
            return True

        lines = [f"*Actions for {domain}:*", ""]
        for r in rows:
            status = r["status"]
            badge = {"pending": "⚪", "done": "✅", "measured": "📊"}.get(status, "•")
            lines.append(f"{badge} `{r['id']}` *{r['title']}* ({status})")
            if r["actual_impact"]:
                try:
                    impact = json.loads(r["actual_impact"])
                    if impact.get("change_pct") is not None:
                        sign = "+" if impact["change_pct"] >= 0 else ""
                        lines.append(f"     → {sign}{impact['change_pct']}% impact")
                except Exception:
                    pass
        await reply.reply_text("\n".join(lines))
        return True

    await reply.reply_text("Unknown subcommand. Run `/seo actions --help`.")
    return True


async def _cmd_search(args, msg, reply) -> bool:
    """Show Google Search Console insights."""
    if args and args[0] == "--help":
        await reply.reply_text(
            "*Usage:* `/seo search [domain]`\n"
            "Shows Search Console data: top queries, CTR opportunities, indexing health.\n\n"
            "Requires GA4 OAuth re-authorization with the new Search Console scope:\n"
            "  `/seo connect <domain>` (then click the link)"
        )
        return True

    company_id = msg.company_id or ""
    site, err = _resolve_site(args, company_id)
    if err:
        await reply.reply_text(err)
        return True

    domain = site["domain"]
    conn = _db()

    # If no snapshot yet, try a live pull
    snap = conn.execute(
        "SELECT * FROM gsc_snapshots WHERE company_id = ? AND domain = ? "
        "ORDER BY created_at DESC LIMIT 1",
        (company_id, domain),
    ).fetchone()

    if not snap:
        client_id = resolve_plugin_setting("seo", "google_client_id")
        client_secret = resolve_plugin_setting("seo", "google_client_secret")
        tokens = site.get("ga4_tokens")
        if not (client_id and client_secret and tokens):
            await reply.reply_text(
                f"No Search Console data for *{domain}*.\n"
                "Re-authorize with `/seo connect` to grant Search Console access."
            )
            return True

        site_url = site.get("gsc_property_id") or f"sc-domain:{domain}"
        await reply.reply_text(f"Fetching Search Console data for {domain}...")
        try:
            summary, refreshed = await _gsc.pull_gsc_summary(
                site_url, tokens, client_id, client_secret,
            )
        except Exception as e:
            await reply.reply_text(f"GSC fetch failed: {e}\n\nMake sure the site is verified in Search Console and the OAuth scope includes Webmasters.")
            return True

        if refreshed:
            idx = _site_index(domain, company_id)
            if idx is not None:
                update_config_key(_site_config_path(company_id, idx, "ga4_tokens"), refreshed)

        week_start = (datetime.now() - timedelta(days=datetime.now().weekday())).strftime("%Y-%m-%d")
        conn.execute(
            "INSERT INTO gsc_snapshots (id, company_id, domain, week_start, total_clicks, "
            "total_impressions, avg_ctr, avg_position, top_queries, top_pages) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (_new_id(), company_id, domain, week_start,
             summary["total_clicks"], summary["total_impressions"],
             summary["avg_ctr"], summary["avg_position"],
             json.dumps(summary["top_queries"]), json.dumps(summary["top_pages"])),
        )
        conn.commit()
        snap = conn.execute(
            "SELECT * FROM gsc_snapshots WHERE company_id = ? AND domain = ? "
            "ORDER BY created_at DESC LIMIT 1",
            (company_id, domain),
        ).fetchone()

    top_queries = json.loads(snap["top_queries"] or "[]")
    top_pages = json.loads(snap["top_pages"] or "[]")
    opportunities = _gsc.find_ctr_opportunities(top_queries)

    lines = [
        f"*Search Console: {domain}*",
        f"_{snap['created_at'][:10]}_",
        "",
        f"*7-day totals:* {snap['total_clicks']:,} clicks, {snap['total_impressions']:,} impressions",
        f"*Avg CTR:* {snap['avg_ctr']:.2f}%   *Avg Position:* {snap['avg_position']:.1f}",
        "",
    ]

    if opportunities:
        lines.append("*🎯 Quick wins (rank 5-15, low CTR):*")
        for q in opportunities[:5]:
            lines.append(f"  • {q['query']} — #{q['position']} | {q['impressions']:,} imp | {q['ctr']}% CTR")
        lines.append("")

    if top_queries:
        lines.append("*Top queries:*")
        for q in top_queries[:8]:
            lines.append(f"  • {q['query']} — {q['clicks']} clicks @ #{q['position']}")
        lines.append("")

    if top_pages:
        lines.append("*Top pages:*")
        for p in top_pages[:5]:
            lines.append(f"  • {p['page']} — {p['clicks']} clicks")

    await reply.reply_text("\n".join(lines))
    return True


async def _cmd_backlinks(args, msg, reply) -> bool:
    """Show backlinks summary, new/lost, and top referring domains."""
    if args and args[0] == "--help":
        await reply.reply_text(
            "*Usage:* `/seo backlinks [domain]`\n"
            "Shows backlink count, referring domains, and new/lost domains since last check.\n"
            "Add `pull` to fetch fresh data: `/seo backlinks pull example.com`"
        )
        return True

    company_id = msg.company_id or ""
    do_pull = "pull" in [a.lower() for a in args]
    filtered = [a for a in args if a.lower() != "pull"]

    site, err = _resolve_site(filtered, company_id)
    if err:
        await reply.reply_text(err)
        return True

    domain = site["domain"]
    conn = _db()

    if do_pull:
        if not resolve_plugin_setting("seo", "dataforseo_login"):
            await reply.reply_text("DataForSEO not configured.")
            return True
        await reply.reply_text(f"Fetching backlinks for {domain}...")
        try:
            # NOTE: _job_backlinks_pull iterates ALL companies' sites. That's
            # slightly wasteful here (it refreshes other clients too) but keeps
            # the code DRY. If that becomes a problem, extract a per-site helper.
            await _job_backlinks_pull({})
        except Exception as e:
            await reply.reply_text(f"Backlinks fetch failed: {e}")
            return True

    snap = conn.execute(
        "SELECT * FROM backlinks_snapshots WHERE company_id = ? AND domain = ? "
        "ORDER BY created_at DESC LIMIT 1",
        (company_id, domain),
    ).fetchone()

    if not snap:
        await reply.reply_text(
            f"No backlinks data for *{domain}*.\n"
            f"Run `/seo backlinks pull {domain}` to fetch."
        )
        return True

    new_domains = json.loads(snap["new_backlinks"] or "[]")
    lost_domains = json.loads(snap["lost_backlinks"] or "[]")

    lines = [
        f"*Backlinks: {domain}*",
        f"_{snap['created_at'][:10]}_",
        "",
        f"*Total backlinks:* {snap['total_backlinks']:,}",
        f"*Referring domains:* {snap['total_referring_domains']:,}",
        f"*Domain rank:* {snap['domain_rank']}",
    ]

    if lost_domains:
        lines.append("")
        lines.append(f"*🔻 Lost ({len(lost_domains)}):*")
        for d in lost_domains[:10]:
            lines.append(f"  • {d}")

    if new_domains:
        lines.append("")
        lines.append(f"*Top referring domains:*")
        for d in new_domains[:15]:
            lines.append(f"  • {d}")

    await reply.reply_text("\n".join(lines))
    return True


async def _cmd_conversion(args, msg, reply) -> bool:
    """Show high-traffic-low-conversion pages with AI recommendations."""
    if args and args[0] == "--help":
        await reply.reply_text(
            "*Usage:* `/seo conversion [domain]`\n"
            "Lists pages with above-median traffic but below-average conversion rate.\n"
            "Add `explain` for per-page AI recommendations: `/seo conversion explain example.com`"
        )
        return True

    company_id = msg.company_id or ""
    do_explain = "explain" in [a.lower() for a in args]
    filtered = [a for a in args if a.lower() != "explain"]

    site, err = _resolve_site(filtered, company_id)
    if err:
        await reply.reply_text(err)
        return True

    domain = site["domain"]
    conn = _db()

    # Re-run the analysis on the latest snapshot for this company
    _analyze_conversion(domain, company_id)

    rows = conn.execute(
        "SELECT * FROM conversion_insights "
        "WHERE company_id = ? AND domain = ? AND flagged = 1 "
        "ORDER BY sessions DESC LIMIT 10",
        (company_id, domain),
    ).fetchall()

    if not rows:
        await reply.reply_text(
            f"No high-traffic-low-conversion pages found for *{domain}*.\n"
            f"Run `/seo pull` first to refresh analytics data."
        )
        return True

    lines = [f"*Conversion Issues: {domain}*", ""]
    for r in rows:
        lines.append(
            f"• *{r['path']}*\n"
            f"  {r['sessions']:,} sessions | {r['conversions']} conv ({r['conversion_rate']}%)\n"
            f"  Site avg: {r['site_avg_rate']}%"
        )
    lines.append("")
    lines.append(f"_{len(rows)} page(s) flagged. Site avg: {rows[0]['site_avg_rate']}%_")

    await reply.reply_text("\n".join(lines))

    # Optionally generate AI recommendations
    if do_explain and rows:
        await reply.reply_text("Generating AI recommendations...")
        flagged_summary = "\n".join([
            f"- {r['path']}: {r['sessions']} sessions, {r['conversion_rate']}% conv (site avg {r['site_avg_rate']}%)"
            for r in rows[:5]
        ])
        prompt = f"""You are a CRO consultant. Below are pages from {domain} with high traffic but low conversion.

For each page, give a 2-sentence recommendation: most likely cause and one specific fix to test.
Be specific (CTA copy, headline, form length, intent mismatch, etc).

Pages:
{flagged_summary}"""
        try:
            advice = await ask_llm(prompt, system="You are a senior CRO consultant. Brief, specific, actionable.", max_tokens=1024) or ""
            if advice:
                await reply.reply_text(f"*AI Recommendations:*\n\n{advice}")
        except Exception as e:
            log.warning("Conversion explanation failed: %s", e)

    return True


async def _cmd_health(args, msg, reply) -> bool:
    """Show combined website health: Web Vitals + Uptime Kuma + form checks."""
    if args and args[0] == "--help":
        await reply.reply_text(
            "*Usage:* `/seo health [domain]`\n"
            "Combined website health: Core Web Vitals (mobile/desktop), Uptime Kuma status, "
            "and form check results."
        )
        return True

    company_id = msg.company_id or ""
    site, err = _resolve_site(args, company_id)
    if err:
        await reply.reply_text(err)
        return True

    domain = site["domain"]
    conn = _db()

    lines = [f"*Website Health: {domain}*", ""]

    # --- Core Web Vitals ---
    vitals_rows = conn.execute(
        "SELECT * FROM web_vitals_snapshots "
        "WHERE company_id = ? AND domain = ? AND url = ? "
        "ORDER BY audited_at DESC LIMIT 2",
        (company_id, domain, f"https://{domain}/"),
    ).fetchall()

    if vitals_rows:
        lines.append("*Core Web Vitals (homepage):*")
        for v in vitals_rows:
            assessment = _psi.assess_vitals(dict(v))
            badge = {"good": "✅", "needs-improvement": "🟡", "poor": "🔴"}.get(assessment, "•")
            lcp = f"{v['lcp_ms']/1000:.1f}s" if v['lcp_ms'] else "—"
            inp = f"{v['inp_ms']}ms" if v['inp_ms'] else "—"
            cls = f"{v['cls']:.2f}" if v['cls'] is not None else "—"
            score = v['performance_score'] or "—"
            lines.append(f"  {badge} *{v['strategy']}*: LCP {lcp} | INP {inp} | CLS {cls} | Score {score}/100")
        lines.append("")
    else:
        lines.append("_No Web Vitals data. Run `/seo pull` or wait for daily PSI job._")
        lines.append("")

    # --- Uptime Kuma ---
    cfg = get_config().get("plugin_settings", {}).get("seo", {}).get("uptime_kuma")
    if cfg and cfg.get("url"):
        try:
            monitors = _uptime_kuma.get_monitors_for_domain(cfg, domain)
        except Exception as e:
            monitors = []
            lines.append(f"_Uptime Kuma error: {e}_")
            lines.append("")

        if monitors:
            lines.append("*Uptime Kuma:*")
            for m in monitors:
                try:
                    summary = _uptime_kuma.get_uptime_summary(cfg, m["id"])
                except Exception as e:
                    summary = {"name": m["name"], "last_status": "error", "error": str(e)}
                status = summary.get("last_status", "unknown")
                badge = {"up": "✅", "down": "🔴"}.get(status, "•")
                u24 = summary.get("uptime_24h")
                u7d = summary.get("uptime_7d")
                ping = summary.get("last_ping_ms")
                line = f"  {badge} *{summary.get('name', m['name'])}*"
                bits = []
                if u24 is not None:
                    bits.append(f"24h: {u24}%")
                if u7d is not None:
                    bits.append(f"7d: {u7d}%")
                if ping is not None:
                    bits.append(f"{ping}ms")
                if bits:
                    line += " — " + " | ".join(bits)
                lines.append(line)
            lines.append("")
    else:
        lines.append("_Uptime Kuma not configured._")
        lines.append("")

    # --- Form checks ---
    form_checks = site.get("form_checks") or []
    if form_checks:
        lines.append("*Form Checks:*")
        for fc_def in form_checks:
            name = fc_def.get("name", "unnamed")
            last = conn.execute(
                "SELECT * FROM form_check_runs "
                "WHERE company_id = ? AND domain = ? AND form_name = ? "
                "ORDER BY ran_at DESC LIMIT 1",
                (company_id, domain, name),
            ).fetchone()
            if last:
                badge = "✅" if last["success"] else "🔴"
                when = last["ran_at"][:16]
                lines.append(f"  {badge} *{name}* — {when}")
                if not last["success"] and last["error"]:
                    lines.append(f"     {last['error']}")
            else:
                lines.append(f"  ⚪ *{name}* — never run")
    else:
        lines.append("_No form checks configured._")

    await reply.reply_text("\n".join(lines))
    return True


async def _cmd_formcheck(args, msg, reply) -> bool:
    """Manually trigger a form check."""
    if not args or args[0] == "--help":
        await reply.reply_text(
            "*Usage:* `/seo formcheck [domain] [name]`\n"
            "Manually trigger a form check. If name is omitted, runs all configured form checks for the site.\n"
            "Form checks are configured per-site in `config.yaml` under `form_checks`."
        )
        return True

    company_id = msg.company_id or ""
    # Resolve site (first arg could be domain or form name)
    site, err = _resolve_site(args, company_id)
    if err:
        await reply.reply_text(err)
        return True

    domain = site["domain"]
    form_checks = site.get("form_checks") or []
    if not form_checks:
        await reply.reply_text(f"No form checks configured for *{domain}*.")
        return True

    # If a non-domain arg was passed, treat it as a form name filter
    name_filter = None
    for a in args:
        if "." not in a and not a.startswith("-"):
            name_filter = a
            break

    to_run = [f for f in form_checks if name_filter is None or name_filter.lower() in f.get("name", "").lower()]
    if not to_run:
        names = ", ".join(f.get("name", "?") for f in form_checks)
        await reply.reply_text(f"No matching form check. Available: {names}")
        return True

    await reply.reply_text(f"Running {len(to_run)} form check(s) for *{domain}*...")

    conn = _db()
    results = []
    for form_def in to_run:
        result = await _form_check.run_form_check(form_def)
        conn.execute(
            "INSERT INTO form_check_runs (id, company_id, domain, form_name, success, "
            "status_code, response_excerpt, error) VALUES (?,?,?,?,?,?,?,?)",
            (_new_id(), company_id, domain, form_def.get("name", "unnamed"),
             1 if result["success"] else 0,
             result["status_code"], result["response_excerpt"], result["error"]),
        )
        results.append((form_def.get("name", "unnamed"), result))
    conn.commit()

    lines = [f"*Form check results for {domain}:*", ""]
    for name, r in results:
        badge = "✅" if r["success"] else "🔴"
        lines.append(f"{badge} *{name}* — HTTP {r['status_code']}")
        if not r["success"] and r["error"]:
            lines.append(f"   {r['error']}")
    await reply.reply_text("\n".join(lines))
    return True


# ---------------------------------------------------------------------------
# Outreach drafting
# ---------------------------------------------------------------------------

OUTREACH_TYPES = ("partnership", "linkbuilding", "guestpost", "customer-followup")

OUTREACH_PROMPTS = {
    "partnership": "Draft a partnership outreach email to a potential collaborator. Tone: friendly, value-first. Focus on mutual benefit.",
    "linkbuilding": "Draft a link-building outreach email asking for a backlink. Tone: helpful, give specific reason why our content is relevant to theirs.",
    "guestpost": "Draft a guest post pitch email. Include 3 specific topic ideas and brief credentials.",
    "customer-followup": "Draft a follow-up email to a recent website visitor or lead. Tone: warm, specific, low-pressure. Single clear next step.",
}


async def _cmd_outreach(args, msg, reply) -> bool:
    """Generate an outreach email draft."""
    if not args or args[0] == "--help":
        await reply.reply_text(
            "*Usage:* `/seo outreach <type> [--site domain] [--target <url-or-email>]`\n\n"
            f"*Types:* {', '.join(OUTREACH_TYPES)}\n\n"
            "*Examples:*\n"
            "  `/seo outreach partnership --target acme.com`\n"
            "  `/seo outreach linkbuilding --site example.com --target https://blog.com/post`\n"
            "  `/seo outreach list` — Show recent drafts"
        )
        return True

    company_id = msg.company_id or ""

    if args[0].lower() == "list":
        site, err = _resolve_site(args[1:], company_id)
        if err:
            await reply.reply_text(err)
            return True
        conn = _db()
        rows = conn.execute(
            "SELECT * FROM outreach_drafts WHERE company_id = ? AND domain = ? "
            "ORDER BY created_at DESC LIMIT 10",
            (company_id, site["domain"]),
        ).fetchall()
        if not rows:
            await reply.reply_text(f"No outreach drafts for *{site['domain']}*.")
            return True
        lines = [f"*Outreach drafts: {site['domain']}*", ""]
        for r in rows:
            badge = {"draft": "📝", "sent": "✉️"}.get(r["status"], "•")
            lines.append(f"{badge} `{r['id']}` *{r['outreach_type']}* → {r['target']}")
        await reply.reply_text("\n".join(lines))
        return True

    outreach_type = args[0].lower()
    if outreach_type not in OUTREACH_TYPES:
        await reply.reply_text(f"Unknown type. Use one of: {', '.join(OUTREACH_TYPES)}")
        return True

    # Parse --site and --target
    site_domain = None
    target = None
    i = 1
    while i < len(args):
        if args[i] == "--site" and i + 1 < len(args):
            site_domain = args[i + 1]
            i += 2
        elif args[i] == "--target" and i + 1 < len(args):
            target = args[i + 1]
            i += 2
        else:
            i += 1

    site_args = [site_domain] if site_domain else []
    site, err = _resolve_site(site_args, company_id)
    if err:
        await reply.reply_text(err)
        return True

    domain = site["domain"]
    brand_voice = site.get("brand_voice", "professional, friendly, value-first")

    # Pull context: top keywords, recent backlinks
    conn = _db()
    kws = conn.execute(
        "SELECT keyword FROM keywords WHERE company_id = ? AND domain = ? "
        "ORDER BY search_volume DESC LIMIT 5",
        (company_id, domain),
    ).fetchall()
    top_keywords = [k["keyword"] for k in kws]

    context = f"Site: {domain}\nBrand voice: {brand_voice}\nTop keywords: {', '.join(top_keywords) if top_keywords else 'unknown'}\nTarget: {target or '(no specific target)'}"

    type_instruction = OUTREACH_PROMPTS[outreach_type]
    prompt = f"""{type_instruction}

Context:
{context}

Output format:
SUBJECT: <subject line>
BODY:
<email body>

Keep it under 200 words. Be specific. Avoid filler. Sign off as "[Your name]"."""

    await reply.reply_text(f"Drafting *{outreach_type}* email...")

    try:
        draft_text = await ask_llm(
            prompt,
            system="You are an expert outreach copywriter. Write emails that get opened and replied to.",
            max_tokens=1024,
        ) or ""
    except Exception as e:
        await reply.reply_text(f"Draft generation failed: {e}")
        return True

    # Parse subject/body
    subject = ""
    body = draft_text
    if "SUBJECT:" in draft_text:
        parts = draft_text.split("BODY:", 1)
        if len(parts) == 2:
            subject = parts[0].replace("SUBJECT:", "").strip()
            body = parts[1].strip()

    draft_id = _new_id()
    conn.execute(
        "INSERT INTO outreach_drafts (id, company_id, domain, outreach_type, target, subject, body) "
        "VALUES (?,?,?,?,?,?,?)",
        (draft_id, company_id, domain, outreach_type, target or "", subject, body),
    )
    conn.commit()

    await reply.reply_text(
        f"*{outreach_type.title()} draft* `[{draft_id}]`\n"
        f"To: {target or '(unspecified)'}\n"
        f"Subject: {subject}\n\n"
        f"{body}"
    )
    return True


async def _cmd_schedule(args, msg, reply) -> bool:
    """Manage auto-scheduling of recurring SEO jobs."""
    if not args or args[0] == "--help":
        await reply.reply_text(
            "*Usage:*\n"
            "  `/seo schedule` — Show current schedule\n"
            "  `/seo schedule on` — Enable all auto-scheduling\n"
            "  `/seo schedule off` — Disable all (on-demand only)\n"
            "  `/seo schedule disable <job>` — Disable one job\n"
            "  `/seo schedule enable <job>` — Re-enable one job (restores default cron)\n"
            "  `/seo schedule set <job> \"<cron>\"` — Override cron for a job\n"
            "  `/seo schedule apply` — Re-apply after config edit (clears pending jobs first)\n\n"
            "*Jobs:* " + ", ".join(sorted(DEFAULT_SEO_SCHEDULES.keys()))
        )
        return True

    sub = args[0].lower()

    # --- Show current state ---
    if sub == "show" or sub == "list":
        seo_cfg = (get_config().get("plugin_settings", {}) or {}).get("seo", {}) or {}
        auto_on = seo_cfg.get("auto_schedule", True)
        active = _resolve_schedules()

        lines = [
            f"*SEO Auto-Schedule:* {'ON' if auto_on else 'OFF (on-demand only)'}",
            "",
        ]
        for queue, default_cron in DEFAULT_SEO_SCHEDULES.items():
            cron = active.get(queue)
            if not cron:
                lines.append(f"  🔕 *{queue}* — disabled")
            elif cron == default_cron:
                lines.append(f"  ✅ *{queue}* — `{cron}` (default)")
            else:
                lines.append(f"  ⚙️ *{queue}* — `{cron}` (custom)")

        lines.append("")
        lines.append("_Edit with `/seo schedule set/disable/enable`._")
        await reply.reply_text("\n".join(lines))
        return True

    # --- Master switch ---
    if sub == "on":
        update_config_key("plugin_settings.seo.auto_schedule", True)
        _purge_pending_seo_jobs()
        _ensure_schedules()
        await reply.reply_text("Auto-scheduling *enabled*. All recurring SEO jobs are queued.")
        return True

    if sub == "off":
        update_config_key("plugin_settings.seo.auto_schedule", False)
        _purge_pending_seo_jobs()
        await reply.reply_text(
            "Auto-scheduling *disabled*. All recurring SEO jobs removed.\n"
            "Run insights on demand via `/seo pull`, `/seo plan`, `/seo report`, etc."
        )
        return True

    # --- Per-job controls ---
    if sub in ("disable", "enable", "set"):
        if len(args) < 2:
            await reply.reply_text(f"Usage: `/seo schedule {sub} <job_name>`")
            return True
        job = args[1]
        if job not in DEFAULT_SEO_SCHEDULES:
            await reply.reply_text(
                f"Unknown job: `{job}`\n\n*Valid:* " + ", ".join(sorted(DEFAULT_SEO_SCHEDULES.keys()))
            )
            return True

        if sub == "disable":
            update_config_key(f"plugin_settings.seo.schedules.{job}", False)
            _purge_pending_seo_jobs(only=job)
            await reply.reply_text(f"Disabled *{job}*. Run manually via the corresponding `/seo` command.")
            return True

        if sub == "enable":
            # Clear any override by setting to default, then let _ensure_schedules handle it
            default_cron = DEFAULT_SEO_SCHEDULES[job]
            update_config_key(f"plugin_settings.seo.schedules.{job}", default_cron)
            _purge_pending_seo_jobs(only=job)
            _ensure_schedules()
            await reply.reply_text(f"Enabled *{job}* with default cron `{default_cron}`.")
            return True

        if sub == "set":
            if len(args) < 3:
                await reply.reply_text(f"Usage: `/seo schedule set {job} \"<cron>\"`")
                return True
            # Join the rest as the cron expression (it may be quoted)
            cron_expr = " ".join(args[2:]).strip('"').strip("'")
            # Validate the cron expression
            try:
                from croniter import croniter
                croniter(cron_expr, datetime.now())
            except Exception as e:
                await reply.reply_text(f"Invalid cron expression: {e}")
                return True
            update_config_key(f"plugin_settings.seo.schedules.{job}", cron_expr)
            _purge_pending_seo_jobs(only=job)
            _ensure_schedules()
            await reply.reply_text(f"Updated *{job}* → `{cron_expr}`.")
            return True

    if sub == "apply":
        _purge_pending_seo_jobs()
        _ensure_schedules()
        await reply.reply_text(
            "Re-applied schedule from config.yaml. Pending jobs cleared and re-queued."
        )
        return True

    await reply.reply_text(f"Unknown subcommand: {sub}\n\nRun `/seo schedule --help`.")
    return True


def _purge_pending_seo_jobs(only: str | None = None):
    """Remove pending SEO jobs from the framework DB.

    If `only` is given, only that queue is purged. Otherwise all SEO queues.
    Used when re-applying config changes so stale entries don't conflict.
    """
    try:
        from cupbots.helpers.db import get_fw_db
        conn = get_fw_db()
        queues = [only] if only else list(DEFAULT_SEO_SCHEDULES.keys())
        placeholders = ",".join("?" * len(queues))
        conn.execute(
            f"DELETE FROM jobs WHERE queue IN ({placeholders}) AND status = 'pending'",
            queues,
        )
        conn.commit()
    except Exception as e:
        log.warning("Failed to purge pending SEO jobs: %s", e)


# ---------------------------------------------------------------------------
# Auto-scheduling: ensure recurring SEO jobs are queued at plugin load
# ---------------------------------------------------------------------------

# Default cron schedules (UTC). Users override via plugin_settings.seo.schedules in config.yaml.
# Set a value to `false` to disable that job. Master switch: plugin_settings.seo.auto_schedule.
DEFAULT_SEO_SCHEDULES: dict[str, str] = {
    "seo_decay_scan":          "0 5 * * *",     # Daily 5am
    "seo_pagespeed_pull":      "0 6 * * *",     # Daily 6am
    "seo_analytics_pull":      "0 6 * * 0",     # Sunday 6am
    "seo_gsc_pull":            "30 6 * * 0",    # Sunday 6:30am
    "seo_backlinks_pull":      "0 7 * * 0",     # Sunday 7am
    "seo_keyword_check":       "0 6 * * 1",     # Monday 6am
    "seo_conversion_analyze":  "30 6 * * 1",    # Monday 6:30am
    "seo_form_check":          "0 9 * * 1",     # Monday 9am
    "seo_action_measure":      "30 9 * * 1",    # Monday 9:30am
    "seo_weekly_report":       "0 10 * * 1",    # Monday 10am
}


def _resolve_schedules() -> dict[str, str]:
    """Return the effective schedules after applying config overrides.

    Config model:
      plugin_settings.seo.auto_schedule: bool (default true) — master switch
      plugin_settings.seo.schedules:
        seo_decay_scan: "0 5 * * *"     # override cron
        seo_keyword_check: false         # disable this job
    """
    seo_cfg = (get_config().get("plugin_settings", {}) or {}).get("seo", {}) or {}
    if seo_cfg.get("auto_schedule") is False:
        return {}

    overrides = seo_cfg.get("schedules", {}) or {}
    effective: dict[str, str] = {}
    for queue, default_cron in DEFAULT_SEO_SCHEDULES.items():
        if queue in overrides:
            val = overrides[queue]
            if val is False or val is None or val == "":
                continue  # explicitly disabled
            effective[queue] = str(val)
        else:
            effective[queue] = default_cron
    return effective


# Exposed for inspection (e.g. /seo schedule)
def get_active_schedules() -> dict[str, str]:
    return _resolve_schedules()


def _next_cron_run(cron_expr: str) -> datetime:
    """Compute the next run time for a cron expression."""
    from croniter import croniter
    return croniter(cron_expr, datetime.now()).get_next(datetime)


def _has_pending_job(queue: str) -> bool:
    """Check if framework.db already has a pending job in this queue."""
    try:
        from cupbots.helpers.db import get_fw_db
        conn = get_fw_db()
        row = conn.execute(
            "SELECT 1 FROM jobs WHERE queue = ? AND status IN ('pending', 'running') LIMIT 1",
            (queue,),
        ).fetchone()
        return row is not None
    except Exception:
        return False


def _ensure_schedules():
    """Enqueue recurring SEO jobs if they're not already scheduled.

    Idempotent — safe to call on /reload. Reads `plugin_settings.seo.auto_schedule`
    (master switch) and `plugin_settings.seo.schedules` (per-job overrides/disables)
    at each call, so config edits take effect on /reload without bot restart.
    """
    schedules = _resolve_schedules()
    if not schedules:
        log.info("Auto-scheduling disabled via plugin_settings.seo.auto_schedule")
        return

    for queue, cron_expr in schedules.items():
        if _has_pending_job(queue):
            continue
        try:
            run_at = _next_cron_run(cron_expr)
            enqueue(queue, {"_auto": True, "cron": cron_expr}, run_at=run_at)
            log.info("Auto-scheduled %s at %s (%s)", queue, run_at.isoformat(), cron_expr)
        except Exception as e:
            log.warning("Failed to auto-schedule %s: %s", queue, e)


def _reenqueue_self(queue: str, payload: dict | None = None):
    """Re-enqueue a job at its next cron time. Called from inside job handlers.

    Respects the current config — if the job was disabled or auto-scheduling was
    turned off since the job was originally enqueued, it won't re-enqueue.
    """
    schedules = _resolve_schedules()
    cron_expr = schedules.get(queue)
    if not cron_expr:
        log.debug("Not re-enqueuing %s: disabled or auto_schedule off", queue)
        return
    try:
        run_at = _next_cron_run(cron_expr)
        enqueue(queue, {"_auto": True, "cron": cron_expr}, run_at=run_at)
    except Exception as e:
        log.warning("Failed to re-enqueue %s: %s", queue, e)


# Run at module import (i.e. plugin load and /reload)
try:
    _ensure_schedules()
except Exception as _e:
    log.warning("Auto-scheduling skipped: %s", _e)
