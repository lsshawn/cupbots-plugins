"""
Uptime Kuma client wrapper.

Uses the `uptime-kuma-api` pip package (lucasheld/uptime-kuma-api) over Socket.io.
Lazy-connect pattern: login on each call, don't hold long-lived sockets.
"""

from urllib.parse import urlparse

from cupbots.helpers.logger import get_logger

log = get_logger("seo.uptime_kuma")


def _get_client(url: str, username: str, password: str):
    """Create + login an UptimeKumaApi client. Caller must call .disconnect()."""
    try:
        from uptime_kuma_api import UptimeKumaApi
    except ImportError:
        raise RuntimeError("uptime-kuma-api package not installed. Add to plugin.json pip_dependencies.")

    api = UptimeKumaApi(url)
    api.login(username, password)
    return api


def get_monitors_for_domain(uk_config: dict, domain: str) -> list[dict]:
    """Find all Uptime Kuma monitors whose URL/hostname contains the domain.

    uk_config: {url, username, password}
    domain: e.g. "example.com"

    Returns list of monitor dicts (id, name, url, type, active).
    """
    if not uk_config or not uk_config.get("url"):
        return []

    api = _get_client(uk_config["url"], uk_config["username"], uk_config["password"])
    try:
        monitors = api.get_monitors() or []
    finally:
        try:
            api.disconnect()
        except Exception:
            pass

    matched = []
    domain_lower = domain.lower().lstrip("www.")
    for m in monitors:
        # Monitor may have url, hostname, or name
        url = (m.get("url") or "").lower()
        hostname = (m.get("hostname") or "").lower()
        name = (m.get("name") or "").lower()

        if domain_lower in url or domain_lower in hostname or domain_lower in name:
            matched.append({
                "id": m.get("id"),
                "name": m.get("name"),
                "url": m.get("url") or m.get("hostname") or "",
                "type": m.get("type"),
                "active": m.get("active", True),
            })

    return matched


def get_uptime_summary(uk_config: dict, monitor_id: int) -> dict:
    """Return uptime summary for a monitor: 24h%, 7d%, 30d%, last status."""
    if not uk_config or not uk_config.get("url"):
        return {}

    api = _get_client(uk_config["url"], uk_config["username"], uk_config["password"])
    try:
        # The uptime_kuma_api library exposes get_monitor_beats and get_monitor
        try:
            beats_24h = api.get_monitor_beats(monitor_id, 24) or []
        except Exception:
            beats_24h = []

        try:
            beats_7d = api.get_monitor_beats(monitor_id, 24 * 7) or []
        except Exception:
            beats_7d = []

        try:
            monitor = api.get_monitor(monitor_id) or {}
        except Exception:
            monitor = {}
    finally:
        try:
            api.disconnect()
        except Exception:
            pass

    def _uptime_pct(beats):
        if not beats:
            return None
        up = sum(1 for b in beats if b.get("status") == 1)
        return round((up / len(beats)) * 100, 2)

    last_status = "unknown"
    last_ping = None
    if beats_24h:
        last = beats_24h[-1]
        last_status = "up" if last.get("status") == 1 else "down"
        last_ping = last.get("ping")

    return {
        "monitor_id": monitor_id,
        "name": monitor.get("name", ""),
        "uptime_24h": _uptime_pct(beats_24h),
        "uptime_7d": _uptime_pct(beats_7d),
        "last_status": last_status,
        "last_ping_ms": last_ping,
        "url": monitor.get("url") or monitor.get("hostname") or "",
    }
