"""
Form submission checker.

Sends a real form submission with a `_bot_check: true` marker (clients add a
server-side filter to ignore these). Looks for a success indicator in the response.
"""

import httpx

from cupbots.helpers.logger import get_logger

log = get_logger("seo.form_check")


async def run_form_check(form_def: dict) -> dict:
    """Execute a form check and return the result.

    form_def keys:
      name (str)
      url (str)
      method (str, default 'POST')
      payload (dict, optional)
      headers (dict, optional)
      success_text (str, optional) — substring to look for in response
      success_status (int, optional, default 200) — required HTTP status

    Returns:
      {success, status_code, response_excerpt, error}
    """
    name = form_def.get("name", "unnamed")
    url = form_def.get("url")
    method = (form_def.get("method") or "POST").upper()
    payload = form_def.get("payload") or {}
    headers = form_def.get("headers") or {}
    success_text = form_def.get("success_text")
    success_status = form_def.get("success_status", 200)

    if not url:
        return {"success": False, "status_code": 0, "response_excerpt": "", "error": "Missing url"}

    # Always include the bot marker so the client's server can filter
    if isinstance(payload, dict):
        payload = {**payload, "_bot_check": True}

    try:
        async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
            if method == "GET":
                resp = await client.get(url, params=payload, headers=headers)
            else:
                # Try form-encoded; if the server expects JSON the user can override via headers
                if headers.get("Content-Type") == "application/json":
                    resp = await client.post(url, json=payload, headers=headers)
                else:
                    resp = await client.post(url, data=payload, headers=headers)

        status = resp.status_code
        body = resp.text or ""
        excerpt = body[:500]

        # Check success criteria
        status_ok = status == success_status
        text_ok = (success_text in body) if success_text else True
        success = status_ok and text_ok

        error = ""
        if not status_ok:
            error = f"Expected HTTP {success_status}, got {status}"
        elif not text_ok:
            error = f"Success text '{success_text}' not found in response"

        return {
            "success": success,
            "status_code": status,
            "response_excerpt": excerpt,
            "error": error,
        }

    except httpx.TimeoutException:
        return {"success": False, "status_code": 0, "response_excerpt": "", "error": "Request timed out"}
    except Exception as e:
        return {"success": False, "status_code": 0, "response_excerpt": "", "error": str(e)}
