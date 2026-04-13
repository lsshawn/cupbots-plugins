# stripe

Stripe subscriptions — auto-invite paying customers to your WhatsApp community

## Commands
- `/subscription`
- `/community`
- `/stripe`
- `/stripe status`
- `/stripe subs`
- `/stripe community`
- `/stripe set`
- `/stripe test`
- `/stripe webhook`

## Intent
USE FOR: Manage Stripe subscriptions, check payment status, configure payment links

## Primitives
```
/subscription — View your subscription & manage billing (customer portal)
/stripe status — Show subscriber stats (admin)
/stripe subs — List recent subscribers (admin)
/stripe community — Show community invite config (admin)
/stripe set <key> <val> — Configure (community_jid, welcome_msg) (admin)
/stripe test <phone> — Test invite flow for a phone number (admin)
/stripe webhook — Show your Stripe webhook URL (admin)
```

## Rules
- Do NOT invent subcommands — only use commands listed above
- Respect explicit timezones in user input
