"""
Gmail API helper for mailwatch — direct REST via httpx, no SDK.

Pure functions, no global state. The caller (mailwatch.py) handles persisting
refreshed tokens back to config.yaml.

All async; uses httpx.AsyncClient. Token refresh is lazy: ensure_access_token()
checks expiry and refreshes if needed, returning the new token dict so the caller
can persist it.
"""

import base64
import email.message
import email.utils
import time

import httpx

GMAIL_API_BASE = "https://gmail.googleapis.com/gmail/v1"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"

# gmail.compose covers BOTH creating and sending drafts — no need for gmail.send
GMAIL_SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.compose",
]

_REFRESH_LEEWAY = 60  # refresh if token expires within this many seconds


async def ensure_access_token(
    tokens: dict,
    client_id: str,
    client_secret: str,
) -> tuple[str, dict | None]:
    """Return a valid access_token, refreshing via refresh_token if needed.

    Returns (access_token, refreshed_dict_or_None). If refreshed_dict is non-None,
    the caller MUST persist it back to config so the new expiry is durable.
    """
    access_token = tokens.get("access_token", "")
    expiry = int(tokens.get("expiry", 0) or 0)
    now = int(time.time())

    if access_token and expiry and expiry - _REFRESH_LEEWAY > now:
        return access_token, None

    refresh_token = tokens.get("refresh_token", "")
    if not refresh_token:
        raise RuntimeError("Gmail tokens missing refresh_token — re-run /mailwatch account connect")
    if not client_id or not client_secret:
        raise RuntimeError("google_client_id / google_client_secret not configured")

    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.post(GOOGLE_TOKEN_URL, data={
            "client_id": client_id,
            "client_secret": client_secret,
            "refresh_token": refresh_token,
            "grant_type": "refresh_token",
        })
        resp.raise_for_status()
        data = resp.json()

    new_access = data.get("access_token", "")
    expires_in = int(data.get("expires_in", 3600))
    refreshed = {
        "access_token": new_access,
        "refresh_token": refresh_token,  # Google doesn't return a new one on refresh
        "expiry": int(time.time()) + expires_in - _REFRESH_LEEWAY,
        "token_type": data.get("token_type", "Bearer"),
        "scope": data.get("scope", tokens.get("scope", "")),
    }
    return new_access, refreshed


def _auth_headers(access_token: str) -> dict:
    return {"Authorization": f"Bearer {access_token}"}


async def list_unread_ids(access_token: str, max_results: int = 25) -> list[str]:
    """Return Gmail message ids for unread messages in INBOX (newest first)."""
    url = f"{GMAIL_API_BASE}/users/me/messages"
    params = {"q": "is:unread in:inbox", "maxResults": max_results}
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(url, headers=_auth_headers(access_token), params=params)
        resp.raise_for_status()
        data = resp.json()
    return [m["id"] for m in data.get("messages", [])]


async def fetch_message(access_token: str, message_id: str) -> dict:
    """Fetch a Gmail message in raw RFC822 form.

    Returns {"raw": bytes, "thread_id": str}. raw is ready to feed into _parse_email.
    """
    url = f"{GMAIL_API_BASE}/users/me/messages/{message_id}"
    params = {"format": "raw"}
    async with httpx.AsyncClient(timeout=20) as client:
        resp = await client.get(url, headers=_auth_headers(access_token), params=params)
        resp.raise_for_status()
        data = resp.json()
    raw_b64 = data.get("raw", "")
    raw_bytes = base64.urlsafe_b64decode(raw_b64.encode("ascii")) if raw_b64 else b""
    return {"raw": raw_bytes, "thread_id": data.get("threadId", "")}


async def mark_read(access_token: str, message_id: str) -> None:
    """Remove the UNREAD label from a message."""
    url = f"{GMAIL_API_BASE}/users/me/messages/{message_id}/modify"
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.post(
            url,
            headers={**_auth_headers(access_token), "Content-Type": "application/json"},
            json={"removeLabelIds": ["UNREAD"]},
        )
        resp.raise_for_status()


async def create_draft(
    access_token: str,
    rfc822_bytes: bytes,
    thread_id: str | None = None,
) -> dict:
    """Create a draft in the user's Gmail. Returns {'id': draft_id, 'message_id': msg_id}."""
    url = f"{GMAIL_API_BASE}/users/me/drafts"
    raw_b64 = base64.urlsafe_b64encode(rfc822_bytes).decode("ascii")
    message: dict = {"raw": raw_b64}
    if thread_id:
        message["threadId"] = thread_id
    payload = {"message": message}
    async with httpx.AsyncClient(timeout=20) as client:
        resp = await client.post(
            url,
            headers={**_auth_headers(access_token), "Content-Type": "application/json"},
            json=payload,
        )
        resp.raise_for_status()
        data = resp.json()
    return {
        "id": data.get("id", ""),
        "message_id": (data.get("message") or {}).get("id", ""),
    }


async def send_draft(access_token: str, draft_id: str) -> dict:
    """Send a previously created draft. Returns {'id': msg_id, 'thread_id': ...}."""
    url = f"{GMAIL_API_BASE}/users/me/drafts/send"
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            url,
            headers={**_auth_headers(access_token), "Content-Type": "application/json"},
            json={"id": draft_id},
        )
        resp.raise_for_status()
        data = resp.json()
    return {"id": data.get("id", ""), "thread_id": data.get("threadId", "")}


def build_reply_rfc822(
    from_addr: str,
    to_addr: str,
    subject: str,
    body: str,
    in_reply_to: str = "",
    references: str = "",
) -> bytes:
    """Build a minimal RFC822 reply message as raw bytes (UTF-8)."""
    msg = email.message.EmailMessage()
    msg["From"] = from_addr
    msg["To"] = to_addr
    msg["Subject"] = subject
    msg["Date"] = email.utils.formatdate(localtime=True)
    msg["Message-ID"] = email.utils.make_msgid()
    if in_reply_to:
        msg["In-Reply-To"] = in_reply_to
    if references:
        msg["References"] = references
    msg.set_content(body)
    return bytes(msg)
