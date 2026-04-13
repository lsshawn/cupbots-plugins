# contacts

Personal CRM — track contacts, tiers, interactions, and follow-ups

## Commands
- `/crm`
- `/whois`
- `/remember` (WRITE)
- `/addcontact` (WRITE)
- `/contacts`

## Intent
USE FOR: Look up contact info, remember details about people, manage CRM entries, log interactions
NOT FOR: Sending messages to contacts

## Primitives
```
/crm — Show contacts due for a check-in
/whois <name> — Look up a contact (fuzzy match)
/remember <name> — <note> — Add a note about someone
/addcontact <name> — Add a new contact
/contacts [tag|tier] — Search/filter contacts
```

## Rules
- Do NOT invent subcommands — only use commands listed above
- For write commands, use the EXACT flag syntax from Primitives above
- Respect explicit timezones in user input
