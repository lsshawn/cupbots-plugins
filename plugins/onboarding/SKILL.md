# onboarding

Welcome new members with guided setup wizard, custom messages, rules, and DM welcomes

## Commands
- `/onboard`

## Intent
USE FOR: Configure welcome messages and onboarding flows for new group members

## Primitives
```
/onboard — Run setup wizard (first time) or show config
/onboard set <key> <value> — Set a config value
/onboard preview — Preview the welcome message
/onboard edit — Re-run the setup wizard
/onboard reset — Clear all config for this group
/onboard on — Enable onboarding for this group
/onboard off — Disable onboarding for this group
```

## Rules
- Do NOT invent subcommands — only use commands listed above
- Respect explicit timezones in user input
