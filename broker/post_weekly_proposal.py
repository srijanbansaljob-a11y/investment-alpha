"""
broker/post_weekly_proposal.py — Weekly Rebalance Proposal Publisher

Run by .github/workflows/weekly_rebalance.yml every Monday at 10:00 AM ET.
Runs the pipeline (analysis only), saves the proposal, then posts a Discord
message with Approve / Reject buttons.

Nothing trades here. Execution only happens when the owner taps Approve,
which fires the Cloudflare Worker → repository_dispatch → cmd_approve_rebalance.

Usage (local test):
    python broker/post_weekly_proposal.py
"""

import json
import logging
import os
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
import config
from broker import discord_notify as dn

log = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-8s %(message)s")

PROPOSAL_FILE = config.DATA_DIR / "proposed_rebalance.json"
MAX_DESC = 3800

_RED    = 0xE74C3C
_ORANGE = 0xE67E22
_GREEN  = 0x2ECC71
_BLUE   = 0x3498DB
_GREY   = 0x95A5A6


def run_analysis() -> dict:
    """Run main.py (analysis only) and capture its stdout for a summary."""
    log.info("Running pipeline analysis...")
    proc = subprocess.run(
        [sys.executable, "main.py"],
        capture_output=True, text=True, timeout=2700,
        cwd=str(config.BASE_DIR),
    )
    ok = proc.returncode == 0
    if not ok:
        log.error("Pipeline failed:\n%s", proc.stderr[-2000:])
    else:
        log.info("Pipeline OK (stdout %d chars)", len(proc.stdout))
    return {"ok": ok, "stdout": proc.stdout[-3000:], "stderr": proc.stderr[-1000:]}


def load_latest_signals() -> dict:
    """Read the signals that signals.py just wrote to latest_portfolio.json."""
    state_path = config.DATA_DIR / "latest_portfolio.json"
    if not state_path.exists():
        # Try outputs folder too
        state_path = config.BASE_DIR / "outputs" / "latest_portfolio.json"
    if not state_path.exists():
        return {}
    try:
        raw = state_path.read_bytes().rstrip(b"\x00")
        return json.loads(raw)
    except Exception as e:
        log.error("Could not read state file: %s", e)
        return {}


def save_proposal(state: dict, run_result: dict) -> dict:
    """Persist the exact proposal so approve_rebalance executes what was shown."""
    proposal = {
        "schema_version":  getattr(config, "STATE_SCHEMA_VERSION", 2),
        "proposed_at":     datetime.now(timezone.utc).isoformat(),
        "expires_at":      None,  # filled below
        "regime":          state.get("regime", "unknown"),
        "signal_summary":  state.get("signal_summary", {}),
        "portfolio":       state.get("portfolio", []),
        "run_ok":          run_result["ok"],
    }
    # Expires end of same trading day (21:00 UTC = 4 PM ET + buffer)
    today = datetime.now(timezone.utc)
    expiry = today.replace(hour=21, minute=0, second=0, microsecond=0)
    proposal["expires_at"] = expiry.isoformat()

    PROPOSAL_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = PROPOSAL_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(proposal, indent=2))
    tmp.replace(PROPOSAL_FILE)
    log.info("Proposal saved → %s", PROPOSAL_FILE)
    return proposal


def rebalance_buttons() -> list:
    """Approve / Reject button row for the weekly rebalance."""
    return [{
        "type": 1,
        "components": [
            {
                "type": 2, "style": 3,
                "label": "✅ Approve — Execute rebalance",
                "custom_id": "ia|approve_rebalance||rebalance",
            },
            {
                "type": 2, "style": 4,
                "label": "❌ Reject — Skip this week",
                "custom_id": "ia|reject_rebalance||rebalance",
            },
        ],
    }]


def format_signal_lines(state: dict) -> str:
    """Build a compact signal table for the Discord embed description."""
    portfolio = state.get("portfolio", [])
    summary   = state.get("signal_summary", {})

    if not portfolio:
        return "_No signals generated — pipeline may have failed or no universe._"

    lines = []
    # Separate by action (BUY first, then HOLD, then EXIT)
    buys  = [p for p in portfolio if p.get("action") == "BUY"]
    holds = [p for p in portfolio if p.get("action") == "HOLD"]
    exits = [p for p in portfolio if p.get("action") == "EXIT"]

    # Also pull exit_signals from the state if present (they're not in portfolio)
    exit_sigs = state.get("exit_signals", [])
    if exit_sigs and not exits:
        exits = exit_sigs

    if buys:
        lines.append("**🟢 BUY (new positions)**")
        for p in buys:
            score = p.get("score", 0)
            price = p.get("current_price") or p.get("entry_price") or 0
            wt    = p.get("weight_pct", "")
            lines.append(f"  `{p['ticker']:<5}` {p.get('name','')[:22]:<22} score {score:.2f}  ${price:.2f}  {wt}")

    if holds:
        lines.append("**⚪ HOLD (keep positions)**")
        for p in holds:
            ep  = p.get("entry_price") or 0
            cp  = p.get("current_price") or ep
            pnl = ((cp / ep) - 1) * 100 if ep else 0
            lines.append(f"  `{p['ticker']:<5}` {p.get('name','')[:22]:<22}  entry ${ep:.2f}  now ${cp:.2f}  ({pnl:+.1f}%)")

    if exits:
        lines.append("**🔴 EXIT (close positions)**")
        for p in exits:
            ep  = p.get("entry_price") or 0
            cp  = p.get("current_price") or ep
            pnl = ((cp / ep) - 1) * 100 if ep else 0
            lines.append(f"  `{p['ticker']:<5}` {p.get('name','')[:22]:<22}  entry ${ep:.2f}  now ${cp:.2f}  ({pnl:+.1f}%)")

    text = "\n".join(lines)
    if len(text) > MAX_DESC:
        text = text[:MAX_DESC] + "\n… _(truncated)_"
    return text


def post_proposal(proposal: dict, state: dict):
    """Post the rebalance proposal to Discord with Approve/Reject buttons."""
    regime    = proposal.get("regime", "unknown").upper()
    summary   = proposal.get("signal_summary") or {}
    n_buy     = summary.get("buy", 0)
    n_hold    = summary.get("hold", 0)
    n_exit    = summary.get("exit", 0)
    run_ok    = proposal.get("run_ok", False)
    today_str = datetime.now(timezone.utc).strftime("%A %d %b %Y")

    color = _GREEN if run_ok else _RED

    description = format_signal_lines(state) if run_ok else (
        "⚠️ Pipeline analysis failed — check the GitHub Actions log before approving.\n"
        "This proposal may be stale or incomplete."
    )

    embed = {
        "title": f"📋 Weekly Rebalance Proposal — {today_str}",
        "color": color,
        "description": description,
        "fields": [
            {"name": "Regime",    "value": regime,            "inline": True},
            {"name": "🟢 BUY",   "value": str(n_buy),        "inline": True},
            {"name": "⚪ HOLD",  "value": str(n_hold),        "inline": True},
            {"name": "🔴 EXIT",  "value": str(n_exit),        "inline": True},
            {"name": "Expires",   "value": "Today at 4 PM ET", "inline": True},
            {"name": "Pipeline",  "value": "✅ OK" if run_ok else "❌ Failed", "inline": True},
        ],
        "footer": {"text": "Tap Approve to execute · Reject or ignore to skip · Alpaca paper account"},
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }

    msg = dn.post_message([embed], components=rebalance_buttons())
    if msg:
        log.info("Proposal posted — message id: %s", msg.get("id"))
    else:
        log.error("Failed to post proposal to Discord")

    return msg


def post_reminder():
    """Pre-run reminder — call this 1 hour before the analysis run."""
    webhook_url = os.getenv("DISCORD_WEBHOOK_URL", "").strip()
    if not webhook_url:
        log.warning("DISCORD_WEBHOOK_URL not set — skipping reminder")
        return

    import requests
    embed = {
        "title": "⏰ Weekly Rebalance — Running in 1 Hour",
        "color": _ORANGE,
        "description": (
            "The weekly portfolio rebalance will run at **10:00 AM ET** and post a "
            "BUY/HOLD/EXIT proposal here.\n\n"
            "**Nothing trades until you tap Approve.**\n"
            "Approval window: until 4:00 PM ET today."
        ),
        "footer": {"text": "Investment Alpha — Pipeline Alpaca account"},
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    try:
        r = requests.post(webhook_url, json={"embeds": [embed]}, timeout=10)
        log.info("Reminder posted: %s", r.status_code)
    except Exception as e:
        log.error("Reminder failed: %s", e)


def main():
    import argparse
    parser = argparse.ArgumentParser(description="Post weekly rebalance proposal to Discord")
    parser.add_argument("--reminder-only", action="store_true", help="Post just the pre-run reminder (9AM job)")
    parser.add_argument("--dry-run", action="store_true", help="Run pipeline + build embed but don't post to Discord")
    args = parser.parse_args()

    if args.reminder_only:
        post_reminder()
        return

    run_result = run_analysis()
    state      = load_latest_signals()
    proposal   = save_proposal(state, run_result)

    if args.dry_run:
        print("\n--- DRY RUN — would post this embed ---")
        print(format_signal_lines(state))
        print("\nProposal saved to:", PROPOSAL_FILE)
        return

    post_proposal(proposal, state)
    log.info("Done.")


if __name__ == "__main__":
    main()
