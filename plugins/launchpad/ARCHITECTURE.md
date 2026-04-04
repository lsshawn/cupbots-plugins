# Launchpad — Architecture

WhatsApp-native landing page builder. Text a prompt, get a deployed site back.

## System Overview

```
User (WhatsApp/Telegram)
  │
  │  /lp A coffee shop in Brooklyn with warm tones
  ▼
CupBots Plugin (launchpad.py)         ← thin client, marketplace-safe
  │
  │  POST /sites { prompt, company_id }
  ▼
Launchpad Service (separate repo)     ← all secrets & heavy lifting here
  │
  ├─ 1. Prompt → Claude formats a Stitch-ready design brief
  ├─ 2. Google Stitch → generates HTML design
  ├─ 3. Claude converts HTML → SvelteKit page (standard template)
  ├─ 4. Injects Umami tracking + SEO meta + llms.txt
  ├─ 5. Git push → Cloudflare Pages auto-deploys
  └─ 6. Returns { slug, url, status } to plugin
         │
         ▼
      Plugin texts back the live URL
```

## Why Two Projects

| Concern | Plugin (this repo) | Launchpad Service (separate) |
|---------|-------------------|------------------------------|
| Lives in | cupbots-plugins marketplace | Private repo (e.g. `launchpad-service`) |
| Exposed to | Anyone who installs the plugin | Only you |
| Contains | WhatsApp UX, local site tracking | API keys, Cloudflare tokens, Stitch integration, SvelteKit template, build pipeline |
| Secrets | None — reads LAUNCHPAD_API_URL + API_KEY from config | All of them — Cloudflare, Stitch, Umami, Claude API |
| Deploy | Installed via `/plugin install launchpad` | Runs on your VPS / Cloudflare Worker |

The plugin is a **dumb client**. It sends prompts, receives URLs. All design generation, conversion, and deployment happens in the service.

## Launchpad Service — API Spec

Base URL: configured per tenant via `LAUNCHPAD_API_URL`
Auth: `Authorization: Bearer <LAUNCHPAD_API_KEY>`

### POST /sites
Create a new landing page.

```json
// Request
{
  "prompt": "A coffee shop in Brooklyn, warm earthy tones, shows menu and hours",
  "company_id": "tenant_123"
}

// Response
{
  "slug": "brooklyn-coffee",
  "url": "https://brooklyn-coffee.cupbots.pages.dev",
  "status": "live",
  "analytics_url": "https://umami.cupbots.dev/share/brooklyn-coffee"
}
```

### POST /sites/:slug/edit
Edit an existing page.

```json
// Request
{
  "change": "change the headline to Fresh Roasted Daily",
  "company_id": "tenant_123"
}

// Response
{
  "slug": "brooklyn-coffee",
  "url": "https://brooklyn-coffee.cupbots.pages.dev",
  "status": "live"
}
```

### GET /sites/:slug?company_id=tenant_123
Get site status and analytics.

```json
// Response
{
  "slug": "brooklyn-coffee",
  "url": "https://brooklyn-coffee.cupbots.pages.dev",
  "status": "live",
  "analytics_url": "https://umami.cupbots.dev/share/brooklyn-coffee",
  "custom_domain": null,
  "created_at": "2025-04-04T01:00:00Z",
  "updated_at": "2025-04-04T01:05:00Z"
}
```

### POST /sites/:slug/domain
Connect a custom domain.

```json
// Request
{
  "domain": "mycoffeeshop.com",
  "company_id": "tenant_123"
}

// Response
{
  "dns_instructions": "Add a CNAME record:\n  mycoffeeshop.com → brooklyn-coffee.cupbots.pages.dev\n\nSSL will be provisioned automatically. Allow 5-10 minutes."
}
```

## Launchpad Service — Internal Pipeline

### Step 1: Prompt Enrichment (Claude)
Takes the raw user prompt and generates:
- A structured design brief for Google Stitch (tone, sections, colors, copy)
- SEO metadata (title, description, OG tags)
- Site slug

### Step 2: Design Generation (Google Stitch)
- Sends the design brief to Stitch API
- Receives complete HTML with inline styles
- This is the "design brain" — Claude doesn't do visual design

### Step 3: SvelteKit Conversion (Claude)
- Takes Stitch HTML output
- Converts to a SvelteKit `+page.svelte` using the standard template
- Applies the project's Tailwind theme + DaisyUI components
- Maps Stitch colors → CSS variables in app.css

### Step 4: Injection
- **Umami**: adds tracking script to `app.html` with site-specific ID
- **SEO**: injects meta tags, Open Graph, structured data (LocalBusiness, etc.)
- **AEO**: generates `llms.txt` / `llms-full.txt` for AI search engines
- **Sitemap**: auto-generates `sitemap.xml`

### Step 5: Deploy (Cloudflare Pages)
- Each tenant site = a Cloudflare Pages project
- Created via Cloudflare API (`POST /accounts/:id/pages/projects`)
- Git-based deploy: push to a repo branch, Pages auto-builds
- OR direct upload via Cloudflare Pages Direct Upload API (no git needed)

### Step 6: Iteration
- Small changes (headline, copy, colors): Claude edits the existing `+page.svelte`, redeploys
- Big changes ("redesign the whole thing"): re-runs full Stitch pipeline
- Current page state stored in service DB, not regenerated from scratch

## Data Model (Service DB)

```sql
CREATE TABLE sites (
    id TEXT PRIMARY KEY,              -- uuid
    company_id TEXT NOT NULL,         -- tenant isolation
    slug TEXT NOT NULL UNIQUE,
    prompt TEXT NOT NULL,             -- original user prompt
    current_page TEXT NOT NULL,       -- current +page.svelte source
    current_css TEXT NOT NULL,        -- current app.css source  
    cloudflare_project_id TEXT,
    custom_domain TEXT,
    umami_site_id TEXT,
    status TEXT NOT NULL DEFAULT 'building',
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE edits (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    site_id TEXT NOT NULL REFERENCES sites(id),
    change_request TEXT NOT NULL,     -- what the user asked for
    diff TEXT NOT NULL,               -- what changed
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
```

## Deployment Options for the Service

| Option | Pros | Cons |
|--------|------|------|
| **Hono on VPS** (recommended) | Full control, simple, cheap, same stack you know | You manage uptime |
| **Cloudflare Worker** | Zero ops, scales free | 30s CPU limit may be tight for Stitch + Claude calls |
| **Hono on Fly.io** | Auto-scale, cheap, no cold starts | Another provider to manage |

**Recommendation:** Start with Hono on your existing VPS. The pipeline involves sequential API calls (Claude → Stitch → Claude → Cloudflare) that can take 30-60s. A VPS has no timeout limits. Move to Workers later if needed.

## Cloudflare Pages — Why Over Vercel

- **Free tier**: unlimited sites, 500 builds/mo, free custom domains + SSL
- **Vercel**: 1 project free, $20/mo per additional project
- **API**: Cloudflare Pages Direct Upload = no git needed, just push static files
- **Custom domains**: API-driven, auto-SSL, simple CNAME
- **At 50 tenants**: Cloudflare = $0. Vercel = $1,000/mo.

## Build Order

### Build the service FIRST (launchpad-service repo)
1. Hono API skeleton with auth middleware
2. Cloudflare Pages project creation + direct upload deploy
3. Claude prompt enrichment → Stitch HTML → SvelteKit conversion pipeline
4. Umami + SEO injection
5. Edit/iteration endpoint
6. Custom domain endpoint

### Then wire up the plugin
The plugin (this file) is already built. Once the service is running, just configure:
```
/plugin config launchpad LAUNCHPAD_API_URL https://launchpad.yourdomain.com
/plugin config launchpad LAUNCHPAD_API_KEY <your-key>
```

## Pricing Tiers (Plugin Side)

| Tier | Price | Sites | Custom Domain | Iterations |
|------|-------|-------|---------------|------------|
| Starter | $29/mo | 1 | cupbots subdomain | 5/mo |
| Pro | $79/mo | 5 | Yes | Unlimited |
| Agency | $199/mo | 20 | Yes, white-label | Unlimited |

Tier enforcement happens in the service, not the plugin. The plugin just passes `company_id` and the service checks limits.
