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
async function postDiscordWebhook(webhookUrl, embeds) {
  if (!webhookUrl) return;
  try {
    await fetch(webhookUrl, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ embeds }),
    });
  } catch (e) {
    console.error("Discord webhook failed:", e.message);
  }
}

// ── Alpaca trade helpers ────────────────────────────────────────────────────
// Two Alpaca accounts:
//   ALPACA_KEY / ALPACA_SECRET         → pipeline account (TradingView webhook, portfolio)
//   ALPACA_KEY_SCREENER / ALPACA_SECRET_SCREENER → screener account (/buy, /sell Discord commands)

function screenerHeaders(env) {
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

async function placeBracketOrder(env, symbol) {
  // Always uses SCREENER account — keeps screener trades separate from pipeline
  const alpacaBase   = "https://paper-api.alpaca.markets";
  const alpacaKey    = (env.ALPACA_KEY_SCREENER    || env.ALPACA_KEY    || "").trim();
  const alpacaSecret = (env.ALPACA_SECRET_SCREENER || env.ALPACA_SECRET || "").trim();
  if (!alpacaKey || !alpacaSecret) return { error: "Alpaca screener credentials not set. Run: wrangler secret put ALPACA_KEY_SCREENER" };

  const headers = screenerHeaders(env);

  // Get live price
  const price = await getAlpacaPrice(env, symbol);
  if (!price) return { error: `Could not fetch price for ${symbol} — market may be closed.` };

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
    qty = Math.max(1, Math.floor((bp * sizePct) / price));
  } catch (e) { console.warn("Sizing error:", e.message); }

  const stopPrice = parseFloat((price * (1 - STOP_LOSS_PCT)).toFixed(2));
  const takePrice = parseFloat((price * (1 + TAKE_PROFIT_PCT)).toFixed(2));

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
    if (!r.ok) return { error: result?.message || `HTTP ${r.status}`, price, qty, stopPrice, takePrice, sizePct };
    return { order: result, price, qty, stopPrice, takePrice, sizePct };
  } catch (e) {
    return { error: e.message, price, qty, stopPrice, takePrice, sizePct };
  }
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

  // ── Button presses ───────────────────────────────────────────────────────
  if (i.type === MESSAGE_COMPONENT) {
    const [tag, action, ticker, trigger] = (i.data.custom_id || "").split("|");
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
      const result = await placeBracketOrder(env, ticker);
      const ok = !!result.order;
      const embeds = i.message.embeds || [];
      if (embeds[0]) {
        embeds[0].color = ok ? C_GREEN : C_RED;
        embeds[0].title = ok
          ? `✅ BUY ${ticker} — Order Placed`
          : `❌ BUY ${ticker} — Order Failed`;
        embeds[0].footer = { text: ok
          ? `Order ID: ${result.order?.id || "—"} · Bracket: stop $${result.stopPrice} / target $${result.takePrice} · Paper trading`
          : `Error: ${result.error}` };
      }
      return json({ type: R_UPDATE_MESSAGE, data: { embeds, components: [] } });
    }

    if (action === "confirm_sell_execute") {
      const alpacaBase = "https://paper-api.alpaca.markets";
      const headers = screenerHeaders(env);  // screener account
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
              "/buy symbol:C    -> preview order:",
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
            value: "`/screener` Top 5 picks + conviction badges\n`/buy symbol:X` Preview & confirm bracket order\n`/sell symbol:X` See P&L then confirm close",
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
            name: "ℹ️ Info",
            value: "`/strategy` Factor weights, universe, sleeve details\n`/help` This guide (4 messages)",
            inline: false,
          },
          {
            name: "\U0001f4a1 Morning routine",
            value: "`/regime` check market · `/screener` see picks · `/buy symbol:X` trade\nMonthly (1st): `/pipeline mode:dry` review · `/pipeline mode:execute` rebalance",
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

    // /buy — preview order then confirm
    if (name === "buy") {
      const symbol = (opts.symbol || "").toUpperCase();
      if (!symbol) return ephemeral("Provide a ticker: `/buy symbol:AAPL`");

      // Regime + bucket gates
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

      // Live price + sizing preview
      const price = await getAlpacaPrice(env, symbol);
      if (!price) return ephemeral(`❌ Could not fetch price for **${symbol}**. Market may be closed or ticker invalid.`);

      const regimeKey = regimeScore >= 60 ? "bull" : regimeScore >= 30 ? "neutral" : "bear";
      const sizePct   = POSITION_SIZE_BY_REGIME[regimeKey];
      let qty = 1;
      try {
        const alpacaBase = (env.ALPACA_BASE_URL || "https://paper-api.alpaca.markets").trim();
        const ah = screenerHeaders(env);
        const acct = await (await fetch(`${alpacaBase}/v2/account`, { headers: ah })).json();
        qty = Math.max(1, Math.floor((parseFloat(acct.buying_power || acct.cash || 0) * sizePct) / price));
      } catch {}

      const stopPrice = parseFloat((price * (1 - STOP_LOSS_PCT)).toFixed(2));
      const takePrice = parseFloat((price * (1 + TAKE_PROFIT_PCT)).toFixed(2));

      return json({ type: R_CHANNEL_MESSAGE, data: { flags: EPHEMERAL, embeds: [{
        title: `🛒 Confirm BUY ${symbol}?`,
        color: C_BLUE,
        fields: [
          { name: "Current price",  value: `$${price.toFixed(2)}`,                                      inline: true },
          { name: "Shares",         value: String(qty),                                                  inline: true },
          { name: "Total cost",     value: `~$${(price * qty).toFixed(0)}`,                             inline: true },
          { name: "Stop loss",      value: `$${stopPrice} (−${(STOP_LOSS_PCT*100).toFixed(0)}%)`,       inline: true },
          { name: "Take profit",    value: `$${takePrice} (+${(TAKE_PROFIT_PCT*100).toFixed(0)}%)`,     inline: true },
          { name: "Position size",  value: `${(sizePct*100).toFixed(0)}% of buying power`,              inline: true },
          { name: "Regime",         value: `${regimeLabel} (${regimeScore}/100)`,                       inline: true },
          { name: "Bucket",         value: bucket,                                                       inline: true },
        ],
        footer: { text: "Bracket order: stop-loss + take-profit auto-managed by Alpaca · Paper trading" },
      }], components: [{ type: 1, components: [
        { type: 2, style: 3, label: `✅ Buy ${qty} shares of ${symbol}`, custom_id: `ia|confirm_buy_execute|${symbol}|buy` },
        { type: 2, style: 2, label: "❌ Cancel", custom_id: `ia|cancel||buy` },
      ]}]}});
    }

    // /sell — preview position then confirm close
    if (name === "sell") {
      const symbol = (opts.symbol || "").toUpperCase();
      if (!symbol) return ephemeral("Provide a ticker: `/sell symbol:AAPL`");

      const alpacaBase = "https://paper-api.alpaca.markets";
      const ah = screenerHeaders(env);

      let position = null;
      try { const r = await fetch(`${alpacaBase}/v2/positions/${symbol}`, { headers: ah }); if (r.ok) position = await r.json(); } catch {}
      if (!position) return ephemeral(`❌ No open position found for **${symbol}**.`);

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
        { type: 2, style: 4, label: `🔴 Sell all ${qty} shares`, custom_id: `ia|confirm_sell_execute|${symbol}|sell` },
        { type: 2, style: 2, label: "❌ Cancel", custom_id: `ia|cancel||sell` },
      ]}]}});
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


export default {
  async fetch(request, env, ctx) {
    const url = new URL(request.url);

    if (request.method === "GET") {
      return new Response("Investment Alpha worker OK");
    }
    if (request.method !== "POST") {
      return new Response("Method not allowed", { status: 405 });
    }
    if (url.pathname === "/webhook") {
      return handleTradingViewWebhook(request, env);
    }
    const bodyText = await request.text();
    if (!(await verifyDiscordSignature(request, bodyText, env.DISCORD_PUBLIC_KEY))) {
      return new Response("invalid request signature", { status: 401 });
    }
    return handleDiscordInteraction(bodyText, env, ctx);
  },
};
