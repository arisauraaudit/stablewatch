# Stablecoin Depeg Monitor v0.1

Real-time stablecoin peg deviation alerts via Telegram. $0 infrastructure.

## What it does

Polls 9 major stablecoins every 60 seconds. Fires Telegram alerts when any coin deviates from $1.00 peg:

| Severity | Threshold | Emoji |
|---|---|---|
| WARNING | > 0.10% | ⚠️ |
| ALERT | > 0.50% | 🚨 |
| CRITICAL | > 1.00% | 🔴 |

Dual-source: CoinGecko primary, DeFiLlama automatic failover. All events logged to SQLite.

## Stack (all free)

- Python 3.11+
- CoinGecko free API (no key)
- DeFiLlama free API (no key)
- Telegram Bot API (free)
- Railway free tier (hosting)
- SQLite (embedded)

## Deploy on Railway (one click)

1. Fork this repo to your GitHub
2. Go to [railway.app](https://railway.app) → New Project → Deploy from GitHub → select the repo
3. Add environment variables (Settings → Variables):
   - `TELEGRAM_TOKEN` — your bot token from @BotFather
   - `TELEGRAM_CHANNEL` — your channel handle (e.g. `@depegalerts`)
4. Railway auto-deploys. Bot is live.

## Create your Telegram bot

1. Open Telegram → search @BotFather → `/newbot`
2. Choose a name (e.g. "Depeg Monitor") and username (e.g. `depeg_monitor_bot`)
3. Copy the token BotFather gives you → set as `TELEGRAM_TOKEN`
4. Create a public channel → add your bot as an admin
5. Set the channel handle as `TELEGRAM_CHANNEL`

## Local testing

```bash
pip install -r requirements.txt
cp .env.example .env
# Edit .env with your tokens
python monitor.py
```

## Monitored stablecoins (v0.1)

USDT, USDC, DAI, FRAX, TUSD, PYUSD, LUSD, USDD, FDUSD

---
*Phase 1 MVP — Aris Autonomous Research Operation*
