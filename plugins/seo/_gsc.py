"""
Google Search Console API client.

Wraps the Search Console API via httpx (no SDK needed).
Used by the SEO plugin to pull search analytics and inspect URL indexing.

Auth: reuses GA4 OAuth tokens with the additional `webmasters.readonly` scope.
"""

from datetime import datetime, timedelta

import httpx

from cupbots.helpers.logger import get_logger

log = get_logger("seo.gsc")

SEARCH_ANALYTICS_URL = "https://www.googleapis.com/webmasters/v3/sites/{site}/searchAnalytics/query"
URL_INSPECTION_URL = "https://searchconsole.googleapis.com/v1/urlInspection/index:inspect"
TOKEN_REFRESH_URL = "https://oauth2.googleapis.com/token"


async def _refresh_if_needed(tokens: dict, client_id: str, client_secret: str) -> tuple[dict, bool]:
    """Refresh access token if expired. Returns (tokens_dict, was_refreshed)."""
    expiry = tokens.get("expiry")
    needs_refresh = False
    if expiry:
        try:
            exp_dt = datetime.fromisoformat(expiry)
            if exp_dt < datetime.now() + timedelta(minutes=2):
                needs_refresh = True
        except Exception:
            needs_refresh = True

    if not needs_refresh:
        return tokens, False

    refresh_token = tokens.get("refresh_token")
    if not refresh_token:
        log.warning("No refresh token available, cannot refresh GSC access token")
        return tokens, False

    async with httpx.AsyncClient(timeout=15) as http:
        resp = await http.post(TOKEN_REFRESH_URL, data={
            "client_id": client_id,
            "client_secret": client_secret,
            "refresh_token": refresh_token,
            "grant_type": "refresh_token",
        })
        resp.raise_for_status()
        new_data = resp.json()

    new_tokens = dict(tokens)
    new_tokens["access_token"] = new_data["access_token"]
    if "expires_in" in new_data:
        new_tokens["expiry"] = (datetime.now() + timedelta(seconds=new_data["expires_in"])).isoformat()
    return new_tokens, True


async def query_search_analytics(
    site_url: str,
    tokens: dict,
    client_id: str,
    client_secret: str,
    *,
    start_date: str | None = None,
    end_date: str | None = None,
    dimensions: list[str] | None = None,
    row_limit: int = 100,
) -> tuple[dict, dict | None]:
    """Query Search Console search analytics.

    Returns (response_dict, refreshed_tokens_or_None).
    Caller must persist refreshed tokens if returned.
    """
    tokens, was_refreshed = await _refresh_if_needed(tokens, client_id, client_secret)

    if not start_date:
        start_date = (datetime.now() - timedelta(days=7)).strftime("%Y-%m-%d")
    if not end_date:
        end_date = datetime.now().strftime("%Y-%m-%d")
    if not dimensions:
        dimensions = ["query"]

    body = {
        "startDate": start_date,
        "endDate": end_date,
        "dimensions": dimensions,
        "rowLimit": row_limit,
    }

    # site_url must be URL-encoded
    from urllib.parse import quote
    encoded_site = quote(site_url, safe="")
    url = SEARCH_ANALYTICS_URL.format(site=encoded_site)

    async with httpx.AsyncClient(timeout=30) as http:
        resp = await http.post(
            url,
            json=body,
            headers={"Authorization": f"Bearer {tokens['access_token']}"},
        )
        resp.raise_for_status()
        data = resp.json()

    return data, tokens if was_refreshed else None


async def pull_gsc_summary(
    site_url: str,
    tokens: dict,
    client_id: str,
    client_secret: str,
) -> tuple[dict, dict | None]:
    """Pull a normalized GSC summary for the last 7 days.

    Returns (summary_dict, refreshed_tokens_or_None).
    """
    # Top queries
    queries_resp, refreshed = await query_search_analytics(
        site_url, tokens, client_id, client_secret,
        dimensions=["query"], row_limit=50,
    )
    if refreshed:
        tokens = refreshed

    # Top pages
    pages_resp, refreshed2 = await query_search_analytics(
        site_url, tokens, client_id, client_secret,
        dimensions=["page"], row_limit=50,
    )
    if refreshed2:
        tokens = refreshed2

    top_queries = []
    total_clicks = 0
    total_impressions = 0
    total_position_weighted = 0.0

    for row in queries_resp.get("rows", []):
        keys = row.get("keys", [])
        clicks = row.get("clicks", 0)
        impressions = row.get("impressions", 0)
        ctr = row.get("ctr", 0)
        position = row.get("position", 0)

        top_queries.append({
            "query": keys[0] if keys else "",
            "clicks": clicks,
            "impressions": impressions,
            "ctr": round(ctr * 100, 2),
            "position": round(position, 1),
        })
        total_clicks += clicks
        total_impressions += impressions
        total_position_weighted += position * impressions

    top_pages = []
    for row in pages_resp.get("rows", []):
        keys = row.get("keys", [])
        top_pages.append({
            "page": keys[0] if keys else "",
            "clicks": row.get("clicks", 0),
            "impressions": row.get("impressions", 0),
            "ctr": round(row.get("ctr", 0) * 100, 2),
            "position": round(row.get("position", 0), 1),
        })

    avg_ctr = (total_clicks / total_impressions * 100) if total_impressions else 0
    avg_position = (total_position_weighted / total_impressions) if total_impressions else 0

    return {
        "total_clicks": total_clicks,
        "total_impressions": total_impressions,
        "avg_ctr": round(avg_ctr, 2),
        "avg_position": round(avg_position, 1),
        "top_queries": top_queries,
        "top_pages": top_pages,
    }, refreshed2 or refreshed


async def inspect_url(
    site_url: str,
    target_url: str,
    tokens: dict,
    client_id: str,
    client_secret: str,
) -> tuple[dict, dict | None]:
    """Inspect a URL via the URL Inspection API.

    Returns (inspection_result, refreshed_tokens_or_None).
    """
    tokens, was_refreshed = await _refresh_if_needed(tokens, client_id, client_secret)

    async with httpx.AsyncClient(timeout=30) as http:
        resp = await http.post(
            URL_INSPECTION_URL,
            json={"inspectionUrl": target_url, "siteUrl": site_url},
            headers={"Authorization": f"Bearer {tokens['access_token']}"},
        )
        resp.raise_for_status()
        return resp.json(), tokens if was_refreshed else None


def find_ctr_opportunities(top_queries: list[dict], min_impressions: int = 100) -> list[dict]:
    """Identify queries ranking 5-15 with high impressions but low CTR (quick wins)."""
    opportunities = []
    for q in top_queries:
        if q["impressions"] < min_impressions:
            continue
        if 5 <= q["position"] <= 15 and q["ctr"] < 5:
            opportunities.append(q)
    return sorted(opportunities, key=lambda x: x["impressions"], reverse=True)[:10]
