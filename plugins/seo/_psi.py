"""
PageSpeed Insights API client (free public API).

Returns Core Web Vitals (LCP, INP, CLS) for a URL on mobile + desktop.
"""

import httpx

from cupbots.helpers.logger import get_logger

log = get_logger("seo.psi")

PSI_URL = "https://www.googleapis.com/pagespeedonline/v5/runPagespeed"


def _extract_metric_ms(loading_exp: dict, metric_key: str) -> int | None:
    """Extract a millisecond value from loadingExperience metrics."""
    metrics = loading_exp.get("metrics") or {}
    metric = metrics.get(metric_key)
    if not metric:
        return None
    return metric.get("percentile")


def _extract_metric_score(loading_exp: dict, metric_key: str) -> float | None:
    """Extract a CLS-style fractional score (returned as int*100 in PSI)."""
    metrics = loading_exp.get("metrics") or {}
    metric = metrics.get(metric_key)
    if not metric:
        return None
    raw = metric.get("percentile")
    if raw is None:
        return None
    return raw / 100.0  # PSI returns CLS as int*100


async def pull_pagespeed(url: str, strategy: str = "mobile", api_key: str | None = None) -> dict:
    """Fetch PageSpeed Insights data for a URL.

    strategy: 'mobile' or 'desktop'
    Returns dict with lcp_ms, inp_ms, cls, fcp_ms, performance_score.
    """
    params = {"url": url, "strategy": strategy, "category": "PERFORMANCE"}
    if api_key:
        params["key"] = api_key

    async with httpx.AsyncClient(timeout=60) as client:
        resp = await client.get(PSI_URL, params=params)
        resp.raise_for_status()
        data = resp.json()

    loading_exp = data.get("loadingExperience") or {}
    lighthouse = data.get("lighthouseResult") or {}

    # Performance score (0-100)
    categories = lighthouse.get("categories") or {}
    perf_cat = categories.get("performance") or {}
    perf_score = perf_cat.get("score")
    if perf_score is not None:
        perf_score = int(perf_score * 100)

    return {
        "url": url,
        "strategy": strategy,
        "lcp_ms": _extract_metric_ms(loading_exp, "LARGEST_CONTENTFUL_PAINT_MS"),
        "inp_ms": _extract_metric_ms(loading_exp, "INTERACTION_TO_NEXT_PAINT")
                  or _extract_metric_ms(loading_exp, "EXPERIMENTAL_INTERACTION_TO_NEXT_PAINT"),
        "cls": _extract_metric_score(loading_exp, "CUMULATIVE_LAYOUT_SHIFT_SCORE"),
        "fcp_ms": _extract_metric_ms(loading_exp, "FIRST_CONTENTFUL_PAINT_MS"),
        "performance_score": perf_score,
    }


def assess_vitals(vitals: dict) -> str:
    """Return a one-word health assessment: good, needs-improvement, poor."""
    lcp = vitals.get("lcp_ms")
    inp = vitals.get("inp_ms")
    cls = vitals.get("cls")

    statuses = []
    if lcp is not None:
        statuses.append("good" if lcp <= 2500 else "needs-improvement" if lcp <= 4000 else "poor")
    if inp is not None:
        statuses.append("good" if inp <= 200 else "needs-improvement" if inp <= 500 else "poor")
    if cls is not None:
        statuses.append("good" if cls <= 0.1 else "needs-improvement" if cls <= 0.25 else "poor")

    if not statuses:
        return "unknown"
    if "poor" in statuses:
        return "poor"
    if "needs-improvement" in statuses:
        return "needs-improvement"
    return "good"
