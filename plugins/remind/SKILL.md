# remind

Set reminders with natural language time parsing

## Commands
- `/remind` (WRITE)
- `/reminders`

## Intent
USE FOR: Set a personal reminder to do something at a specific time
NOT FOR: Creating calendar events or sending WhatsApp messages

## Primitives
```
/remind <time> <message> — Set a reminder
/reminders — List pending reminders
```

## Rules
- Do NOT invent subcommands — only use commands listed above
- For write commands, use the EXACT flag syntax from Primitives above
- Respect explicit timezones in user input
