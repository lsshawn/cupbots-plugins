"""
Action plan generator for SEO plugin.

Transforms raw SEO data into 3-5 prioritized weekly actions via LLM.
This is the Execution Engine — turning insights into actions, not dashboards.
"""

import json
from datetime import datetime, timedelta

from cupbots.helpers.db import get_plugin_db
from cupbots.helpers.llm import ask_llm
from cupbots.helpers.logger import get_logger

log = get_logger("seo.action_planner")

PLUGIN_NAME = "seo"

ACTION_SYSTEM_PROMPT = """You are a senior SEO consultant advising a paying client.
You write to busy founders/marketers who want to know exactly what to do this week.

Your output is ALWAYS:
1. Specific — reference exact pages (with paths) and keywords from the data
2. Justified — cite the data evidence for each recommendation
3. Prioritized — most impactful action first
4. Actionable — describe a concrete task that can be done in <5 hours

You will be given a JSON dump of a site's SEO data. Generate 3 to 5 actions.
Return ONLY a JSON array, nothing else. Each action object has:
{
  "title": "Short imperative title (max 60 chars)",
  "why": "1-2 sentences citing specific data points",
  "how": "1-3 sentences describing concrete steps",
  "expected_impact": "What metric will move and roughly by how much",
  "target_metric": "sessions|conversions|position|backlinks|uptime|web_vitals|none",
  "target_ref": "page path, keyword, or URL this action targets (or empty)",
  "priority": 1-5 (1 = highest)
}

If there is insufficient data to recommend anything specific, return an empty array []."""


def _collect_site_data(domain: str, company_id: str = "") -> dict:
    """Gather all available SEO data for a site into one dict for the LLM.

    Scoped by (company_id, domain) so Client A never sees Client B's data
    even if they configure the same domain.
    """
    conn = get_plugin_db(PLUGIN_NAME)
    data: dict = {"domain": domain, "generated_at": datetime.now().isoformat()}

    # Latest analytics snapshot + previous for trend
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

    if snap:
        data["analytics"] = {
            "week_start": snap["week_start"],
            "total_sessions": snap["total_sessions"],
            "total_conversions": snap["total_conversions"],
            "top_pages": json.loads(snap["top_pages"] or "[]")[:10],
            "traffic_sources": json.loads(snap["traffic_sources"] or "[]")[:10],
        }
        if prev_snap:
            prev_sessions = prev_snap["total_sessions"] or 0
            if prev_sessions:
                change = ((snap["total_sessions"] - prev_sessions) / prev_sessions) * 100
                data["analytics"]["wow_sessions_change_pct"] = round(change, 1)

    # Keyword rankings — top 20 with current + previous position
    kws = conn.execute(
        "SELECT keyword, position, previous_position, search_volume, difficulty, url "
        "FROM keywords WHERE company_id = ? AND domain = ? "
        "ORDER BY search_volume DESC LIMIT 20",
        (company_id, domain),
    ).fetchall()
    if kws:
        data["keywords"] = [dict(k) for k in kws]

    # Decaying pages
    decayed = conn.execute(
        "SELECT path, current_sessions, four_week_avg, decay_pct FROM content_scores "
        "WHERE company_id = ? AND domain = ? AND flagged = 1 "
        "ORDER BY decay_pct DESC LIMIT 10",
        (company_id, domain),
    ).fetchall()
    if decayed:
        data["decaying_pages"] = [dict(d) for d in decayed]

    # Search Console (if available)
    try:
        gsc = conn.execute(
            "SELECT * FROM gsc_snapshots WHERE company_id = ? AND domain = ? "
            "ORDER BY created_at DESC LIMIT 1",
            (company_id, domain),
        ).fetchone()
        if gsc:
            data["search_console"] = {
                "total_clicks": gsc["total_clicks"],
                "total_impressions": gsc["total_impressions"],
                "avg_ctr": gsc["avg_ctr"],
                "avg_position": gsc["avg_position"],
                "top_queries": json.loads(gsc["top_queries"] or "[]")[:15],
                "top_pages": json.loads(gsc["top_pages"] or "[]")[:10],
            }
    except Exception:
        pass

    # Backlinks (if available)
    try:
        bl = conn.execute(
            "SELECT * FROM backlinks_snapshots WHERE company_id = ? AND domain = ? "
            "ORDER BY created_at DESC LIMIT 1",
            (company_id, domain),
        ).fetchone()
        if bl:
            data["backlinks"] = {
                "total_backlinks": bl["total_backlinks"],
                "total_referring_domains": bl["total_referring_domains"],
                "new_backlinks": json.loads(bl["new_backlinks"] or "[]")[:10],
                "lost_backlinks": json.loads(bl["lost_backlinks"] or "[]")[:10],
                "domain_rank": bl["domain_rank"],
            }
    except Exception:
        pass

    # Conversion insights (if available)
    try:
        ci = conn.execute(
            "SELECT path, sessions, conversions, conversion_rate, flagged FROM conversion_insights "
            "WHERE company_id = ? AND domain = ? AND flagged = 1 "
            "ORDER BY sessions DESC LIMIT 10",
            (company_id, domain),
        ).fetchall()
        if ci:
            data["low_converting_pages"] = [dict(c) for c in ci]
    except Exception:
        pass

    # Web vitals (if available)
    try:
        wv = conn.execute(
            "SELECT url, strategy, lcp_ms, inp_ms, cls, performance_score FROM web_vitals_snapshots "
            "WHERE company_id = ? AND domain = ? ORDER BY audited_at DESC LIMIT 10",
            (company_id, domain),
        ).fetchall()
        if wv:
            data["web_vitals"] = [dict(w) for w in wv]
    except Exception:
        pass

    # Form check failures (if available)
    try:
        fc = conn.execute(
            "SELECT form_name, ran_at, success, status_code, error FROM form_check_runs "
            "WHERE company_id = ? AND domain = ? ORDER BY ran_at DESC LIMIT 5",
            (company_id, domain),
        ).fetchall()
        if fc:
            data["form_checks"] = [dict(f) for f in fc]
    except Exception:
        pass

    return data


async def generate_action_plan(domain: str, company_id: str = "") -> list[dict]:
    """Generate a prioritized list of actions for a site.

    Returns a list of action dicts. Persists them to the actions table
    scoped to the given company_id.
    """
    site_data = _collect_site_data(domain, company_id)

    has_data = any(k != "domain" and k != "generated_at" for k in site_data.keys())
    if not has_data:
        log.info("No SEO data yet for %s (company=%s), skipping action plan",
                 domain, company_id or "-")
        return []

    user_prompt = f"""Site: {domain}

Data:
```json
{json.dumps(site_data, indent=2, default=str)}
```

Generate 3-5 prioritized actions for this week. Return only a JSON array."""

    response = await ask_llm(
        user_prompt,
        system=ACTION_SYSTEM_PROMPT,
        max_tokens=2048,
        json_mode=True,
    ) or ""

    try:
        # Strip markdown code fences if present
        cleaned = response.strip()
        if cleaned.startswith("```"):
            cleaned = cleaned.split("```", 2)[1]
            if cleaned.startswith("json"):
                cleaned = cleaned[4:]
            cleaned = cleaned.rsplit("```", 1)[0].strip()
        actions = json.loads(cleaned)
        if not isinstance(actions, list):
            log.warning("Action plan response is not a list: %s", type(actions))
            return []
    except Exception as e:
        log.error("Failed to parse action plan JSON: %s\nResponse: %s", e, response[:500])
        return []

    # Persist actions
    conn = get_plugin_db(PLUGIN_NAME)
    saved = []
    for action in actions:
        if not isinstance(action, dict) or not action.get("title"):
            continue
        action_id = _new_action_id()
        baseline = _capture_baseline(
            domain, action.get("target_metric"), action.get("target_ref"), company_id,
        )
        conn.execute(
            "INSERT INTO actions (id, company_id, domain, title, why, how_, expected_impact, "
            "target_metric, target_ref, priority, status, baseline_value) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                action_id, company_id, domain,
                action.get("title", "")[:200],
                action.get("why", ""),
                action.get("how", ""),
                action.get("expected_impact", ""),
                action.get("target_metric", "none"),
                action.get("target_ref", ""),
                int(action.get("priority", 3)),
                "pending",
                baseline,
            ),
        )
        action["id"] = action_id
        saved.append(action)
    conn.commit()

    log.info("Generated %d actions for %s (company=%s)", len(saved), domain, company_id or "-")
    return saved


def _new_action_id() -> str:
    import secrets
    return secrets.token_urlsafe(8)


def _capture_baseline(
    domain: str, target_metric: str | None, target_ref: str | None,
    company_id: str = "",
) -> str:
    """Snapshot the current value of the target metric so we can measure impact later."""
    if not target_metric or target_metric == "none":
        return ""
    conn = get_plugin_db(PLUGIN_NAME)
    try:
        if target_metric == "sessions" and target_ref:
            snap = conn.execute(
                "SELECT top_pages FROM analytics_snapshots "
                "WHERE company_id = ? AND domain = ? "
                "ORDER BY created_at DESC LIMIT 1",
                (company_id, domain),
            ).fetchone()
            if snap:
                pages = json.loads(snap["top_pages"] or "[]")
                for p in pages:
                    if p.get("path") == target_ref:
                        return str(p.get("sessions", 0))
        elif target_metric == "conversions" and target_ref:
            snap = conn.execute(
                "SELECT top_pages FROM analytics_snapshots "
                "WHERE company_id = ? AND domain = ? "
                "ORDER BY created_at DESC LIMIT 1",
                (company_id, domain),
            ).fetchone()
            if snap:
                pages = json.loads(snap["top_pages"] or "[]")
                for p in pages:
                    if p.get("path") == target_ref:
                        return str(p.get("conversions", 0))
        elif target_metric == "position" and target_ref:
            kw = conn.execute(
                "SELECT position FROM keywords "
                "WHERE company_id = ? AND domain = ? AND keyword = ? LIMIT 1",
                (company_id, domain, target_ref),
            ).fetchone()
            if kw and kw["position"]:
                return str(kw["position"])
        elif target_metric == "backlinks":
            bl = conn.execute(
                "SELECT total_backlinks FROM backlinks_snapshots "
                "WHERE company_id = ? AND domain = ? "
                "ORDER BY created_at DESC LIMIT 1",
                (company_id, domain),
            ).fetchone()
            if bl:
                return str(bl["total_backlinks"])
    except Exception as e:
        log.debug("Baseline capture failed: %s", e)
    return ""


async def measure_action_impact(action_id: str) -> dict | None:
    """Measure the impact of a completed action by comparing to baseline.

    Reads company_id from the action row; all scoped lookups use that.
    """
    conn = get_plugin_db(PLUGIN_NAME)
    action = conn.execute("SELECT * FROM actions WHERE id = ?", (action_id,)).fetchone()
    if not action:
        return None

    baseline = action["baseline_value"]
    if not baseline:
        return None

    try:
        baseline_num = float(baseline)
    except ValueError:
        return None

    action_company = action["company_id"] if "company_id" in action.keys() else ""
    current = _capture_baseline(
        action["domain"], action["target_metric"], action["target_ref"], action_company,
    )
    if not current:
        return None
    try:
        current_num = float(current)
    except ValueError:
        return None

    if baseline_num == 0:
        change_pct = None
    else:
        # For position, lower is better — invert the sign
        if action["target_metric"] == "position":
            change_pct = ((baseline_num - current_num) / baseline_num) * 100
        else:
            change_pct = ((current_num - baseline_num) / baseline_num) * 100

    impact = {
        "baseline": baseline_num,
        "current": current_num,
        "change_pct": round(change_pct, 1) if change_pct is not None else None,
    }

    conn.execute(
        "UPDATE actions SET status = 'measured', actual_impact = ?, measured_at = datetime('now') "
        "WHERE company_id = ? AND id = ?",
        (json.dumps(impact), action_company, action_id),
    )
    conn.commit()
    return impact


def format_action_plan(actions: list[dict]) -> str:
    """Format an action list for WhatsApp display."""
    if not actions:
        return "_No actions generated. Run `/seo pull` first to gather data._"

    lines = ["*This Week's Actions:*", ""]
    for i, a in enumerate(actions, 1):
        priority = a.get("priority", 3)
        marker = "🔴" if priority == 1 else "🟠" if priority == 2 else "🟡"
        title = a.get("title", "Untitled")
        action_id = a.get("id", "")
        lines.append(f"{marker} *{i}. {title}* `[{action_id}]`")
        if a.get("why"):
            lines.append(f"   _Why:_ {a['why']}")
        if a.get("how"):
            lines.append(f"   _How:_ {a['how']}")
        if a.get("expected_impact"):
            lines.append(f"   _Impact:_ {a['expected_impact']}")
        lines.append("")

    lines.append("_Mark done with:_ `/seo actions done <id>`")
    return "\n".join(lines)
