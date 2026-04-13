# Mailwatch

Watch email inboxes and process messages with rules. Supports multiple IMAP accounts and AgentMail for outbound sending + inbound webhook reception.

## Quickest path: forward Gmail to AgentMail

If you want the bot to act on your Gmail (Workspace or consumer), the **simplest path is to set up a forwarding rule** from gmail.com to your AgentMail address. The bot processes forwarded emails through the same rule pipeline as polled IMAP — no OAuth, no app password, no per-vendor integration.

**Setup (1 minute):**

1. In gmail.com → Settings → Filters and Blocked Addresses → Create a new filter
2. Add a filter (or "Has the words: -from:nobody" to forward everything)
3. Choose **Forward it to** → enter your AgentMail address (e.g. `pa@ai.cupbots.com`)
4. Save

That's it. Forwarded emails arrive at AgentMail, hit the webhook, and run through your mailwatch rules. The same pattern works for Outlook, Fastmail, ProtonMail, Yahoo, and any other provider with forwarding rules.

For deeper integration where the bot needs to **mark emails as read in your real inbox** or **fetch them via IMAP polling** (without forwarding), use the IMAP backend below.

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
2. In AgentMail, set webhook URL to `https://hub.cupbots.com/webhooks/agentmail/<your-bot-id>`

### Outbound (AgentMail)

When `agentmail_api_key` is configured, the `draft_reply` action can send emails via AgentMail:
- LLM classifies the email category and drafts a reply
- Auto-send rules determine if the category sends immediately or needs WhatsApp approval (react with a thumbs up/down)
- Reply-To is set dynamically from the inbound email's To address

## Multiple Inboxes

All mailboxes are configured in config.yaml under `plugin_settings.mailwatch.mailboxes`. You can also add/remove via slash commands (which write to config.yaml).

```
/mailwatch account add user@gmail.com abcd-efgh-ijkl-mnop
/mailwatch account add user@company.com p4ssw0rd imap.company.com
/mailwatch account list
/mailwatch account remove user@gmail.com
```

For Gmail, use an [App Password](https://myaccount.google.com/apppasswords) (not your regular password). Default IMAP host is `imap.gmail.com` — pass a third argument for other providers.

## Config

All settings live in `plugin_settings.mailwatch` in config.yaml:

```yaml
plugin_settings:
  mailwatch:
    mailwatch_notify_chat: '60123456789@s.whatsapp.net'  # global fallback notify chat

    # Mailboxes (each polled independently)
    mailboxes:
      - address: client@gmail.com
        app_password: xxxx-xxxx-xxxx-xxxx
        imap_host: imap.gmail.com              # optional, default: imap.gmail.com
        notify_chat: '60123456789@s.whatsapp.net'  # optional, overrides global
      - address: user@company.com
        app_password: p4ssw0rd
        imap_host: imap.company.com

    # Rules (first match wins; managed via /mailwatch add or edit here)
    rules:
      - type: attachment
        pattern: ".ics"
        action: calendar
      - type: keyword
        field: subject               # subject, sender, body, any (default)
        pattern: "invoice"
        action: notify
      - type: ai
        pattern: "is this a meeting invitation?"
        action: calendar
    ai_fallback: true                # AI triage for unmatched emails (~$0.01/email)

    # AgentMail (optional — outbound sending + inbound webhook)
    agentmail_api_key: am_xxxxxxxxxx
    agentmail_address: pa@ai.cupbots.com  # inbox address (used as sender + inbox_id)
    agentmail_display_name: "ClientName Assistant"
    agentmail_reply_to: admin@clientdomain.com
    agentmail_webhook_secret: whsec_xxxxxxxxxx  # Svix secret for inbound via hub

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

Examples:
```
/mailwatch add keyword subject "invoice" -> notify
/mailwatch add regex sender ".*@acme\.com" -> notify
/mailwatch add attachment .ics -> calendar
/mailwatch add ai "is this a client project update?" -> crm_update
```
