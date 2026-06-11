"""
pipeline/postmortem.py — Learning from exits and from YOU

Two analyses, posted to Discord weekly (learning.yml):

1. STOP-LOSS POST-MORTEM
   Every executed stop exit >= 30 days old is re-examined: where is the
   price now vs the stop? If most stopped-out names recovered well above
   their stop, the stops are too tight → suggests a higher ATR multiplier
   for that regime. If they kept falling, the stops earned their keep.
   Suggestions only (STOP_TUNING_AUTO=False by default) — you stay in
   control; the suggestion appears in Discord and data/stop_tuning.json.

2. DECISION JOURNAL REVIEW
   Every ✅/❌ you pressed >= 14 days ago is scored: when you REJECTED a
   sell, did the position recover (you were right) or keep falling (the
   model was right)? Builds an honest track record of human vs model.
"""

import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
import config

log = logging.getLogger(__name__)

JOURNAL_FILE     = config.DATA_DIR / "decision_journal.json"
STOP_TUNING_FILE = config.DATA_DIR / "stop_tuning.json"

RECOVERY_THRESHOLD = 0.05   # recovered = now >5% above the stop price
MIN_EXITS_FOR_SUGGESTION = 4


def _read_json(path: Path, default):
    if not path.exists():
        return default
    try:
        raw = path.read_bytes().rstrip(b"\x00")
        return json.loads(raw) if raw else default
    except Exception:
        return default


def _prices_now(tickers: list) -> dict:
    try:
        from broker import market_data
        return market_data.get_latest_prices(list(set(tickers)))
    except Exception as exc:
        log.warning("Price fetch failed in postmortem: %s", exc)
        return {}


# ── 1. Stop-loss post-mortem ───────────────────────────────────────────────

def stop_postmortem(min_age_days: int = 30) -> dict:
    """Re-examine executed stop exits. Returns report dict."""
    entries = _read_json(Path(config.STOP_LOSS_LOG_FILE), [])
    today = datetime.now(timezone.utc)

    candidates = []
    for e in entries:
        if not e.get("executed") or e.get("dry_run"):
            continue
        try:
            ts = datetime.fromisoformat(e["timestamp"])
        except Exception:
            continue
        if (today - ts).days >= min_age_days and not e.get("postmortem_done"):
            candidates.append(e)

    report = {"examined": 0, "recovered": 0, "kept_falling": 0,
              "by_regime": {}, "suggestions": [], "details": []}
    if not candidates:
        return report

    prices = _prices_now([e["ticker"] for e in candidates])
    for e in candidates:
        now = prices.get(e["ticker"])
        if not now:
            continue
        stop_p = e.get("stop_price") or e.get("current_price")
        recovered = now > stop_p * (1 + RECOVERY_THRESHOLD)
        regime = e.get("regime", "unknown")
        node = report["by_regime"].setdefault(regime, {"recovered": 0, "total": 0})
        node["total"] += 1
        node["recovered"] += int(recovered)
        report["examined"] += 1
        report["recovered"] += int(recovered)
        report["kept_falling"] += int(not recovered)
        move = (now - stop_p) / stop_p * 100
        report["details"].append(
            f"{'🔄' if recovered else '✅'} {e['ticker']}: stopped @ ${stop_p:.2f}, "
            f"now ${now:.2f} ({move:+.1f}%) — "
            f"{'recovered (stop too tight?)' if recovered else 'stop saved you'}"
        )
        e["postmortem_done"] = True
        e["postmortem_price"] = now
        e["postmortem_recovered"] = recovered

    # Write back the marked entries
    try:
        Path(config.STOP_LOSS_LOG_FILE).write_text(
            json.dumps(entries, indent=2), encoding="utf-8")
    except Exception as exc:
        log.warning("Could not mark stop log entries: %s", exc)

    # Per-regime tuning suggestion
    suggestions = _read_json(STOP_TUNING_FILE, {})
    for regime, node in report["by_regime"].items():
        if node["total"] < MIN_EXITS_FOR_SUGGESTION:
            continue
        rate = node["recovered"] / node["total"]
        current = getattr(config, "ATR_STOP_MULTIPLIER", {}).get(regime, 2.0)
        if rate >= 0.5:
            sug = round(current + 0.25, 2)
            msg = (f"{regime.upper()}: {node['recovered']}/{node['total']} stopped-out names "
                   f"recovered — stops look TOO TIGHT. Suggest ATR multiplier {current} → {sug}")
        elif rate <= 0.2:
            msg = (f"{regime.upper()}: only {node['recovered']}/{node['total']} recovered — "
                   f"stops are earning their keep. Keep multiplier at {current}")
            sug = current
        else:
            continue
        suggestions[regime] = {"suggested_multiplier": sug, "current": current,
                               "recovery_rate": round(rate, 2),
                               "as_of": datetime.now(timezone.utc).date().isoformat()}
        report["suggestions"].append(msg)

    if suggestions:
        STOP_TUNING_FILE.write_text(json.dumps(suggestions, indent=2), encoding="utf-8")

    return report


# ── 2. Decision journal review ─────────────────────────────────────────────

def decision_review(min_age_days: int = 14) -> dict:
    """Score your past approve/reject decisions against what happened next."""
    journal = _read_json(JOURNAL_FILE, [])
    today = datetime.now(timezone.utc)

    pending = []
    for d in journal:
        if d.get("reviewed") or not d.get("price_at_decision"):
            continue
        try:
            ts = datetime.fromisoformat(d["timestamp"])
        except Exception:
            continue
        if (today - ts).days >= min_age_days:
            pending.append(d)

    report = {"reviewed": 0, "reject_right": 0, "reject_wrong": 0,
              "sell_saved": 0, "sell_cost": 0, "lines": []}
    if not pending:
        return report

    prices = _prices_now([d["ticker"] for d in pending])
    for d in pending:
        now = prices.get(d["ticker"])
        if not now:
            continue
        p0 = d["price_at_decision"]
        move = (now - p0) / p0 * 100
        report["reviewed"] += 1
        if d["decision"] == "reject":
            # You kept it. Up since = you were right.
            right = move > 0
            report["reject_right" if right else "reject_wrong"] += 1
            report["lines"].append(
                f"{'🏆 You' if right else '🤖 Model'} — {d['ticker']}: you rejected the sell at "
                f"${p0:.2f}, it's {move:+.1f}% since")
        elif d["decision"] == "approve_sell":
            # You sold. Down since = selling saved you money.
            saved = move < 0
            report["sell_saved" if saved else "sell_cost"] += 1
            report["lines"].append(
                f"{'✅ Good exit' if saved else '💸 Early exit'} — {d['ticker']}: sold at "
                f"${p0:.2f}, it's {move:+.1f}% since")
        d["reviewed"] = True
        d["review_price"] = now
        d["review_move_pct"] = round(move, 2)

    try:
        JOURNAL_FILE.write_text(json.dumps(journal, indent=2), encoding="utf-8")
    except Exception as exc:
        log.warning("Could not mark journal entries: %s", exc)
    return report


# ── Discord report ─────────────────────────────────────────────────────────

def post_report(stop_rep: dict, dec_rep: dict) -> None:
    from broker import discord_notify as dn
    from broker.remote_commands import _embed, _BLUE, _ORANGE

    embeds = []
    if stop_rep["examined"]:
        desc = "\n".join(stop_rep["details"][:15])
        if stop_rep["suggestions"]:
            desc += "\n\n**Tuning suggestions:**\n" + "\n".join(stop_rep["suggestions"])
            desc += "\n_Suggestion only — update ATR_STOP_MULTIPLIER in config.py if you agree._"
        embeds.append(_embed(
            f"🔬 Stop-Loss Post-Mortem — {stop_rep['recovered']}/{stop_rep['examined']} recovered",
            desc, _ORANGE if stop_rep["suggestions"] else _BLUE))
    if dec_rep["reviewed"]:
        you = dec_rep["reject_right"] + dec_rep["sell_saved"]
        desc = (f"**Your decisions, scored:** {you}/{dec_rep['reviewed']} aged well\n"
                f"Rejects: {dec_rep['reject_right']} right / {dec_rep['reject_wrong']} wrong · "
                f"Sells: {dec_rep['sell_saved']} saved money / {dec_rep['sell_cost']} sold too early\n\n"
                + "\n".join(dec_rep["lines"][:15]))
        embeds.append(_embed("🧠 Human vs Model — decision review", desc, _BLUE))
    if embeds:
        dn.post_message(embeds)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    s = stop_postmortem()
    d = decision_review()
    print(json.dumps({"stop_postmortem": {k: v for k, v in s.items() if k != "details"},
                      "decision_review": {k: v for k, v in d.items() if k != "lines"}}, indent=2))
    post_report(s, d)
