# Discord Approval Bot — Setup Guide (~30 minutes, one time)

What you're building: tap-to-approve trading from Discord. Flagged trades arrive
as cards with ✅/❌ buttons; slash commands replace PC scripts. Free forever
(Discord + Cloudflare Workers + GitHub Actions free tiers).

```
Alert/command in Discord → Cloudflare Worker (verifies it's YOU)
                                 ↓
                     GitHub Actions runs the Python
                                 ↓
                  Alpaca paper order + result card in Discord
```

---

## Step 1 — Create the Discord application (5 min)

1. Go to **https://discord.com/developers/applications** → **New Application** → name it `Investment Alpha`
2. **General Information** page — copy two values into Notepad:
   - **Application ID**
   - **Public Key**
3. **Bot** tab → **Reset Token** → copy the **Bot Token** (you see it once)
4. Bot tab: leave "Public Bot" OFF (so only you can add it)

## Step 2 — Invite the bot to your server (2 min)

1. **OAuth2 → URL Generator**: tick `bot` and `applications.commands`
2. Bot Permissions: tick **Send Messages**, **Embed Links**, **Read Message History**
3. Copy the generated URL, open it, pick your server, **Authorize**

## Step 3 — Get your IDs (2 min)

1. Discord → User Settings → **Advanced** → enable **Developer Mode**
2. Right-click your alerts channel → **Copy Channel ID**
3. Right-click your own name in any message → **Copy User ID**

## Step 4 — Deploy the Cloudflare Worker (10 min)

1. Create a free account at **https://dash.cloudflare.com/sign-up** (no card needed)
2. In your terminal:
   ```powershell
   cd "D:\Office Transfer\OneDrive_2026-05-01\Investment dashboard\Investment Alpha\Investment Aplha\worker"
   npx wrangler login          # opens browser, click Allow
   npx wrangler deploy         # prints your worker URL — copy it
   ```
3. Set the 4 worker secrets (each command prompts you to paste the value):
   ```powershell
   npx wrangler secret put DISCORD_PUBLIC_KEY   # from Step 1
   npx wrangler secret put OWNER_ID             # your User ID from Step 3
   npx wrangler secret put GH_TOKEN             # your GitHub PAT (repo scope)
   npx wrangler secret put GH_REPO              # srijanbansaljob-a11y/investment-alpha
   ```
4. Back in the Discord Developer Portal → **General Information** →
   **Interactions Endpoint URL** → paste your worker URL → **Save**
   (Discord pings the worker; if it saves, the signature check works.)

## Step 5 — Add 2 new GitHub Secrets (3 min)

At https://github.com/srijanbansaljob-a11y/investment-alpha/settings/secrets/actions:

| Name | Value |
|---|---|
| `DISCORD_BOT_TOKEN` | Bot token from Step 1 |
| `DISCORD_CHANNEL_ID` | Channel ID from Step 3 |

(Your existing 5 secrets stay as they are.)

## Step 6 — Register the slash commands (2 min)

Add to your local `.env`:
```
DISCORD_APP_ID=<Application ID from Step 1>
DISCORD_BOT_TOKEN=<Bot token from Step 1>
DISCORD_CHANNEL_ID=<Channel ID from Step 3>
```
Then:
```powershell
python scripts/register_discord_commands.py
```
You should see "✅ Registered 6 slash commands". They appear in your server within ~1 hour (usually minutes).

## Step 7 — Push the new code (2 min)

```powershell
cd "D:\Office Transfer\OneDrive_2026-05-01\Investment dashboard\Investment Alpha\Investment Aplha"
git add .
git commit -m "Discord button approval system: alert-only monitor, worker bridge, slash commands"
git push
```

## Step 8 — Test (5 min)

1. In Discord type `/help` → instant command list = worker is alive
2. `/status` → "thinking…" then your portfolio card (~1–2 min) = GitHub bridge works
3. (Optional) Ask a friend to try `/status` → they should get "⛔ Not authorized"
4. Pin `DISCORD_GUIDE.md` content in your #commands channel

---

## Troubleshooting

| Symptom | Fix |
|---|---|
| Interactions URL won't save | `DISCORD_PUBLIC_KEY` secret wrong — re-put it, redeploy |
| `/status` stuck on "thinking…" forever | Worker can't reach GitHub: check `GH_TOKEN` (needs repo scope) and `GH_REPO` value |
| Commands don't appear after 1 hr | Re-run register script; check the App ID matches the invited bot |
| Alerts have no buttons | `DISCORD_BOT_TOKEN`/`DISCORD_CHANNEL_ID` missing from GitHub Secrets |
| "⛔ Not authorized" for yourself | `OWNER_ID` secret doesn't match your User ID — re-copy with Developer Mode on |
