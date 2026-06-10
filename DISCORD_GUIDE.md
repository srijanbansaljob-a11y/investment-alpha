# 📌 Investment Alpha — Discord Command Guide
*Pin this in your #commands channel.*

## The golden rule
**Nothing ever sells without your tap.** Alerts arrive with buttons; orders happen only when you press ✅.

---

## Approval buttons (arrive automatically)

When the monitor flags a trade (stop-loss breach or profit target), you get a card like:

> 🛑 **STOP-LOSS BREACHED — AAPL**
> Current $172.10 · Stop $175.40 · Loss −9.2%
> [✅ Approve SELL AAPL]  [❌ Reject (keep position)]

| Button | What happens | How fast |
|---|---|---|
| ✅ Approve | Market SELL submitted to Alpaca paper; card updates with order status; confirmation posts below | Order in ~1–2 min |
| ❌ Reject | Card stamped "Rejected — position kept"; nothing else happens | Instant |
| (ignore it) | Nothing — no countdown, no auto-sell, ever | — |

If the same breach persists, you'll get at most **one alert per ticker per day** (dedupe via channel history).

---

## Slash commands (type `/` and pick)

| Command | What it does | Orders? | Wait time |
|---|---|---|---|
| `/status` | All positions: qty, entry, current, P&L, stop distance, equity, cash, regime | No | ~1–2 min |
| `/regime` | Market regime: BULL / NEUTRAL / BEAR (SPY vs 50/200-day SMA) | No | ~1–2 min |
| `/monitor` | Immediate position check — alert cards post if anything triggers | No | ~2 min |
| `/stoploss mode:check` | Each position vs its ATR stop, breach flags | No | ~2 min |
| `/stoploss mode:execute` | Sells breached positions — **asks you to confirm first** | Yes, after confirm | ~2–3 min |
| `/pipeline mode:dry` | Full pipeline: rankings, signals, sector weights — no trades | No | ~10–30 min |
| `/pipeline mode:execute` | Full rebalance — **asks you to confirm first** | Yes, after confirm | ~10–30 min |
| `/help` | This list, in Discord (only you see it) | No | Instant |

**Why the wait?** Commands run on GitHub's servers (your PC stays off). Each run installs dependencies (~1 min) before working.

**Long runs:** if a pipeline run exceeds ~15 min, Discord's "thinking…" placeholder may time out — results still arrive as a fresh message in the channel.

---

## Typical weekly routine

1. **Every morning 9 AM ET** — summary card posts automatically. Glance, done.
2. **Tuesday (or any day)** — `/pipeline mode:dry` → review signals → `/pipeline mode:execute` → press ✅ confirm.
3. **During the day** — monitor runs every 15 min. Alert with buttons = your decision, whenever you're ready.

## Safety facts

- Only **you** can press buttons or run commands — anyone else gets "⛔ Not authorized."
- Execute-class commands always show a **confirm button** first — a mistyped command can't trade.
- Everything runs against **Alpaca paper trading** — the live endpoint isn't even in the code.
- Two commands can't trade simultaneously (queued one at a time).

## If something looks wrong

- No response after 5 min → check the run logs: `github.com/srijanbansaljob-a11y/investment-alpha/actions`
- Buttons missing on alerts → `DISCORD_BOT_TOKEN`/`DISCORD_CHANNEL_ID` secrets missing (webhook fallback has no buttons)
- "⛔ Not authorized" for you → `OWNER_ID` in the Cloudflare Worker doesn't match your Discord user ID
