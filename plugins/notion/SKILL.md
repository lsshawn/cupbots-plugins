# notion

Connect Notion pages and databases to WhatsApp

## Commands
- `/notion`

## Intent
USE FOR: Read, search, or create pages in Notion workspace

## Primitives
```
/notion — Show linked sources and status
/notion link <name> <notion-url> — Link a Notion page or database
/notion unlink <name> — Remove a linked source
/notion sources — List all linked sources
/notion read <name> [query] — Read/search a source
/notion add <name> <title> [-- k=v, ...] — Add entry to a database
/notion update <name> <title> -- k=v, ... — Update an entry
/notion sync <name> — Pull changes, LLM-summarize, post
/notion add clients "Acme Corp" — status=Active, contact=John
```

## Rules
- Do NOT invent subcommands — only use commands listed above
- Respect explicit timezones in user input
