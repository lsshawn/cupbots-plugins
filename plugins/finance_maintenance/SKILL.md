# finance_maintenance

Journal maintenance tools.

## Commands
- `/validate`
- `/fxsync`
- `/void`
- `/edit`
- `/summary`

## Primitives
```
/validate [personal] — Run bean-check
/fxsync — Fetch missing FX rates
/void [personal] <search> — Void an entry (reversing entry)
/edit [personal] <search> — Edit entry with LLM assistance
/summary [personal] — Regenerate journal summary
```

## Rules
- Do NOT invent subcommands — only use commands listed above
- Respect explicit timezones in user input
