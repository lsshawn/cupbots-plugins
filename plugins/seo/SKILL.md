# seo

SEO monitoring, keyword tracking, content decay detection, and blog draft generation. Supports GA4 and Umami analytics backends. Delivers weekly intelligence reports via WhatsApp.

## Commands
- `/seo`

## Intent
USE FOR: SEO analytics, keyword tracking, content decay, blog drafts, Google Analytics, Umami, traffic reports, SERP rankings
NOT FOR: Social media posting, advertising, PPC campaigns

## Primitives
```
/seo connect <domain> — Register site with GA4 (starts OAuth)
/seo connect <domain> --umami — Register site with Umami backend
/seo sites — List registered sites
/seo status [domain] — Quick health summary
/seo pull [domain] — Manual data pull (analytics + keywords + decay)
/seo autosend on|off [domain] — Toggle automatic weekly reports
/seo schedule — View/edit auto-scheduling of recurring jobs
/seo report [domain] — Full weekly intelligence report (with action plan)
/seo plan [domain] — Generate this week's prioritized actions
/seo actions [list|done <id>] — Manage action items
/seo keywords [domain] — Keyword rankings + suggestions
/seo decay [domain] — Flagged decaying pages
/seo search [domain] — Google Search Console insights (CTR opportunities)
/seo backlinks [domain] — Backlink summary + new/lost
/seo conversion [domain] — High-traffic-low-conversion pages
/seo health [domain] — Web Vitals + uptime + form check status
/seo draft <topic> [--site domain] — Generate SEO blog draft
/seo outreach <type> [--site dom] — Draft outreach email (partnership/linkbuilding/etc)
/seo formcheck [domain] [name] — Manually trigger a form submission check
```

## Examples
- "set up seo tracking for example.com" → `/seo connect example.com`
- "how is my seo doing?" → `/seo status` or `/seo report`
- "write a blog post about best CRM for agencies" → `/seo draft "best CRM for agencies" --site example.com`

## Rules
- Do NOT invent subcommands — only use commands listed above
- Respect explicit timezones in user input
