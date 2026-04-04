"""
Stripe — Manage subscriptions and community access.

Commands (works in any topic):
  /stripe status          — Show subscriber stats
  /stripe subs            — List recent subscribers
  /stripe set <key> <val> — Configure (community_jid, welcome_msg)
  /stripe test <phone>    — Test invite flow for a phone number
  /stripe webhook         — Show your Stripe webhook URL

Flow:
  1. Customer completes Stripe Checkout on your landing page
  2. Stripe webhook fires to your hosted endpoint (managed by launchpad service)
  3. Customer gets a WhatsApp DM with your community invite link
"""

import os

import httpx
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

from cupbots.helpers.logger import get_logger

log = get_logger("stripe")

WA_API_URL = os.environ.get("WA_API_URL", "http://127.0.0.1:3100")


def _get_api():
    """Return (api_url, api_key) for the launchpad service."""
    api_url = os.environ.get("LAUNCHPAD_API_URL", "").rstrip("/")
    api_key = os.environ.get("LAUNCHPAD_API_KEY", "")
    if not api_url:
        raise ValueError("LAUNCHPAD_API_URL not set")
    return api_url, api_key


# ---------------------------------------------------------------------------
# API helpers
# ---------------------------------------------------------------------------

async def _api_get(path: str) -> dict:
    api_url, api_key = _get_api()
    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.get(
            f"{api_url}{path}",
            headers={"Authorization": f"Bearer {api_key}"},
        )
        r.raise_for_status()
        return r.json()


async def _api_post(path: str, payload: dict) -> dict:
    api_url, api_key = _get_api()
    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.post(
            f"{api_url}{path}",
            headers={"Authorization": f"Bearer {api_key}"},
            json=payload,
        )
        r.raise_for_status()
        return r.json()


# ---------------------------------------------------------------------------
# WhatsApp invite (for /stripe test)
# ---------------------------------------------------------------------------

async def _get_invite_link(group_jid: str) -> str | None:
    async with httpx.AsyncClient(timeout=10) as client:
        try:
            r = await client.get(f"{WA_API_URL}/group/invite/{group_jid}")
            r.raise_for_status()
            return r.json().get("link")
        except Exception as e:
            log.error("Failed to get invite link for %s: %s", group_jid, e)
            return None


async def _send_wa_message(phone: str, text: str):
    phone = phone.lstrip("+").replace(" ", "").replace("-", "")
    jid = f"{phone}@s.whatsapp.net"
    async with httpx.AsyncClient(timeout=10) as client:
        r = await client.post(f"{WA_API_URL}/send", json={"chatId": jid, "text": text})
        r.raise_for_status()


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

async def _cmd_status(tenant_id: str) -> str:
    try:
        data = await _api_get(f"/stripe/subs/{tenant_id}")
    except httpx.HTTPStatusError:
        return "No subscription data yet."
    except ValueError as e:
        return str(e)

    return (
        f"Stripe Subscribers\n\n"
        f"Active: {data.get('active', 0)}\n"
        f"Total: {data.get('total', 0)}\n"
        f"Invites sent: {data.get('invitesSent', 0)}"
    )


async def _cmd_subs(tenant_id: str) -> str:
    try:
        data = await _api_get(f"/stripe/subs/{tenant_id}")
    except httpx.HTTPStatusError:
        return "No subscription data yet."
    except ValueError as e:
        return str(e)

    subs = data.get("subscriptions", [])
    if not subs:
        return "No subscribers yet."

    lines = ["Recent subscribers:\n"]
    for s in subs[:20]:
        name = s.get("name") or s.get("email") or "unknown"
        status_icon = "\u2705" if s.get("status") == "active" else "\u274c"
        invite_icon = "\ud83d\udce8" if s.get("inviteSent") else "\u23f3"
        lines.append(f"{status_icon} {name} \u2014 {s.get('phone') or 'no phone'} {invite_icon}")

    return "\n".join(lines)


async def _cmd_set(args: list[str], tenant_id: str) -> str:
    valid_keys = {"community_jid", "welcome_msg"}
    if len(args) < 2:
        return f"Usage: /stripe set <key> <value>\nKeys: {', '.join(sorted(valid_keys))}"

    key = args[0].lower()
    if key not in valid_keys:
        return f"Unknown key: {key}. Valid: {', '.join(sorted(valid_keys))}"

    value = " ".join(args[1:])
    try:
        await _api_post(f"/stripe/config/{tenant_id}", {key: value})
        return f"Set {key} = {value}"
    except ValueError as e:
        return str(e)


async def _cmd_test(phone: str, tenant_id: str) -> str:
    # Get config from API
    try:
        # Use a quick config read — the subs endpoint returns enough info
        # but we need the community_jid from the config endpoint
        # For test, just try to get invite link using env var or ask user
        community_jid = os.environ.get("STRIPE_COMMUNITY_JID", "")
        if not community_jid:
            return "Set STRIPE_COMMUNITY_JID env var or /stripe set community_jid <jid> first"

        invite_link = await _get_invite_link(community_jid)
        if not invite_link:
            return f"Failed to get invite link for {community_jid}"

        await _send_wa_message(phone, f"Welcome! Here's your invite:\n\n{invite_link}")
        return f"Test invite sent to {phone}"
    except Exception as e:
        return f"Failed: {e}"


async def _cmd_webhook(tenant_id: str) -> str:
    try:
        api_url, _ = _get_api()
    except ValueError as e:
        return str(e)

    url = f"{api_url}/stripe/webhook/{tenant_id}"
    return (
        f"Your Stripe webhook URL:\n\n{url}\n\n"
        f"Add this in Stripe Dashboard > Developers > Webhooks\n"
        f"Events to send: checkout.session.completed, customer.subscription.deleted\n\n"
        f"Make sure to enable 'Collect phone number' in your Checkout session."
    )


# ---------------------------------------------------------------------------
# Cross-platform handler
# ---------------------------------------------------------------------------

async def handle_command(msg, reply) -> bool:
    if msg.command != "stripe":
        return False

    tenant_id = msg.company_id or "default"
    args = msg.args

    try:
        if not args or args[0] == "status":
            result = await _cmd_status(tenant_id)
        elif args[0] == "subs":
            result = await _cmd_subs(tenant_id)
        elif args[0] == "set":
            result = await _cmd_set(args[1:], tenant_id)
        elif args[0] == "test":
            if len(args) < 2:
                result = "Usage: /stripe test <phone>"
            else:
                result = await _cmd_test(args[1], tenant_id)
        elif args[0] == "webhook":
            result = await _cmd_webhook(tenant_id)
        else:
            result = "Unknown subcommand. Try /stripe --help"
    except Exception as e:
        log.error("Stripe command error: %s", e)
        result = f"Error: {e}"

    await reply.reply_text(result)
    return True


# ---------------------------------------------------------------------------
# Telegram handlers
# ---------------------------------------------------------------------------

async def cmd_stripe(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message:
        return
    from cupbots.helpers.channel import TelegramReplyContext, IncomingMessage, parse_command

    chat_id = str(update.effective_chat.id)
    text = update.message.text or ""
    command, args = parse_command(text)

    from cupbots.helpers.db import get_group_config
    group_cfg = await get_group_config(chat_id)

    msg = IncomingMessage(
        platform="telegram",
        chat_id=chat_id,
        sender_id=str(update.effective_user.id) if update.effective_user else "",
        sender_name=update.effective_user.first_name if update.effective_user else "",
        text=text,
        command=command,
        args=args,
        company_id=group_cfg.get("company_id", "default") if group_cfg else "default",
        group_config=group_cfg,
    )
    reply_ctx = TelegramReplyContext(update)
    await handle_command(msg, reply_ctx)


def register(app: Application):
    app.add_handler(CommandHandler("stripe", cmd_stripe))
