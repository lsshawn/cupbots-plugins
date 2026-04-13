# wiki

AI-maintained wiki that synthesizes entity profiles from raw documents, emails, and calendar events. Implements the LLM Wiki pattern for incremental knowledge synthesis.

## Commands
- `wiki`

## Intent
USE FOR: Search wiki, find entity info, ingest documents, manage knowledge base, look up people or companies
NOT FOR: General notes, bookmarks, or reminders

## Primitives
```
/wiki log [ — limit N]      Show recent actions
```

## Rules
- Do NOT invent subcommands — only use commands listed above
- Respect explicit timezones in user input
