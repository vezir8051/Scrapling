# Homegate Rental Alert Bot

Scrapes Homegate rentals on a schedule, filters by price/rooms, and pushes
**new** listings to Telegram and/or Discord. Built on Scrapling's stealth browser.

Files:
- `homegate_scraper.py` — scraping + parsing + `filter_listings()` (importable).
- `homegate_bot.py` — the alert loop (env-configured).
- `Dockerfile.homegate-bot` — ready-to-deploy image (browsers included).

## Quick local test

```bash
pip install "scrapling[fetchers]"
scrapling install

export HOMEGATE_PROXY="http://user:pass@ch-host:port"   # Swiss residential proxy
export HOMEGATE_LOCATION="zurich"
export FILTER_MAX_PRICE=2500
export FILTER_MIN_ROOMS=2.5
# Notifier (pick one or both):
export TELEGRAM_BOT_TOKEN="123:abc"
export TELEGRAM_CHAT_ID="123456789"
# export DISCORD_WEBHOOK_URL="https://discord.com/api/webhooks/..."

python homegate_bot.py --once     # single check; drop --once to loop
```

The first run only *seeds* the seen-store (so you don't get spammed with the
whole first page); subsequent runs alert only on genuinely new listings.

## Deploy on Railway

1. Push this branch to GitHub (already done) and create a Railway project from
   the repo.
2. In the service **Settings → Build**, set the Dockerfile path to
   `Dockerfile.homegate-bot` (or set the variable `RAILWAY_DOCKERFILE_PATH=Dockerfile.homegate-bot`).
3. In **Variables**, add:

   | Variable | Example | Notes |
   |---|---|---|
   | `HOMEGATE_PROXY` | `http://user:pass@ch-host:port` | **Required** — Swiss residential proxy |
   | `HOMEGATE_LOCATION` | `zurich` | Homegate location slug |
   | `HOMEGATE_PAGES` | `1` | Pages per check |
   | `CHECK_INTERVAL` | `900` | Seconds between checks |
   | `TELEGRAM_BOT_TOKEN` | `123:abc` | From @BotFather |
   | `TELEGRAM_CHAT_ID` | `123456789` | Your chat/channel id |
   | `DISCORD_WEBHOOK_URL` | `https://discord.com/...` | Alternative to Telegram |
   | `FILTER_MAX_PRICE` | `2500` | Optional |
   | `FILTER_MIN_PRICE` | `1000` | Optional |
   | `FILTER_MIN_ROOMS` | `2.5` | Optional |
   | `FILTER_MAX_ROOMS` | `4` | Optional |
   | `FILTER_MIN_SPACE` | `50` | m², optional |
   | `FILTER_MAX_SPACE` | `120` | m², optional |
   | `FILTER_LOCALITY` | `Zürich` | substring match, optional |

4. Deploy. The service runs the loop continuously.

### Notes / limits
- **Swiss residential proxy is effectively required** — Homegate geo/IP-blocks
  datacenter IPs (and Railway gives you a datacenter IP), so the proxy is what
  makes requests succeed.
- Railway's filesystem is ephemeral; the `seen_listings.json` store survives
  while the service runs but resets on redeploy (you may get one repeat alert
  after a redeploy). For durable dedup, attach a Railway Volume and point
  `SEEN_STORE` at it, or swap in a small database.
- Scrapling natively solves Cloudflare only; if Homegate uses DataDome/Akamai,
  the stealth browser + Swiss proxy is what carries you through.
- For legitimate use only — respect Homegate's Terms of Service and robots.txt.
