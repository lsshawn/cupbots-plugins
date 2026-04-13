"""
Task — Basecamp-style task management from chat.

Commands:
  /task add <title> [--owner @name] [--scope <project>] [--due <date>]
  /task list [--scope <project>] [--owner @name] [--all]
  /task done <id>            — Mark task complete
  /task drop <id>            — Drop a task (won't do it)
  /task status <id> <status> — Set task status
  /task hill <id> <0-100>    — Update hill progress
  /task mine                 — Show my open tasks
  /task assign <id> @name    — Reassign a task
  /task scope <name>         — Create a new scope (project)
  /task scopes               — List active scopes
  /task members              — List team members
  /task history <id>         — Show change log for a task
  /task changes [--days N]   — Recent changes across all tasks
  /task board                — Get a private kanban board link
  /task checkin              — Trigger a check-in now
  /tasks                     — Alias for /task list

Statuses (configurable):
  not now → maybe → doing → done  (or "dropped" at any point)
  Configure via plugin_settings.task.statuses in config.yaml

Hill chart positions:
  0-50   = figuring it out (uphill)
  50     = peak — figured it out, now executing
  51-100 = making it happen (downhill)

Quick add (natural language via AI):
  /task Buy office supplies by Friday for @sarah
  → auto-parsed into: /task add --title Buy office supplies --due 2026-04-10 --owner sarah

Examples:
  /task add Fix login bug --scope backend
  /task add Order new chairs --owner sarah --due friday
  /task status 3 doing
  /task list --scope backend
  /task hill 3 75
  /task done 3
  /tasks --all
"""

import shlex
from datetime import datetime, date, timedelta

from telegram.ext import Application, CommandHandler

from cupbots.helpers.db import get_plugin_db, get_plugin_config
from cupbots.helpers.jobs import enqueue, register_handler
from cupbots.helpers.logger import get_logger

log = get_logger("task")
PLUGIN_NAME = "task"
DEFAULT_STATUSES = ["not now", "maybe", "doing", "done"]

# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------

def create_tables(conn):
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS tasks (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            company_id  TEXT NOT NULL DEFAULT '',
            title       TEXT NOT NULL,
            owner       TEXT NOT NULL DEFAULT '',
            scope       TEXT NOT NULL DEFAULT '',
            hill        INTEGER NOT NULL DEFAULT 0,
            status      TEXT NOT NULL DEFAULT 'not now',
            due_date    TEXT,
            created_by  TEXT NOT NULL DEFAULT '',
            created_at  TEXT NOT NULL DEFAULT (datetime('now')),
            done_at     TEXT,
            cycle       TEXT NOT NULL DEFAULT ''
        );
        CREATE INDEX IF NOT EXISTS idx_tasks_company
            ON tasks(company_id, status);
        CREATE INDEX IF NOT EXISTS idx_tasks_owner
            ON tasks(company_id, owner, status);

        CREATE TABLE IF NOT EXISTS scopes (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            company_id  TEXT NOT NULL DEFAULT '',
            name        TEXT NOT NULL,
            created_at  TEXT NOT NULL DEFAULT (datetime('now'))
        );
        CREATE UNIQUE INDEX IF NOT EXISTS idx_scopes_name
            ON scopes(company_id, name);

        CREATE TABLE IF NOT EXISTS task_changes (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id     INTEGER NOT NULL,
            company_id  TEXT NOT NULL DEFAULT '',
            field       TEXT NOT NULL,
            old_value   TEXT NOT NULL DEFAULT '',
            new_value   TEXT NOT NULL DEFAULT '',
            changed_by  TEXT NOT NULL DEFAULT '',
            changed_at  TEXT NOT NULL DEFAULT (datetime('now'))
        );
        CREATE INDEX IF NOT EXISTS idx_task_changes_task
            ON task_changes(task_id, changed_at);
        CREATE INDEX IF NOT EXISTS idx_task_changes_company
            ON task_changes(company_id, changed_at);
    """)
    # Migrate legacy 'open' status to default first status
    conn.execute("UPDATE tasks SET status = 'not now' WHERE status = 'open'")
    conn.commit()


def _db():
    return get_plugin_db(PLUGIN_NAME)


# ---------------------------------------------------------------------------
# Statuses
# ---------------------------------------------------------------------------

def _get_statuses() -> list[str]:
    """Read configured statuses or return defaults."""
    raw = get_plugin_config(PLUGIN_NAME, "statuses")
    if raw:
        parsed = [s.strip().lower() for s in raw.split(",") if s.strip()]
        if len(parsed) >= 2:
            return parsed
    return list(DEFAULT_STATUSES)


def _terminal_status() -> str:
    """Last status in the list is terminal (done)."""
    return _get_statuses()[-1]


def _active_statuses() -> list[str]:
    """All statuses except terminal and dropped."""
    return _get_statuses()[:-1]


def _is_terminal(status: str) -> bool:
    return status in (_terminal_status(), "dropped")


# ---------------------------------------------------------------------------
# Members (resolved from config.yaml)
# ---------------------------------------------------------------------------

def _resolve_members(platform: str = "whatsapp") -> list[dict]:
    """Resolve team members from config.yaml: admins + user_roles + DM allowlist."""
    from cupbots.helpers.access import get_admins, get_user_roles_list, get_allow_list
    from cupbots.config import get_config

    seen = set()
    members = []

    # Super admin
    sa = get_config().get("super_admin", {})
    sa_id = str(sa.get(platform, ""))
    if sa_id:
        label = "admin"
        # Try to find a better label from admins list
        for a in get_admins(platform):
            if a["user_id"] == sa_id and a.get("label"):
                label = a["label"]
                break
        if sa_id not in seen:
            seen.add(sa_id)
            members.append({"name": label.lower(), "jid": sa_id})

    # Admins
    for a in get_admins(platform):
        jid = a["user_id"]
        if jid and jid not in seen:
            seen.add(jid)
            name = (a.get("label") or jid.split("@")[0]).lower()
            members.append({"name": name, "jid": jid})

    # User roles
    for ur in get_user_roles_list(platform):
        jid = ur["user_id"]
        if jid and jid not in seen:
            seen.add(jid)
            # Try to find label from allowlist
            name = jid.split("@")[0]
            for entry in get_allow_list(platform):
                if entry.get("chat_id") == jid and entry.get("label"):
                    name = entry["label"].lower()
                    break
            members.append({"name": name, "jid": jid})

    # DM entries from allowlist with labels
    for entry in get_allow_list(platform):
        if entry.get("type") == "dm" and entry.get("label"):
            jid = entry["chat_id"]
            if jid and jid not in seen:
                seen.add(jid)
                members.append({"name": entry["label"].lower(), "jid": jid})

    return members


def _resolve_owner(name: str, platform: str = "whatsapp") -> str:
    """Fuzzy match an owner name against known members. Returns best match or original."""
    if not name:
        return name
    name_lower = name.lower()
    members = _resolve_members(platform)

    # Exact match
    for m in members:
        if m["name"] == name_lower:
            return m["name"]

    # Prefix match
    matches = [m for m in members if m["name"].startswith(name_lower)]
    if len(matches) == 1:
        return matches[0]["name"]

    # Substring match
    matches = [m for m in members if name_lower in m["name"]]
    if len(matches) == 1:
        return matches[0]["name"]

    return name_lower


# ---------------------------------------------------------------------------
# Flag parser
# ---------------------------------------------------------------------------

def _parse_flags(args: list[str]) -> dict:
    """Parse --flag value pairs from args list."""
    try:
        tokens = shlex.split(" ".join(args))
    except ValueError:
        tokens = list(args)

    out: dict = {}
    positional: list[str] = []
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        if tok.startswith("--"):
            key = tok[2:].lower()
            values = []
            i += 1
            while i < len(tokens) and not tokens[i].startswith("--"):
                values.append(tokens[i])
                i += 1
            out[key] = " ".join(values).strip()
        else:
            positional.append(tok)
            i += 1
    if positional:
        out["_positional"] = positional
    return out


def _parse_owner_input(raw: str) -> str:
    """Normalize owner input: strip @ prefix, resolve against members."""
    return _resolve_owner(raw.lstrip("@").strip().lower())


def _parse_due(raw: str) -> str | None:
    """Parse a due date string into ISO date. Returns None if unparseable."""
    raw = raw.strip().lower()
    today = date.today()

    day_names = {
        "monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3,
        "friday": 4, "saturday": 5, "sunday": 6,
        "mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6,
    }

    if raw == "today":
        return today.isoformat()
    if raw == "tomorrow":
        return (today + timedelta(days=1)).isoformat()
    if raw in day_names:
        target = day_names[raw]
        days_ahead = (target - today.weekday()) % 7
        if days_ahead == 0:
            days_ahead = 7
        return (today + timedelta(days=days_ahead)).isoformat()

    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%m/%d/%Y"):
        try:
            return datetime.strptime(raw, fmt).date().isoformat()
        except ValueError:
            continue
    return None


# ---------------------------------------------------------------------------
# Hill chart labels
# ---------------------------------------------------------------------------

def _hill_label(pos: int) -> str:
    if pos == 0:
        return "not started"
    elif pos <= 25:
        return "figuring it out"
    elif pos <= 50:
        return "almost figured out"
    elif pos <= 75:
        return "making it happen"
    elif pos < 100:
        return "almost done"
    else:
        return "done"


def _hill_bar(pos: int, width: int = 10) -> str:
    filled = round(pos / 100 * width)
    return "\u2588" * filled + "\u2591" * (width - filled)


# ---------------------------------------------------------------------------
# Change log
# ---------------------------------------------------------------------------

def _log_change(task_id: int, company_id: str, field: str,
                old_value: str, new_value: str, changed_by: str = ""):
    _db().execute(
        """INSERT INTO task_changes (task_id, company_id, field, old_value, new_value, changed_by)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (task_id, company_id, field, str(old_value), str(new_value), changed_by),
    )
    _db().commit()


def _get_task_history(company_id: str, task_id: int, limit: int = 30) -> list[dict]:
    rows = _db().execute(
        """SELECT * FROM task_changes
           WHERE company_id = ? AND task_id = ?
           ORDER BY changed_at DESC LIMIT ?""",
        (company_id, task_id, limit),
    ).fetchall()
    return [dict(r) for r in rows]


def _get_recent_changes(company_id: str, days: int = 7, limit: int = 30) -> list[dict]:
    rows = _db().execute(
        """SELECT tc.*, t.title FROM task_changes tc
           LEFT JOIN tasks t ON t.id = tc.task_id AND t.company_id = tc.company_id
           WHERE tc.company_id = ? AND tc.changed_at >= datetime('now', ?)
           ORDER BY tc.changed_at DESC LIMIT ?""",
        (company_id, f"-{days} days", limit),
    ).fetchall()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Core operations
# ---------------------------------------------------------------------------

def _add_task(company_id: str, title: str, owner: str = "",
              scope: str = "", due_date: str | None = None,
              created_by: str = "") -> int:
    statuses = _get_statuses()
    first_status = statuses[0]
    conn = _db()
    cur = conn.execute(
        """INSERT INTO tasks (company_id, title, owner, scope, due_date, created_by, status)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (company_id, title, owner, scope.lower(), due_date, created_by, first_status),
    )
    conn.commit()
    task_id = cur.lastrowid
    _log_change(task_id, company_id, "created", "", title, created_by)
    if owner:
        _log_change(task_id, company_id, "owner", "", owner, created_by)
    if scope:
        _log_change(task_id, company_id, "scope", "", scope.lower(), created_by)
    if due_date:
        _log_change(task_id, company_id, "due_date", "", due_date, created_by)
    return task_id


def _get_task(company_id: str, task_id: int) -> dict | None:
    row = _db().execute(
        "SELECT * FROM tasks WHERE company_id = ? AND id = ?",
        (company_id, task_id),
    ).fetchone()
    return dict(row) if row else None


def _set_status(company_id: str, task_id: int, new_status: str,
                changed_by: str = "") -> bool:
    task = _get_task(company_id, task_id)
    if not task or _is_terminal(task["status"]):
        return False
    done_at = "datetime('now')" if _is_terminal(new_status) else "NULL"
    hill = 100 if new_status == _terminal_status() else task["hill"]
    conn = _db()
    conn.execute(
        f"""UPDATE tasks SET status = ?, hill = ?,
            done_at = CASE WHEN ? IN (?, 'dropped') THEN datetime('now') ELSE NULL END
            WHERE company_id = ? AND id = ?""",
        (new_status, hill, new_status, _terminal_status(), company_id, task_id),
    )
    conn.commit()
    _log_change(task_id, company_id, "status", task["status"], new_status, changed_by)
    return True


def _update_hill(company_id: str, task_id: int, position: int,
                 changed_by: str = "") -> bool:
    task = _get_task(company_id, task_id)
    if not task or _is_terminal(task["status"]):
        return False
    conn = _db()
    conn.execute(
        "UPDATE tasks SET hill = ? WHERE company_id = ? AND id = ?",
        (position, company_id, task_id),
    )
    conn.commit()
    _log_change(task_id, company_id, "hill", str(task["hill"]), str(position), changed_by)
    return True


def _reassign(company_id: str, task_id: int, new_owner: str,
              changed_by: str = "") -> bool:
    task = _get_task(company_id, task_id)
    if not task or _is_terminal(task["status"]):
        return False
    conn = _db()
    conn.execute(
        "UPDATE tasks SET owner = ? WHERE company_id = ? AND id = ?",
        (new_owner, company_id, task_id),
    )
    conn.commit()
    _log_change(task_id, company_id, "owner", task["owner"], new_owner, changed_by)
    return True


def _list_tasks(company_id: str, scope: str = "", owner: str = "",
                include_done: bool = False, limit: int = 20) -> list[dict]:
    conn = _db()
    where = ["company_id = ?"]
    params: list = [company_id]

    if not include_done:
        terminal = _terminal_status()
        where.append("status NOT IN (?, 'dropped')")
        params.append(terminal)
    if scope:
        where.append("scope = ?")
        params.append(scope.lower())
    if owner:
        where.append("owner = ?")
        params.append(owner)

    rows = conn.execute(
        f"""SELECT * FROM tasks WHERE {' AND '.join(where)}
            ORDER BY
                CASE WHEN due_date IS NOT NULL AND due_date <= date('now') THEN 0 ELSE 1 END,
                due_date ASC NULLS LAST,
                created_at DESC
            LIMIT ?""",
        params + [limit],
    ).fetchall()
    return [dict(r) for r in rows]


def _create_scope(company_id: str, name: str) -> bool:
    conn = _db()
    try:
        conn.execute(
            "INSERT INTO scopes (company_id, name) VALUES (?, ?)",
            (company_id, name.lower()),
        )
        conn.commit()
        return True
    except Exception:
        return False


def _list_scopes(company_id: str) -> list[str]:
    rows = _db().execute(
        "SELECT name FROM scopes WHERE company_id = ? ORDER BY name",
        (company_id,),
    ).fetchall()
    return [r["name"] for r in rows]


# ---------------------------------------------------------------------------
# Formatting
# ---------------------------------------------------------------------------

def _fmt_task(t: dict, show_scope: bool = True) -> str:
    parts = [f"#{t['id']}"]

    terminal = _terminal_status()
    if t["status"] == terminal:
        parts.append(f"[{terminal}]")
    elif t["status"] == "dropped":
        parts.append("[dropped]")
    else:
        parts.append(f"[{t['status']}]")

    parts.append(t["title"])

    if t["owner"]:
        parts.append(f"@{t['owner']}")
    if show_scope and t["scope"]:
        parts.append(f"({t['scope']})")
    if t["due_date"]:
        due = t["due_date"]
        today = date.today().isoformat()
        if due < today and not _is_terminal(t["status"]):
            parts.append(f"OVERDUE {due}")
        else:
            parts.append(f"due {due}")
    if not _is_terminal(t["status"]) and t["hill"] > 0:
        parts.append(f"{_hill_bar(t['hill'])} {t['hill']}%")

    return " ".join(parts)


def _fmt_task_list(tasks: list[dict], title: str = "Tasks",
                   show_scope: bool = True) -> str:
    if not tasks:
        return f"{title}: none"

    lines = [f"*{title}* ({len(tasks)})\n"]
    for t in tasks:
        lines.append(_fmt_task(t, show_scope=show_scope))
    return "\n".join(lines)


def _fmt_change(c: dict) -> str:
    ts = c["changed_at"][:16] if c.get("changed_at") else ""
    who = f"@{c['changed_by']}" if c.get("changed_by") else ""
    title = c.get("title", "")
    prefix = f"#{c['task_id']}" + (f" {title}" if title else "")

    if c["field"] == "created":
        return f"{ts} {who} created {prefix}"
    return f"{ts} {who} {prefix} {c['field']}: {c['old_value']} -> {c['new_value']}"


# ---------------------------------------------------------------------------
# Check-in (scheduled daily digest)
# ---------------------------------------------------------------------------

async def _run_checkin(company_id: str, chat_id: str) -> str:
    open_tasks = _list_tasks(company_id, limit=50)
    if not open_tasks:
        return "No open tasks. Enjoy the calm."

    overdue = [t for t in open_tasks if t["due_date"] and t["due_date"] < date.today().isoformat()]
    stale = [t for t in open_tasks if t["hill"] < 50 and
             (datetime.now() - datetime.fromisoformat(t["created_at"])).days >= 5]
    statuses = _get_statuses()
    terminal = statuses[-1]

    lines = [f"*Daily check-in* ({len(open_tasks)} open)\n"]

    if overdue:
        lines.append(f"*Overdue ({len(overdue)}):*")
        for t in overdue:
            lines.append(f"  {_fmt_task(t)}")
        lines.append("")

    if stale:
        lines.append(f"*Stuck? ({len(stale)} tasks uphill 5+ days):*")
        for t in stale:
            lines.append(f"  {_fmt_task(t)}")
        lines.append("Can you cut scope on any of these?")
        lines.append("")

    # Group by status
    for s in statuses[:-1]:  # skip terminal
        group = [t for t in open_tasks if t["status"] == s]
        if group:
            lines.append(f"*{s.title()} ({len(group)}):*")
            for t in group:
                lines.append(f"  {_fmt_task(t)}")
            lines.append("")

    lines.append("Reply with how things are going, or /task hill <id> <0-100>")
    return "\n".join(lines)


async def _handle_checkin_job(payload: dict, bot=None):
    company_id = payload.get("company_id", "")
    chat_id = payload.get("chat_id", "")
    platform = payload.get("platform", "whatsapp")

    text = await _run_checkin(company_id, chat_id)

    if platform == "whatsapp":
        import httpx
        wa_url = payload.get("wa_api_url", "http://127.0.0.1:3100")
        async with httpx.AsyncClient(timeout=15) as client:
            await client.post(
                f"{wa_url}/send",
                json={"chatId": chat_id, "text": text},
            )
    elif platform == "telegram" and bot:
        await bot.send_message(chat_id=chat_id, text=text)


# ---------------------------------------------------------------------------
# Board (hub-hosted kanban)
# ---------------------------------------------------------------------------

async def _push_board(company_id: str, reply) -> str | None:
    """Push task snapshot to hub, return magic link URL or None."""
    from cupbots.helpers.hub import is_connected, register_task_board

    if not is_connected():
        await reply.reply_text("Hub not connected. Board requires the hub.")
        return None

    tasks = _list_tasks(company_id, include_done=True, limit=500)
    statuses = _get_statuses() + ["dropped"]
    title = "Task Board"

    token = await register_task_board(
        company_id=company_id,
        tasks=tasks,
        statuses=statuses,
        title=title,
    )
    if token:
        return f"https://hub.cupbots.com/b/{token}"
    return None


# ---------------------------------------------------------------------------
# Command handler
# ---------------------------------------------------------------------------

async def handle_command(msg, reply) -> bool:
    cmd = msg.command
    if cmd not in ("task", "tasks"):
        return False

    args = msg.args or []
    company_id = msg.company_id or ""
    changed_by = (msg.sender_name or msg.sender_id or "").lower().split("@")[0]

    # /tasks → alias for /task list
    if cmd == "tasks":
        args = ["list"] + args

    if not args:
        await reply.reply_text(__doc__.strip())
        return True

    sub = args[0].lower()
    sub_args = args[1:]

    # -- /task add -------------------------------------------------------
    if sub == "add":
        if not sub_args:
            await reply.reply_text(
                "Usage: /task add <title> [--owner @name] [--scope <project>] [--due <date>]")
            return True

        flags = _parse_flags(sub_args)
        title_parts = flags.get("_positional", [])
        title = flags.get("title") or " ".join(title_parts)
        if not title:
            await reply.reply_text("Give the task a title.")
            return True

        owner = _parse_owner_input(flags.get("owner", ""))
        scope = flags.get("scope", "").lower()
        due_raw = flags.get("due", "")
        due_date = _parse_due(due_raw) if due_raw else None

        if due_raw and not due_date:
            await reply.reply_text(f"Couldn't parse due date: {due_raw}")
            return True

        if scope:
            _create_scope(company_id, scope)

        task_id = _add_task(
            company_id=company_id,
            title=title,
            owner=owner,
            scope=scope,
            due_date=due_date,
            created_by=changed_by,
        )

        statuses = _get_statuses()
        parts = [f"Task #{task_id}: {title}"]
        if owner:
            parts.append(f"@{owner}")
        if scope:
            parts.append(f"({scope})")
        if due_date:
            parts.append(f"due {due_date}")
        parts.append(f"[{statuses[0]}]")
        await reply.reply_text(" ".join(parts))
        return True

    # -- /task list ------------------------------------------------------
    if sub == "list":
        flags = _parse_flags(sub_args)
        scope = flags.get("scope", "").lower()
        owner = _parse_owner_input(flags.get("owner", "")) if "owner" in flags else ""
        include_done = "all" in flags

        tasks = _list_tasks(company_id, scope=scope, owner=owner,
                            include_done=include_done)
        title = "Tasks"
        if scope:
            title = f"Tasks ({scope})"
        if owner:
            title = f"Tasks (@{owner})"
        await reply.reply_text(_fmt_task_list(tasks, title=title,
                                              show_scope=not bool(scope)))
        return True

    # -- /task done ------------------------------------------------------
    if sub == "done":
        if not sub_args:
            await reply.reply_text("Usage: /task done <id>")
            return True
        try:
            task_id = int(sub_args[0].lstrip("#"))
        except ValueError:
            await reply.reply_text("Task ID must be a number.")
            return True
        terminal = _terminal_status()
        if _set_status(company_id, task_id, terminal, changed_by):
            t = _get_task(company_id, task_id)
            await reply.reply_text(f"[{terminal}] #{task_id} {t['title'] if t else ''}")
        else:
            await reply.reply_text(f"Task #{task_id} not found or already closed.")
        return True

    # -- /task drop ------------------------------------------------------
    if sub == "drop":
        if not sub_args:
            await reply.reply_text("Usage: /task drop <id>")
            return True
        try:
            task_id = int(sub_args[0].lstrip("#"))
        except ValueError:
            await reply.reply_text("Task ID must be a number.")
            return True
        if _set_status(company_id, task_id, "dropped", changed_by):
            t = _get_task(company_id, task_id)
            await reply.reply_text(f"[dropped] #{task_id} {t['title'] if t else ''}")
        else:
            await reply.reply_text(f"Task #{task_id} not found or already closed.")
        return True

    # -- /task status ----------------------------------------------------
    if sub == "status":
        if len(sub_args) < 2:
            statuses = _get_statuses()
            await reply.reply_text(
                f"Usage: /task status <id> <status>\n\n"
                f"Available: {', '.join(statuses)}, dropped")
            return True
        try:
            task_id = int(sub_args[0].lstrip("#"))
        except ValueError:
            await reply.reply_text("Task ID must be a number.")
            return True
        new_status = " ".join(sub_args[1:]).lower()
        valid = _get_statuses() + ["dropped"]
        if new_status not in valid:
            await reply.reply_text(f"Invalid status. Choose from: {', '.join(valid)}")
            return True
        if _set_status(company_id, task_id, new_status, changed_by):
            t = _get_task(company_id, task_id)
            await reply.reply_text(f"[{new_status}] #{task_id} {t['title'] if t else ''}")
        else:
            await reply.reply_text(f"Task #{task_id} not found or already closed.")
        return True

    # -- /task hill ------------------------------------------------------
    if sub == "hill":
        if len(sub_args) < 2:
            await reply.reply_text("Usage: /task hill <id> <0-100>")
            return True
        try:
            task_id = int(sub_args[0].lstrip("#"))
            position = int(sub_args[1])
        except ValueError:
            await reply.reply_text("Usage: /task hill <id> <0-100>")
            return True
        if not 0 <= position <= 100:
            await reply.reply_text("Hill position must be 0-100.")
            return True
        if _update_hill(company_id, task_id, position, changed_by):
            t = _get_task(company_id, task_id)
            label = _hill_label(position)
            await reply.reply_text(
                f"#{task_id} {t['title'] if t else ''}\n"
                f"{_hill_bar(position)} {position}% — {label}"
            )
        else:
            await reply.reply_text(f"Task #{task_id} not found or already closed.")
        return True

    # -- /task mine ------------------------------------------------------
    if sub == "mine":
        owner = (msg.sender_name or msg.sender_id or "").lower().split("@")[0]
        owner = _resolve_owner(owner)
        tasks = _list_tasks(company_id, owner=owner)
        await reply.reply_text(_fmt_task_list(tasks, title=f"Your tasks (@{owner})"))
        return True

    # -- /task assign ----------------------------------------------------
    if sub == "assign":
        if len(sub_args) < 2:
            await reply.reply_text("Usage: /task assign <id> @name")
            return True
        try:
            task_id = int(sub_args[0].lstrip("#"))
        except ValueError:
            await reply.reply_text("Task ID must be a number.")
            return True
        new_owner = _parse_owner_input(sub_args[1])
        if _reassign(company_id, task_id, new_owner, changed_by):
            t = _get_task(company_id, task_id)
            await reply.reply_text(f"#{task_id} {t['title'] if t else ''} -> @{new_owner}")
        else:
            await reply.reply_text(f"Task #{task_id} not found or already closed.")
        return True

    # -- /task scope -----------------------------------------------------
    if sub == "scope":
        if not sub_args:
            await reply.reply_text("Usage: /task scope <name>")
            return True
        name = " ".join(sub_args).lower()
        if _create_scope(company_id, name):
            await reply.reply_text(f"Scope created: {name}")
        else:
            await reply.reply_text(f"Scope '{name}' already exists.")
        return True

    # -- /task scopes ----------------------------------------------------
    if sub == "scopes":
        scopes = _list_scopes(company_id)
        if scopes:
            lines = ["*Scopes:*\n"]
            for s in scopes:
                count = len(_list_tasks(company_id, scope=s, limit=100))
                lines.append(f"  {s} ({count} open)")
            await reply.reply_text("\n".join(lines))
        else:
            await reply.reply_text("No scopes yet. Create one: /task scope <name>")
        return True

    # -- /task members ---------------------------------------------------
    if sub == "members":
        members = _resolve_members(msg.platform or "whatsapp")
        if members:
            lines = ["*Team members:*\n"]
            for m in members:
                lines.append(f"  @{m['name']}")
            await reply.reply_text("\n".join(lines))
        else:
            await reply.reply_text("No members found. Add admins or user_roles in config.yaml.")
        return True

    # -- /task history ---------------------------------------------------
    if sub == "history":
        if not sub_args:
            await reply.reply_text("Usage: /task history <id>")
            return True
        try:
            task_id = int(sub_args[0].lstrip("#"))
        except ValueError:
            await reply.reply_text("Task ID must be a number.")
            return True
        task = _get_task(company_id, task_id)
        if not task:
            await reply.reply_text(f"Task #{task_id} not found.")
            return True
        changes = _get_task_history(company_id, task_id)
        if not changes:
            await reply.reply_text(f"No history for #{task_id}.")
            return True
        lines = [f"*History for #{task_id}: {task['title']}*\n"]
        for c in changes:
            lines.append(_fmt_change(c))
        await reply.reply_text("\n".join(lines))
        return True

    # -- /task changes ---------------------------------------------------
    if sub == "changes":
        flags = _parse_flags(sub_args)
        days = int(flags.get("days", "7"))
        changes = _get_recent_changes(company_id, days=days)
        if not changes:
            await reply.reply_text(f"No changes in the last {days} days.")
            return True
        lines = [f"*Recent changes* (last {days} days)\n"]
        for c in changes:
            lines.append(_fmt_change(c))
        await reply.reply_text("\n".join(lines))
        return True

    # -- /task board -----------------------------------------------------
    if sub == "board":
        await reply.send_typing()
        url = await _push_board(company_id, reply)
        if url:
            await reply.reply_text(f"Task board: {url}")
        else:
            await reply.reply_text("Could not generate board link.")
        return True

    # -- /task checkin ---------------------------------------------------
    if sub == "checkin":
        text = await _run_checkin(company_id, msg.chat_id)
        await reply.reply_text(text)
        return True

    # -- /task --help ----------------------------------------------------
    if sub in ("--help", "help"):
        await reply.reply_text(__doc__.strip())
        return True

    # Unrecognized subcommand — return False so write-command orchestrator
    # can try NL resolution
    return False


# ---------------------------------------------------------------------------
# Telegram handlers
# ---------------------------------------------------------------------------

async def _tg_task(update, context):
    from cupbots.helpers.channel import IncomingMessage, _tg_reply_ctx
    if not update.message:
        return
    msg = IncomingMessage(
        platform="telegram",
        chat_id=str(update.message.chat_id),
        sender_id=str(update.message.from_user.id),
        sender_name=update.message.from_user.first_name or "",
        text=update.message.text or "",
        command="task",
        args=list(context.args) if context.args else [],
        company_id="",
    )
    r = _tg_reply_ctx(update)
    await handle_command(msg, r)


def register(app: Application):
    register_handler("task_checkin", _handle_checkin_job)
    app.add_handler(CommandHandler("task", _tg_task))
    app.add_handler(CommandHandler("tasks", _tg_task))
