/**
 * Investment Alpha — Cloudflare Workers Edge Handler
 *
 * Two entry points on one Worker URL:
 *
 *  POST /          — Discord interactions (button presses + slash commands)
 *                    Verifies Ed25519 signature, owner-locks all actions,
 *                    dispatches to GitHub Actions via repository_dispatch.
 *
 *  POST /webhook   — TradingView strategy alerts (auto-execution path)
 *                    Validates HMAC secret, reads regime from KV, checks
 *                    stock bucket + earnings flag, places Alpaca bracket
 *                    order directly (sub-second latency), posts Discord
 *                    notification.
 *
 * Worker secrets (set via `wrangler secret put NAME`):
 *   DISCORD_PUBLIC_KEY  — Developer Portal → General Information
 *   OWNER_ID            — your Discord user ID
 *   GH_TOKEN            — GitHub PAT with repo scope
 *   GH_REPO             — e.g. "srijanbansal/investment-alpha"
 *   DISCORD_WEBHOOK_URL — channel webhook for trade notifications
 *   TV_SECRET           — secret you put in every TradingView alert JSON
 *   ALPACA_KEY          — Alpaca paper API key
 *   ALPACA_SECRET       — Alpaca paper secret key
 *   ALPACA_BASE_URL     — https://paper-api.alpaca.markets
 *
 * KV namespace (bind as `KV` in wrangler.toml):
 *   regime_signal   — written daily by screener_daily.yml workflow
 *   stock_buckets   — written daily by screener_daily.yml workflow
 *   screener_summary— top picks for /screener command
 */

// ── Discord interaction constants ──────────────────────────────────────────
const PING = 1, APPLICATION_COMMAND = 2, MESSAGE_COMPONENT = 3;
const R_PONG = 1, R_CHANNEL_MESSAGE = 4, R_DEFERRED_MESSAGE = 5,
      R_DEFERRED_UPDATE = 6, R_UPDATE_MESSAGE = 7;
const EPHEMERAL = 64;
const MODAL_SUBMIT = 5, R_MODAL = 9, MAX_POSITIONS = 8;

// ── Order sizing (% of buying power per trade, by regime) ─────────────────
// Regime score ≥60 = bull (5%), 30–59 = neutral (3%), <30 = bear (1.5%)
const POSITION_SIZE_BY_REGIME = { bull: 0.05, neutral: 0.03, bear: 0.015 };
const STOP_LOSS_PCT            = 0.05;   // 5% below entry
const TAKE_PROFIT_PCT          = 0.12;   // 12% above entry

// ── Discord colours ────────────────────────────────────────────────────────
const C_GREEN = 0x2ECC71, C_RED = 0xE74C3C, C_ORANGE = 0xE67E22,
      C_BLUE  = 0x3498DB, C_GREY = 0x95A5A6;

// ── Helpers ────────────────────────────────────────────────────────────────
function hexToBytes(hex) {
  const b = new Uint8Array(hex.length / 2);
  for (let i = 0; i < b.length; i++) b[i] = parseInt(hex.substr(i * 2, 2), 16);
  return b;
}

const json = (obj) => new Response(JSON.stringify(obj), {
  headers: { "Content-Type": "application/json" },
});
const ephemeral = (content) =>
  json({ type: R_CHANNEL_MESSAGE, data: { content, flags: EPHEMERAL } });

// ── Ed25519 Discord signature verification ────────────────────────────────
async function verifyDiscordSignature(request, bodyText, publicKey) {
  const sig = request.headers.get("X-Signature-Ed25519");
  const ts  = request.headers.get("X-Signature-Timestamp");
  if (!sig || !ts) return false;
  try {
    const key = await crypto.subtle.importKey(
      "raw", hexToBytes(publicKey), { name: "Ed25519" }, false, ["verify"]
    );
    return await crypto.subtle.verify(
      "Ed25519", key, hexToBytes(sig), new TextEncoder().encode(ts + bodyText)
    );
  } catch { return false; }
}

// ── GitHub dispatch ────────────────────────────────────────────────────────
async function dispatchToGitHub(env, payload) {
  let r;
  try {
    r = await fetch(`https://api.github.com/repos/${env.GH_REPO}/dispatches`, {
      method: "POST",
      headers: {
        Authorization: `Bearer ${(env.GH_TOKEN || "").trim()}`,
        Accept:         "application/vnd.github+json",
        "Content-Type": "application/json",
        "User-Agent":   "investment-alpha-worker",
      },
      body: JSON.stringify({ event_type: "discord-command", client_payload: payload }),
    });
  } catch (e) { return `fetch error: ${e.message}`; }
  if (r.status === 204) return null;
  return `GitHub HTTP ${r.status}: ${(await r.text()).slice(0, 200)}`;
}

// ── Discord webhook notification ───────────────────────────────────────────
async function postDiscordWebhook(webhookUrl, embeds, components = []) {
  if (!webhookUrl) return;
  try {
    const body = { embeds };
    if (components.length) body.components = components;
    await fetch(webhookUrl, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
  } catch (e) {
    console.error("Discord webhook failed:", e.message);
  }
}

// ── Alpaca trade helpers ────────────────────────────────────────────────────
// Two Alpaca accounts:
//   ALPACA_KEY / ALPACA_SECRET         → pipeline account (TradingView webhook, portfolio)
//   ALPACA_KEY_SCREENER / ALPACA_SECRET_SCREENER → screener account (/buy, /sell Discord commands)

function screenerHeaders(env) { return portfolioHeaders(env, "screener"); }

function portfolioHeaders(env, portfolio) {
  if (portfolio === "pipeline") {
    return {
      "APCA-API-KEY-ID":     (env.ALPACA_KEY    || "").trim(),
      "APCA-API-SECRET-KEY": (env.ALPACA_SECRET  || "").trim(),
      "Content-Type":        "application/json",
    };
  }
  return {
    "APCA-API-KEY-ID":     (env.ALPACA_KEY_SCREENER || env.ALPACA_KEY    || "").trim(),
    "APCA-API-SECRET-KEY": (env.ALPACA_SECRET_SCREENER || env.ALPACA_SECRET || "").trim(),
    "Content-Type":        "application/json",
  };
}

async function getAlpacaPrice(env, symbol) {
  // Use screener account credentials for price (both accounts see same market data)
  const headers = {
    "APCA-API-KEY-ID":     (env.ALPACA_KEY_SCREENER || env.ALPACA_KEY    || "").trim(),
    "APCA-API-SECRET-KEY": (env.ALPACA_SECRET_SCREENER || env.ALPACA_SECRET || "").trim(),
  };
  try {
    const r = await fetch(
      `https://data.alpaca.markets/v2/stocks/snapshots?symbols=${symbol}&feed=iex`,
      { headers }
    );
    if (!r.ok) return null;
    const data = await r.json();
    const snap = data[symbol];
    return snap?.latestTrade?.p || snap?.latestQuote?.ap || null;
  } catch { return null; }
}

async function placeBracketOrder(env, symbol, customQty = null, portfolio = "screener") {
  const alpacaBase   = "https://paper-api.alpaca.markets";
  const alpacaKey    = portfolio === "pipeline" ? (env.ALPACA_KEY || "").trim() : (env.ALPACA_KEY_SCREENER || env.ALPACA_KEY || "").trim();
  const alpacaSecret = portfolio === "pipeline" ? (env.ALPACA_SECRET || "").trim() : (env.ALPACA_SECRET_SCREENER || env.ALPACA_SECRET || "").trim();
  if (!alpacaKey || !alpacaSecret) return { error: `Alpaca ${portfolio} credentials not set in Cloudflare secrets.` };

  const headers = portfolioHeaders(env, portfolio);

  // Get live price
  const price = await getAlpacaPrice(env, symbol);
  if (!price) return { error: `Could not fetch price for ${symbol} — market may be closed.` };

  // Read ATR-based dynamic targets from screener KV (set daily by screener workflow)
  let stopPct     = STOP_LOSS_PCT;    // fallback: 5%
  let tpAlpacaPct = TAKE_PROFIT_PCT;  // fallback: 12%
  let tpMonitorPct = tpAlpacaPct * 0.80; // fallback: 9.6%
  let atrPct = null;
  try {
    const [summaryRaw, bucketsRaw] = await Promise.all([
      env.KV.get("screener_summary"),
      env.KV.get("stock_buckets"),
    ]);
    let pick = null;
    if (summaryRaw) {
      const summary = JSON.parse(summaryRaw);
      pick = (summary.top_picks || []).find(p => p.ticker === symbol) || null;
    }
    if (!pick && bucketsRaw) {
      const buckets = JSON.parse(bucketsRaw);
      const b = buckets[symbol];
      if (b && b.atr_pct) pick = b;
    }
    if (pick && pick.atr_pct) {
      atrPct       = pick.atr_pct;
      stopPct      = (pick.stop_pct      || STOP_LOSS_PCT * 100)   / 100;
      tpAlpacaPct  = (pick.tp_alpaca_pct || TAKE_PROFIT_PCT * 100) / 100;
      tpMonitorPct = (pick.tp_monitor_pct || tpAlpacaPct * 80)     / 100;
    }
  } catch (e) { console.warn("ATR target read error:", e.message); }

  // Size from buying power + regime
  let qty = 1, sizePct = POSITION_SIZE_BY_REGIME.neutral;
  try {
    const raw = await env.KV.get("regime_signal");
    if (raw) {
      const r   = JSON.parse(raw);
      const sc  = r.total ?? r.score ?? 0;
      const key = sc >= 60 ? "bull" : sc >= 30 ? "neutral" : "bear";
      sizePct   = POSITION_SIZE_BY_REGIME[key];
    }
    const acctR = await fetch(`${alpacaBase}/v2/account`, { headers });
    const acct  = await acctR.json();
    const bp    = parseFloat(acct.buying_power || acct.cash || 0);
    qty = (customQty && customQty > 0) ? customQty : Math.max(1, Math.floor((bp * sizePct) / price));
  } catch (e) { console.warn("Sizing error:", e.message); }

  const stopPrice      = parseFloat((price * (1 - stopPct)).toFixed(2));
  const takePrice      = parseFloat((price * (1 + tpAlpacaPct)).toFixed(2));
  const monitorPrice   = parseFloat((price * (1 + tpMonitorPct)).toFixed(2));

  let orderResult;
  try {
    const r = await fetch(`${alpacaBase}/v2/orders`, {
      method: "POST", headers,
      body: JSON.stringify({
        symbol, qty: String(qty), side: "buy",
        type: "market", time_in_force: "day",
        order_class: "bracket",
        stop_loss:   { stop_price:  String(stopPrice) },
        take_profit: { limit_price: String(takePrice) },
      }),
    });
    const result = await r.json();
    if (!r.ok) return { error: result?.message || `HTTP ${r.status}`, price, qty, stopPrice, takePrice, monitorPrice, sizePct, atrPct };

    // Poll fill status — market orders during market hours usually fill in <2s
    let fillStatus = result.status || "submitted";
    let fillQty    = result.filled_qty || "0";
    let fillAvg    = result.filled_avg_price || null;
    if (fillStatus !== "filled") {
      try {
        await new Promise(res => setTimeout(res, 2500));
        const poll = await fetch(`https://paper-api.alpaca.markets/v2/orders/${result.id}`, { headers });
        if (poll.ok) {
          const polled = await poll.json();
          fillStatus = polled.status || fillStatus;
          fillQty    = polled.filled_qty || fillQty;
          fillAvg    = polled.filled_avg_price || fillAvg;
        }
      } catch (e) { console.warn("Fill poll error:", e.message); }
    }

    orderResult = { order: result, price, qty, stopPrice, takePrice, monitorPrice, sizePct, atrPct, fillStatus, fillQty, fillAvg };
  } catch (e) {
    return { error: e.message, price, qty, stopPrice, takePrice, monitorPrice, sizePct, atrPct };
  }

  // Store position targets in KV so the 2-min monitor can check them
  try {
    const posKey = `position_target_${portfolio.toUpperCase()}_${symbol}`;
    const posVal = JSON.stringify({
      symbol,
      entry_price:      price,
      qty,
      stop_price:       stopPrice,
      tp_monitor_price: monitorPrice,
      tp_alpaca_price:  takePrice,
      atr_pct:          atrPct,
      stop_pct:         stopPct * 100,
      tp_monitor_pct:   tpMonitorPct * 100,
      tp_alpaca_pct:    tpAlpacaPct * 100,
      placed_at:        new Date().toISOString(),
      order_id:         orderResult.order?.id || null,
    });
    // TTL 8 days (position shouldn’t stay open longer)
    await env.KV.put(posKey, posVal, { expirationTtl: 8 * 24 * 3600 });
    console.log(`Stored position targets: ${posKey}`);
  } catch (e) { console.warn("Position KV write error:", e.message); }

  return orderResult;
}

// ══════════════════════════════════════════════════════════════════════════
//  TRADINGVIEW WEBHOOK HANDLER
// ══════════════════════════════════════════════════════════════════════════

/**
 * TradingView alert JSON format (set in TradingView alert → Message):
 * {
 *   "secret":   "{{strategy.order.comment}}",   ← put your TV_SECRET here
 *   "ticker":   "{{ticker}}",
 *   "action":   "buy",                          ← "buy" | "sell" | "close"
 *   "strategy": "momentum",                     ← matches permitted_strategies
 *   "price":    {{close}},
 *   "comment":  "RSI bounce + above 20MA"
 * }
 */
async function handleTradingViewWebhook(request, env) {
  let body;
  try {
    body = await request.json();
  } catch {
    return new Response("Invalid JSON", { status: 400 });
  }

  // 1. Validate secret
  const expectedSecret = (env.TV_SECRET || "").trim();
  if (!expectedSecret || body.secret !== expectedSecret) {
    console.warn("TradingView webhook: bad secret");
    return new Response("Unauthorized", { status: 401 });
  }

  const { ticker, action, strategy, price, comment } = body;
  if (!ticker || !action) {
    return new Response("Missing ticker or action", { status: 400 });
  }

  const ts = new Date().toISOString();
  console.log(`Webhook: ${action.toUpperCase()} ${ticker} | strategy=${strategy} | price=${price}`);

  // 2. Read regime from KV (set daily by the screener workflow)
  let regime = null;
  try {
    const raw = await env.KV.get("regime_signal");
    if (raw) regime = JSON.parse(raw);
  } catch (e) {
    console.warn("KV regime read failed:", e.message);
  }

  const regimeLabel = regime?.label || "UNKNOWN";
  const regimeScore = regime?.total ?? regime?.score ?? 0;  // "total" from screener, fallback "score"
  const permitted   = new Set(regime?.permitted_strategies || []);

  // 3. Check stock bucket
  let stockInfo = null;
  try {
    const bucketsRaw = await env.KV.get("stock_buckets");
    if (bucketsRaw) {
      const buckets = JSON.parse(bucketsRaw);
      stockInfo = buckets[ticker] || null;
    }
  } catch (e) {
    console.warn("KV stock_buckets read failed:", e.message);
  }

  // Block if: earnings within 3d, bucket is "avoid", or strategy not permitted by regime
  const nearEarnings = stockInfo?.near_earnings === true;
  const bucket = stockInfo?.bucket || "watch";
  const regimeOk = stockInfo?.regime_ok !== false;  // default permissive if no data

  // For BUY signals, apply regime + bucket gates
  if (action.toLowerCase() === "buy") {
    if (nearEarnings) {
      await postDiscordWebhook(env.DISCORD_WEBHOOK_URL, [{
        title: `⚠️ ${ticker} — Blocked (earnings blackout)`,
        description: `TradingView fired a BUY signal for **${ticker}** but it's within 3 days of earnings. Skipping to avoid gap risk.`,
        color: C_ORANGE,
        fields: [
          { name: "Strategy", value: strategy || "—", inline: true },
          { name: "Price", value: price ? `$${price}` : "market", inline: true },
          { name: "Regime", value: `${regimeLabel} (${regimeScore}/100)`, inline: true },
        ],
        timestamp: ts,
        footer: { text: "Investment Alpha • Earnings Blackout" },
      }]);
      return new Response(JSON.stringify({ status: "blocked", reason: "earnings_blackout" }),
        { headers: { "Content-Type": "application/json" } });
    }

    if (bucket === "avoid") {
      await postDiscordWebhook(env.DISCORD_WEBHOOK_URL, [{
        title: `🚫 ${ticker} — Blocked (avoid bucket)`,
        description: `${ticker} is classified as **avoid** by today's screener. Signal ignored.`,
        color: C_RED,
        fields: [
          { name: "Strategy", value: strategy || "—", inline: true },
          { name: "Bucket", value: bucket, inline: true },
          { name: "Regime", value: regimeLabel, inline: true },
        ],
        timestamp: ts,
        footer: { text: "Investment Alpha • Bucket Gate" },
      }]);
      return new Response(JSON.stringify({ status: "blocked", reason: "avoid_bucket" }),
        { headers: { "Content-Type": "application/json" } });
    }

    if (strategy && permitted.size > 0 && !permitted.has(strategy)) {
      await postDiscordWebhook(env.DISCORD_WEBHOOK_URL, [{
        title: `🧭 ${ticker} — Blocked (regime gate)`,
        description: `**${strategy}** strategy is not permitted in a **${regimeLabel}** market.\nPermitted today: ${[...permitted].join(", ") || "none"}`,
        color: C_ORANGE,
        fields: [
          { name: "Regime score", value: `${regimeScore}/100`, inline: true },
          { name: "Bucket", value: bucket, inline: true },
          { name: "Price", value: price ? `$${price}` : "market", inline: true },
        ],
        timestamp: ts,
        footer: { text: "Investment Alpha • Regime Gate" },
      }]);
      return new Response(JSON.stringify({ status: "blocked", reason: "regime_gate", regime: regimeLabel }),
        { headers: { "Content-Type": "application/json" } });
    }
  }

  // 4. Place order via Alpaca REST API
  const alpacaBase  = (env.ALPACA_BASE_URL || "https://paper-api.alpaca.markets").trim();
  const alpacaKey   = (env.ALPACA_KEY   || "").trim();
  const alpacaSecret = (env.ALPACA_SECRET || "").trim();

  if (!alpacaKey || !alpacaSecret) {
    console.error("Alpaca credentials not configured in Worker secrets");
    await postDiscordWebhook(env.DISCORD_WEBHOOK_URL, [{
      title: `❌ ${ticker} — Order Failed`,
      description: "Alpaca API credentials not configured in Worker secrets. Set `ALPACA_KEY` and `ALPACA_SECRET` via `wrangler secret put`.",
      color: C_RED,
      timestamp: ts,
    }]);
    return new Response("Alpaca credentials missing", { status: 500 });
  }

  const alpacaHeaders = {
    "APCA-API-KEY-ID":     alpacaKey,
    "APCA-API-SECRET-KEY": alpacaSecret,
    "Content-Type":        "application/json",
  };

  // Determine qty from buying power — scaled by regime
  const regimeKey = regimeScore >= 60 ? "bull" : regimeScore >= 30 ? "neutral" : "bear";
  const positionSizePct = POSITION_SIZE_BY_REGIME[regimeKey];
  let qty = 1;
  try {
    const acctResp = await fetch(`${alpacaBase}/v2/account`, { headers: alpacaHeaders });
    const acct = await acctResp.json();
    const buyingPower = parseFloat(acct.buying_power || acct.cash || 0);
    const entryPrice = price || 100;  // fallback if price not sent
    const dollarAlloc = buyingPower * positionSizePct;
    qty = Math.max(1, Math.floor(dollarAlloc / entryPrice));
    console.log(`Regime ${regimeKey} (score ${regimeScore}) → sizing ${positionSizePct*100}% → $${dollarAlloc.toFixed(0)} → ${qty} shares`);
  } catch (e) {
    console.warn("Could not fetch Alpaca buying power:", e.message);
  }

  let orderResult = null;
  let orderError  = null;

  if (action.toLowerCase() === "buy") {
    // Bracket order: entry + stop loss + take profit
    const entryPrice  = price || 0;
    const stopPrice   = entryPrice > 0 ? parseFloat((entryPrice * (1 - STOP_LOSS_PCT)).toFixed(2))   : null;
    const limitPrice  = entryPrice > 0 ? parseFloat((entryPrice * (1 + TAKE_PROFIT_PCT)).toFixed(2)) : null;

    const orderPayload = {
      symbol:        ticker,
      qty:           String(qty),
      side:          "buy",
      type:          entryPrice > 0 ? "limit" : "market",
      time_in_force: "day",
      ...(entryPrice > 0 && { limit_price: String(entryPrice) }),
      ...(stopPrice && limitPrice && {
        order_class:   "bracket",
        stop_loss:     { stop_price: String(stopPrice) },
        take_profit:   { limit_price: String(limitPrice) },
      }),
    };

    try {
      const resp = await fetch(`${alpacaBase}/v2/orders`, {
        method:  "POST",
        headers: alpacaHeaders,
        body:    JSON.stringify(orderPayload),
      });
      orderResult = await resp.json();
      if (!resp.ok) {
        orderError = orderResult?.message || `HTTP ${resp.status}`;
        orderResult = null;
      }
    } catch (e) {
      orderError = e.message;
    }

    // Notify Discord
    const ok = !!orderResult;
    await postDiscordWebhook(env.DISCORD_WEBHOOK_URL, [{
      title: ok
        ? `✅ BUY ${ticker} — Order Placed`
        : `❌ BUY ${ticker} — Order Failed`,
      description: ok
        ? `**${qty} shares** of **${ticker}** submitted via TradingView → Alpaca`
        : `Order failed: ${orderError}`,
      color: ok ? C_GREEN : C_RED,
      fields: [
        { name: "Strategy",    value: strategy || "—",               inline: true },
        { name: "Shares",      value: String(qty),                   inline: true },
        { name: "Entry",       value: entryPrice ? `$${entryPrice}` : "market", inline: true },
        { name: "Stop loss",   value: stopPrice  ? `$${stopPrice}`  : "—",      inline: true },
        { name: "Take profit", value: limitPrice ? `$${limitPrice}` : "—",      inline: true },
        { name: "Regime",      value: `${regimeLabel} (${regimeScore}/100)`,     inline: true },
        { name: "Bucket",      value: bucket,                        inline: true },
        ...(comment ? [{ name: "Signal reason", value: comment, inline: false }] : []),
        ...(ok ? [{ name: "Order ID", value: orderResult.id || "—", inline: false }] : []),
      ],
      timestamp: ts,
      footer: { text: "Investment Alpha • Auto-Execute" },
    }]);

  } else if (action.toLowerCase() === "sell" || action.toLowerCase() === "close") {
    // Market sell / close position
    try {
      // Try close_position first (handles fractional shares cleanly)
      const resp = await fetch(`${alpacaBase}/v2/positions/${ticker}`, {
        method:  "DELETE",
        headers: alpacaHeaders,
      });
      if (resp.ok) {
        orderResult = await resp.json();
      } else {
        // Fall back to market sell order
        const fallback = await fetch(`${alpacaBase}/v2/orders`, {
          method:  "POST",
          headers: alpacaHeaders,
          body:    JSON.stringify({
            symbol: ticker, qty: String(qty), side: "sell",
            type: "market", time_in_force: "day",
          }),
        });
        orderResult = await fallback.json();
        if (!fallback.ok) {
          orderError = orderResult?.message || `HTTP ${fallback.status}`;
          orderResult = null;
        }
      }
    } catch (e) {
      orderError = e.message;
    }

    const ok = !!orderResult;
    await postDiscordWebhook(env.DISCORD_WEBHOOK_URL, [{
      title: ok
        ? `✅ SELL ${ticker} — Order Placed`
        : `❌ SELL ${ticker} — Order Failed`,
      description: ok
        ? `Position in **${ticker}** closed via TradingView exit signal`
        : `Close failed: ${orderError}`,
      color: ok ? C_ORANGE : C_RED,
      fields: [
        { name: "Strategy", value: strategy || "—", inline: true },
        { name: "Regime",   value: regimeLabel,     inline: true },
        ...(comment ? [{ name: "Signal reason", value: comment, inline: false }] : []),
      ],
      timestamp: ts,
      footer: { text: "Investment Alpha • Auto-Execute" },
    }]);
  }

  const status = orderResult ? "filled" : "failed";
  return new Response(
    JSON.stringify({ status, ticker, action, strategy, regime: regimeLabel, error: orderError }),
    { headers: { "Content-Type": "application/json" } }
  );
}


// ══════════════════════════════════════════════════════════════════════════
//  DISCORD INTERACTION HANDLER (unchanged from original)
// ══════════════════════════════════════════════════════════════════════════


// ── Buy preview helper — shared by /buy slash cmd, screener Buy button, modal submit ──
async function buildBuyPreview(env, ticker, customQty = null, portfolio = "screener") {
  const alpacaBase = "https://paper-api.alpaca.markets";
  const headers    = portfolioHeaders(env, portfolio);

  // Parallel fetch: price + account + all positions
  let price = null, buyingPower = 0, openPositions = [], existingPos = null;
  try {
    const [acctR, posR] = await Promise.all([
      fetch(`${alpacaBase}/v2/account`,   { headers }),
      fetch(`${alpacaBase}/v2/positions`, { headers }),
    ]);
    const acct = await acctR.json();
    buyingPower   = parseFloat(acct.buying_power || acct.cash || 0);
    const posData = await posR.json();
    if (Array.isArray(posData)) {
      openPositions = posData;
      existingPos   = posData.find(p => p.symbol === ticker) || null;
    }
  } catch (e) { console.warn("buildBuyPreview positions error:", e.message); }

  price = await getAlpacaPrice(env, ticker);
  if (!price) return { error: `❌ Could not fetch price for **${ticker}**. Market may be closed.` };

  // Max positions guard (only counts if this would be a new position)
  if (!existingPos && openPositions.length >= MAX_POSITIONS) {
    const held = openPositions.map(p => p.symbol).join(", ");
    return { error: `🚫 Max positions reached (${MAX_POSITIONS}/8). Currently holding: ${held}.\nClose a position before buying ${ticker}.` };
  }

  // Duplicate warning (not a block)
  const dupNote = existingPos
    ? `⚠️ You already hold **${existingPos.qty} shares** of ${ticker} (P&L: ${parseFloat(existingPos.unrealized_pl || 0) >= 0 ? "+" : ""}$${parseFloat(existingPos.unrealized_pl || 0).toFixed(2)}). This will add to your position.`
    : null;

  // ATR-based targets: check screener_summary top_picks first, then stock_buckets fallback
  // stock_buckets covers ALL screened stocks; top_picks only covers top 5
  let stopPct = STOP_LOSS_PCT, tpAlpacaPct = TAKE_PROFIT_PCT, tpMonitorPct = TAKE_PROFIT_PCT * 0.8, atrPct = null;
  try {
    const [summaryRaw, bucketsRaw] = await Promise.all([
      env.KV.get("screener_summary"),
      env.KV.get("stock_buckets"),
    ]);
    let pick = null;
    if (summaryRaw) {
      const summary = JSON.parse(summaryRaw);
      pick = (summary.top_picks || []).find(p => p.ticker === ticker) || null;
    }
    // Fallback: stock_buckets has ATR data for every screened stock
    if (!pick && bucketsRaw) {
      const buckets = JSON.parse(bucketsRaw);
      const b = buckets[ticker];
      if (b && b.atr_pct) pick = b;
    }
    if (pick && pick.atr_pct) {
      atrPct       = pick.atr_pct;
      stopPct      = (pick.stop_pct       || STOP_LOSS_PCT * 100)   / 100;
      tpAlpacaPct  = (pick.tp_alpaca_pct  || TAKE_PROFIT_PCT * 100) / 100;
      tpMonitorPct = (pick.tp_monitor_pct || tpAlpacaPct * 80)      / 100;
    }
  } catch (e) { console.warn("buildBuyPreview KV error:", e.message); }

  // Regime-based position size
  let regimeKey = "neutral";
  try {
    const rr = await env.KV.get("regime_signal");
    if (rr) { const r = JSON.parse(rr); const sc = r.total ?? r.score ?? 0; regimeKey = sc >= 60 ? "bull" : sc >= 30 ? "neutral" : "bear"; }
  } catch (e) {}

  const sizePct      = POSITION_SIZE_BY_REGIME[regimeKey];
  const suggestedQty = Math.max(1, Math.floor((buyingPower * sizePct) / price));
  const qty          = (customQty && customQty > 0) ? customQty : suggestedQty;

  // Dollar calculations
  const totalCost    = qty * price;
  const stopPrice    = price * (1 - stopPct);
  const monitorPrice = price * (1 + tpMonitorPct);
  const ceilPrice    = price * (1 + tpAlpacaPct);
  const maxLoss      = totalCost * stopPct;
  const monitorGain  = totalCost * tpMonitorPct;
  const rr           = (tpMonitorPct / stopPct).toFixed(1);

  const embed = {
    title: `🛒 Buy Preview — ${ticker} [${portfolio === "screener" ? "Screener" : "Pipeline"}]`,
    color: C_BLUE,
    description: dupNote || undefined,
    fields: [
      { name: "Price",             value: `$${price.toFixed(2)}`,                                       inline: true },
      { name: "Shares",            value: `${qty}${qty !== suggestedQty ? " (custom)" : " (auto)"}`,    inline: true },
      { name: "Total cost",        value: `$${totalCost.toFixed(2)}`,                                   inline: true },
      { name: "Stop loss",         value: `$${stopPrice.toFixed(2)} (−${(stopPct*100).toFixed(1)}%)`,   inline: true },
      { name: "Monitor target",    value: `$${monitorPrice.toFixed(2)} (+${(tpMonitorPct*100).toFixed(1)}%)`, inline: true },
      { name: "Alpaca ceiling",    value: `$${ceilPrice.toFixed(2)} (+${(tpAlpacaPct*100).toFixed(1)}%)`, inline: true },
      { name: "Max loss",          value: `−$${maxLoss.toFixed(2)}`,                                    inline: true },
      { name: "Monitor gain",      value: `+$${monitorGain.toFixed(2)}`,                                inline: true },
      { name: "Risk / reward",     value: `1 : ${rr}`,                                                 inline: true },
      { name: "ATR",               value: atrPct ? `${atrPct.toFixed(2)}%` : "default",                inline: true },
      { name: "Buying power left", value: `$${(buyingPower - totalCost).toFixed(2)}`,                   inline: true },
      { name: "Positions",         value: `${openPositions.length} / ${MAX_POSITIONS} open`,            inline: true },
    ],
    footer: { text: `[${portfolio === "screener" ? "Screener" : "Pipeline"}] Bracket order · Stop + ceiling auto-managed by Alpaca · Paper trading` },
  };

  // qty encoded in confirm custom_id so placeBracketOrder uses it
  const components = [{ type: 1, components: [
    { type: 2, style: 3, label: `✅ Buy ${qty} — $${totalCost.toFixed(0)}`, custom_id: `ia|confirm_buy_execute|${ticker}|buy|${qty}|${portfolio}` },
    { type: 2, style: 2, label: "✏️ Change qty",                            custom_id: `ia|change_qty|${ticker}|buy||${portfolio}` },
    { type: 2, style: 4, label: "❌ Cancel",                                 custom_id: `ia|cancel|${ticker}|buy` },
  ]}];

  return { embeds: [embed], components };
}

async function handleDiscordInteraction(bodyText, env, ctx) {
  const i = JSON.parse(bodyText);
  if (i.type === PING) return json({ type: R_PONG });

  // Owner lock
  const userId = i.member?.user?.id ?? i.user?.id;
  if (userId !== env.OWNER_ID) {
    return ephemeral("⛔ Not authorized. This bot only takes orders from its owner.");
  }

  const common = {
    channel_id:        i.channel_id,
    application_id:    i.application_id,
    interaction_token: i.token,
  };

    // ── Modal submit (Change qty flow) ──────────────────────────────────────
  if (i.type === MODAL_SUBMIT) {
    const [mtag, mticker, mportfolio] = (i.data.custom_id || "").split("|");
    if (mtag === "ia_qty_modal") {
      const rawQty    = (i.data.components?.[0]?.components?.[0]?.value || "").trim();
      const customQty = parseInt(rawQty);
      if (!customQty || customQty < 1 || customQty > 10000) {
        return ephemeral("❌ Invalid quantity. Enter a whole number between 1 and 10,000.");
      }
      const preview = await buildBuyPreview(env, mticker, customQty, mportfolio || "screener");
      if (preview.error) return ephemeral(preview.error);
      return json({ type: R_CHANNEL_MESSAGE, data: { flags: EPHEMERAL, embeds: preview.embeds, components: preview.components } });
    }
    return ephemeral("Unknown modal.");
  }

  // ── Button presses ───────────────────────────────────────────────────────
  if (i.type === MESSAGE_COMPONENT) {
    const [tag, action, ticker, trigger, qtyOverride, portfolioId] = (i.data.custom_id || "").split("|");
    const portfolio = portfolioId || "screener";
    if (tag !== "ia") return ephemeral("Unknown button.");

    if (action === "reject") {
      await dispatchToGitHub(env, {
        command: "reject", ticker, trigger, message_id: i.message.id, ...common,
      });
      const embeds = i.message.embeds || [];
      if (embeds[0]) embeds[0].footer = { text: trigger === "mr"
        ? "❌ Skipped by you — no buy"
        : "❌ Rejected by you — position kept" };
      return json({ type: R_UPDATE_MESSAGE, data: { embeds, components: [] } });
    }

    // ── Weekly rebalance approval buttons ───────────────────────────────
    if (action === "approve_rebalance") {
      const err = await dispatchToGitHub(env, {
        command: "approve_rebalance", message_id: i.message.id, ...common,
      });
      const embeds = i.message.embeds || [];
      if (embeds[0]) {
        embeds[0].color = err ? 0xE74C3C : 0x2ECC71;
        embeds[0].footer = { text: err
          ? `❌ Approval NOT sent — ${err}`
          : "✅ Approved — executing rebalance now. Results post here in ~1–2 min." };
      }
      return json({ type: R_UPDATE_MESSAGE, data: { embeds, components: [] } });
    }

    if (action === "reject_rebalance") {
      await dispatchToGitHub(env, {
        command: "reject_rebalance", message_id: i.message.id, ...common,
      });
      const embeds = i.message.embeds || [];
      if (embeds[0]) {
        embeds[0].color = 0x95A5A6;
        embeds[0].footer = { text: "❌ Rejected — no trades this week. Proposal expired." };
      }
      return json({ type: R_UPDATE_MESSAGE, data: { embeds, components: [] } });
    }

    if (action === "approve_buy") {
      const err = await dispatchToGitHub(env, {
        command: "approve_buy", ticker, trigger, message_id: i.message.id, ...common,
      });
      const embeds = i.message.embeds || [];
      if (embeds[0]) embeds[0].footer = { text: err
        ? `❌ Approval NOT executed — ${err}`
        : `⏳ Approved — sizing & submitting BUY ${ticker}…` };
      return json({ type: R_UPDATE_MESSAGE, data: { embeds, components: err ? i.message.components : [] } });
    }

    if (action === "approve_sell") {
      const err = await dispatchToGitHub(env, {
        command: "approve_sell", ticker, trigger, message_id: i.message.id, ...common,
      });
      const embeds = i.message.embeds || [];
      if (embeds[0]) embeds[0].footer = { text: err
        ? `❌ Approval NOT executed — ${err}`
        : `⏳ Approved — submitting SELL ${ticker}…` };
      return json({ type: R_UPDATE_MESSAGE, data: { embeds, components: err ? i.message.components : [] } });
    }

    if (action === "confirm_pipeline_execute" || action === "confirm_stoploss_execute") {
      const command = action === "confirm_pipeline_execute" ? "pipeline_execute" : "stoploss_execute";
      const err = await dispatchToGitHub(env, { command, ...common });
      return json({
        type: R_UPDATE_MESSAGE,
        data: {
          content: err
            ? `❌ NOT executed — ${err}`
            : `🚀 Confirmed — \`${command}\` is running. Results post in ~2–5 min.`,
          components: err ? i.message.components : [],
        },
      });
    }

    if (action === "confirm_buy_execute") {
      const result = await placeBracketOrder(env, ticker, qtyOverride ? parseInt(qtyOverride) : null, portfolio);
      const ok = !!result.order;
      const embeds = i.message.embeds || [];
      if (embeds[0]) {
        const filled   = result.fillStatus === "filled";
        const fillNote = filled
          ? `✅ Filled ${result.fillQty} @ $${parseFloat(result.fillAvg || result.price).toFixed(2)}`
          : `⏳ Status: ${result.fillStatus || "submitted"} — market orders typically fill within seconds`;
        embeds[0].color = ok ? (filled ? C_GREEN : 0xF39C12) : C_RED;
        embeds[0].title = ok
          ? `✅ BUY ${ticker} — Order ${filled ? "Filled" : "Placed"}`
          : `❌ BUY ${ticker} — Order Failed`;
        embeds[0].footer = { text: ok
          ? `${fillNote} · Stop $${result.stopPrice} · ATR ${result.atrPct ? result.atrPct.toFixed(2) + "%" : "default"}`
          : `Error: ${result.error}` };
      }
      return json({ type: R_UPDATE_MESSAGE, data: { embeds, components: [] } });
    }

    if (action === "tp_sell") {
      const alpacaBase = "https://paper-api.alpaca.markets";
      const headers = portfolioHeaders(env, portfolio);
      let ok = false, errMsg = "";
      try {
        const r = await fetch(`${alpacaBase}/v2/positions/${ticker}`, { method: "DELETE", headers });
        ok = r.status === 200 || r.status === 204;
        if (!ok) { const b = await r.json(); errMsg = b?.message || `HTTP ${r.status}`; }
      } catch (e) { errMsg = e.message; }

      try {
        const raw = await env.KV.get(`pending_take_profits_${portfolio}`);
        const pending = raw ? JSON.parse(raw) : {};
        if (pending[ticker]) {
          pending[ticker].status = ok ? "executed" : "failed";
          pending[ticker].executed_at = new Date().toISOString();
          await env.KV.put(`pending_take_profits_${portfolio}`, JSON.stringify(pending), { expirationTtl: 8 * 24 * 3600 });
        }
      } catch (e) { console.warn("TP KV update error:", e.message); }

      const tpEmbeds = i.message.embeds || [];
      if (tpEmbeds[0]) {
        tpEmbeds[0].color = ok ? C_GREEN : C_RED;
        tpEmbeds[0].title = ok ? `✅ TAKE PROFIT ${ticker} — Sold` : `❌ TAKE PROFIT ${ticker} — Failed`;
        tpEmbeds[0].footer = { text: ok ? "Position closed · Paper trading" : `Error: ${errMsg}` };
      }
      return json({ type: R_UPDATE_MESSAGE, data: { embeds: tpEmbeds, components: [] } });
    }

    if (action === "tp_hold") {
      // User chose to hold — cancel the pending auto-sell
      try {
        const raw = await env.KV.get(`pending_take_profits_${portfolio}`);
        const pending = raw ? JSON.parse(raw) : {};
        if (pending[ticker]) {
          pending[ticker].status = "hold";
          await env.KV.put(`pending_take_profits_${portfolio}`, JSON.stringify(pending), { expirationTtl: 8 * 24 * 3600 });
        }
      } catch (e) { console.warn("TP hold KV error:", e.message); }
      const holdEmbeds = i.message.embeds || [];
      if (holdEmbeds[0]) {
        holdEmbeds[0].color = C_GREY;
        holdEmbeds[0].footer = { text: "🤚 Holding — auto-sell cancelled. Alpaca bracket still active." };
      }
      return json({ type: R_UPDATE_MESSAGE, data: { embeds: holdEmbeds, components: [] } });
    }

    if (action === "move_stop_breakeven") {
      // Cancel existing Alpaca stop leg + resubmit at entry price (breakeven)
      // ⚠️  ~1 second unprotected window between cancel and new stop — acceptable on paper trading.
      // On IBKR this would be a single modifyOrder() call with zero gap.
      const alpacaBase = "https://paper-api.alpaca.markets";
      const headers    = portfolioHeaders(env, portfolio);

      // Read position targets from KV (entry_price, qty stored at buy time)
      let targets = null;
      try {
        const tr = await env.KV.get(`position_target_${portfolio.toUpperCase()}_${ticker}`);
        if (tr) targets = JSON.parse(tr);
      } catch (e) {}
      if (!targets) return ephemeral(`❌ No position data found for ${ticker}. Cannot locate stop order.`);

      const entryPrice = targets.entry_price;
      const qty        = targets.qty || 1;

      // Step 1: Find the open stop-sell order for this symbol
      let stopOrderId = null, currentStopPrice = null;
      try {
        const r = await fetch(`${alpacaBase}/v2/orders?status=open&limit=50`, { headers });
        if (r.ok) {
          const orders = await r.json();
          const stopLeg = orders.find(o =>
            o.symbol === ticker && o.side === "sell" &&
            (o.type === "stop" || o.type === "stop_limit")
          );
          if (stopLeg) { stopOrderId = stopLeg.id; currentStopPrice = parseFloat(stopLeg.stop_price || 0); }
        }
      } catch (e) { return ephemeral(`❌ Failed to fetch open orders: ${e.message}`); }

      if (!stopOrderId) return ephemeral(`❌ No open stop order found for **${ticker}**.\nIt may have already been cancelled or triggered.`);
      if (currentStopPrice >= entryPrice) return ephemeral(`ℹ️ Stop for **${ticker}** is already at $${currentStopPrice.toFixed(2)} — at or above breakeven ($${entryPrice.toFixed(2)}). Nothing to do.`);

      // Step 2: Cancel the existing stop leg
      // ── UNPROTECTED WINDOW STARTS (~1 second) ──────────────────────────────
      let cancelOk = false;
      try {
        const r = await fetch(`${alpacaBase}/v2/orders/${stopOrderId}`, { method: "DELETE", headers });
        cancelOk = r.status === 200 || r.status === 204;
      } catch (e) { return ephemeral(`❌ Failed to cancel stop order: ${e.message}\nYour original stop at $${currentStopPrice.toFixed(2)} should still be active.`); }

      if (!cancelOk) return ephemeral(`❌ Could not cancel existing stop. It may have already filled or been cancelled.`);

      // Step 3: Submit new standalone stop at breakeven
      let newStopOk = false, newStopId = null, newStopErr = "";
      try {
        const r = await fetch(`${alpacaBase}/v2/orders`, {
          method: "POST", headers,
          body: JSON.stringify({
            symbol:        ticker,
            qty:           String(qty),
            side:          "sell",
            type:          "stop",
            time_in_force: "gtc",
            stop_price:    entryPrice.toFixed(2),
          }),
        });
        newStopOk = r.status === 200 || r.status === 201;
        if (newStopOk) { const o = await r.json(); newStopId = o.id; }
        else { const b = await r.json(); newStopErr = b?.message || `HTTP ${r.status}`; }
      } catch (e) { newStopErr = e.message; }
      // ── UNPROTECTED WINDOW ENDS ────────────────────────────────────────────

      // Update KV with new stop price regardless (so monitor tracks it)
      if (newStopOk) {
        targets.stop_price        = entryPrice;
        targets.trailing_notified = true;
        await env.KV.put(`position_target_${portfolio.toUpperCase()}_${ticker}`, JSON.stringify(targets), { expirationTtl: 8 * 24 * 3600 });
      }

      const msEmbeds = i.message.embeds || [];
      if (msEmbeds[0]) {
        msEmbeds[0].color = newStopOk ? C_GREEN : C_RED;
        if (newStopOk) {
          msEmbeds[0].title = `✅ Stop moved to breakeven — ${ticker}`;
          msEmbeds[0].footer = { text: `New stop: $${entryPrice.toFixed(2)} (breakeven) · GTC · Order ID: ${newStopId} · Take-profit ceiling still active · Paper trading` };
        } else {
          msEmbeds[0].title = `⚠️ STOP CANCELLED BUT NEW STOP FAILED — ${ticker}`;
          msEmbeds[0].description = `Old stop was cancelled but new stop at $${entryPrice.toFixed(2)} failed to submit.\n**Error:** ${newStopErr}\n\n🚨 **${ticker} currently has NO stop protection. Place one manually in Alpaca immediately.**`;
          msEmbeds[0].color = C_RED;
        }
      }
      return json({ type: R_UPDATE_MESSAGE, data: { embeds: msEmbeds, components: [] } });
    }


    if (action === "change_qty") {
      // Open a Discord modal for custom share count input
      return json({
        type: R_MODAL,
        data: {
          custom_id: `ia_qty_modal|${ticker}|${portfolio}`,
          title:     `Custom quantity — ${ticker}`,
          components: [{ type: 1, components: [{
            type:        4,
            custom_id:   "qty",
            label:       "Number of shares to buy",
            style:       1,
            min_length:  1,
            max_length:  6,
            required:    true,
            placeholder: "e.g. 10",
          }]}],
        },
      });
    }

    if (action === "tp_half_sell") {
      const alpacaBase = "https://paper-api.alpaca.markets";
      const headers    = portfolioHeaders(env, portfolio);
      let posQty = 0, ok = false, errMsg = "";
      try {
        const pr = await fetch(`${alpacaBase}/v2/positions/${ticker}`, { headers });
        if (pr.ok) { const pd = await pr.json(); posQty = parseInt(pd.qty || 0); }
        else if (pr.status !== 404) { const b = await pr.json().catch(() => ({})); errMsg = `Alpaca ${pr.status}: ${b?.message || 'error'}`; }
      } catch (e) { errMsg = e.message; }

      const halfQty = Math.max(1, Math.floor(posQty / 2));
      if (!posQty) return ephemeral(`❌ No open position found for ${ticker}.`);

      try {
        const r = await fetch(`${alpacaBase}/v2/orders`, {
          method: "POST", headers,
          body: JSON.stringify({ symbol: ticker, qty: String(halfQty), side: "sell", type: "market", time_in_force: "day" }),
        });
        ok = r.status === 200 || r.status === 201;
        if (!ok) { const b = await r.json(); errMsg = b?.message || `HTTP ${r.status}`; }
      } catch (e) { errMsg = e.message; }

      // Mark as half-executed in KV
      try {
        const raw = await env.KV.get(`pending_take_profits_${portfolio}`);
        const pending = raw ? JSON.parse(raw) : {};
        if (pending[ticker]) {
          pending[ticker].status     = ok ? "half_executed" : "failed";
          pending[ticker].executed_at = new Date().toISOString();
          await env.KV.put(`pending_take_profits_${portfolio}`, JSON.stringify(pending), { expirationTtl: 8 * 24 * 3600 });
        }
      } catch (e) { console.warn("tp_half_sell KV:", e.message); }

      const halfEmbeds = i.message.embeds || [];
      if (halfEmbeds[0]) {
        halfEmbeds[0].color = ok ? C_GREEN : C_RED;
        halfEmbeds[0].title = ok ? `✅ PARTIAL SELL ${ticker} — ${halfQty} shares` : `❌ Partial sell failed ${ticker}`;
        halfEmbeds[0].footer = { text: ok
          ? `Sold ${halfQty} of ${posQty} shares · ${posQty - halfQty} remaining · Alpaca bracket still active on remaining · Paper trading`
          : `Error: ${errMsg}` };
      }
      return json({ type: R_UPDATE_MESSAGE, data: { embeds: halfEmbeds, components: [] } });
    }

    if (action === "screener_buy") {
      // Buy button pressed directly on a /screener embed pick
      // Ticker passed via custom_id — run gates then show buy preview
      let nearEarnings = false, regimeOk = true, bucket = "unknown", regimeLabel = "UNKNOWN", permitted = [];
      try {
        const [rr, br] = await Promise.all([env.KV.get("regime_signal"), env.KV.get("stock_buckets")]);
        if (rr) { const r = JSON.parse(rr); regimeLabel = r.label || "UNKNOWN"; permitted = r.permitted_strategies || []; }
        if (br) { const b = JSON.parse(br); const info = b[ticker]; if (info) { bucket = info.bucket; regimeOk = info.regime_ok !== false; nearEarnings = info.near_earnings === true; } }
      } catch (e) {}

      if (nearEarnings) return ephemeral(`⚠️ **${ticker}** is in earnings blackout (within 14 days). Wait until after earnings.`);
      if (!regimeOk)    return ephemeral(`🚫 **${ticker}** bucket \`${bucket}\` not permitted in **${regimeLabel}**.\nPermitted: ${permitted.join(", ") || "none"}`);

      const preview = await buildBuyPreview(env, ticker, null, "screener");
      if (preview.error) return ephemeral(preview.error);
      return json({ type: R_CHANNEL_MESSAGE, data: { flags: EPHEMERAL, embeds: preview.embeds, components: preview.components } });
    }


    if (action === "confirm_sell_execute") {
      const alpacaBase = "https://paper-api.alpaca.markets";
      const headers = portfolioHeaders(env, portfolio);
      let ok = false, errMsg = "";
      try {
        const r = await fetch(`${alpacaBase}/v2/positions/${ticker}`, { method: "DELETE", headers });
        ok = r.status === 200 || r.status === 204;
        if (!ok) { const b = await r.json(); errMsg = b?.message || `HTTP ${r.status}`; }
      } catch (e) { errMsg = e.message; }

      const embeds = i.message.embeds || [];
      if (embeds[0]) {
        embeds[0].color = ok ? C_GREEN : C_RED;
        embeds[0].title = ok ? `✅ SELL ${ticker} — Position Closed` : `❌ SELL ${ticker} — Failed`;
        embeds[0].footer = { text: ok ? "Market order submitted · Paper trading" : `Error: ${errMsg}` };
      }
      return json({ type: R_UPDATE_MESSAGE, data: { embeds, components: [] } });
    }

    if (action === "cancel") {
      return json({ type: R_UPDATE_MESSAGE, data: { content: "🚫 Cancelled.", components: [] } });
    }
    return ephemeral("Unknown action.");
  }

  // ── Slash commands ───────────────────────────────────────────────────────
  if (i.type === APPLICATION_COMMAND) {
    const name = i.data.name;
    const opts = Object.fromEntries((i.data.options || []).map((o) => [o.name, o.value]));

    if (name === "help") {
      // Help uses follow-up webhooks to bypass the 6000-char single-message limit.
      // Initial response: embeds 1+2 (Overview + Regime)
      // ctx.waitUntil -> follow-up 1: Screener workflow
      // ctx.waitUntil -> follow-up 2: Pipeline workflow
      // ctx.waitUntil -> follow-up 3: Command reference
      const appId = env.DISCORD_APP_ID || "";
      const token = i.token;
      const followUpUrl = `https://discord.com/api/v10/webhooks/${appId}/${token}`;

      async function postFollowUp(embeds) {
        await fetch(followUpUrl, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ embeds, flags: EPHEMERAL }),
        });
      }

      // Embed 1: System Overview
      const e1 = {
        title: "\U0001f916  Investment Alpha — System Overview",
        color: C_BLUE,
        description: [
          "Two parallel trading workflows, both gated by a shared **Regime Engine** that scores the market 3x daily.",
          "```",
          "        REGIME ENGINE  (auto, 3x daily)",
          "  Reads: VIX / Fear&Greed / SPY / Breadth / ADX",
          "  Score: 0-100 -> STRONG BULL / MOD BULL / NEUTRAL / BEARISH",
          "                    |",
          "          +---------+---------+",
          "          v                   v",
          "   SCREENER (daily)    PIPELINE (monthly)",
          "   100 stocks          575 stocks",
          "   Tactical entries    Full rebalance",
          "   Screener acct       Pipeline acct",
          "```",
          "The two Alpaca accounts are **intentionally separate** so you can compare performance before committing real capital.",
        ].join("\n"),
        footer: { text: "3 more sections follow — Regime · Screener · Pipeline · Commands" },
      };

      // Embed 2: Regime Engine
      const e2 = {
        title: "\U0001f321️  Workflow 0 — The Regime Engine",
        color: C_GREY,
        description: "Runs automatically 3x per day (8 AM / 11 AM / 3:30 PM ET on weekdays). Score stored in Cloudflare KV — every command reads from it.",
        fields: [
          {
            name: "Scoring (100 pts total)",
            value: [
              "```",
              "VIX Level          20pt  Low VIX = calm market",
              "VIX Term Structure 10pt  Short vs long-term fear",
              "Fear & Greed       15pt  CNN index (0=fear, 100=greed)",
              "ADX on SPY         20pt  Trend strength of S&P 500",
              "SPY vs 200MA       20pt  S&P above long-term average?",
              "Sector Breadth     15pt  % of 11 sectors above 200MA",
              "```",
            ].join("\n"),
            inline: false,
          },
          {
            name: "Regime levels & what they unlock",
            value: [
              "```",
              ">=75  STRONG BULL   momentum + breakout + mean_rev + catalyst",
              "55-74 MOD BULL      momentum + mean_reversion + catalyst",
              "40-54 NEUTRAL       mean_reversion + defensive",
              "<40   BEARISH       defensive only",
              "```",
              "Position size: **5%** (Strong Bull) · **3%** (Mod Bull/Neutral) · **1.5%** (Bearish) of buying power",
            ].join("\n"),
            inline: false,
          },
        ],
      };

      // Return initial response immediately (embeds 1+2 are well within 6000-char limit)
      const initialResp = json({
        type: R_CHANNEL_MESSAGE,
        data: { flags: EPHEMERAL, embeds: [e1, e2] },
      });

      // Follow-up 1: Screener Workflow
      const e3 = {
        title: "\U0001f4ca  Workflow 1 — Daily Screener (Tactical)",
        color: C_GREEN,
        description: "Runs automatically 3x per day. Finds the best stock opportunities within the current regime. All trades go to the **Screener Alpaca account** (separate from Pipeline).",
        fields: [
          {
            name: "How it works (fully automated)",
            value: [
              "```",
              "AUTO: 8AM / 11AM / 3:30PM ET",
              "  1. Regime scored (6 components) -> KV",
              "  2. 100 stocks fetched",
              "     Price & MA data  -> Alpaca",
              "     Fundamentals     -> Finnhub",
              "  3. Each stock classified into a bucket:",
              "     avoid / watch / catalyst / breakout",
              "     momentum / defensive / mean_reversion",
              "  4. Regime gate: only permitted buckets pass",
              "  5. Remaining stocks scored 0-100",
              "  6. Top 5 stored in KV with conviction flags",
              "     [check] >=55 pts = high conviction",
              "     [warn]  <55 pts  = viable, lower confidence",
              "```",
            ].join("\n"),
            inline: false,
          },
          {
            name: "Your daily action (manual)",
            value: [
              "```",
              "/screener        -> see today's top 5 picks",
              "/buy symbol:C portfolio:Screener -> preview order:",
              "                    Price / Shares / Total cost",
              "                    Stop loss  -5%  (Alpaca auto-manages)",
              "                    Take profit +12% (Alpaca auto-manages)",
              "Click confirm    -> bracket order placed",
              "                    No further action needed",
              "```",
            ].join("\n"),
            inline: false,
          },
          {
            name: "\U0001f4e6 Universe",
            value: "~100 large-cap US stocks: Mega-cap tech · Cybersecurity · Financials · Healthcare · Consumer · Energy · Industrials · Growth.",
            inline: true,
          },
          {
            name: "\U0001f4b0 Position sizing",
            value: "Auto by regime: 5% (Strong Bull) · 3% (Mod Bull/Neutral) · 1.5% (Bearish) of buying power.",
            inline: true,
          },
        ],
      };

      // Follow-up 2: Pipeline Workflow
      const e4 = {
        title: "\U0001f504  Workflow 2 — Monthly Pipeline (Strategic)",
        color: C_ORANGE,
        description: "Runs **manually once a month** (1st of month). Scores 575 stocks on a 6-factor model and rebalances the full portfolio. All trades go to the **Pipeline Alpaca account**.",
        fields: [
          {
            name: "How it works",
            value: [
              "```",
              "YOU: /pipeline mode:dry  (run on 1st of month)",
              "  1. 575 stocks scored on 6 factors:",
              "     momentum    28%  [========]",
              "     trend       20%  [======]",
              "     quality     18%  [=====]",
              "     valuation   14%  [====]",
              "     sentiment   10%  [===]  (analyst rev + congress trades)",
              "     volatility  10%  [===]  (penalty)",
              "  2. Regime sets portfolio size:",
              "     STRONG/MOD BULL -> top 10 positions",
              "     NEUTRAL         -> top  8 positions",
              "     BEARISH         -> top  5 positions",
              "  3. ATR-based stop levels per position:",
              "     BULL    -> ATR x 2.5  (wider, avoids shakeouts)",
              "     NEUTRAL -> ATR x 2.0",
              "     BEAR    -> ATR x 1.5  (tight, protect capital)",
              "  4. Rebalance proposal shown with BUY/SELL signals",
              "     + confirm button",
              "YOU: /pipeline mode:execute -> orders placed",
              "```",
            ].join("\n"),
            inline: false,
          },
          {
            name: "\U0001f4c8 Sentiment factor (10%)",
            value: "Analyst revisions (70%) + congressional stock trades (30%). Unusual political buying is a historically strong leading indicator.",
            inline: false,
          },
          {
            name: "\U0001f6d1 Stop loss",
            value: "ATR x multiplier. Wider in bull markets to avoid shake-outs, tightens in bear markets to protect capital. `/stoploss mode:check` shows current distances.",
            inline: true,
          },
          {
            name: "\U0001f501 Learning loop",
            value: "Every Saturday: approve/reject decisions scored vs the model. Stop exits get a 30-day post-mortem. Factor weights auto-adjust over time.",
            inline: true,
          },
        ],
      };

      // Follow-up 3: Command Reference
      const e5 = {
        title: "⌨️  Command Reference",
        color: C_BLUE,
        fields: [
          {
            name: "\U0001f4ca Daily Screener",
            value: "`/screener` Top 5 picks + conviction badges\n`/buy symbol:X portfolio:Screener|Pipeline` Preview & confirm bracket order\n`/sell symbol:X portfolio:Screener|Pipeline` See P&L then confirm close",
            inline: false,
          },
          {
            name: "\U0001f321️ Regime",
            value: "`/regime` Score, label, VIX, SPY vs 200MA, sector breadth, permitted strategies",
            inline: false,
          },
          {
            name: "\U0001f504 Pipeline",
            value: "`/pipeline mode:dry` 575-stock analysis, signals only (~10-30 min)\n`/pipeline mode:execute` Rebalance portfolio (confirm button)",
            inline: false,
          },
          {
            name: "\U0001f4cb Portfolio & Charts",
            value: "`/status` All positions, P&L, cost basis\n`/chart symbol:AAPL` Price chart\n`/chart symbol:portfolio` Equity curve vs SPY\n`/monitor` Immediate position check",
            inline: false,
          },
          {
            name: "\U0001f6d1 Stop Loss",
            value: "`/stoploss mode:check` Stop distances, no orders\n`/stoploss mode:execute` Exit breached positions (confirm button)",
            inline: false,
          },
          {
            name: "\U0001f4c8 Take Profit (Auto)",
            value: "Worker monitors every **2 min** (4am–8pm ET).\nTargets set at buy time via ATR-14 + regime:\nStrong Bull: stop 2×ATR / ceiling 4×ATR \u2022 Mod Bull: 1.5/3 \u2022 Neutral: 1.25/2 \u2022 Bearish: 1/1.5\nMonitor = 80% of ceiling — fires Discord alert + **Sell Now / Hold** buttons.\nRegular hours (9:30am–4pm): **auto-sells after 2 min** if no action.",
            inline: false,
          },
          {
            name: "\U0001f534 Kill Switch",
            value: "`/pausetrading` Disable webhook buys + TP auto-sells\n`/resumetrading` Re-enable both\nAlpaca brackets + manual /buy /sell always work.",
            inline: false,
          },
          {
            name: "ℹ️ Info",
            value: "`/strategy` Factor weights, universe, sleeve details\n`/help` This guide (5 messages)",
            inline: false,
          },
          {
            name: "\U0001f4a1 Morning routine",
            value: "`/regime` check market · `/screener` see picks · `/buy symbol:X portfolio:Screener` trade\nMonthly (1st): `/pipeline mode:dry` review · `/pipeline mode:execute` rebalance",
            inline: false,
          },
        ],
        footer: { text: "Screener -> Screener Alpaca acct  |  Pipeline -> Pipeline Alpaca acct  |  Bracket stops auto-managed by Alpaca" },
      };

      ctx.waitUntil(postFollowUp([e3]));
      ctx.waitUntil(postFollowUp([e4]));
      ctx.waitUntil(postFollowUp([e5]));
      return initialResp;
    }

    // /screener — served directly from KV (no GitHub round-trip needed)
    if (name === "screener") {
      try {
        const raw = await env.KV.get("screener_summary");
        if (!raw) {
          return json({ type: R_CHANNEL_MESSAGE, data: {
            flags: EPHEMERAL,
            content: "No screener data yet — runs at 8 AM ET on weekdays.",
          }});
        }
        const s = JSON.parse(raw);
        const picks = s.top_picks || [];
        const highConviction = s.high_conviction_count ?? picks.filter(p => p.conviction_ok).length;
        const allBelowThreshold = picks.length > 0 && highConviction === 0;

        const pickLines = picks.map((p, idx) => {
          const badge = p.conviction_ok ? "✅" : "⚠️";
          return `${badge} ${idx + 1}. **${p.ticker}** — score ${p.score}/100 (${p.bucket})`;
        }).join("\n");

        let description = pickLines || "_No stocks passed the regime gate today._";
        let convictionNote = "";
        if (allBelowThreshold) {
          convictionNote = "⚠️ **Regime is healthy but no stocks cleared the 55-pt conviction bar.** " +
            "These are the best available — consider reduced position size or wait for a stronger setup. " +
            "✅ = high conviction (≥55)  ⚠️ = below threshold but regime-permitted.";
        } else if (picks.length > 0 && highConviction < picks.length) {
          convictionNote = `✅ ${highConviction} high-conviction pick${highConviction > 1 ? "s" : ""}. ` +
            "⚠️ stocks are below the 55-pt threshold — viable but lower confidence.";
        }

        return json({ type: R_CHANNEL_MESSAGE, data: {
          flags: EPHEMERAL,
          embeds: [{
            title: `📊 Screener — ${s.date || "today"}`,
            description: (convictionNote ? convictionNote + "\n\n" : "") + description,
            color: allBelowThreshold ? C_ORANGE : C_BLUE,
            fields: [
              { name: "Regime",     value: `${s.regime_label} (${s.regime_score}/100)`, inline: true },
              { name: "Permitted",  value: (s.permitted_strategies || []).join(", ") || "—", inline: true },
              { name: "Scored",     value: String(s.total_scored || 0), inline: true },
            ],
            footer: { text: "Updated 8 AM ET · ✅ ≥55 pts = high conviction · ⚠️ = below threshold" },
          }],
          // Buy buttons — one per pick (up to 5)
          ...(picks.length ? [{ type: 1, components: picks.slice(0, 5).map(p => ({
            type: 2, style: 1, label: `Buy ${p.ticker}`,
            custom_id: `ia|screener_buy|${p.ticker}|screener`,
          })) }] : []),
        }});
      } catch (e) {
        return ephemeral(`Error reading screener: ${e.message}`);
      }
    }

    // /regime — served directly from KV (updated 3x daily by screener)
    if (name === "regime") {
      try {
        const raw = await env.KV.get("regime_signal");
        if (!raw) {
          return json({ type: R_CHANNEL_MESSAGE, data: {
            flags: EPHEMERAL,
            content: "No regime data yet — runs at 8 AM ET on weekdays.",
          }});
        }
        const r = JSON.parse(raw);
        const label = (r.label || "UNKNOWN").toUpperCase();
        const score = r.total ?? r.score ?? 0;
        const color = label === "BULL" ? C_GREEN : label === "BEAR" ? C_RED : C_ORANGE;
        const d = r.details || {};
        const noteParts = [];
        if (d.spy_pct_from_200ma != null) noteParts.push(`SPY ${d.spy_pct_from_200ma > 0 ? "+" : ""}${Number(d.spy_pct_from_200ma).toFixed(1)}% vs 200MA`);
        if (d.adx != null)                noteParts.push(`ADX ${Number(d.adx).toFixed(0)} (${d.spy_trend || "flat"})`);
        if (d.breadth_pct != null)        noteParts.push(`Breadth ${d.breadth_pct}%`);
        if (d.fg != null)                 noteParts.push(`F&G ${d.fg}`);
        if (d.vix_struct)                 noteParts.push(`VIX ${d.vix_struct}`);
        const autoNotes = noteParts.length ? noteParts.join(" · ") : "—";
        return json({ type: R_CHANNEL_MESSAGE, data: {
          flags: EPHEMERAL,
          embeds: [{
            title: `🧭 Market Regime: ${label}`,
            description: "Regime measures the **structural trend** (200-day MA, volatility, credit) — not today's move. A red day inside an uptrend is still BULL.",
            color,
            fields: [
              { name: "Score",       value: `${score}/100`,                                    inline: true },
              { name: "Permitted",   value: (r.permitted_strategies || []).join(", ") || "—",  inline: true },
              { name: "VIX",         value: (r.vix ?? r.vix_value) != null ? String(r.vix ?? r.vix_value) : "n/a", inline: true },
              { name: "Notes",       value: autoNotes,                                         inline: false },
            ],
            footer: { text: `Updated 3× daily (8 AM, 11 AM, 3:30 PM ET) · as of ${r.date || new Date().toISOString().slice(0,10)}` },
          }],
        }});
      } catch (e) {
        return ephemeral(`Error reading regime: ${e.message}`);
      }
    }

    // /buy — enriched calculator preview with Change qty support
    if (name === "buy") {
      const symbol = (opts.symbol || "").toUpperCase();
      const portfolio = (opts.portfolio || "screener").toLowerCase();
      if (!symbol) return ephemeral("Provide a ticker: `/buy symbol:AAPL`");

      // Regime + bucket gates (same as before)
      let regimeLabel = "UNKNOWN", regimeScore = 0, permitted = [];
      try {
        const raw = await env.KV.get("regime_signal");
        if (raw) { const r = JSON.parse(raw); regimeLabel = r.label || "UNKNOWN"; regimeScore = r.total ?? r.score ?? 0; permitted = r.permitted_strategies || []; }
      } catch {}

      let bucket = "unknown", regimeOk = true, nearEarnings = false;
      try {
        const raw = await env.KV.get("stock_buckets");
        if (raw) { const b = JSON.parse(raw); const info = b[symbol]; if (info) { bucket = info.bucket || "unknown"; regimeOk = info.regime_ok !== false; nearEarnings = info.near_earnings === true; } }
      } catch {}

      if (nearEarnings) return ephemeral(`⚠️ **${symbol}** is in earnings blackout (within 14 days). Wait until after earnings to avoid gap risk.`);
      if (!regimeOk)    return ephemeral(`🚫 **${symbol}** bucket \`${bucket}\` is not permitted in **${regimeLabel}** market.\nPermitted: ${permitted.join(", ") || "none"}`);

      // Build enriched preview (handles duplicate guard, max positions, ATR targets)
      const preview = await buildBuyPreview(env, symbol, null, portfolio);
      if (preview.error) return ephemeral(preview.error);
      return json({ type: R_CHANNEL_MESSAGE, data: { flags: EPHEMERAL, embeds: preview.embeds, components: preview.components } });
    }


    // /sell — preview position then confirm close
    if (name === "sell") {
      const symbol = (opts.symbol || "").toUpperCase();
      const portfolio = (opts.portfolio || "screener").toLowerCase();
      if (!symbol) return ephemeral("Provide a ticker: `/sell symbol:AAPL`");

      const alpacaBase = "https://paper-api.alpaca.markets";
      const ah = portfolioHeaders(env, portfolio);

      let position = null;
      let posApiErr = null;
      try {
        const r = await fetch(`${alpacaBase}/v2/positions/${symbol}`, { headers: ah });
        if (r.ok) { position = await r.json(); }
        else if (r.status === 404) { posApiErr = null; }
        else { const b = await r.json().catch(() => ({})); posApiErr = `Alpaca API error ${r.status}: ${b?.message || 'unknown'}`; }
      } catch (e) { posApiErr = `Network error: ${e.message}`; }
      if (posApiErr) return ephemeral(`❌ Alpaca API error for **${symbol}**: ${posApiErr}`);
      if (!position) return ephemeral(`❌ No open position found for **${symbol}**. Confirmed via Alpaca API (404).`);

      const qty    = position.qty;
      const price  = parseFloat(position.current_price || 0);
      const pnl    = parseFloat(position.unrealized_pl || 0);
      const pnlPct = parseFloat(position.unrealized_plpc || 0) * 100;
      const pnlStr = `$${pnl.toFixed(2)} (${pnl >= 0 ? "+" : ""}${pnlPct.toFixed(1)}%)`;

      return json({ type: R_CHANNEL_MESSAGE, data: { flags: EPHEMERAL, embeds: [{
        title: `⚠️ Confirm SELL ${symbol}?`,
        color: C_ORANGE,
        fields: [
          { name: "Shares",       value: String(qty),              inline: true },
          { name: "Current price",value: `$${price.toFixed(2)}`,  inline: true },
          { name: "Unrealised P&L",value: pnlStr,                 inline: true },
        ],
        footer: { text: "Market order — closes entire position · Paper trading" },
      }], components: [{ type: 1, components: [
        { type: 2, style: 4, label: `🔴 Sell all ${qty} shares [${portfolio === 'screener' ? 'Screener' : 'Pipeline'}]`, custom_id: `ia|confirm_sell_execute|${symbol}|sell||${portfolio}` },
        { type: 2, style: 2, label: "❌ Cancel", custom_id: `ia|cancel||sell` },
      ]}]}});
    }

    if (name === "pausetrading") {
      await env.KV.put("trading_paused", "1", { expirationTtl: 7 * 24 * 3600 });
      return ephemeral("🔴 **Auto-trading paused.**\nWebhook buys + auto-sells are disabled.\nAlpaca brackets and manual /buy /sell still work.\nResume with `/resumetrading`.");
    }

    if (name === "resumetrading") {
      await env.KV.delete("trading_paused");
      return ephemeral("🟢 **Auto-trading resumed.** Webhook buys + auto-sells are enabled.");
    }

    // All other commands → GitHub Actions
    const commandMap = {
      "status":    "status",
      "monitor":   "monitor_check",
      "strategy":  "strategy",
      "chart":     "chart",
    };
    const stoplossMode  = opts.mode;
    const pipelineMode  = opts.mode;

    let command;
    if (name === "stoploss") {
      command = stoplossMode === "execute" ? "stoploss_execute" : "stoploss_check";
    } else if (name === "pipeline") {
      if (pipelineMode === "execute") {
        // Show confirm button before executing
        return json({
          type: R_CHANNEL_MESSAGE,
          data: {
            content: "⚠️ **Confirm pipeline execute?** This will rebalance your Alpaca portfolio.",
            components: [{
              type: 1,
              components: [
                { type: 2, style: 4, label: "🚀 Yes, rebalance now", custom_id: "ia|confirm_pipeline_execute||pipeline" },
                { type: 2, style: 2, label: "Cancel", custom_id: "ia|cancel||pipeline" },
              ],
            }],
          },
        });
      }
      command = "pipeline_dry";
    } else {
      command = commandMap[name] || name;
    }

    // Defer response (pipeline takes minutes)
    const needsDefer = ["status", "monitor_check", "strategy", "chart",
                        "stoploss_check", "stoploss_execute", "pipeline_dry"].includes(command);

    if (needsDefer) {
      // Kick off GitHub dispatch — ctx.waitUntil ensures Cloudflare keeps the
      // Worker alive until the fetch completes even after the Response is sent.
      const payload = { command, symbol: opts.symbol, mode: opts.mode, ...common };
      ctx.waitUntil(dispatchToGitHub(env, payload));
      return json({ type: R_DEFERRED_MESSAGE, data: { flags: EPHEMERAL } });
    }

    const err = await dispatchToGitHub(env, { command, ...common });
    return err
      ? ephemeral("❌ Failed to dispatch: " + err)
      : ephemeral("⏳ Running… results will appear shortly.");
  }

  return new Response("Unknown interaction type", { status: 400 });
}


// ══════════════════════════════════════════════════════════════════════════
//  SCHEDULED HANDLER — runs every 2 min (cron: "*/2 * * * *")
//  Monitor window: 4am–8pm ET (08:00–00:00 UTC)
//  Auto-execute:   regular hours only 9:30am–4pm ET (13:30–20:00 UTC)
// ══════════════════════════════════════════════════════════════════════════
// ══════════════════════════════════════════════════════════════════════════
//  MORNING BRIEF — fires at 9:25am ET (13:25 UTC) on weekdays
//  Summarises: regime, positions P&L, buying power, top screener pick
// ══════════════════════════════════════════════════════════════════════════
async function runMorningBrief(env) {
  const webhookUrl = env.DISCORD_WEBHOOK_URL || "";
  if (!webhookUrl) return;

  const alpacaBase = "https://paper-api.alpaca.markets";

  let sPos = [], pPos = [], sAcct = null, pAcct = null, regime = null, summary = null;
  try {
    const [spR, saR, ppR, paR, rr, sr] = await Promise.all([
      fetch(`${alpacaBase}/v2/positions`, { headers: portfolioHeaders(env, "screener") }),
      fetch(`${alpacaBase}/v2/account`,   { headers: portfolioHeaders(env, "screener") }),
      fetch(`${alpacaBase}/v2/positions`, { headers: portfolioHeaders(env, "pipeline") }),
      fetch(`${alpacaBase}/v2/account`,   { headers: portfolioHeaders(env, "pipeline") }),
      env.KV.get("regime_signal"),
      env.KV.get("screener_summary"),
    ]);
    if (spR.ok) sPos  = await spR.json();
    if (saR.ok) sAcct = await saR.json();
    if (ppR.ok) pPos  = await ppR.json();
    if (paR.ok) pAcct = await paR.json();
    if (rr) regime  = JSON.parse(rr);
    if (sr) summary = JSON.parse(sr);
  } catch (e) { console.error("Morning brief fetch error:", e.message); return; }

  const fmtPositions = (positions, label) => {
    const pnl = positions.reduce((s, p) => s + parseFloat(p.unrealized_pl || 0), 0);
    const val = positions.reduce((s, p) => s + parseFloat(p.market_value  || 0), 0);
    const lines = positions.length
      ? positions.map(p => {
          const pp = parseFloat(p.unrealized_pl || 0);
          const pc = parseFloat(p.unrealized_plpc || 0) * 100;
          return `${pp >= 0 ? "📈" : "📉"} **${p.symbol}** ${pp >= 0 ? "+" : ""}$${pp.toFixed(2)} (${pc.toFixed(1)}%)`;
        }).join("\n")
      : "_No positions_";
    return { pnl, val, lines };
  };

  const sc = fmtPositions(Array.isArray(sPos) ? sPos : [], "Screener");
  const pc = fmtPositions(Array.isArray(pPos) ? pPos : [], "Pipeline");
  const totalPnl = sc.pnl + pc.pnl;
  const sBp      = parseFloat(sAcct?.buying_power || 0);
  const pBp      = parseFloat(pAcct?.buying_power || 0);
  const regLabel = regime?.label || "UNKNOWN";
  const regScore = regime?.total  || 0;
  const topPick  = summary?.top_picks?.[0];
  const today    = new Date().toLocaleDateString("en-US", { weekday: "short", month: "short", day: "numeric" });

  await postDiscordWebhook(webhookUrl, [{
    title:       `☀️ Morning Brief — ${today}`,
    color:       totalPnl >= 0 ? C_GREEN : C_RED,
    description: `**📊 Screener**\n${sc.lines}\n\n**🔧 Pipeline**\n${pc.lines}`,
    fields: [
      { name: "Regime",              value: `${regLabel} (${regScore}/100)`,                                inline: true },
      { name: "Screener P&L",        value: `${sc.pnl >= 0 ? "+" : ""}$${sc.pnl.toFixed(2)}`,             inline: true },
      { name: "Pipeline P&L",        value: `${pc.pnl >= 0 ? "+" : ""}$${pc.pnl.toFixed(2)}`,             inline: true },
      { name: "Screener value",      value: `$${sc.val.toFixed(2)}`,                                       inline: true },
      { name: "Pipeline value",      value: `$${pc.val.toFixed(2)}`,                                       inline: true },
      { name: "Screener buying pwr", value: `$${sBp.toFixed(2)}`,                                          inline: true },
      { name: "Pipeline buying pwr", value: `$${pBp.toFixed(2)}`,                                          inline: true },
      { name: "Top pick today",      value: topPick ? `**${topPick.ticker}** (score ${topPick.score}/100)` : "No picks today", inline: true },
    ],
    footer:    { text: "Paper trading · Market opens 9:30am ET · Use /screener for full picks" },
    timestamp: new Date().toISOString(),
  }]);
}

async function runTakeProfitMonitor(env) {
  const now     = new Date();
  const utcMins = now.getUTCHours() * 60 + now.getUTCMinutes();

  // Only run 4am–8pm ET = 08:00–00:00 UTC (480–1440 mins)
  if (utcMins < 480 || utcMins >= 1440) return;

  // Regular market hours: 9:30am–4pm ET = 13:30–20:00 UTC (810–1200 mins)
  const isRegularHours = utcMins >= 810 && utcMins < 1200;

  // Kill switch
  const paused = await env.KV.get("trading_paused");
  if (paused) { console.log("TP monitor: paused, skipping"); return; }

  const alpacaBase  = "https://paper-api.alpaca.markets";
  const portfolios  = ["screener", "pipeline"];
  let allPositions  = [];
  for (const port of portfolios) {
    try {
      const r = await fetch(`${alpacaBase}/v2/positions`, { headers: portfolioHeaders(env, port) });
      if (r.ok) { const ps = await r.json(); if (Array.isArray(ps)) ps.forEach(p => allPositions.push({ ...p, _portfolio: port })); }
    } catch (e) { console.warn(`TP monitor ${port} positions:`, e.message); }
  }
  if (!allPositions.length) return;

  // Load pending TP state per portfolio
  const pending = {};
  for (const port of portfolios) {
    try { const raw = await env.KV.get(`pending_take_profits_${port}`); pending[port] = raw ? JSON.parse(raw) : {}; }
    catch (e) { pending[port] = {}; }
  }

  const webhookUrl     = env.DISCORD_WEBHOOK_URL || "";
  const pendingChanged = { screener: false, pipeline: false };

  for (const pos of allPositions) {
    const symbol       = pos.symbol;
    const portfolio    = pos._portfolio;
    const currentPrice = parseFloat(pos.current_price || 0);
    if (!currentPrice) continue;

    // Load targets stored at buy time
    let targets = null;
    try {
      const tRaw = await env.KV.get(`position_target_${portfolio.toUpperCase()}_${symbol}`);
      if (tRaw) targets = JSON.parse(tRaw);
    } catch (e) { console.warn(`TP ${symbol} target read:`, e.message); }
    if (!targets) continue;

    const monitorPrice  = targets.tp_monitor_price;
    const entryPrice    = targets.entry_price;
    const stopPrice     = targets.stop_price;
    const ceilingPrice  = targets.tp_alpaca_price;

    const entry = pending[portfolio][symbol];

    // Already triggered — check for 2-min auto-execute
    if (entry && entry.status === "pending" && isRegularHours) {
      const elapsed = (Date.now() - new Date(entry.triggered_at).getTime()) / 1000;
      if (elapsed >= 120) {
        let ok = false, errMsg = "";
        try {
          const r = await fetch(`${alpacaBase}/v2/positions/${symbol}`, { method: "DELETE", headers });
          ok = r.status === 200 || r.status === 204;
          if (!ok) { const b = await r.json(); errMsg = b?.message || `HTTP ${r.status}`; }
        } catch (e) { errMsg = e.message; }

        pending[portfolio][symbol] = { ...entry, status: ok ? "executed" : "failed",
                            executed_at: new Date().toISOString(), auto: true };
        pendingChanged[portfolio] = true;

        // Patch the Discord embed we sent earlier
        if (entry.message_id && webhookUrl) {
          const pnl    = currentPrice - entryPrice;
          const pnlPct = ((currentPrice / entryPrice) - 1) * 100;
          const updEmbed = {
            title: ok ? `✅ AUTO-SOLD ${symbol}` : `❌ AUTO-SELL FAILED ${symbol}`,
            color: ok ? C_GREEN : C_RED,
            fields: [
              { name: "Sell price", value: `$${currentPrice.toFixed(2)}`, inline: true },
              { name: "P&L",        value: `${pnl >= 0 ? "+" : ""}$${pnl.toFixed(2)} (${pnlPct.toFixed(1)}%)`, inline: true },
            ],
            footer: { text: ok ? "Auto-executed after 2-min window · Paper trading" : `Error: ${errMsg}` },
          };
          try {
            await fetch(`${webhookUrl}/messages/${entry.message_id}`, {
              method: "PATCH",
              headers: { "Content-Type": "application/json" },
              body: JSON.stringify({ embeds: [updEmbed], components: [] }),
            });
          } catch (e) { console.warn("Discord embed patch error:", e.message); }
        }
      }
      continue;
    }

    // Skip if already handled
    if (entry && ["executed", "hold", "failed", "half_executed"].includes(entry.status)) continue;
    const portLabel = portfolio === "screener" ? "📊 Screener" : "🔧 Pipeline";

    // ── Trailing stop: if up ≥5% and stop still below entry, notify to move it manually ──
    // NOTE: Alpaca bracket stops CANNOT be modified via API after placement.
    // The bracket links market buy + stop-loss + take-profit atomically.
    // Modifying a bracket leg returns: "cannot replace a leg of a bracket order".
    // To truly move the stop you would need to cancel the entire bracket (losing all
    // protection for ~2 seconds) then resubmit — too risky for automated code.
    // Instead: we update our KV tracking and send a Discord nudge so YOU decide.
    const unrealPct = targets.entry_price ? ((currentPrice / targets.entry_price) - 1) * 100 : 0;
    if (unrealPct >= 5 && targets.stop_price < targets.entry_price && !targets.trailing_notified) {
      targets.stop_price          = targets.entry_price; // KV tracking updated to breakeven
      targets.trailing_notified   = true;               // only notify once
      try {
        await env.KV.put(`position_target_${portfolio.toUpperCase()}_${symbol}`, JSON.stringify(targets), { expirationTtl: 8 * 24 * 3600 });
      } catch (e) { console.warn("Trailing stop KV update:", e.message); }
      if (webhookUrl) {
        await postDiscordWebhook(webhookUrl, [{
          title: `🛡️ [${portLabel}] Trailing Stop Alert — ${symbol} up ${unrealPct.toFixed(1)}%`,
          color: C_BLUE,
          description: [
            `**${symbol}** is up **${unrealPct.toFixed(1)}%** — consider moving your Alpaca stop to breakeven ($${targets.entry_price.toFixed(2)}).`,
            "",
            "**Why this is manual:**",
            "Alpaca bracket orders link your stop and take-profit atomically. The API rejects",
            "any attempt to modify a bracket leg after placement. The only workaround is to",
            "cancel the entire bracket and resubmit — leaving you unprotected for 1-2 seconds.",
            "On paper trading that is acceptable; with real money it is not worth the risk.",
            "",
            `**To move the stop manually:** Alpaca dashboard → ${symbol} position → edit stop order → set to $${targets.entry_price.toFixed(2)}.`,
            "",
            "Our KV tracking has been updated to breakeven. The 2-min monitor now uses the new stop level."
          ].join("\n"),
          fields: [
            { name: "Current price",  value: `$${currentPrice.toFixed(2)}`,          inline: true },
            { name: "Entry price",    value: `$${targets.entry_price.toFixed(2)}`,   inline: true },
            { name: "Gain",           value: `+${unrealPct.toFixed(1)}%`,            inline: true },
            { name: "New KV stop",    value: `$${targets.entry_price.toFixed(2)} (breakeven)`, inline: true },
            { name: "Original stop",  value: `$${stopPrice.toFixed(2)} (still in Alpaca!)`, inline: true },
            { name: "Action needed",  value: "Move stop in Alpaca dashboard manually", inline: true },
          ],
          footer: { text: "KV tracking updated · Alpaca bracket unchanged · Paper trading" },
        }], [{
          type: 1,
          components: [
            { type: 2, style: 1, label: "🔄 Move Stop to Breakeven", custom_id: `ia|move_stop_breakeven|${symbol}|trailing||${portfolio}` },
          ],
        }]);
      }
    }

    // Not yet triggered — check if price hit monitor threshold
    if (currentPrice >= monitorPrice) {
      const pnl    = currentPrice - entryPrice;
      const pnlPct = ((currentPrice / entryPrice) - 1) * 100;

      const embed = {
        title: `🔔 [${portLabel}] TAKE PROFIT ALERT — ${symbol}`,
        color: C_GREEN,
        description: isRegularHours
          ? `Price hit monitor target. **Auto-sell in 2 minutes** unless you act.`
          : `Price hit monitor target. Market closed — sell manually with /sell when open.`,
        fields: [
          { name: "Current",   value: `$${currentPrice.toFixed(2)}`,  inline: true },
          { name: "Monitor",   value: `$${monitorPrice.toFixed(2)}`,  inline: true },
          { name: "Ceiling",   value: `$${ceilingPrice.toFixed(2)}`,  inline: true },
          { name: "Entry",     value: `$${entryPrice.toFixed(2)}`,    inline: true },
          { name: "P&L",       value: `+$${pnl.toFixed(2)} (+${pnlPct.toFixed(1)}%)`, inline: true },
          { name: "Stop",      value: `$${stopPrice.toFixed(2)}`,     inline: true },
        ],
        footer: { text: isRegularHours
          ? "Sell Now to exit · Hold to skip auto-sell · Auto-executes in 2 min"
          : "Extended hours — no auto-execute until 9:30am ET" },
        timestamp: new Date().toISOString(),
      };

      const components = isRegularHours ? [{
        type: 1,
        components: [
          { type: 2, style: 3, label: "💰 Sell All",  custom_id: `ia|tp_sell|${symbol}|tp||${portfolio}` },
          { type: 2, style: 1, label: "📊 Sell 50%",  custom_id: `ia|tp_half_sell|${symbol}|tp||${portfolio}` },
          { type: 2, style: 2, label: "🤚 Hold",      custom_id: `ia|tp_hold|${symbol}|tp||${portfolio}` },
        ],
      }] : [];

      let messageId = null;
      if (webhookUrl) {
        try {
          const wr = await fetch(`${webhookUrl}?wait=true`, {
            method:  "POST",
            headers: { "Content-Type": "application/json" },
            body:    JSON.stringify({ embeds: [embed], components }),
          });
          if (wr.ok) {
            const wMsg = await wr.json();
            messageId = wMsg.id || null;
          }
        } catch (e) { console.warn("TP Discord post error:", e.message); }
      }

      pending[portfolio][symbol] = {
        status:        "pending",
        triggered_at:  new Date().toISOString(),
        message_id:    messageId,
        current_price: currentPrice,
        monitor_price: monitorPrice,
        entry_price:   entryPrice,
        qty:           parseInt(pos.qty || 0),
      };
      pendingChanged[portfolio] = true;
    }
  }

  for (const port of portfolios) {
    if (pendingChanged[port]) {
      try {
        await env.KV.put(`pending_take_profits_${port}`, JSON.stringify(pending[port]), { expirationTtl: 8 * 24 * 3600 });
      } catch (e) { console.warn(`TP pending KV write ${port}:`, e.message); }
    }
  }
}


export default {
  async fetch(request, env, ctx) {
    const url = new URL(request.url);
    if (request.method === "GET") return new Response("Investment Alpha worker OK");
    if (request.method !== "POST") return new Response("Method not allowed", { status: 405 });
    if (url.pathname === "/webhook") return handleTradingViewWebhook(request, env);
    const bodyText = await request.text();
    if (!await verifyDiscordSignature(request, bodyText, env.DISCORD_PUBLIC_KEY)) {
      return new Response("invalid request signature", { status: 401 });
    }
    return handleDiscordInteraction(bodyText, env, ctx);
  },
  async scheduled(event, env, ctx) {
    if (event.cron === "25 13 * * 1-5") {
      ctx.waitUntil(runMorningBrief(env));
    } else {
      ctx.waitUntil(runTakeProfitMonitor(env));
    }
  },
};
