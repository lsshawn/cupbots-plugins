# finance_query

Beancount ledger queries.

## Commands
- `/fbal`
- `/fsearch`
- `/fquery`
- `/faccount`

## Primitives
```
/fbal [personal] [filter] — Show account balances
/fsearch [personal] <query> — AI-powered journal search
/fquery [personal] <BQL> — Run raw BQL query
/faccount [personal] [filter] — List chart of accounts
```

## Rules
- Do NOT invent subcommands — only use commands listed above
- Respect explicit timezones in user input
