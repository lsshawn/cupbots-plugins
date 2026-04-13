# whatsapp_schedule

Schedule WhatsApp messages with AI-parsed natural language

## Commands
- `/wa schedule` (WRITE)
- `/wa scheduled`
- `/wa unschedule` (WRITE)

## Intent
USE FOR: Send a WhatsApp message to a specific person or group at a scheduled time
NOT FOR: Creating calendar events, meetings, or appointments

## Primitives
```
/wa schedule <everything> — Schedule a WhatsApp message (AI-parsed)
/wa scheduled — List all pending scheduled messages
/wa unschedule — Delete scheduled messages
```

## Rules
- Do NOT invent subcommands — only use commands listed above
- For write commands, use the EXACT flag syntax from Primitives above
- Respect explicit timezones in user input
