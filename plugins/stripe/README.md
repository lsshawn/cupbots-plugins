# Stripe Plugin

Subscription management commands for WhatsApp. The core Stripe webhook handling lives in the framework (`wa_router.py`); this plugin provides user-facing commands.

## Setup

### 1. Add Stripe keys to config.yaml

```yaml
stripe:
  mode: test  # "test" or "live"
  live:
    secret_key: sk_live_...
    webhook_secret: whsec_...
    payment_links:
      inner_circle: https://buy.stripe.com/xxx
  test:
    secret_key: sk_test_...
    webhook_secret: whsec_...
    payment_links:
      inner_circle: https://buy.stripe.com/test_xxx
  communities:
    inner_circle:
      groups:
        - jid: "120363xxx@g.us"
          max_members: 1024
      welcome: "Welcome! 🎉"
```

### 2. Add webhook in Stripe Dashboard

- URL: `https://your-hub.com/webhooks/stripe/<bot_id>` (if using hub) or `https://your-domain.com/webhooks/stripe` (direct)
- Events: `checkout.session.completed`, `customer.subscription.updated`, `customer.subscription.deleted`, `invoice.payment_failed`, `invoice.payment_succeeded`

### 3. Enable phone collection in Stripe Checkout

Without a phone number, the bot can't send the WhatsApp invite.

## Bot commands

| Command | Description |
|---|---|
| `/stripe` or `/stripe status` | Show subscriber stats |
| `/stripe subs` | List recent subscribers |
| `/stripe community on\|off` | Toggle auto-add to community groups |
| `/subscription` | (User-facing) View/cancel own subscription |

## Testing with Stripe CLI

```bash
stripe listen --forward-to http://localhost:3102/webhooks/stripe/my-bot
```

Set the `whsec_...` output as `stripe.test.webhook_secret` in config.yaml and `/reload`.
