/**
 * Investment Alpha — Discord Interactions Worker (Cloudflare Workers, free tier)
 *
 * Receives every button press and slash command from Discord, then:
 *   1. Verifies the Ed25519 signature (Discord requirement — rejects forgeries)
 *   2. Verifies the user is YOU (OWNER_ID) — anyone else gets an ephemeral
 *      "not authorized" and nothing happens  [BA critical gap #3]
 *   3. Handles instant actions locally (reject button, execute-confirm prompts)
 *   4. Forwards real work to GitHub Actions via repository_dispatch, where
 *      broker/remote_commands.py talks to Alpaca and answers back in Discord
 *
 * Worker secrets (set via `wrangler secret put NAME`):
 *   DISCORD_PUBLIC_KEY — Developer Portal → General Information
 *   OWNER_ID           — your Discord user ID (Settings → Advanced →
 *                        Developer Mode on → right-click yourself → Copy User ID)
 *   GH_TOKEN           — GitHub PAT with repo scope (same one you use for pushes)
 *   GH_REPO            — e.g. "srijanbansaljob-a11y/investment-alpha"
 */

// ── Discord interaction constants ──────────────────────────────────────────
const PING = 1, APPLICATION_COMMAND = 2, MESSAGE_COMPONENT = 3;
const R_PONG = 1, R_CHANNEL_MESSAGE = 4, R_DEFERRED_MESSAGE = 5,
      R_DEFERRED_UPDATE = 6, R_UPDATE_MESSAGE = 7;
const EPHEMERAL = 64;

// ── Ed25519 signature verification (Web Crypto, no dependencies) ──────────
function hexToBytes(hex) {
  const bytes = new Uint8Array(hex.length / 2);
  for (let i = 0; i < bytes.length; i++) bytes[i] = parseInt(hex.substr(i * 2, 2), 16);
  return bytes;
}

async function verifySignature(request, bodyText, publicKey) {
  const sig = request.headers.get("X-Signature-Ed25519");
  const ts = request.headers.get("X-Signature-Timestamp");
  if (!sig || !ts) return false;
  try {
    const key = await crypto.subtle.importKey(
      "raw", hexToBytes(publicKey), { name: "Ed25519" }, false, ["verify"]
    );
    return await crypto.subtle.verify(
      "Ed25519", key, hexToBytes(sig), new TextEncoder().encode(ts + bodyText)
    );
  } catch (e) {
    return false;
  }
}

// ── GitHub dispatch ────────────────────────────────────────────────────────
// Returns null on success, or a diagnostic string on failure.
async function dispatchToGitHub(env, payload) {
  let r;
  try {
    r = await fetch(`https://api.github.com/repos/${env.GH_REPO}/dispatches`, {
      method: "POST",
      headers: {
        Authorization: `Bearer ${(env.GH_TOKEN || "").trim()}`,
        Accept: "application/vnd.github+json",
        "Content-Type": "application/json",
        "User-Agent": "investment-alpha-worker",
      },
      body: JSON.stringify({ event_type: "discord-command", client_payload: payload }),
    });
  } catch (e) {
    return `fetch error: ${e.message}`;
  }
  if (r.status === 204) return null;
  const body = (await r.text()).slice(0, 200);
  return `GitHub HTTP ${r.status} for repo "${env.GH_REPO}": ${body}`;
}

const json = (obj) => new Response(JSON.stringify(obj), {
  headers: { "Content-Type": "application/json" },
});

const ephemeral = (content) =>
  json({ type: R_CHANNEL_MESSAGE, data: { content, flags: EPHEMERAL } });

// ── Main handler ───────────────────────────────────────────────────────────
export default {
  async fetch(request, env) {
    if (request.method !== "POST") return new Response("Investment Alpha worker OK");

    const bodyText = await request.text();
    if (!(await verifySignature(request, bodyText, env.DISCORD_PUBLIC_KEY))) {
      return new Response("invalid request signature", { status: 401 });
    }

    const i = JSON.parse(bodyText);
    if (i.type === PING) return json({ type: R_PONG });

    // ── Owner lock: only YOU can do anything ──────────────────────────────
    const userId = i.member?.user?.id ?? i.user?.id;
    if (userId !== env.OWNER_ID) {
      return ephemeral("⛔ Not authorized. This bot only takes orders from its owner.");
    }

    const common = {
      channel_id: i.channel_id,
      application_id: i.application_id,
      interaction_token: i.token,
    };

    // ── Button presses ─────────────────────────────────────────────────────
    if (i.type === MESSAGE_COMPONENT) {
      const [tag, action, ticker, trigger] = (i.data.custom_id || "").split("|");
      if (tag !== "ia") return ephemeral("Unknown button.");

      // Reject: instant UI update here + background dispatch so the
      // decision journal records it (your rejects are learning data too)
      if (action === "reject") {
        await dispatchToGitHub(env, {
          command: "reject", ticker, trigger, message_id: i.message.id, ...common,
        }); // best-effort — UI updates regardless
        const embeds = i.message.embeds || [];
        if (embeds[0]) embeds[0].footer = { text: trigger === "mr"
          ? "❌ Skipped by you — no buy"
          : "❌ Rejected by you — position kept" };
        return json({ type: R_UPDATE_MESSAGE, data: { embeds, components: [] } });
      }

      // Approve buy (strategy sleeve proposals): GitHub sizes + executes
      if (action === "approve_buy") {
        const err = await dispatchToGitHub(env, {
          command: "approve_buy", ticker, trigger,
          message_id: i.message.id, ...common,
        });
        const embeds = i.message.embeds || [];
        if (embeds[0]) {
          embeds[0].footer = { text: err
            ? `❌ Approval NOT executed — ${err}`
            : `⏳ Approved — sizing & submitting BUY ${ticker}…` };
        }
        return json({ type: R_UPDATE_MESSAGE, data: { embeds, components: err ? i.message.components : [] } });
      }

      // Approve sell: acknowledge instantly, then GitHub executes via Alpaca
      if (action === "approve_sell") {
        const err = await dispatchToGitHub(env, {
          command: "approve_sell", ticker, trigger,
          message_id: i.message.id, ...common,
        });
        const embeds = i.message.embeds || [];
        if (embeds[0]) {
          embeds[0].footer = { text: err
            ? `❌ Approval NOT executed — ${err}`
            : `⏳ Approved — submitting SELL ${ticker}…` };
        }
        // Keep the buttons if dispatch failed so you can retry
        return json({ type: R_UPDATE_MESSAGE, data: { embeds, components: err ? i.message.components : [] } });
      }

      // Confirm prompts for execute-class commands
      if (action === "confirm_pipeline_execute" || action === "confirm_stoploss_execute") {
        const command = action === "confirm_pipeline_execute" ? "pipeline_execute" : "stoploss_execute";
        const err = await dispatchToGitHub(env, { command, ...common });
        return json({
          type: R_UPDATE_MESSAGE,
          data: {
            content: err
              ? `❌ NOT executed — ${err}`
              : `🚀 Confirmed — \`${command}\` is running. Results will post here in ~2–5 min (pipeline runs can take longer).`,
            components: err ? i.message.components : [],
          },
        });
      }

      if (action === "cancel") {
        return json({ type: R_UPDATE_MESSAGE, data: { content: "🚫 Cancelled — nothing was executed.", components: [] } });
      }

      return ephemeral("Unknown action.");
    }

    // ── Slash commands ─────────────────────────────────────────────────────
    if (i.type === APPLICATION_COMMAND) {
      const name = i.data.name;
      const opts = Object.fromEntries((i.data.options || []).map((o) => [o.name, o.value]));

      // /help — answered instantly, no GitHub round-trip
      if (name === "help") {
        return json({
          type: R_CHANNEL_MESSAGE,
          data: {
            flags: EPHEMERAL,
            content: [
              "**Investment Alpha — commands**",
              "`/status` — positions, P&L, stops, regime (~1 min)",
              "`/strategy` — how the model picks stocks, live from config (~1 min)",
              "`/chart symbol:AAPL` or `symbol:portfolio` — price/equity charts (~2 min)",
              "`/regime` — current market regime (~1 min)",
              "`/monitor` — run a position check right now (~2 min)",
              "`/stoploss mode:check` — stop levels, no orders (~2 min)",
              "`/stoploss mode:execute` — exit breached positions (confirm button)",
              "`/pipeline mode:dry` — full pipeline, signals only (~10–30 min)",
              "`/pipeline mode:execute` — rebalance portfolio (confirm button)",
              "",
              "Alerts with ✅/❌ buttons: nothing sells unless you press ✅.",
            ].join("\n"),
          },
        });
      }

      // Execute-class commands get a confirm step (replaces the old `!confirm` idea)
      if ((name === "pipeline" && opts.mode === "execute") ||
          (name === "stoploss" && opts.mode === "execute")) {
        const which = name === "pipeline" ? "confirm_pipeline_execute" : "confirm_stoploss_execute";
        const label = name === "pipeline"
          ? "⚠️ This will REBALANCE your portfolio — real paper orders will be placed."
          : "⚠️ This will SELL any position below its stop — real paper orders will be placed.";
        return json({
          type: R_CHANNEL_MESSAGE,
          data: {
            content: label,
            components: [{
              type: 1,
              components: [
                { type: 2, style: 4, label: "✅ Yes, execute", custom_id: `ia|${which}||` },
                { type: 2, style: 2, label: "Cancel", custom_id: "ia|cancel||" },
              ],
            }],
          },
        });
      }

      // Everything else: defer, dispatch to GitHub, results edit this message
      const map = {
        status: "status",
        regime: "regime",
        strategy: "strategy",
        chart: "chart",
        monitor: "monitor_check",
        stoploss: "stoploss_check",
        pipeline: "pipeline_dry",
      };
      const command = map[name];
      if (!command) return ephemeral("Unknown command.");

      const extra = name === "chart" ? { ticker: String(opts.symbol || "portfolio") } : {};
      const err = await dispatchToGitHub(env, { command, ...extra, ...common });
      if (err) return ephemeral(`❌ Could not reach GitHub — ${err}`);
      return json({ type: R_DEFERRED_MESSAGE });
    }

    return ephemeral("Unsupported interaction.");
  },
};
