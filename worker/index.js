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
      return json({
        type: R_CHANNEL_MESSAGE,
        data: {
          flags: EPHEMERAL,
          content: [
            "**Investment Alpha — commands**",
            "`/status` — positions, P&L, stops, regime (~1 min)",
            "`/screener` — today's top picks from the morning screener",
            "`/strategy` — how the model picks stocks, live from config (~1 min)",
            "`/chart symbol:AAPL` or `symbol:portfolio` — price/equity charts (~2 min)",
            "`/regime` — current market regime (~1 min)",
            "`/monitor` — run a position check right now (~2 min)",
            "`/stoploss mode:check` — stop levels, no orders (~2 min)",
            "`/stoploss mode:execute` — exit breached positions (confirm button)",
            "`/pipeline mode:dry` — full pipeline, signals only (~10–30 min)",
            "`/pipeline mode:execute` — rebalance portfolio (confirm button)",
            "`/help` — this message",
          ].join("\n"),
        },
      });
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
