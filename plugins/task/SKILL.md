# task

Basecamp-style task management from WhatsApp — flat lists, custom statuses, hill charts, change log, automatic check-ins

## Commands
- `/task` (WRITE)
- `/task add`
- `/task list`
- `/task done`
- `/task drop`
- `/task status`
- `/task hill`
- `/task mine`
- `/task assign`
- `/task scope`
- `/task scopes`
- `/task members`
- `/task history`
- `/task changes`
- `/task board`
- `/task checkin`
- `/tasks`

## Intent
USE FOR: Create tasks, mark tasks done/dropped, change task status, assign tasks, update hill progress, manage scopes/projects, view task history and board
NOT FOR: Reminders (use /remind), calendar events (use /event), bookmarks, notes

## Primitives
```
/task add <title> [ — owner @name] [--scope <project>] [--due <date>]
/task list [ — scope <project>] [--owner @name] [--all]
/task done <id> — Mark task complete
/task drop <id> — Drop a task (won't do it)
/task status <id> <status> — Set task status
/task hill <id> <0-100> — Update hill progress
/task mine — Show my open tasks
/task assign <id> @name — Reassign a task
/task scope <name> — Create a new scope (project)
/task scopes — List active scopes
/task members — List team members
/task history <id> — Show change log for a task
/task changes [--days N] — Recent changes across all tasks
/task board — Get a private kanban board link
/task checkin — Trigger a check-in now
/tasks — Alias for /task list
```

## Examples
- "add a task to fix login bug in the backend scope" → `/task add Fix login bug --scope backend`
- "add task order new chairs for sarah by friday" → `/task add Order new chairs --owner sarah --due friday`
- "show backend tasks" → `/task list --scope backend`
- "show all tasks" → `/tasks --all`

## Rules
- Do NOT invent subcommands — only use commands listed above
- For write commands, use the EXACT flag syntax from Primitives above
- Respect explicit timezones in user input
