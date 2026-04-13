# mailwatch

Watch email inboxes (IMAP or AgentMail) and process unread messages with rules — keyword, regex, attachment, or AI matching with actions like notify, CRM update, calendar import, and draft replies. Send replies via AgentMail with auto-send rules or WhatsApp approval. For Gmail integration, set up a forwarding rule to your AgentMail address — works for both Workspace and consumer Gmail with zero OAuth.

## Commands
- `/mailwatch`

## Intent
USE FOR: Monitor email inboxes, forward matching emails to WhatsApp, and send replies via AgentMail

## Primitives
```
/mailwatch — Show help
/mailwatch status — Connection status, last poll, rule count
/mailwatch rules — List all rules
/mailwatch add <rule> — Add a rule (see examples)
/mailwatch remove <id> — Remove a rule
/mailwatch start — Test connection and begin polling
/mailwatch stop — Stop polling
/mailwatch test <rule_id> — Test a rule against recent emails
/mailwatch account add <email> <app_password> [host] — Add an email account (saves to config.yaml)
/mailwatch account remove <email> — Remove an account (from config.yaml)
/mailwatch account list — List all accounts
/mailwatch send <to> <subject> -- <body> — Send email via AgentMail
/mailwatch sent [N] — Show N most recent sent emails (default 10)
/mailwatch autosend — Show auto-send rules
/mailwatch send user@example.com "Meeting follow-up" — Hi, just following up...
```

## Rules
- Do NOT invent subcommands — only use commands listed above
- Respect explicit timezones in user input
