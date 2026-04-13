# community

All-in-one WhatsApp community management — gamification, moderation, onboarding, analytics, roles, and scheduled content

## Commands
- `/community`

## Intent
USE FOR: Manage WhatsApp community — gamification, moderation, member onboarding, analytics

## Primitives
```
/community — Show dashboard
/community leaderboard — Top 10 by points
/community points [phone] — Check points
/community award <phone> <pts> [reason] — Award points
/community role list — List roles
/community role add <name> — Create a role
/community role assign <phone> <role> — Assign role
/community role remove <phone> <role> — Remove role
/community analytics [days] — Activity report
/community onboard — Show onboarding config
/community onboard set <key> <value> — Configure onboarding
/community onboard preview — Preview welcome DM
/community onboard send <phone> — Send welcome DM manually
/community onboard on|off — Toggle onboarding
/community mod — Show moderation config
/community mod filter add|remove|list — Manage keyword filters
/community mod warn <phone> [reason] — Warn a user
/community mod history <phone> — View warnings
/community mod kick <phone> — Kick user from group
/community mod spam on|off — Toggle spam detection
/community mod spam limit <N> — Max messages per minute
/community mod warn_limit <N> — Auto-kick after N warnings
/community schedule add <time> <message> — Add recurring content
/community schedule list — List scheduled content
/community schedule remove <id> — Remove scheduled content
/community set <key> <value> — Configure community settings
```

## Rules
- Do NOT invent subcommands — only use commands listed above
- Respect explicit timezones in user input
