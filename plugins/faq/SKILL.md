# faq

Trainable Q&A bot — add FAQ entries, fuzzy search, auto-answer in groups

## Commands
- `/faq`

## Intent
USE FOR: Add or search frequently asked questions for auto-answers in groups
NOT FOR: Knowledge base document search

## Primitives
```
/faq <query> — Search FAQ for an answer
/faq add <question> | <answer> — Add a Q&A entry
/faq remove <id> — Remove an entry
/faq list — List all entries
/faq auto on|off — Toggle auto-answer on plain messages
```

## Rules
- Do NOT invent subcommands — only use commands listed above
- Respect explicit timezones in user input
