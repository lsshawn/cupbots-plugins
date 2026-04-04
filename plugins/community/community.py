"""
Community — All-in-one WhatsApp community management.

Commands:
  /community                    — Show dashboard
  /community leaderboard        — Top 10 by points
  /community points [phone]     — Check points
  /community award <phone> <pts> [reason] — Award points
  /community role list          — List roles
  /community role add <name>    — Create a role
  /community role assign <phone> <role> — Assign role
  /community role remove <phone> <role> — Remove role
  /community analytics [days]   — Activity report
  /community onboard            — Show onboarding config
  /community onboard set <key> <value> — Configure onboarding
  /community onboard preview    — Preview welcome DM
  /community onboard send <phone> — Send welcome DM manually
  /community onboard on|off     — Toggle onboarding
  /community mod                — Show moderation config
  /community mod filter add|remove|list — Manage keyword filters
  /community mod warn <phone> [reason] — Warn a user
  /community mod history <phone> — View warnings
  /community mod kick <phone>   — Kick user from group
  /community mod spam on|off    — Toggle spam detection
  /community mod spam limit <N> — Max messages per minute
  /community mod warn_limit <N> — Auto-kick after N warnings
  /community schedule add <time> <message> — Add recurring content
  /community schedule list      — List scheduled content
  /community schedule remove <id> — Remove scheduled content
  /community set <key> <value>  — Configure community settings

Setup keys:
  welcome     — Onboarding DM message
  intro       — Ask new members to introduce themselves (true/false)
  delay       — Seconds before sending welcome DM (0-300)
  points_msg  — Points per message (default: 1)
  points_hour — Max points per hour per user (default: 20)
  level_names — Comma-separated level names (default: Newcomer,Regular,Active,Star,Legend)

Examples:
  /community set welcome Hey! Welcome to our community.
  /community set points_msg 2
  /community leaderboard
  /community mod filter add crypto
  /community mod spam on
  /community schedule add daily 9am Good morning everyone!
"""

import json
import asyncio
import os
import re
import time
from collections import defaultdict
from datetime import datetime, timedelta

import httpx

from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

from cupbots.helpers.db import get_plugin_db
from cupbots.helpers.logger import get_logger

log = get_logger("community")

PLUGIN_NAME = "community"
WA_API_URL = os.environ.get("WA_API_URL", "http://127.0.0.1:3100")

DEFAULT_LEVELS = ["Newcomer", "Regular", "Active", "Star", "Legend"]
LEVEL_THRESHOLDS = [0, 50, 200, 500, 1500]
# In-memory trackers
_points_tracker: dict[str, dict[str, list[float]]] = {}
_spam_tracker: dict[str, dict[str, list[float]]] = defaultdict(lambda: defaultdict(list))


# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------

def create_tables(conn):
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS community_config (
            chat_id TEXT PRIMARY KEY,
            company_id TEXT NOT NULL DEFAULT '',
            config TEXT NOT NULL DEFAULT '{}',
            enabled INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            updated_at TEXT NOT NULL DEFAULT (datetime('now'))
        );
        CREATE TABLE IF NOT EXISTS community_points (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            company_id TEXT NOT NULL DEFAULT '',
            chat_id TEXT NOT NULL,
            user_id TEXT NOT NULL,
            user_name TEXT NOT NULL DEFAULT '',
            points INTEGER NOT NULL DEFAULT 0,
            level INTEGER NOT NULL DEFAULT 0,
            message_count INTEGER NOT NULL DEFAULT 0,
            last_active TEXT NOT NULL DEFAULT (datetime('now')),
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            UNIQUE(chat_id, user_id)
        );
        CREATE TABLE IF NOT EXISTS community_roles (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            company_id TEXT NOT NULL DEFAULT '',
            chat_id TEXT NOT NULL,
            user_id TEXT NOT NULL DEFAULT '',
            role_name TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            UNIQUE(chat_id, user_id, role_name)
        );
        CREATE TABLE IF NOT EXISTS community_schedules (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            company_id TEXT NOT NULL DEFAULT '',
            chat_id TEXT NOT NULL,
            schedule_desc TEXT NOT NULL,
            message TEXT NOT NULL,
            job_id TEXT NOT NULL DEFAULT '',
            enabled INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        );
        CREATE TABLE IF NOT EXISTS community_mod_filters (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            company_id TEXT NOT NULL DEFAULT '',
            chat_id TEXT NOT NULL,
            pattern TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            UNIQUE(chat_id, pattern)
        );
        CREATE TABLE IF NOT EXISTS community_mod_warnings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            company_id TEXT NOT NULL DEFAULT '',
            chat_id TEXT NOT NULL,
            user_id TEXT NOT NULL,
            reason TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        );
    """)


def _db():
    return get_plugin_db(PLUGIN_NAME)


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

def _get_config(chat_id: str) -> dict:
    row = _db().execute(
        "SELECT config, enabled FROM community_config WHERE chat_id = ?", (chat_id,),
    ).fetchone()
    if not row:
        return {}
    cfg = json.loads(row["config"]) if row["config"] else {}
    cfg["_enabled"] = bool(row["enabled"])
    return cfg


def _find_config(chat_id: str, parent_group: str | None = None) -> dict:
    if parent_group:
        cfg = _get_config(parent_group)
        if cfg:
            return cfg
    return _get_config(chat_id)


def _config_key(chat_id: str, parent_group: str | None = None) -> str:
    return parent_group or chat_id


def _save_config(chat_id: str, config: dict, company_id: str = ""):
    enabled = config.pop("_enabled", True)
    _db().execute(
        """INSERT INTO community_config (chat_id, company_id, config, enabled, updated_at)
           VALUES (?, ?, ?, ?, datetime('now'))
           ON CONFLICT(chat_id) DO UPDATE SET
             config = excluded.config, enabled = excluded.enabled,
             updated_at = datetime('now')""",
        (chat_id, company_id, json.dumps(config), int(enabled)),
    )
    _db().commit()


def _normalize_jid(phone: str) -> str:
    phone = phone.lstrip("+").replace(" ", "").replace("-", "")
    if not phone.endswith("@s.whatsapp.net"):
        phone = f"{phone}@s.whatsapp.net"
    return phone


def _extract_set_value(raw_text: str, key: str) -> str | None:
    m = re.match(r"/community\s+(?:onboard\s+)?set\s+" + re.escape(key) + r"\s", raw_text, re.IGNORECASE)
    if m:
        return raw_text[m.end():]
    return None


# ---------------------------------------------------------------------------
# Points / Gamification
# ---------------------------------------------------------------------------

def _get_level(points: int, config: dict) -> tuple[int, str]:
    names = config.get("level_names", ",".join(DEFAULT_LEVELS)).split(",")
    names = [n.strip() for n in names] or DEFAULT_LEVELS
    level = 0
    for i, threshold in enumerate(LEVEL_THRESHOLDS):
        if points >= threshold:
            level = i
    name = names[level] if level < len(names) else names[-1]
    return level, name


def _can_earn_points(chat_id: str, sender_id: str, max_per_hour: int) -> bool:
    now = time.time()
    if chat_id not in _points_tracker:
        _points_tracker[chat_id] = {}
    if sender_id not in _points_tracker[chat_id]:
        _points_tracker[chat_id][sender_id] = []
    window = _points_tracker[chat_id][sender_id]
    _points_tracker[chat_id][sender_id] = [t for t in window if now - t < 3600]
    if len(_points_tracker[chat_id][sender_id]) >= max_per_hour:
        return False
    _points_tracker[chat_id][sender_id].append(now)
    return True


def _add_points(chat_id: str, company_id: str, user_id: str, user_name: str,
                points: int, config: dict) -> tuple[int, bool]:
    row = _db().execute(
        "SELECT points, level FROM community_points WHERE chat_id = ? AND user_id = ?",
        (chat_id, user_id),
    ).fetchone()
    if row:
        old_level = row["level"]
        new_points = row["points"] + points
        new_level, _ = _get_level(new_points, config)
        _db().execute(
            """UPDATE community_points SET points = ?, level = ?, message_count = message_count + 1,
               user_name = ?, last_active = datetime('now') WHERE chat_id = ? AND user_id = ?""",
            (new_points, new_level, user_name, chat_id, user_id),
        )
    else:
        new_points = points
        old_level = -1
        new_level, _ = _get_level(new_points, config)
        _db().execute(
            """INSERT INTO community_points (company_id, chat_id, user_id, user_name, points, level, message_count)
               VALUES (?, ?, ?, ?, ?, ?, 1)""",
            (company_id, chat_id, user_id, user_name, new_points, new_level),
        )
    _db().commit()
    return new_points, new_level > old_level


def _get_leaderboard(chat_id: str, limit: int = 10) -> list[dict]:
    return _db().execute(
        "SELECT user_name, points, level, message_count FROM community_points WHERE chat_id = ? ORDER BY points DESC LIMIT ?",
        (chat_id, limit),
    ).fetchall()


def _get_user_points(chat_id: str, user_id: str) -> dict | None:
    return _db().execute(
        "SELECT user_name, points, level, message_count, last_active FROM community_points WHERE chat_id = ? AND user_id = ?",
        (chat_id, user_id),
    ).fetchone()


# ---------------------------------------------------------------------------
# Roles
# ---------------------------------------------------------------------------

def _get_roles(chat_id: str) -> list[str]:
    rows = _db().execute(
        "SELECT DISTINCT role_name FROM community_roles WHERE chat_id = ? AND user_id = ''",
        (chat_id,),
    ).fetchall()
    return [r["role_name"] for r in rows]


async def _handle_role(args: list[str], chat_id: str, company_id: str) -> str:
    if not args:
        return "Usage: `/community role list|add|assign|remove`"
    sub = args[0].lower()

    if sub == "list":
        roles = _get_roles(chat_id)
        if not roles:
            return "No roles defined. Create with `/community role add <name>`"
        return "*Roles:*\n" + "\n".join(f"• {r}" for r in roles)

    if sub == "add" and len(args) >= 2:
        role_name = " ".join(args[1:])
        try:
            _db().execute(
                "INSERT INTO community_roles (company_id, chat_id, user_id, role_name) VALUES (?, ?, '', ?)",
                (company_id, chat_id, role_name),
            )
            _db().commit()
            return f"✅ Role created: {role_name}"
        except Exception:
            return f"Role '{role_name}' already exists."

    if sub == "assign" and len(args) >= 3:
        uid = _normalize_jid(args[1])
        role_name = " ".join(args[2:])
        if role_name not in _get_roles(chat_id):
            return f"Role '{role_name}' doesn't exist. Create with `/community role add {role_name}`"
        try:
            _db().execute(
                "INSERT INTO community_roles (company_id, chat_id, user_id, role_name) VALUES (?, ?, ?, ?)",
                (company_id, chat_id, uid, role_name),
            )
            _db().commit()
            return f"✅ Assigned {role_name} to {args[1]}"
        except Exception:
            return f"{args[1]} already has role '{role_name}'."

    if sub == "remove" and len(args) >= 3:
        uid = _normalize_jid(args[1])
        role_name = " ".join(args[2:])
        deleted = _db().execute(
            "DELETE FROM community_roles WHERE chat_id = ? AND user_id = ? AND role_name = ?",
            (chat_id, uid, role_name),
        ).rowcount
        _db().commit()
        return f"✅ Removed {role_name} from {args[1]}" if deleted else "Role assignment not found."

    return "Usage: `/community role list|add|assign|remove`"


# ---------------------------------------------------------------------------
# Moderation
# ---------------------------------------------------------------------------

def _mod_get_filters(chat_id: str, company_id: str) -> list[str]:
    rows = _db().execute(
        "SELECT pattern FROM community_mod_filters WHERE chat_id = ? AND company_id = ?",
        (chat_id, company_id),
    ).fetchall()
    return [r["pattern"] for r in rows]


def _mod_add_warning(chat_id: str, company_id: str, user_id: str, reason: str) -> int:
    _db().execute(
        "INSERT INTO community_mod_warnings (company_id, chat_id, user_id, reason) VALUES (?, ?, ?, ?)",
        (company_id, chat_id, user_id, reason),
    )
    _db().commit()
    return _db().execute(
        "SELECT COUNT(*) as n FROM community_mod_warnings WHERE chat_id = ? AND user_id = ?",
        (chat_id, user_id),
    ).fetchone()["n"]


def _mod_get_warnings(chat_id: str, user_id: str) -> list[dict]:
    return _db().execute(
        "SELECT reason, created_at FROM community_mod_warnings WHERE chat_id = ? AND user_id = ? ORDER BY created_at DESC LIMIT 20",
        (chat_id, user_id),
    ).fetchall()


def _mod_check_spam(chat_id: str, sender_id: str, limit: int) -> bool:
    now = time.time()
    window = _spam_tracker[chat_id][sender_id]
    _spam_tracker[chat_id][sender_id] = [t for t in window if now - t < 60]
    _spam_tracker[chat_id][sender_id].append(now)
    return len(_spam_tracker[chat_id][sender_id]) > limit


async def _mod_kick_user(chat_id: str, user_jid: str):
    async with httpx.AsyncClient(timeout=10) as client:
        r = await client.post(
            f"{WA_API_URL}/group/remove",
            json={"groupId": chat_id, "participantId": user_jid},
        )
        r.raise_for_status()


async def _handle_mod(args: list[str], chat_id: str, company_id: str,
                      config: dict, key: str, reply) -> str:
    if not args:
        filters = _mod_get_filters(chat_id, company_id)
        spam_on = config.get("mod_spam", False)
        spam_limit = config.get("mod_spam_limit", 10)
        warn_limit = config.get("mod_warn_limit", 3)
        lines = [
            "*Moderation config:*",
            "",
            f"*Filters:* {len(filters)} active",
            f"*Spam detection:* {'on' if spam_on else 'off'} (limit: {spam_limit}/min)",
            f"*Auto-kick after:* {warn_limit} warnings" if warn_limit else "*Auto-kick:* disabled",
        ]
        return "\n".join(lines)

    sub = args[0].lower()

    if sub == "filter":
        if len(args) < 2:
            return "Usage: `/community mod filter add|remove|list`"
        action = args[1].lower()
        if action == "add" and len(args) >= 3:
            pattern = " ".join(args[2:]).lower()
            try:
                _db().execute(
                    "INSERT INTO community_mod_filters (company_id, chat_id, pattern) VALUES (?, ?, ?)",
                    (company_id, chat_id, pattern),
                )
                _db().commit()
                return f"✅ Filter added: {pattern}"
            except Exception:
                return f"Filter '{pattern}' already exists."
        if action == "remove" and len(args) >= 3:
            pattern = " ".join(args[2:]).lower()
            deleted = _db().execute(
                "DELETE FROM community_mod_filters WHERE chat_id = ? AND company_id = ? AND pattern = ?",
                (chat_id, company_id, pattern),
            ).rowcount
            _db().commit()
            return f"✅ Removed: {pattern}" if deleted else f"Filter '{pattern}' not found."
        if action == "list":
            filters = _mod_get_filters(chat_id, company_id)
            if not filters:
                return "No filters. Add with `/community mod filter add <word>`"
            return "*Filters:*\n" + "\n".join(f"• {f}" for f in filters)
        return "Usage: `/community mod filter add|remove|list`"

    if sub == "warn":
        if len(args) < 2:
            return "Usage: `/community mod warn <phone> [reason]`"
        jid = _normalize_jid(args[1])
        reason = " ".join(args[2:]) if len(args) > 2 else "Manual warning"
        count = _mod_add_warning(chat_id, company_id, jid, reason)
        warn_limit = config.get("mod_warn_limit", 3)
        if warn_limit and count >= warn_limit:
            try:
                await _mod_kick_user(chat_id, jid)
                return f"⚠️ Warning #{count} for {args[1]}: {reason}\n🚫 Auto-kicked ({warn_limit} warnings)"
            except Exception as e:
                return f"⚠️ Warning #{count} for {args[1]}: {reason}\nKick failed: {e}"
        return f"⚠️ Warning #{count} for {args[1]}: {reason}"

    if sub == "history":
        if len(args) < 2:
            return "Usage: `/community mod history <phone>`"
        jid = _normalize_jid(args[1])
        warnings = _mod_get_warnings(chat_id, jid)
        if not warnings:
            return f"No warnings for {args[1]}"
        lines = [f"*Warnings for {args[1]}:* ({len(warnings)} total)\n"]
        for w in warnings:
            lines.append(f"• {w['reason']} ({w['created_at']})")
        return "\n".join(lines)

    if sub == "kick":
        if len(args) < 2:
            return "Usage: `/community mod kick <phone>`"
        jid = _normalize_jid(args[1])
        try:
            await _mod_kick_user(chat_id, jid)
            return f"🚫 Kicked {args[1]}"
        except Exception as e:
            return f"Kick failed: {e}"

    if sub == "spam":
        if len(args) < 2:
            return "Usage: `/community mod spam on|off` or `/community mod spam limit <N>`"
        if args[1].lower() in ("on", "off"):
            config["mod_spam"] = args[1].lower() == "on"
            _save_config(key, config, company_id)
            return f"✅ Spam detection {'enabled' if config['mod_spam'] else 'disabled'}"
        if args[1].lower() == "limit" and len(args) >= 3 and args[2].isdigit():
            config["mod_spam_limit"] = max(1, int(args[2]))
            _save_config(key, config, company_id)
            return f"✅ Spam limit: {config['mod_spam_limit']}/min"
        return "Usage: `/community mod spam on|off` or `/community mod spam limit <N>`"

    if sub == "warn_limit":
        if len(args) < 2 or not args[1].isdigit():
            return "Usage: `/community mod warn_limit <N>` (0 to disable)"
        config["mod_warn_limit"] = int(args[1])
        _save_config(key, config, company_id)
        return f"✅ Auto-kick after {config['mod_warn_limit']} warnings" if config["mod_warn_limit"] else "✅ Auto-kick disabled"

    return f"Unknown: {sub}. Try `/community mod --help`"


# ---------------------------------------------------------------------------
# Onboarding
# ---------------------------------------------------------------------------

def _build_welcome(config: dict) -> str:
    parts = [config.get("welcome", "Welcome! We're glad you're here.")]
    if config.get("intro", "true") == "true":
        parts.append("\n💬 Tell us a bit about yourself!")
    return "\n".join(parts)


async def _send_welcome_dm(config: dict, jid: str):
    from cupbots.helpers.channel import WhatsAppReplyContext
    await WhatsAppReplyContext(jid).reply_text(_build_welcome(config))


async def _handle_onboard(args: list[str], chat_id: str, config: dict, key: str,
                          company_id: str, raw_text: str) -> str:
    KEYS = {"welcome", "intro", "delay"}
    PARSERS = {
        "intro": lambda v: "true" if v.lower() in ("yes", "y", "true", "1") else "false",
        "delay": lambda v: str(max(0, min(300, int(v)))) if v.isdigit() else "5",
    }
    if not args:
        enabled = config.get("_enabled", True)
        return "\n".join([
            f"*Onboarding {'✅ ON' if enabled else '❌ OFF'}*", "",
            f"*Welcome:* {config.get('welcome', '(default)')}",
            f"*Ask intro:* {config.get('intro', 'true')}",
            f"*Delay:* {config.get('delay', '5')}s",
        ])
    sub = args[0].lower()
    if sub == "preview":
        return f"*Preview:*\n─────────────────\n{_build_welcome(config)}\n─────────────────"
    if sub == "on":
        config["_enabled"] = True
        _save_config(key, config, company_id)
        return "✅ Onboarding enabled."
    if sub == "off":
        config["_enabled"] = False
        _save_config(key, config, company_id)
        return "❌ Onboarding disabled."
    if sub == "send":
        if len(args) < 2:
            return "Usage: `/community onboard send <phone>`"
        phone = args[1].lstrip("+").replace(" ", "").replace("-", "")
        try:
            await _send_welcome_dm(config, f"{phone}@s.whatsapp.net")
            return f"✅ Welcome DM sent to {phone}"
        except Exception as e:
            return f"Failed: {e}"
    if sub == "set" and len(args) >= 3:
        sk = args[1].lower()
        if sk not in KEYS:
            return f"Unknown key. Valid: {', '.join(sorted(KEYS))}"
        value = _extract_set_value(raw_text, sk) if raw_text else None
        if value is None:
            value = " ".join(args[2:])
        p = PARSERS.get(sk)
        value = p(value) if p else value
        config[sk] = value
        _save_config(key, config, company_id)
        return f"✅ Set *{sk}* = {value}"
    return "Unknown. Try `/community onboard`"


# ---------------------------------------------------------------------------
# Scheduling
# ---------------------------------------------------------------------------

async def _handle_schedule(args: list[str], chat_id: str, company_id: str,
                           raw_text: str) -> str:
    if not args:
        return "Usage: `/community schedule add|list|remove`"
    sub = args[0].lower()

    if sub == "list":
        rows = _db().execute(
            "SELECT id, schedule_desc, message, enabled FROM community_schedules WHERE chat_id = ? AND company_id = ?",
            (chat_id, company_id),
        ).fetchall()
        if not rows:
            return "No scheduled content. Add with `/community schedule add <time> <message>`"
        lines = ["*Scheduled content:*\n"]
        for r in rows:
            status = "✅" if r["enabled"] else "❌"
            lines.append(f"{status} #{r['id']} — {r['schedule_desc']}: {r['message'][:50]}")
        return "\n".join(lines)

    if sub == "add":
        if len(args) < 3:
            return "Usage: `/community schedule add <time> <message>`\nTime: daily 9am, weekly monday 9am"
        schedule_desc = args[1]
        after = raw_text.split(None, 4) if raw_text else None
        message = after[4] if after and len(after) > 4 else " ".join(args[2:])
        next_run = _compute_next_run(schedule_desc)
        if not next_run:
            return f"Couldn't parse: {schedule_desc}. Try: daily, weekly"
        from cupbots.helpers.jobs import enqueue
        job_id = enqueue("community_schedule", {
            "chat_id": chat_id, "company_id": company_id,
            "message": message, "schedule_desc": schedule_desc,
        }, run_at=next_run)
        _db().execute(
            "INSERT INTO community_schedules (company_id, chat_id, schedule_desc, message, job_id) VALUES (?, ?, ?, ?, ?)",
            (company_id, chat_id, schedule_desc, message, job_id),
        )
        _db().commit()
        return f"✅ Scheduled: {schedule_desc} — next: {next_run.strftime('%Y-%m-%d %H:%M')}"

    if sub == "remove":
        if len(args) < 2 or not args[1].isdigit():
            return "Usage: `/community schedule remove <id>`"
        row = _db().execute(
            "SELECT job_id FROM community_schedules WHERE id = ? AND chat_id = ?",
            (int(args[1]), chat_id),
        ).fetchone()
        if not row:
            return f"Schedule #{args[1]} not found."
        if row["job_id"]:
            from cupbots.helpers.jobs import cancel_job
            cancel_job(row["job_id"])
        _db().execute("DELETE FROM community_schedules WHERE id = ?", (int(args[1]),))
        _db().commit()
        return f"✅ Removed schedule #{args[1]}"

    return "Usage: `/community schedule add|list|remove`"


def _compute_next_run(desc: str) -> datetime | None:
    desc = desc.lower().strip()
    now = datetime.now()
    m = re.match(r'daily(?:\s+(\d{1,2})(?::(\d{2}))?\s*(am|pm)?)?', desc)
    if m:
        hour = int(m.group(1) or 9)
        minute = int(m.group(2) or 0)
        if m.group(3) == "pm" and hour < 12:
            hour += 12
        target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if target <= now:
            target += timedelta(days=1)
        return target
    m = re.match(r'weekly(?:\s+(\w+))?(?:\s+(\d{1,2})(?::(\d{2}))?\s*(am|pm)?)?', desc)
    if m:
        days = {"monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3,
                "friday": 4, "saturday": 5, "sunday": 6,
                "mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6}
        target_day = days.get(m.group(1) or "monday", 0)
        hour = int(m.group(2) or 9)
        minute = int(m.group(3) or 0)
        if m.group(4) == "pm" and hour < 12:
            hour += 12
        days_ahead = (target_day - now.weekday()) % 7
        if days_ahead == 0:
            target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
            if target <= now:
                days_ahead = 7
        target = now.replace(hour=hour, minute=minute, second=0, microsecond=0) + timedelta(days=days_ahead)
        return target
    return None


async def _handle_schedule_job(payload: dict, bot=None):
    chat_id = payload.get("chat_id", "")
    message = payload.get("message", "")
    schedule_desc = payload.get("schedule_desc", "")
    company_id = payload.get("company_id", "")
    if not chat_id or not message:
        return
    from cupbots.helpers.channel import WhatsAppReplyContext
    try:
        await WhatsAppReplyContext(chat_id).reply_text(message)
        log.info("Scheduled content sent to %s", chat_id)
    except Exception as e:
        log.error("Failed scheduled content to %s: %s", chat_id, e)
    next_run = _compute_next_run(schedule_desc)
    if next_run:
        from cupbots.helpers.jobs import enqueue
        new_id = enqueue("community_schedule", payload, run_at=next_run)
        _db().execute(
            "UPDATE community_schedules SET job_id = ? WHERE chat_id = ? AND company_id = ? AND schedule_desc = ? AND message = ?",
            (new_id, chat_id, company_id, schedule_desc, message),
        )
        _db().commit()


try:
    from cupbots.helpers.jobs import register_handler
    register_handler("community_schedule", _handle_schedule_job)
except Exception:
    pass


# ---------------------------------------------------------------------------
# Core command handler
# ---------------------------------------------------------------------------

async def _handle_community(args: list[str], chat_id: str, company_id: str,
                            sender_id: str, sender_name: str, reply,
                            raw_text: str = "",
                            parent_group: str | None = None) -> str | None:
    key = _config_key(chat_id, parent_group)
    config = _find_config(chat_id, parent_group)

    if not args:
        leaderboard = _get_leaderboard(key, 5)
        total = _db().execute(
            "SELECT COUNT(*) as n, SUM(message_count) as msgs FROM community_points WHERE chat_id = ?",
            (key,),
        ).fetchone()
        members = total["n"] or 0
        msgs = total["msgs"] or 0
        enabled = config.get("_enabled", True) if config else False
        lines = [
            "*Community Dashboard*", "",
            f"Members tracked: {members}",
            f"Total messages: {msgs}",
            f"Onboarding: {'✅ on' if enabled else '❌ off'}",
        ]
        if leaderboard:
            lines.append("\n*Top 5:*")
            for i, r in enumerate(leaderboard, 1):
                _, lname = _get_level(r["points"], config or {})
                lines.append(f"{i}. {r['user_name'] or 'Unknown'} — {r['points']} pts ({lname})")
        lines.append(f"\nRun `/community --help` for all commands.")
        return "\n".join(lines)

    sub = args[0].lower()

    if sub in ("--help", "-h", "help"):
        return __doc__.strip()

    if sub == "leaderboard":
        lb = _get_leaderboard(key, 10)
        if not lb:
            return "No activity yet."
        lines = ["*Leaderboard:*\n"]
        for i, r in enumerate(lb, 1):
            _, lname = _get_level(r["points"], config or {})
            lines.append(f"{i}. {r['user_name'] or 'Unknown'} — {r['points']} pts ({lname}, {r['message_count']} msgs)")
        return "\n".join(lines)

    if sub == "points":
        uid = _normalize_jid(args[1]) if len(args) >= 2 else sender_id
        user = _get_user_points(key, uid)
        if not user:
            return "No activity recorded for this user."
        _, lname = _get_level(user["points"], config or {})
        return f"*{user['user_name'] or uid}*\nPoints: {user['points']}\nLevel: {lname}\nMessages: {user['message_count']}\nLast active: {user['last_active']}"

    if sub == "award":
        if len(args) < 3 or not args[2].lstrip("-").isdigit():
            return "Usage: `/community award <phone> <points> [reason]`"
        uid = _normalize_jid(args[1])
        pts = int(args[2])
        new_total, _ = _add_points(key, company_id, uid, "", pts, config or {})
        return f"✅ Awarded {pts} pts to {args[1]} (total: {new_total})"

    if sub == "role":
        return await _handle_role(args[1:], key, company_id)

    if sub == "analytics":
        days = int(args[1]) if len(args) > 1 and args[1].isdigit() else 7
        since = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
        active = _db().execute(
            "SELECT COUNT(*) as n FROM community_points WHERE chat_id = ? AND last_active >= ?",
            (key, since),
        ).fetchone()["n"]
        total = _db().execute(
            "SELECT COUNT(*) as n FROM community_points WHERE chat_id = ?", (key,),
        ).fetchone()["n"]
        return f"*Analytics ({days} days):*\nActive members: {active}\nTotal members: {total}\nEngagement: {round(active / total * 100) if total else 0}%"

    if sub == "onboard":
        return await _handle_onboard(args[1:], chat_id, config or {}, key, company_id, raw_text)

    if sub == "mod":
        return await _handle_mod(args[1:], chat_id, company_id, config or {}, key, reply)

    if sub == "schedule":
        return await _handle_schedule(args[1:], key, company_id, raw_text)

    if sub == "set" and len(args) >= 3:
        sk = args[1].lower()
        valid = {"welcome", "intro", "delay", "points_msg", "points_hour", "level_names"}
        if sk not in valid:
            return f"Unknown key. Valid: {', '.join(sorted(valid))}"
        value = _extract_set_value(raw_text, sk) if raw_text else None
        if value is None:
            value = " ".join(args[2:])
        parsers = {
            "intro": lambda v: "true" if v.lower() in ("yes", "y", "true", "1") else "false",
            "delay": lambda v: str(max(0, min(300, int(v)))) if v.isdigit() else "5",
            "points_msg": lambda v: str(max(1, int(v))) if v.isdigit() else "1",
            "points_hour": lambda v: str(max(1, int(v))) if v.isdigit() else "20",
        }
        p = parsers.get(sk)
        value = p(value) if p else value
        if not config:
            config = {}
        config.pop("_enabled", None)
        config[sk] = value
        _save_config(key, config, company_id)
        return f"✅ Set *{sk}* = {value}"

    if sub == "set":
        return "Usage: `/community set <key> <value>`"

    return f"Unknown: {sub}. Try `/community --help`"


# ---------------------------------------------------------------------------
# handle_message — moderation + FAQ auto-answer + gamification
# ---------------------------------------------------------------------------

async def handle_message(msg, reply):
    """Runs on every group message: moderation, FAQ, then gamification."""
    if not msg.text or not msg.is_group:
        return False

    parent_group = getattr(msg, "parent_group", None)
    key = _config_key(msg.chat_id, parent_group)
    config = _find_config(msg.chat_id, parent_group)
    if not config:
        return False

    company_id = msg.company_id or ""

    # --- Moderation: keyword filters ---
    if not msg.command:
        text_lower = msg.text.lower()
        filters = _mod_get_filters(msg.chat_id, company_id)
        for pattern in filters:
            if pattern in text_lower:
                jid = msg.sender_id
                if not jid.endswith("@s.whatsapp.net"):
                    jid = f"{jid}@s.whatsapp.net"
                count = _mod_add_warning(msg.chat_id, company_id, jid, f"Keyword: {pattern}")
                warn_limit = config.get("mod_warn_limit", 3)
                if warn_limit and count >= warn_limit:
                    try:
                        await _mod_kick_user(msg.chat_id, jid)
                        await reply.reply_text(f"🚫 {msg.sender_name} kicked — {count} warnings (keyword: {pattern})")
                    except Exception as e:
                        log.error("Auto-kick failed: %s", e)
                else:
                    await reply.reply_text(f"⚠️ {msg.sender_name}: blocked word detected. Warning {count}.")
                return "block"

    # --- Moderation: spam detection ---
    if not msg.command and config.get("mod_spam"):
        limit = config.get("mod_spam_limit", 10)
        if _mod_check_spam(msg.chat_id, msg.sender_id, limit):
            jid = msg.sender_id
            if not jid.endswith("@s.whatsapp.net"):
                jid = f"{jid}@s.whatsapp.net"
            count = _mod_add_warning(msg.chat_id, company_id, jid, "Spam: rate limit")
            warn_limit = config.get("mod_warn_limit", 3)
            if warn_limit and count >= warn_limit:
                try:
                    await _mod_kick_user(msg.chat_id, jid)
                    await reply.reply_text(f"🚫 {msg.sender_name} kicked — spam ({count} warnings)")
                except Exception as e:
                    log.error("Auto-kick failed: %s", e)
            else:
                await reply.reply_text(f"⚠️ {msg.sender_name}: slow down! Warning {count}.")
            return "block"

    # --- Gamification: points ---
    if not msg.command and len(msg.text.strip()) >= 3:
        points_per_msg = int(config.get("points_msg", "1"))
        max_per_hour = int(config.get("points_hour", "20"))
        if _can_earn_points(key, msg.sender_id, max_per_hour):
            new_total, leveled_up = _add_points(
                key, company_id, msg.sender_id, msg.sender_name, points_per_msg, config,
            )
            if leveled_up:
                _, lname = _get_level(new_total, config)
                await reply.reply_text(f"🎉 {msg.sender_name} leveled up to *{lname}*! ({new_total} pts)")

    return False


# ---------------------------------------------------------------------------
# handle_group_event — onboarding DM on join
# ---------------------------------------------------------------------------

async def handle_group_event(data: dict, reply, group_cfg: dict | None = None):
    if data.get("action") != "add":
        return
    chat_id = data.get("chatId", "")
    parent_group = data.get("parentGroup")
    config = _find_config(chat_id, parent_group)
    if not config or not config.get("_enabled", True):
        return
    participants = data.get("participants", [])
    delay = int(config.get("delay", "5"))
    if delay > 0:
        await asyncio.sleep(delay)
    for jid in participants:
        try:
            await _send_welcome_dm(config, jid)
            log.info("Sent onboarding DM to %s", jid)
        except Exception as e:
            log.error("Failed onboarding DM to %s: %s", jid, e)


# ---------------------------------------------------------------------------
# Cross-platform handler
# ---------------------------------------------------------------------------

async def handle_command(msg, reply) -> bool:
    if msg.command != "community":
        return False
    if not msg.is_group:
        await reply.reply_text("Community only works in groups and communities.")
        return True
    result = await _handle_community(
        msg.args, msg.chat_id, msg.company_id or "",
        msg.sender_id, msg.sender_name, reply,
        raw_text=msg.text or "",
        parent_group=getattr(msg, "parent_group", None),
    )
    if result:
        await reply.reply_text(result)
    return True


# ---------------------------------------------------------------------------
# Telegram handler
# ---------------------------------------------------------------------------

async def cmd_community(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message:
        return
    chat_id = str(update.effective_chat.id)
    args = context.args or []
    from cupbots.helpers.channel import TelegramReplyContext
    reply = TelegramReplyContext(update)
    raw_text = update.message.text or ""
    result = await _handle_community(
        args, chat_id, "",
        str(update.effective_user.id) if update.effective_user else "",
        update.effective_user.first_name if update.effective_user else "",
        reply, raw_text=raw_text,
    )
    if result:
        await reply.reply_text(result)


def register(app: Application):
    app.add_handler(CommandHandler("community", cmd_community))
