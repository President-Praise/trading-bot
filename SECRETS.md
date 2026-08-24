# Secrets & keys for `trading-bot`

Where to re-provision each environment variable if you've lost the original values.
Generated automatically from a scan of the codebase — verify against the actual code before trusting.

| Variable | Service | Where to get it |
|---|---|---|
| `DERIV_API_TOKEN` | unknown | unclear — grep the codebase for `DERIV_API_TOKEN` to see how it's used |
| `DERIV_APP_ID` | unknown | unclear — grep the codebase for `DERIV_APP_ID` to see how it's used |
| `TELEGRAM_BOT_TOKEN` | Telegram Bot | @BotFather in Telegram (/newbot or /token) |
| `TELEGRAM_CHAT_ID` | Telegram Bot | @BotFather in Telegram (/newbot or /token) |

## Recovery workflow

1. Copy `.env.example` to `.env`
2. For each row above, visit the linked dashboard and either find the existing key or create a new one
3. Paste values into `.env` (never commit)
4. `npm install` / `pip install -r requirements.txt` then run
