"""
Stripe — Manage subscriptions and community access.

Commands (works in any topic):
  /subscription           — View your subscription & manage billing (customer portal)
  /stripe status          — Show subscriber stats (admin)
  /stripe subs            — List recent subscribers (admin)
  /stripe community       — Show community invite config (admin)
  /stripe set <key> <val> — Configure (community_jid, welcome_msg) (admin)
  /stripe test <phone>    — Test invite flow for a phone number (admin)
  /stripe webhook         — Show your Stripe webhook URL (admin)

Flow:
  1. Customer completes Stripe Checkout on your landing page
  2. Stripe webhook fires → stores subscription, sends welcome DM
  3. If community auto-add is on, customer is added to the WhatsApp group

Community config (config.yaml):
  stripe.communities.<plan>.groups — list of {jid, max_members}
  stripe.communities.<plan>.welcome — message sent in group after add
"""

import os

import httpx
import stripe as stripe_mod
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

from cupbots.helpers.db import get_fw_db
from cupbots.helpers.logger import get_logger
from cupbots.config import get_config

log = get_logger("stripe")

WA_API_URL = os.environ.get("WA_API_URL", "http://127.0.0.1:3100")


def _get_api():
    """Return (api_url, api_key) for the launchpad service."""
    from cupbots.helpers.db import resolve_plugin_setting
    api_url = (resolve_plugin_setting("stripe", "launchpad_api_url") or "").rstrip("/")
    api_key = resolve_plugin_setting("stripe", "launchpad_api_key") or ""
    if not api_url:
        raise ValueError("Set launchpad_api_url in plugin_settings.stripe in config.yaml")
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
        # Get first community group JID from stripe config
        stripe_cfg = get_config().get("stripe", {})
        communities = stripe_cfg.get("communities", {})
        community_jid = ""
        for _plan, _pcfg in communities.items():
            for g in _pcfg.get("groups", []):
                community_jid = g.get("jid", "")
                if community_jid:
                    break
            if community_jid:
                break
        if not community_jid:
            return "No community groups configured in stripe.communities in config.yaml"

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


async def _cmd_community(args: list[str]) -> str:
    """Show community config, or enable/disable approval mode."""
    cfg = get_config()
    communities = cfg.get("stripe", {}).get("communities", {})

    if not communities:
        return "No communities configured in config.yaml"

    # /stripe community gate on|off — toggle approval mode on all community groups
    if args and args[0] == "gate":
        mode = args[1] if len(args) > 1 else ""
        if mode not in ("on", "off"):
            return "Usage: /stripe community gate on|off"
        async with httpx.AsyncClient(timeout=10) as client:
            for plan, community in communities.items():
                for g in community.get("groups", []):
                    jid = g.get("jid")
                    if not jid:
                        continue
                    try:
                        r = await client.post(
                            f"{WA_API_URL}/group/approval-mode",
                            json={"groupId": jid, "mode": mode},
                        )
                        r.raise_for_status()
                    except Exception as e:
                        return f"Failed for {jid}: {e}"
        return f"Join approval mode: {mode} (all community groups)"

    lines = ["Community config:\n"]
    for plan, community in communities.items():
        groups = community.get("groups", [])
        lines.append(f"Plan: {plan}")
        for g in groups:
            lines.append(f"  Group: {g.get('jid', '?')} (max {g.get('max_members', '?')})")
        if community.get("welcome"):
            lines.append(f"  Welcome: {community['welcome'][:80]}...")
        if community.get("gate_message"):
            lines.append(f"  Gate msg: {community['gate_message'][:80]}...")
        lines.append("")

    lines.append("Commands:")
    lines.append("  /stripe community gate on  — require approval to join (bot auto-approves subscribers)")
    lines.append("  /stripe community gate off — anyone can join freely")
    return "\n".join(lines)


def _get_stripe_key():
    """Get the active Stripe secret key based on mode from config.yaml."""
    cfg = get_config()
    stripe_cfg = cfg.get("stripe", {})
    mode = stripe_cfg.get("mode", "live")
    return stripe_cfg.get(mode, {}).get("secret_key", "")


def _find_subscription_by_phone(phone: str) -> dict | None:
    """Look up an active subscription by phone number."""
    phone = phone.lstrip("+").replace(" ", "").replace("-", "")
    # Strip @s.whatsapp.net suffix if present
    if "@" in phone:
        phone = phone.split("@")[0]
    conn = get_fw_db()
    row = conn.execute(
        "SELECT * FROM subscriptions WHERE phone = ? AND status = 'active' ORDER BY created_at DESC LIMIT 1",
        (phone,),
    ).fetchone()
    return dict(row) if row else None


async def _cmd_subscription(sender_id: str) -> str:
    """Generate a Stripe customer portal link for the caller."""
    phone = sender_id.split("@")[0] if "@" in sender_id else sender_id
    sub = _find_subscription_by_phone(phone)
    if not sub:
        return "No active subscription found for your number."

    api_key = _get_stripe_key()
    if not api_key:
        return "Stripe is not configured. Contact support."

    stripe_mod.api_key = api_key
    try:
        session = stripe_mod.billing_portal.Session.create(
            customer=sub["stripe_customer_id"],
            return_url="https://cupbots.com",
        )
        plan = sub.get("plan", "").replace("_", " ").title()
        return (
            f"Your subscription: *{plan}* (active)\n\n"
            f"Manage your plan, update payment method, or cancel:\n{session.url}"
        )
    except Exception as e:
        log.error("Failed to create portal session: %s", e)
        return f"Something went wrong. Contact support.\n\nError: {e}"


# ---------------------------------------------------------------------------
# Cross-platform handler
# ---------------------------------------------------------------------------

async def _cmd_community_join(sender_id: str) -> str:
    """Manually add a subscriber to their community group."""
    phone = sender_id.split("@")[0] if "@" in sender_id else sender_id
    sub = _find_subscription_by_phone(phone)
    if not sub:
        return "No active subscription found for your number."

    plan = sub.get("plan", "")
    if not plan:
        return "Your subscription doesn't have a plan linked to a community."

    wa_jid = f"{phone}@s.whatsapp.net"
    cfg = get_config()
    communities = cfg.get("stripe", {}).get("communities", {})
    community_cfg = communities.get(plan)
    if not community_cfg:
        return "No community configured for your plan."

    groups = community_cfg.get("groups", [])
    invite_msg_tpl = community_cfg.get("invite_to_community_message", "")

    async with httpx.AsyncClient(timeout=10) as client:
        for group in groups:
            group_jid = group.get("jid")
            if not group_jid:
                continue

            # Send invite link
            try:
                r = await client.get(f"{WA_API_URL}/group/invite/{group_jid}")
                r.raise_for_status()
                invite_link = r.json().get("link", "")
                if invite_link:
                    if invite_msg_tpl:
                        return invite_msg_tpl.replace("{invite_link}", invite_link)
                    return f"Join here:\n{invite_link}"
            except Exception:
                continue

    return "All community groups are currently full. Please contact support."


async def handle_command(msg, reply) -> bool:
    if msg.command == "subscription":
        result = await _cmd_subscription(msg.sender_id)
        await reply.reply_text(result)
        return True

    if msg.command == "community":
        result = await _cmd_community_join(msg.sender_id)
        await reply.reply_text(result)
        return True

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
        elif args[0] == "community":
            result = await _cmd_community(args[1:])
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
