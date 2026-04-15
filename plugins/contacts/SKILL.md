# contacts

Personal CRM — all contact management under /crm.

## Commands
- `/crm`

## Primitives
```
/crm — Overdue check-ins
/crm whois <name> — Look up a contact (fuzzy match)
/crm remember <name> -- <note> — Add a note about someone
/crm list [tier|tag|query] — Search/filter contacts
/crm touched <name> [note] — Log an interaction
```

## Examples
```
/crm                                Overdue check-ins
/crm whois John                     Look up John
/crm remember Sarah -- met at conf, interested in API
/crm list                           Contact summary by tier
/crm list A                         All tier-A contacts
/crm list developer                 Search by tag/keyword
/crm touched Ahmad called to catch up
```

## Rules
- Do NOT invent subcommands — only use commands listed above
- Use `--` or `—` to separate name from note in /crm remember
- Contacts are fuzzy-matched by name
