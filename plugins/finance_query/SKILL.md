# finance_query

Beancount ledger queries. Access via `/money search`, `/money query`, `/money bal`, `/money accounts`.

## Commands (standalone, prefer /money variants)
- `/fbal` → use `/money bal`
- `/fsearch` → use `/money search`
- `/fquery` → use `/money query`
- `/faccount` → use `/money accounts`

## Rules
- `/money search` accepts full natural language — pass the ENTIRE user query as-is
- CRITICAL: Relay the full output verbatim — do NOT summarize or reformat
