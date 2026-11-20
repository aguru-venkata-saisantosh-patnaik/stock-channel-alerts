# Sigma Channel Watch

This repo automates the "standard deviation channel" strategy from a Revelio Trading video. It runs for free on GitHub Actions and works like this:

- **Watchlist:** US stocks with a market cap of at least $50B, primary listings only. It comes from the TradingView screener and refreshes weekly.
- **Indicator:** a 2,500-day (about 10-year) linear-regression channel on daily closes, with ±σ lines. It matches TradingView's *Standard Deviation Channel*. It was checked against the video's trades: MSFT crossed −3σ on 2026-03-27 and reached the regression line on 2026-08-03, and META crossed −4σ in June 2022.
- **Buy zone:** the top 10 by market cap enter at **−3σ**, every other stock at **−4σ**.
- **Excluded stocks:**
  - fewer than 2,500 daily bars of history,
  - the channel is flat or slopes down,
  - the buy line is below $0.
- **Take profit:** a stock you hold touches the **regression line**.
- **Re-check reminder:** every 90 days per holding, re-run the fundamental check. Failing it is the only reason to sell at a loss.

A scan runs **every 10 minutes during NYSE hours**. It publishes the dashboard to GitHub Pages and sends alerts **only when something changes**, by email, Telegram and WhatsApp.

Everything is free:
- GitHub Actions (unlimited minutes on public repos) and GitHub Pages
- Yahoo Finance data through `yfinance`
- the TradingView screener API
- Gmail SMTP
- the Telegram Bot API
- CallMeBot

## Setup

### 1. Turn on GitHub Pages
Do this after the first workflow run has created the `gh-pages` branch:
1. Open **Settings → Pages**.
2. Under **Source**, choose *Deploy from a branch*.
3. Pick **gh-pages** and **/(root)**.

The dashboard is then at `https://<your-user>.github.io/stock-channel-alerts/`.

### 2. Add secrets
Go to **Settings → Secrets and variables → Actions → New repository secret**. Every channel is optional, so set up whichever ones you want.

| Secret | How to get it |
|---|---|
| `GMAIL_USER` | Your Gmail address. |
| `GMAIL_APP_PASSWORD` | Turn on 2-Step Verification for your Google account, then go to https://myaccount.google.com/apppasswords and create one. The 16 letters go here. |
| `ALERT_EMAIL_TO` | Where to send alerts. Defaults to `GMAIL_USER`. |
| `TELEGRAM_BOT_TOKEN` | In Telegram, message **@BotFather**, send `/newbot`, and copy the token. |
| `TELEGRAM_CHAT_ID` | Send any message to your new bot, then open `https://api.telegram.org/bot<TOKEN>/getUpdates` and copy `chat.id`. |
| `CALLMEBOT_PHONE` | Your WhatsApp number with country code, e.g. `+919876543210`. |
| `CALLMEBOT_APIKEY` | Add +34 644 66 32 62 to your contacts and send it *"I allow callmebot to send me messages"* on WhatsApp. The reply contains your API key. Instructions: https://www.callmebot.com/blog/free-api-whatsapp-messages/ |
| `HOLDINGS_JSON` | The stocks you bought, so take-profit and re-check alerts work. Example: `[{"ticker":"MCD","entry":230.94,"date":"2026-10-07"}]`. Update it whenever you buy or sell. Because it's a secret, your positions never appear on the public dashboard. |

### 3. Test
1. Open **Actions → scan → Run workflow**.
2. Tick **test_alerts** and run it.

You should get a test message on every channel you configured, and the dashboard updates.

## Run locally
```bash
python3.12 -m venv .venv && .venv/bin/pip install -r requirements.txt
```
```bash
.venv/bin/python -m tests.test_engine
```
```bash
.venv/bin/python -m engine.run --out out --force --dry-run
```
```bash
cp site/* out/ && python3 -m http.server 8765 --directory out
```

## Settings
`config.yml` holds:
- the lookback windows (`windows_years`)
- the σ levels and the "near" band
- the market-cap minimum and the top-N tier size
- the re-check interval
- **your own tickers** (`my_tickers`) and their alert levels (`my_buy_levels`)

To add or remove a ticker, edit `my_tickers` and push.

**Windows and alerts:**
- Every ticker is scanned on its 10, 7, 5, 3 and 1-year channels.
- A buy alert fires when the price is at or below a buy line on **any** of them.
- Alerts are tracked separately for each ticker, window and level, and re-arm after the price climbs 0.5σ back above that line.
- Watchlist stocks need 10 years of history to qualify, as in the video.
- Your tickers use the longest window they have data for.

**Dashboard lookback buttons:** *Main · 10y · 7y · 5y · 3y · 1y*. They recalculate the lines and the table instantly in your browser.

## Is everything working?
- **Health bar at the top of the dashboard:**
  - Green means all checks pass.
  - Amber or red names the problem. Open **Details** for the full list: fresh prices, stock count, missing prices, channel math, the MSFT known answer, chart files, alert channels and screener age.
  - It turns red if no scan has run for 60 minutes during market hours.
- **Failure alerts:** any failing check sends one Telegram/email alert per day, and an "all clear" when it recovers.
- **Daily summary:** one message after each US close. **If it doesn't arrive, something is wrong.**
- **Fire drill:** run **Actions → scan → Run workflow** with `simulate = stale`. You should get a red health bar and one failure alert. The next normal run sends the all-clear.
- **GitHub emails:** keep the email for failed workflow runs turned on (GitHub → Settings → Notifications → Actions).

## Limits
- GitHub can start scheduled runs 5–15 minutes late.
- Yahoo Finance and the TradingView screener are unofficial free sources and can change or rate-limit. If a scan fails, you get one alert per day.
- CallMeBot is a free relay for personal use. Telegram is the most reliable push channel.
- The fundamental check is not automated. Each BUY alert includes a ready-made prompt to paste into an AI assistant with web search turned on.

Educational tool for your own rules. Not financial advice.
