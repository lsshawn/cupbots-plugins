# Stripe Plugin

Auto-invite paying customers to your WhatsApp community when they complete Stripe Checkout.

## How it works

```
Customer pays on your landing page
  → Stripe fires checkout.session.completed webhook
  → Launchpad service receives it at /stripe/webhook/{tenant_id}
  → Stores subscription in local SQLite (data/stripe.db)
  → Calls WhatsApp bot API to get invite link + DM the customer
```

Data lives in `launchpad-service/data/stripe.db` — a local SQLite file in WAL mode. Backup is just `cp data/stripe.db data/stripe.db.bak`.

## Setup

### 1. Set env vars on the launchpad service

```bash
# Required — from Stripe Dashboard > Developers > Webhooks > Signing secret
STRIPE_WEBHOOK_SECRET=whsec_...

# Default community JID (can also set per-tenant via /stripe set)
STRIPE_COMMUNITY_JID=120363406663672595@g.us

# Already set if launchpad is running
WA_API_URL=http://127.0.0.1:3100
```

The SQLite database is created automatically on first boot — no migration step needed.

### 2. Add webhook in Stripe Dashboard

**Developers > Webhooks > Add endpoint:**

- URL: `https://your-domain.com/stripe/webhook/{tenant_id}`
- Events: `checkout.session.completed`, `customer.subscription.deleted`

Replace `{tenant_id}` with your company_id (e.g. `default`).

### 3. Enable phone collection in Stripe Checkout

```js
phone_number_collection: { enabled: true }
```

Without a phone number, the plugin can't send the WhatsApp invite.

### 4. Configure community JID

Send `/wajid` in the WhatsApp group you want to invite people to, then:

```
/stripe set community_jid 120363406663672595@g.us
```

### 5. Reload the bot

```
/reload
```

## Bot commands

| Command | Description |
|---|---|
| `/stripe` or `/stripe status` | Show subscriber stats |
| `/stripe subs` | List recent subscribers |
| `/stripe set community_jid <jid>` | Set which group to invite to |
| `/stripe set welcome_msg <text>` | Custom invite message |
| `/stripe webhook` | Show your Stripe webhook URL |
| `/stripe test <phone>` | Test invite flow with a phone number |

## API endpoints (launchpad service)

| Endpoint | Auth | Description |
|---|---|---|
| `POST /stripe/webhook/:tenantId` | Stripe signature | Webhook receiver |
| `GET /stripe/subs/:tenantId` | Bearer token | List subscriptions |
| `POST /stripe/config/:tenantId` | Bearer token | Update tenant config |

## Data safety

- SQLite WAL mode — readers never block writers, crash-safe
- `busy_timeout = 5000ms` — concurrent writes queue instead of failing
- `synchronous = NORMAL` — balanced durability/performance
- CHECK constraints on status field
- Stripe signature verification prevents spoofed webhooks
- Returns 200 to Stripe even on handler errors to prevent infinite retries (errors are logged)

## Backup

```bash
cp launchpad-service/data/stripe.db launchpad-service/data/stripe.db.bak
```

Or use SQLite's online backup:

```bash
sqlite3 launchpad-service/data/stripe.db ".backup 'stripe.db.bak'"
```

## Cancellation handling

When `customer.subscription.deleted` fires, the subscription is marked as cancelled. Group removal is not automatic — handle manually or extend later.
