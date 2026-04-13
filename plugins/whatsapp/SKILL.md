# whatsapp

Control WhatsApp from Telegram — read, reply, search, summarize

## Commands
- `/wa`

## Intent
USE FOR: Control WhatsApp from Telegram — read, reply, search, summarize chats

## Primitives
```
/wa — Show WhatsApp status and recent chats
/wa chats — List chats with unread counts
/wa read <n> — Read last N messages from a chat
/wa reply — Reply to a WhatsApp chat
/wa search <q> — Search across all WhatsApp messages
/wa <question> — Ask anything about your WhatsApp messages (AI)
/wa summary — Summarize a WhatsApp chat using Claude
/wa schedule <…> — Schedule a WhatsApp message (AI-parsed)
/wa scheduled — List all pending scheduled messages
/wa unschedule — Delete scheduled messages
/wa sync — Thorough sync of all chat history
/wa pair — Scan new QR code
/wa reconnect — Reconnect without re-pairing
/wajid — Show this chat's JID and your sender ID
```

## Rules
- Do NOT invent subcommands — only use commands listed above
- Respect explicit timezones in user input
