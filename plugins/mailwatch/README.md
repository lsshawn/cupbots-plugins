# Mailwatch

Watch email inboxes and process messages with rules. Supports multiple IMAP accounts and AgentMail for outbound sending + inbound webhook reception.

## How It Works

### Inbound (IMAP Polling)

Polls each configured mailbox every 5 minutes for **unread (UNSEEN) emails only**.

- Emails you've already read in your mail client (Gmail, Outlook, etc.) are **skipped** — they're marked as Seen and won't be picked up.
- If an unread email doesn't match any rule, it **stays unread** so newly added rules can catch it on the next poll.
- Once a rule matches and the action executes, the email is marked as Seen and recorded in `processed_emails` to prevent duplicates.

### Inbound (AgentMail Webhook)

Emails sent or forwarded to your AgentMail address are received via webhook, relayed through the CupBots Hub, and processed through the **same rule-matching pipeline** as IMAP-polled emails.

Flow: `AgentMail -> POST /webhooks/agentmail/:botId -> Hub -> WebSocket -> Bot -> mailwatch`

Setup:
1. Set `agentmail_webhook_secret` in plugin config (Svix signing secret from AgentMail dashboard, starts with `whsec_`)
2. In AgentMail, set webhook URL to `https://hub.cupbots.dev/webhooks/agentmail/<your-bot-id>`

### Outbound (AgentMail)

When `agentmail_api_key` is configured, the `draft_reply` action can send emails via AgentMail:
- LLM classifies the email category and drafts a reply
- Auto-send rules determine if the category sends immediately or needs WhatsApp approval (react with a thumbs up/down)
- Reply-To is set dynamically from the inbound email's To address

## Multiple Inboxes

Add multiple email accounts via slash commands. Each account is polled independently.

```
/mailwatch account add user@gmail.com abcd-efgh-ijkl-mnop
/mailwatch account add user@company.com p4ssw0rd imap.company.com
/mailwatch account list
/mailwatch account remove user@gmail.com
```

The first account can also be configured via `plugin_settings.mailwatch` in config.yaml (shared config). Additional accounts must be added via `/mailwatch account add`.

For Gmail, use an [App Password](https://myaccount.google.com/apppasswords) (not your regular password). Default IMAP host is `imap.gmail.com` — pass a third argument for other providers.

## Config

All settings live in `plugin_settings.mailwatch` in config.yaml:

```yaml
plugin_settings:
  mailwatch:
    # IMAP (required for polling)
    mailwatch_email: client@gmail.com
    mailwatch_app_password: xxxx-xxxx-xxxx-xxxx
    mailwatch_imap_host: imap.gmail.com
    mailwatch_notify_chat: '60123456789@s.whatsapp.net'

    # AgentMail (optional)
    agentmail_api_key: am_xxxxxxxxxx
    agentmail_inbox_id: inbox_xxxxxxxx
    agentmail_address: pa@ai.cupbots.com
    agentmail_display_name: "ClientName Assistant"
    agentmail_reply_to: admin@clientdomain.com
    agentmail_webhook_secret: whsec_xxxxxxxxxx  # for inbound webhook verification

    # Auto-send rules (unlisted categories require WhatsApp approval)
    auto_send_rules:
      - category: meeting_confirmation
        auto_send: true
      - category: invoice_acknowledgment
        auto_send: true
```

## Commands

```
/mailwatch                  — Show help
/mailwatch status           — Connection status, last poll, rule count
/mailwatch rules            — List all rules
/mailwatch add <rule>       — Add a rule (see below)
/mailwatch remove <id>      — Remove a rule
/mailwatch start            — Test connection and begin polling
/mailwatch stop             — Stop polling
/mailwatch test <rule_id>   — Test a rule against recent emails
/mailwatch account add <email> <app_password> [host]  — Add an email account
/mailwatch account remove <email>  — Remove an account
/mailwatch account list     — List all accounts
/mailwatch send <to> <subject> -- <body>  — Send email via AgentMail
/mailwatch sent [N]         — Show N most recent sent emails
/mailwatch autosend         — Show auto-send rules
```

## Rules

Format: `/mailwatch add <type> [field] <pattern> -> <action>`

| Type | Description |
|------|-------------|
| `keyword` | Case-insensitive keyword match |
| `regex` | Regular expression match |
| `attachment` | Match by file extension (e.g. `.pdf`, `.ics`) |
| `ai` | LLM-based classification (natural language prompt) |

| Field | Description |
|-------|-------------|
| `subject` | Match against subject only |
| `sender` | Match against sender only |
| `body` | Match against body only |
| `any` | Match against all fields (default) |

| Action | Description |
|--------|-------------|
| `notify` | Forward to WhatsApp notification chat |
| `calendar` | Import .ics attachments to calendar |
| `crm_update` | Update wiki/CRM entities |
| `draft_reply` | AI-draft a reply (sends via AgentMail if configured) |
| `custom` | Custom action handler |

Examples:
```
/mailwatch add keyword subject "invoice" -> notify
/mailwatch add regex sender ".*@acme\.com" -> notify
/mailwatch add attachment .ics -> calendar
/mailwatch add ai "is this a client project update?" -> crm_update
```
