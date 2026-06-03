# 🚀 New PC Quick Start — Investment Alpha Agent

If you're on a new machine and want to resume where you left off, do these steps in order.

---

## Step 1 — Install & Login
- Download Claude Desktop from claude.ai/download
- Sign in with **srijanbansal@gmail.com**

## Step 2 — Connect Your Folder
- Open Cowork mode
- Select folder: `OneDrive > Desktop > Investment dashboard > Investment Alpha > Investment Aplha`

## Step 3 — Resume the Agent
Start a new conversation and paste this exact message:

> "Read memory/AGENT_MEMORY.md and memory/SESSION_LOG.md and tell me a summary of where we left off, then wait for my instructions."

The agent will load all context and be ready to continue.

## Step 4 — Python Environment
Open a terminal in the project folder and run:
```bash
pip install -r requirements.txt
```

## Step 5 — Verify API Keys
Check that `.env` keys are working:
```bash
python main.py --test
```

## Step 6 — Scheduled Tasks (if needed)
Re-register Windows Task Scheduler jobs:
```powershell
.\setup_task_scheduler.ps1
```

## Step 7 — Plugins
Re-enable any Cowork plugins from the plugin marketplace inside the app.

---

## 💡 Reminder: End of Session Save

Always end sessions by telling the agent:
> *"Save this session to memory"*

This keeps `AGENT_MEMORY.md` and `SESSION_LOG.md` current so the next session
on any device picks up exactly where you left off.

---
*Last updated: 2026-04-30 | Session 002*
