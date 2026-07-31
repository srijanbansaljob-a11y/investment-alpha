# TradingView → Cloudflare Worker → Alpaca — Setup Guide

## Architecture

```
TradingView alert
    ↓  POST /webhook  (JSON with secret)
Cloudflare Worker
    ↓  reads KV          (regime_signal, stock_buckets — set at 8 AM by GitHub Action)
    ↓  gates: regime OK? bucket OK? no earnings?
    ↓  POST /v2/orders   (bracket order: entry + stop + take-profit)
Alpaca Paper Trading
    ↓  fills
Discord notification
```

---

## Step 1 — Cloudflare Worker & KV

### 1a. Create KV namespace
```bash
cd worker
npx wrangler login
npx wrangler kv:namespace create "investment-alpha-kv"
# Copy the returned `id` value into wrangler.toml → [[kv_namespaces]] id = "..."
```

### 1b. Set Worker secrets
```bash
npx wrangler secret put DISCORD_PUBLIC_KEY   # Discord Developer Portal → General Info
npx wrangler secret put OWNER_ID             # Your Discord user ID (right-click yourself)
npx wrangler secret put GH_TOKEN             # GitHub PAT, repo scope
npx wrangler secret put GH_REPO             # e.g. srijanbansal/investment-alpha
npx wrangler secret put DISCORD_WEBHOOK_URL  # your Discord channel webhook URL
npx wrangler secret put TV_SECRET            # any strong random string:  openssl rand -hex 32
npx wrangler secret put ALPACA_KEY           # from alpaca.markets → Paper Trading → API Keys
npx wrangler secret put ALPACA_SECRET
npx wrangler secret put ALPACA_BASE_URL      # https://paper-api.alpaca.markets
```

### 1c. Deploy
```bash
npx wrangler deploy
# Output: https://investment-alpha-bot.<your-subdomain>.workers.dev
```

---

## Step 2 — GitHub Secrets (for KV sync)

Add these in your repo → Settings → Secrets → Actions:

| Secret | Where to get it |
|---|---|
| `CF_ACCOUNT_ID` | Cloudflare dashboard → right sidebar |
| `CF_KV_NAMESPACE` | Workers & Pages → KV → your namespace ID |
| `CF_API_TOKEN` | Cloudflare → My Profile → API Tokens → Create Token (Workers KV Edit) |

The `screener_daily.yml` workflow runs at 8 AM ET, runs the screener, and pushes the regime + stock buckets to KV automatically.

---

## Step 3 — TradingView

### 3a. Load the strategy
1. Open TradingView chart for any stock (daily timeframe recommended)
2. Open Pine Script editor (bottom panel)
3. Paste contents of `TradingView/momentum_strategy.pine`
4. Click **Add to chart**

### 3b. Create alert
1. Click the **clock icon** (Alerts) → **+ Create alert**
2. Condition: **Investment Alpha — Momentum** → **Order fills**
3. Webhook URL: `https://investment-alpha-bot.<subdomain>.workers.dev/webhook`
4. Message (replace YOUR_TV_SECRET with your actual secret):
```json
{"secret":"YOUR_TV_SECRET","ticker":"{{ticker}}","action":"{{strategy.order.action}}","strategy":"momentum","price":{{close}},"comment":"{{strategy.order.comment}}"}
```
5. Name it: `IA Momentum — AAPL` (or whatever stock)
6. Save

### 3c. Test the connection
Send a test webhook:
```bash
curl -X POST https://investment-alpha-bot.<subdomain>.workers.dev/webhook \
  -H "Content-Type: application/json" \
  -d '{"secret":"YOUR_TV_SECRET","ticker":"AAPL","action":"buy","strategy":"momentum","price":200,"comment":"test"}'
```

Expected response: `{"status":"filled","ticker":"AAPL","action":"buy",...}` or a regime-gated block.

---

## What gets gated

| Condition | Result |
|---|---|
| Earnings within 3 days | ⚠️ Blocked — earnings blackout |
| Stock in "avoid" bucket | 🚫 Blocked — bucket gate |
| Strategy not in regime's permitted list | 🧭 Blocked — regime gate |
| All clear | ✅ Bracket order placed on Alpaca |

Regime permitted strategies (from daily screener):
- **STRONG BULL** (≥75): momentum, breakout, mean_reversion, catalyst
- **MODERATE BULL** (55-74): momentum, mean_reversion, catalyst
- **NEUTRAL** (40-54): mean_reversion, defensive
- **BEARISH** (<40): defensive only

---

## Order parameters (set in worker/index.js)
- **Position size**: 5% of buying power per trade
- **Stop loss**: 5% below entry (bracket order)
- **Take profit**: 12% above entry (bracket order)

To change: edit `POSITION_SIZE_PCT`, `STOP_LOSS_PCT`, `TAKE_PROFIT_PCT` at the top of `worker/index.js` and redeploy.
