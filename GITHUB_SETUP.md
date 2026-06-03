# GitHub Actions Setup — Investment Alpha Monitor

This guide sets up the intraday monitor to run in the cloud via GitHub Actions.
Your PC does not need to be on. Takes about 15 minutes.

---

## Prerequisites

- A free GitHub account (github.com)
- Git installed on your PC (download from git-scm.com if needed — check with `git --version`)

---

## Step 1 — Create a private GitHub repository

1. Go to https://github.com/new
2. Repository name: `investment-alpha` (or anything you like)
3. Set visibility to **Private** ← important
4. Do NOT tick "Add a README file" or anything else
5. Click **Create repository**
6. Copy the repository URL shown (looks like `https://github.com/YOUR_USERNAME/investment-alpha.git`)

---

## Step 2 — Push your code to GitHub

Open a Command Prompt or PowerShell and run these commands.
Replace the path and URL with your actual values.

```powershell
# Navigate to your project folder
cd "D:\Office Transfer\OneDrive_2026-05-01\Investment dashboard\Investment Alpha\Investment Aplha"

# Initialise git (only needed once — safe to run again if already done)
git init

# Set your identity (only needed once per machine)
git config user.email "srijanbansal@gmail.com"
git config user.name "Srijan"

# Stage all files (.gitignore will automatically exclude .env and outputs/)
git add .

# First commit
git commit -m "Initial commit — Investment Alpha pipeline"

# Point to your GitHub repo (replace URL with yours from Step 1)
git remote add origin https://github.com/YOUR_USERNAME/investment-alpha.git

# Push
git branch -M main
git push -u origin main
```

You'll be prompted for your GitHub username and password.
**Note:** GitHub no longer accepts passwords — use a Personal Access Token instead.
To create one: GitHub → Settings → Developer settings → Personal access tokens → Tokens (classic) → Generate new token
  - Select scopes: `repo` (full control of private repositories)
  - Copy the token and use it as your "password" when prompted

---

## Step 3 — Add your secrets to GitHub

Your API keys are never stored in code. They live as encrypted GitHub Secrets.

1. Go to your repository on GitHub
2. Click **Settings** (top menu)
3. In the left sidebar: **Secrets and variables → Actions**
4. Click **New repository secret** for each of the following:

| Secret Name | Value |
|---|---|
| `ALPACA_API_KEY` | Your Alpaca paper trading API key |
| `ALPACA_SECRET_KEY` | Your Alpaca paper trading secret key |
| `ALPACA_BASE_URL` | `https://paper-api.alpaca.markets` |
| `DISCORD_WEBHOOK_URL` | Your Discord webhook URL |
| `FINNHUB_API_KEY` | Your Finnhub API key (from .env) |

These values are in your local `.env` file — copy them from there.
They are encrypted and never visible again after saving.

---

## Step 4 — Verify the workflow is active

1. Go to your repository on GitHub
2. Click the **Actions** tab
3. You should see "Intraday Portfolio Monitor" listed
4. Click **Run workflow** → **Run workflow** to trigger a manual test run
5. Watch it run — it should complete in ~2 minutes with a green tick
6. Check your Discord channel — you should see a check-in or a "market closed" log

---

## Step 5 — How to update your code going forward

Whenever you make changes locally (editing main.py, config.py, etc.):

```powershell
cd "D:\Office Transfer\OneDrive_2026-05-01\Investment dashboard\Investment Alpha\Investment Aplha"

git add .
git commit -m "Brief description of what you changed"
git push
```

That's it. GitHub Actions will automatically use the new code on the next scheduled run.

---

## How to check if it's working

- **GitHub Actions tab** → see every run, its logs, and whether it succeeded or failed
- **Discord channel** → alerts appear here when levels are breached
- **No news is good news** — if no Discord alerts, all positions are within bounds

---

## Troubleshooting

**Run fails with "credential error"**
→ Check that all 5 Secrets are added correctly (Step 3). Common mistake: extra spaces.

**Run fails with "ModuleNotFoundError"**
→ A new dependency was added. Run `pip freeze > requirements.txt` locally, then push.

**Market is open but no runs happening**
→ GitHub cron schedules can be delayed by up to 15 minutes during heavy load. This is normal.
→ Also check: is the workflow enabled? (Actions tab → click the workflow → Enable)

**I want to stop the monitor temporarily**
→ GitHub → Actions → Intraday Portfolio Monitor → top-right "..." menu → Disable workflow
→ Re-enable the same way when ready.

---

## Minute usage (free tier check)

GitHub gives private repos 2,000 free Action minutes/month.
This workflow runs ~29 times/day × 20 trading days = ~580 runs/month.
Each run takes ~2 minutes = ~1,160 minutes/month.
This is **within the free tier** (2,000 min/month). You have ~840 min buffer.
