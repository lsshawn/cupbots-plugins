# Launchpad Plugin

Deploy landing pages from WhatsApp. Text a description, get a live URL back.

## Commands

| Command | Description |
|---------|-------------|
| `/lp <description>` | Create a new landing page from a text prompt |
| `/lp sites` | List your deployed sites |
| `/lp edit <site> — <change>` | Edit an existing page |
| `/lp status <site>` | Check deploy status and analytics link |
| `/lp domain <site> <domain>` | Connect a custom domain |

## Examples

```
/lp A coffee shop in Brooklyn, warm earthy tones, shows menu and hours
/lp sites
/lp edit brooklyn-coffee — change the headline to "Fresh Roasted Daily"
/lp domain brooklyn-coffee mycoffeeshop.com
```

## How it Works

This plugin is a thin client. All heavy lifting happens in the **Launchpad Service** (separate repo):

```
You text a prompt
  → Plugin sends it to Launchpad Service API
  → AI designs and builds a landing page
  → Deploys to Cloudflare Pages
  → Plugin texts you back the live URL
```

## Setup

After installing, configure the service connection:

```
/plugin config launchpad LAUNCHPAD_API_URL https://launchpad.yourdomain.com
/plugin config launchpad LAUNCHPAD_API_KEY your-api-key
```

## Pricing

| Tier | Price | Sites | Custom Domain | Iterations |
|------|-------|-------|---------------|------------|
| Starter | $29/mo | 1 | cupbots subdomain | 5/mo |
| Pro | $79/mo | 5 | Yes | Unlimited |
| Agency | $199/mo | 20 | Yes, white-label | Unlimited |

## Related

- **Launchpad Service**: `launchpad-service/` — the deployment API
- **Architecture**: `ARCHITECTURE.md` — full system design
