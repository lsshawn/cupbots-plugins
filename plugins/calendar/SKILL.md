# calendar

Calendar management via CalDAV or Google Calendar

## Commands
- `/cal`
- `/event` (WRITE)

## Intent
USE FOR: Create, view, edit, or delete calendar events, meetings, appointments, huddles. Recurring event schedules. Manage availability preferences (no weekends, work hours) and refuse overlapping events.
NOT FOR: Sending WhatsApp messages to someone at a scheduled time

## Primitives
```
/cal                                       — today's events
/cal week                                  — next 7 days
/cal <N>                                   — next N days (e.g. /cal 5)
/cal <date>                                — events on a date (e.g. /cal 8 apr)
/cal free [date]                           — free slots
/event create --title X --start 2026-04-09T14:00 --end 2026-04-09T15:00 [--location L] [--rrule R]
/event create --title X --start 2026-04-09T14:00 --duration 30m
/event delete --uid <uid>                  — delete by exact UID (get UID from /event search)
/event delete --query "standup"            — delete by keyword
/event search --query "standup" [--days 30] — list matching events with UIDs
```

## Examples
- "schedule a dentist appointment tomorrow at 2pm for 1 hour" → `/event create --title "Dentist" --start 2026-04-11T14:00 --duration 1h`
- "recurring meeting Wednesday 10am EST with Larry" → `/event create --title "Meeting with Larry" --start 2026-04-16T10:00-05:00 --duration 1h --rrule FREQ=WEEKLY;BYDAY=WE`
- "delete the standup meeting" → first `/event search --query "standup"` to get UID, then `/event delete --uid <uid>`
- "what's on my calendar this week?" → `/cal week`
- "am I free Thursday afternoon?" → `/cal free thursday`

## Rules
- Do NOT invent subcommands like `/cal list`, `/cal add`, `/event list` — they DO NOT exist
- For write commands, use the EXACT flag syntax from Primitives above
- Respect explicit timezones: if user says "10am EST", use EST offset (-05:00), do NOT convert to your default timezone
- Always get UID from `/event search` before deleting
- Use `--duration` for simpler event creation when end time isn't specified
- For recurring events, use `--rrule` with iCalendar RRULE format
- If the user only gave a date/time with no subject (e.g. "next wed", "tomorrow 10am"), refuse with reason "no subject given"
- DATE PARSING: when a numeric date AND time are already present (e.g. "5/5 9.15am"), treat the rest of the text as the TITLE — even if it contains day-like words (sun, mon, wed, fri). These are often part of names (Sunway, Monday.com) not date modifiers. Only treat a word as a day-of-week when no numeric date is given.
