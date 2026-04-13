# dm_onboarding

Welcome first-time DM users with a scripted onboarding flow and Stripe payment links

## Commands
- `/dmonboarding`

## Intent
USE FOR: Configure first-time DM welcome flow with Stripe payment links

## Primitives
```
/dmonboarding stats — Show onboarding stats
/dmonboarding reset <phone> — Reset a user so they see onboarding again
/dmonboarding test — Trigger onboarding on yourself
```

## Rules
- Do NOT invent subcommands — only use commands listed above
- Respect explicit timezones in user input
