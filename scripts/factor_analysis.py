"""
scripts/factor_analysis.py — Weekly win-rate breakdown by signal type

Reads data/trade_outcomes.json and posts a Discord card showing which
signals, regimes and buckets correlate with profitable trades.

Run cadence:
  - Every Saturday at 9 AM ET via weekly_report.yml workflow
  - On demand: python scripts/factor_analysis.py

Output:
  - Discord embed with signal value table, regime breakdown, bucket breakdown
  - data/factor_analysis_latest.json (machine-readable snapshot)
"""

import json
import logging
import os
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).parent.parent))

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
log = logging.getLogger(__name__)

_DATA_DIR      = Path(__file__).parent.parent / "data"
_OUTCOMES_FILE = _DATA_DIR / "trade_outcomes.json"

C_GREEN  = 0x2ECC71
C_ORANGE = 0xE67E22
C_RED    = 0xE74C3C
C_BLUE   = 0x3498DB


# ── Analysis ───────────────────────────────────────────────────────────────

def _group_stats(outcomes: list, key_fn) -> list[dict]:
    """Group outcomes by key_fn; compute win rate + avg P&L per group."""
    groups: dict[str, list] = defaultdict(list)
    for o in outcomes:
        groups[key_fn(o)].append(o)

    results = []
    for label, group in sorted(groups.items(), key=lambda x: -len(x[1])):
        wins   = [o for o in group if o.get("win")]
        avg_pl = sum(o.get("pnl_pct", 0) for o in group) / len(group)
        results.append({
            "label":    label,
            "trades":   len(group),
            "wins":     len(wins),
            "win_rate": round(len(wins) / len(group) * 100, 1),
            "avg_pl":   round(avg_pl, 2),
        })
    return results


# Minimum trades in EACH group before a comparison means anything.
# Below this the "edge" is an artefact of sample size, not a finding.
MIN_GROUP_N = 3


def _signal_comparison(outcomes: list, signal_key: str) -> dict:
    """
    Win rate with vs without a signal.

    BUG FIX 2026-08-01: the empty-group win rate used to fall back to 0, so a
    signal present on ZERO trades reported "0% with vs 92% without = -92pp
    edge". That reads as devastating evidence against the signal when it is
    simply an absence of data — and the card advised tuning factor weights on
    it. An edge is now reported only when BOTH groups have at least
    MIN_GROUP_N trades; otherwise it is None and the caller must say
    "insufficient data".
    """
    with_sig    = [o for o in outcomes if (o.get("signals") or {}).get(signal_key)]
    without_sig = [o for o in outcomes if not (o.get("signals") or {}).get(signal_key)]

    def _wr(lst):
        return round(sum(1 for o in lst if o.get("win")) / len(lst) * 100, 1) if lst else None

    def _avg(lst):
        return round(sum(o.get("pnl_pct", 0) for o in lst) / len(lst), 2) if lst else None

    with_wr, without_wr = _wr(with_sig), _wr(without_sig)
    sufficient = len(with_sig) >= MIN_GROUP_N and len(without_sig) >= MIN_GROUP_N

    return {
        "with_n":      len(with_sig),
        "without_n":   len(without_sig),
        "with_wr":     with_wr,
        "without_wr":  without_wr,
        "with_avg_pl": _avg(with_sig),
        "sufficient":  sufficient,
        "edge":        round(with_wr - without_wr, 1) if sufficient else None,
        "note":        None if sufficient else
                       f"insufficient data (need {MIN_GROUP_N}+ trades in each group; "
                       f"have {len(with_sig)} with / {len(without_sig)} without)",
    }


def _model_outcomes(outcomes: list) -> list:
    """
    Only trades the MODEL decided. Drops administrative exits (owner
    liquidations, migrations) which carry real P&L but zero information about
    whether the strategy works — see trade_outcome_logger._admin_exits.
    """
    kept = [o for o in outcomes if not o.get("exclude_from_learning")]
    dropped = len(outcomes) - len(kept)
    if dropped:
        log.info("Factor analysis: excluded %d administrative exit(s) from %d outcomes",
                 dropped, len(outcomes))
    return kept


def analyze(outcomes: list) -> dict:
    outcomes = _model_outcomes(outcomes)
    if not outcomes:
        return {}

    total  = len(outcomes)
    wins   = sum(1 for o in outcomes if o.get("win"))
    avg_pl = round(sum(o.get("pnl_pct", 0) for o in outcomes) / total, 2)
    avg_dur = round(sum(o.get("duration_days", 0) for o in outcomes) / total, 1)

    signal_keys = ["insider_buy", "congress_buy", "earnings_beat"]
    signals = {k: _signal_comparison(outcomes, k) for k in signal_keys}

    # RS vs SPY: split positive (outperforming) vs negative
    rs_positive = [o for o in outcomes if (o.get("signals") or {}).get("rs_vs_spy", 0) > 0]
    rs_negative = [o for o in outcomes if (o.get("signals") or {}).get("rs_vs_spy", 0) < 0]
    rs_wr_pos = round(sum(1 for o in rs_positive if o.get("win")) / len(rs_positive) * 100, 1) if rs_positive else 0
    rs_wr_neg = round(sum(1 for o in rs_negative if o.get("win")) / len(rs_negative) * 100, 1) if rs_negative else 0

    by_regime    = _group_stats(outcomes, lambda o: (o.get("signals") or {}).get("regime", "UNKNOWN"))
    by_bucket    = _group_stats(outcomes, lambda o: (o.get("signals") or {}).get("bucket", "unknown"))
    by_portfolio = _group_stats(outcomes, lambda o: o.get("portfolio", "unknown"))
    by_exit_type = _group_stats(outcomes, lambda o: o.get("exit_type", "unknown"))

    return {
        "total":        total,
        "wins":         wins,
        "win_rate":     round(wins / total * 100, 1),
        "avg_pl":       avg_pl,
        "avg_duration": avg_dur,
        "signals":      signals,
        "rs_vs_spy":    {"positive_n": len(rs_positive), "positive_wr": rs_wr_pos,
                         "negative_n": len(rs_negative), "negative_wr": rs_wr_neg},
        "by_regime":    by_regime,
        "by_bucket":    by_bucket,
        "by_portfolio": by_portfolio,
        "by_exit_type": by_exit_type,
    }


# ── Formatting ─────────────────────────────────────────────────────────────

def _group_table(groups: list[dict], max_rows: int = 6) -> str:
    if not groups:
        return "_No data yet_"
    lines = []
    for g in groups[:max_rows]:
        bar = "█" * int(g["win_rate"] / 10) + "░" * (10 - int(g["win_rate"] / 10))
        lines.append(
            f"{g['label']:<16} {bar} {g['win_rate']:5.0f}%  "
            f"({g['wins']}/{g['trades']})  avg {g['avg_pl']:+.1f}%"
        )
    return "```\n" + "\n".join(lines) + "\n```"


def _fmt_wr(wr, n):
    """'92% (13)' — or 'n/a  (0)' when the group is empty. Never 0%."""
    return f"{'  n/a' if wr is None else format(wr, '5.0f') + '%'} ({n:2d})"


def _signal_table(signals: dict, rs: dict) -> str:
    lines = ["Signal             WITH         WITHOUT     Edge"]
    for key, d in signals.items():
        if d["with_n"] == 0 and d["without_n"] == 0:
            continue
        label = key.replace("_", " ").title()
        # An edge is only shown when both groups are big enough. Printing
        # "-92pp" off an empty group is not a finding, it is arithmetic on
        # nothing — and it previously sat under a footer advising weight tuning.
        edge = (f"{d['edge']:+.0f}pp" if d.get("sufficient") and d.get("edge") is not None
                else "  n/a")
        lines.append(
            f"{label:<18} {_fmt_wr(d['with_wr'], d['with_n'])}  "
            f"{_fmt_wr(d['without_wr'], d['without_n'])}  {edge}"
        )
    if any(not s.get("sufficient") for s in signals.values()):
        lines.append(f"  (n/a = fewer than {MIN_GROUP_N} trades in a group — no conclusion)")
    if rs["positive_n"] > 0 or rs["negative_n"] > 0:
        lines.append(
            f"{'RS > SPY':<18} {rs['positive_wr']:5.0f}% ({rs['positive_n']:2d})  "
            f"{rs['negative_wr']:5.0f}% ({rs['negative_n']:2d})  "
            f"{rs['positive_wr'] - rs['negative_wr']:+.0f}pp"
        )
    return "```\n" + "\n".join(lines) + "\n```" if len(lines) > 1 else "_Not enough data (need trades with signals)_"


# ── Discord post ───────────────────────────────────────────────────────────

def post_to_discord(analysis: dict):
    bot_token  = os.getenv("DISCORD_BOT_TOKEN", "").strip()
    channel_id = os.getenv("DISCORD_CHANNEL_ID", "").strip()

    if not analysis:
        log.info("No outcomes yet — nothing to post")
        return

    wr    = analysis["win_rate"]
    color = C_GREEN if wr >= 55 else C_ORANGE if wr >= 40 else C_RED
    today = datetime.now(timezone.utc).strftime("%b %d, %Y")

    embed = {
        "title": f"🎯 Weekly Factor Analysis — {today}",
        "description": (
            f"**{analysis['wins']}/{analysis['total']} trades won** · "
            f"**{wr:.0f}% win rate** · avg P&L **{analysis['avg_pl']:+.1f}%** · "
            f"avg hold **{analysis['avg_duration']:.0f} days**\n"
            f"Which signals actually predict winning trades?"
        ),
        "color": color,
        "fields": [
            {
                "name":   "📡 Signal Value (win rate WITH vs WITHOUT)",
                "value":  _signal_table(analysis["signals"], analysis["rs_vs_spy"]),
                "inline": False,
            },
            {
                "name":   "🧭 Win Rate by Regime",
                "value":  _group_table(analysis["by_regime"]),
                "inline": False,
            },
            {
                "name":   "🗂️ Win Rate by Bucket",
                "value":  _group_table(analysis["by_bucket"]),
                "inline": False,
            },
            {
                "name":   "💼 By Portfolio",
                "value":  _group_table(analysis["by_portfolio"], max_rows=3),
                "inline": True,
            },
            {
                "name":   "🚪 By Exit Type",
                "value":  _group_table(analysis["by_exit_type"], max_rows=3),
                "inline": True,
            },
        ],
        "footer": {
            # Advice must scale with evidence. Recommending weight changes off
            # a handful of trades is how noise becomes strategy.
            "text": (
                f"Investment Alpha · {analysis['total']} model trades · "
                + ("Tune factor weights in config.py based on signal edge"
                   if analysis["total"] >= 30 else
                   f"Too few trades to conclude anything — {analysis['total']}/30 "
                   "before weights should change")
            )
        },
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }

    if not bot_token or not channel_id:
        log.warning("No Discord credentials — printing report")
        print(json.dumps(analysis, indent=2))
        return

    r = requests.post(
        f"https://discord.com/api/v10/channels/{channel_id}/messages",
        headers={"Authorization": f"Bot {bot_token}", "Content-Type": "application/json"},
        json={"embeds": [embed]},
        timeout=10,
    )
    if r.ok:
        log.info("Factor analysis posted to Discord ✓")
    else:
        log.warning("Discord post failed %d: %s", r.status_code, r.text[:200])


# ── Main ───────────────────────────────────────────────────────────────────

def main():
    log.info("=== Factor Analysis ===")

    outcomes = []
    try:
        if _OUTCOMES_FILE.exists():
            outcomes = json.loads(_OUTCOMES_FILE.read_text(encoding="utf-8")).get("outcomes", [])
    except Exception as e:
        log.error("Could not load outcomes: %s", e)
        return

    log.info("Loaded %d trade outcomes", len(outcomes))

    if not outcomes:
        log.info("No outcomes yet — skipping (data accumulates after first exits are logged)")
        return

    result = analyze(outcomes)

    # Save machine-readable snapshot
    out_path = _DATA_DIR / "factor_analysis_latest.json"
    _DATA_DIR.mkdir(parents=True, exist_ok=True)
    out_path.write_text(
        json.dumps({**result, "generated_at": datetime.now(timezone.utc).isoformat()},
                   indent=2, default=str),
        encoding="utf-8",
    )
    log.info("Analysis saved → %s", out_path)

    post_to_discord(result)

    log.info(
        "Win rate: %.0f%% (%d/%d)  avg P&L: %+.1f%%  avg hold: %.0f days",
        result["win_rate"], result["wins"], result["total"],
        result["avg_pl"], result["avg_duration"],
    )


if __name__ == "__main__":
    main()
