# note

Create and list zk-format markdown notes

## Commands
- `/note` (WRITE)
- `/notes`

## Intent
USE FOR: Create or search personal notes, quick memos, zettelkasten entries
NOT FOR: Bookmarks, ideas, or knowledge base documents

## Primitives
```
/note <title> <body> — Create a zk note
/note <title> — Create a note (body via reply or empty)
/notes — Show recent notes
```

## Rules
- Do NOT invent subcommands — only use commands listed above
- For write commands, use the EXACT flag syntax from Primitives above
- Respect explicit timezones in user input
