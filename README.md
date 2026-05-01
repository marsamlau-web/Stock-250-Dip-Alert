# Stock 250 Dip Alert

Standalone watchlist monitor for individual stocks. Sends silent Telegram messages when configured stocks drop below their all-time high by configured thresholds.

This is a sibling project to `dip-alerts` (which monitors indexes and ETFs with full deployment suggestions). This project is **signal-only**: it never suggests how much to invest, because individual stocks have idiosyncratic risk and "buy the dip" math that works for indexes does not transfer to single names.

## What it does

- Tracks ~65 large-cap US + European stocks across all major sectors
- Fires silent Telegram alerts when a stock crosses **-15%, -25%, or -40%** from its all-time high
- Sends a daily heartbeat at 22:00 Paris listing all stocks currently down ≥10% from ATH
- Runs every 4 hours on GitHub Actions (free)
- Re-arms thresholds when a stock makes a new all-time high

## Files

- `monitor.py` — main script
- `config.json` — stock watchlist + thresholds + heartbeat settings
- `requirements.txt` — Python dependencies (`yfinance`, `requests`)
- `.github/workflows/check.yml` — GitHub Actions workflow
- `state.json` — auto-generated; tracks per-ticker ATH and fired thresholds

## Setup

1. Create a private GitHub repo and upload these files
2. Add two repository secrets in **Settings → Secrets and variables → Actions**:
   - `TELEGRAM_BOT_TOKEN` — your Telegram bot token from @BotFather
   - `TELEGRAM_CHAT_ID` — your chat ID
3. Trigger a forced-heartbeat run from the **Actions** tab to verify

## Customization

Edit `config.json` to change:
- `tickers` — add/remove stocks
- `thresholds_pct` — change drawdown levels that trigger alerts
- `heartbeat_min_drawdown_pct` — how deep a drawdown must be to appear in the daily heartbeat (default 10%)
- `heartbeat_hour_local` — what time the daily heartbeat fires (default 22:00 Paris)

## Notes on data

Uses Yahoo Finance via `yfinance`. Some niche European listings may occasionally fail to fetch — these are reported as "fetch failed" in the heartbeat and can be swapped for alternate exchange listings if needed.
