"""
pipeline/learning.py — Weekly Learning Update (v2 of the feedback loop)

Upgrades over the original monthly feedback.py:
  1. SHADOW OBSERVATIONS — learns from the top-30 ranked stocks every run
     (via pipeline/shadow.py), not just the 10 bought. ~3× more data,
     no selection bias.
  2. REGIME-CONDITIONAL WEIGHTS — separate learned weights for BULL /
     NEUTRAL / BEAR. Momentum that works in a bull tape and fails in a bear
     tape no longer averages out to "meh".
  3. EWMA INFORMATION COEFFICIENT — factor predictiveness is tracked as an
     exponentially weighted average (recent months matter more), instead of
     jumping ±5% on each month's noisy correlation.
  4. WEEKLY CADENCE with a smaller step size (run by learning.yml).

Compatibility: scoring.py keeps reading the flat data/learned_weights.json.
This module maintains the rich store in data/learned_weights_v2.json and
EXPORTS the current regime's weights to the flat file — zero changes needed
in scoring.py.

Usage:
    python pipeline/learning.py            # evaluate shadow, update, export
    python pipeline/learning.py --dry-run  # show changes, save nothing
"""

import argparse
import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).parent.parent))
import config
from pipeline import shadow
from pipeline.feedback import WEIGHT_BOUNDS, INVERTED_FACTORS, load_weights

log = logging.getLogger(__name__)

V2_FILE      = config.DATA_DIR / "learned_weights_v2.json"
FLAT_FILE    = Path(getattr(config, "LEARNED_WEIGHTS_FILE", config.BASE_DIR / "data" / "learned_weights.json"))

EWMA_ALPHA   = 0.30   # weight of the newest IC reading
DRIFT_RATE   = 0.02   # weekly step (gentler than the old monthly 5%)
MIN_OBS      = 15     # stock-rows needed per regime (secondary check)
# Independent weekly snapshots needed per regime before ANY weight moves.
# This is the binding constraint: rows within one week are correlated, so
# only the number of distinct periods carries information. 12 weeks is about
# three months of forward-return evidence — modest, but it is the difference
# between measuring a factor and measuring one week's weather.
MIN_PERIODS  = 12
# Minimum stocks in a single cross-section before its IC is computed at all.
# Below this the correlation is dominated by sampling noise.
MIN_IC_SAMPLE = 30
# |t| a factor's mean IC must reach ACROSS periods before its weight moves.
#
# 2.5, not the conventional 2.0, because SIX factors are tested every week.
# At t=2.0 each test has a ~5% false-positive rate, so six weekly tests throw
# up a spurious "real" factor roughly every third week — verified in
# simulation, where a factor with a TRUE edge of zero scored t=-2.95 on an
# unlucky draw. 2.5 is an informal multiple-comparison correction; it costs a
# little sensitivity and buys far fewer phantom findings.
IC_TSTAT_MIN  = 2.5


def _default_weights() -> dict:
    return dict(load_weights())


def _load_v2() -> dict:
    if V2_FILE.exists():
        try:
            raw = V2_FILE.read_bytes().rstrip(b"\x00")
            return json.loads(raw)
        except Exception:
            pass
    base = _default_weights()
    return {
        "regimes": {r: {"weights": dict(base), "ewma_ic": {}, "n_obs": 0}
                    for r in ("bull", "neutral", "bear")},
        "history": [],
    }


def _rank(a: np.ndarray) -> np.ndarray:
    """Average ranks, ties shared — the ranking Spearman needs."""
    order = a.argsort()
    ranks = np.empty(len(a), dtype=float)
    ranks[order] = np.arange(1, len(a) + 1, dtype=float)
    # average tied ranks
    uniq, inv, counts = np.unique(a, return_inverse=True, return_counts=True)
    if (counts > 1).any():
        sums = np.zeros(len(uniq))
        np.add.at(sums, inv, ranks)
        ranks = (sums / counts)[inv]
    return ranks


def _spearman(x: np.ndarray, y: np.ndarray) -> float:
    """Spearman rank correlation = Pearson on ranks. No scipy required."""
    rx, ry = _rank(x), _rank(y)
    rx -= rx.mean(); ry -= ry.mean()
    denom = np.sqrt((rx ** 2).sum() * (ry ** 2).sum())
    return float((rx * ry).sum() / denom) if denom > 0 else float("nan")


def _ic_tstat(ic_series: list) -> tuple[float, float]:
    """
    Is a factor's edge real, judged ACROSS periods rather than within one?

    Returns (mean_ic, t_stat) where t = mean / (std / sqrt(k)).

    This is the standard way factor skill is measured, and my first attempt
    got it wrong: I tested each period's IC against 2/sqrt(n-1), which for a
    150-stock cross-section demands |IC| > 0.164. Real equity factors run
    0.02-0.10 per period, so that bar would have frozen the learner forever —
    a guard so strict it silently becomes an off switch.

    A single period genuinely cannot establish skill. Consistency across many
    periods can: a factor with a true IC of 0.05 and period-to-period noise of
    0.10 reaches t≈1.7 after 12 weeks and t≈2.2 after 20. That is the signal
    worth acting on.
    """
    arr = np.asarray([x for x in ic_series if x is not None and np.isfinite(x)],
                     dtype=float)
    if len(arr) < 2:
        return (float(arr[0]) if len(arr) == 1 else float("nan"), float("nan"))
    mean = float(arr.mean())
    sd   = float(arr.std(ddof=1))
    if sd <= 0:
        return mean, float("inf") if mean != 0 else 0.0
    return mean, float(mean / (sd / np.sqrt(len(arr))))


def _spearman_ic(observations: list) -> dict:
    """
    Spearman IC per factor. Returns {factor: {"ic", "n", "significant"}}.

    scipy dependency removed 2026-08-01: it is not installed on the owner's
    machine (the older feedback loop fails with ModuleNotFoundError every
    run), so the learner would have crashed the moment it had enough data.
    Rank correlation is a dozen lines of numpy.
    """
    factor_scores, returns = {}, []
    for o in observations:
        returns.append(o["actual_return"])
        for f, s in o["scores"].items():
            factor_scores.setdefault(f, []).append(s)
    y = np.asarray(returns, dtype=float)
    ics = {}
    for f, scores in factor_scores.items():
        if len(scores) != len(y) or len(y) < MIN_IC_SAMPLE:
            continue
        x = np.asarray(scores, dtype=float)
        mask = np.isfinite(x) & np.isfinite(y)
        if mask.sum() < MIN_IC_SAMPLE:
            continue
        ic = _spearman(x[mask], y[mask])
        if ic != ic:
            continue
        ics[f] = {"ic": round(ic, 4), "n": int(mask.sum())}
    return ics


# Hard cap on how far a learned weight may wander from its config baseline.
#
# Measured by Monte Carlo (4,000 trials, threshold |t|>=2.5): with 26 weeks of
# data the learner detects a genuine edge 65% of the time and fires spuriously
# on ~1.8% of factors — but because the test re-runs weekly on an accumulating
# series, a false positive that once crosses tends to STAY crossed and keeps
# drifting 0.28pp every week. Roughly one week in nine, some factor fires by
# chance. This cap means the worst a sustained false positive can do is move a
# weight 6 points; real learning still has ample room, and the model can never
# wander far from a baseline chosen deliberately.
MAX_DRIFT_FROM_BASELINE = 0.06


def _drift(weights: dict, ewma_ic: dict, confidence: dict | None = None,
           baseline: dict | None = None) -> dict:
    """
    Weekly drift toward factors whose edge has been demonstrated.

    STEP SIZE FIX 2026-08-01: the step used to be scaled by min(|IC|, 1.0).
    Real equity factor ICs are ~0.03-0.05, so the step collapsed to
    0.02 x 0.28 x 0.04 ≈ 0.0002 — two hundredths of a percentage point per
    week. Verified in simulation: a factor that PASSED the significance test
    at t=2.37 moved its weight from 28.0% to 28.0%. The learner would clear
    its own bar and still do nothing.

    Significance is now the gate (handled by the caller) and magnitude is set
    by DRIFT_RATE, optionally scaled by how strong the evidence is — capped at
    2x so one emphatic reading cannot lurch the model.
    """
    conf = confidence or {}
    new = dict(weights)
    for f, ic in ewma_ic.items():
        if f not in new:
            continue
        eff = -ic if f in INVERTED_FACTORS else ic
        # |t| / threshold, clamped to [1, 2]: qualifying evidence moves a full
        # step, twice-as-convincing evidence moves at most double.
        t = abs(conf.get(f) or IC_TSTAT_MIN)
        scale = max(1.0, min(2.0, t / IC_TSTAT_MIN))
        new[f] = new[f] + DRIFT_RATE * new[f] * np.sign(eff) * scale
    base = baseline or dict(getattr(config, "FACTOR_WEIGHTS_WITH_SENTIMENT",
                                    config.FACTOR_WEIGHTS))
    for f in new:
        lo, hi = WEIGHT_BOUNDS.get(f, (0.01, 0.50))
        # Absolute bounds AND a leash to the deliberate baseline.
        if f in base:
            lo = max(lo, base[f] - MAX_DRIFT_FROM_BASELINE)
            hi = min(hi, base[f] + MAX_DRIFT_FROM_BASELINE)
        new[f] = round(max(lo, min(hi, new[f])), 4)
    pos = [f for f in new if f != "volatility"]
    total = sum(new[f] for f in pos)
    if total > 0:
        scale = (1.0 - new.get("volatility", 0.10)) / total
        for f in pos:
            new[f] = round(new[f] * scale, 4)
    return new


def run(dry_run: bool = False) -> dict:
    # 1. Evaluate any shadow snapshots that have matured
    observations = shadow.evaluate()
    log.info("Shadow observations: %d total", len(observations))

    store = _load_v2()
    report = {"updated_regimes": [], "exported": None, "obs_total": len(observations),
              "weight_changes": {}, "skipped_regimes": {},
              "_observations": observations}

    # 2. Per-regime IC + EWMA + drift
    for regime in ("bull", "neutral", "bear"):
        obs_r = [o for o in observations if o["regime"] == regime]
        node = store["regimes"][regime]
        node["n_obs"] = len(obs_r)

        # Count INDEPENDENT PERIODS, not rows (fixed 2026-08-01).
        #
        # Ten stocks ranked in the same week are not ten independent facts —
        # they all rode the same market move. The old guard compared
        # len(obs_r) >= 15 and passed on "30 observations" that were really
        # 10 stocks x 3 weekly snapshots: three data points. Weights were then
        # drifted on noise and, because scoring.py prefers the learned file,
        # that noise chose your stocks.
        #
        # An information coefficient computed on one cross-section is one
        # estimate. You need many of them before the average means anything.
        periods = sorted({o.get("date") or o.get("snapshot_date") for o in obs_r
                          if (o.get("date") or o.get("snapshot_date"))})
        n_periods = len(periods)
        node["n_periods"] = n_periods
        if n_periods < MIN_PERIODS:
            # Reset rather than merely skip. Any drift already applied happened
            # under the old row-counting guard and was fitted to noise; leaving
            # it in place would let that noise keep choosing stocks now that
            # learned_weights.json is no longer gitignored.
            baseline = dict(getattr(config, "FACTOR_WEIGHTS_WITH_SENTIMENT",
                                    config.FACTOR_WEIGHTS))
            if node.get("weights") != baseline:
                log.warning("  %s: resetting weights to config defaults "
                            "(previous values were drifted on insufficient evidence)",
                            regime)
                node["weights"] = baseline
                node["ewma_ic"] = {}
            log.info("  %s: %d observation(s) across only %d independent period(s) "
                     "(need %d) — weights held at config defaults",
                     regime, len(obs_r), n_periods, MIN_PERIODS)
            report["skipped_regimes"][regime] = {
                "observations": len(obs_r), "periods": n_periods,
                "periods_required": MIN_PERIODS,
            }
            continue
        if len(obs_r) < MIN_OBS:
            log.info("  %s: %d obs (< %d) — weights unchanged", regime, len(obs_r), MIN_OBS)
            report["skipped_regimes"][regime] = {"observations": len(obs_r),
                                                 "periods": n_periods}
            continue
        # ── One IC per factor PER PERIOD, then judge across periods ───────
        # A period is one weekly snapshot: its IC answers "did my scores rank
        # that week's outcomes?". Skill is whether that holds up repeatedly.
        per_period = {}          # factor -> [ic, ic, ...] across snapshots
        for period in periods:
            obs_p = [o for o in obs_r if (o.get("date") or o.get("snapshot_date")) == period]
            for f, d in _spearman_ic(obs_p).items():
                per_period.setdefault(f, []).append(d["ic"])

        node["ic_series"] = {f: [round(v, 4) for v in s] for f, s in per_period.items()}
        ic_detail, ics = {}, {}
        for f, series in per_period.items():
            mean_ic, t = _ic_tstat(series)
            significant = np.isfinite(t) and abs(t) >= IC_TSTAT_MIN
            ic_detail[f] = {"mean_ic": round(mean_ic, 4),
                            "t_stat": None if not np.isfinite(t) else round(t, 2),
                            "periods": len(series), "significant": bool(significant)}
            if not significant:
                log.info("    %-11s mean IC %+.3f over %d periods, t=%s — not "
                         "distinguishable from noise, ignored",
                         f, mean_ic, len(series),
                         "n/a" if not np.isfinite(t) else f"{t:.2f}")
                continue
            log.info("    %-11s mean IC %+.3f over %d periods, t=%.2f — REAL, "
                     "weight will move", f, mean_ic, len(series), t)
            ics[f] = mean_ic
            prev = node["ewma_ic"].get(f)
            node["ewma_ic"][f] = round(
                mean_ic if prev is None else EWMA_ALPHA * mean_ic + (1 - EWMA_ALPHA) * prev, 4
            )

        node["ic_detail"] = ic_detail
        node.setdefault("ic_history", []).append({
            "date": datetime.now(timezone.utc).date().isoformat(),
            "n_periods": n_periods,
            "detail": ic_detail,
        })
        node["ic_history"] = node["ic_history"][-52:]   # keep a year

        if not ics:
            log.info("  %s: %d periods evaluated, but no factor's IC is "
                     "statistically distinguishable from noise (need |t| >= %.1f) "
                     "— weights unchanged", regime, n_periods, IC_TSTAT_MIN)
            report["skipped_regimes"][regime] = {
                "observations": len(obs_r), "periods": n_periods,
                "reason": "no factor IC significant across periods",
                "ic_detail": ic_detail,
            }
            continue
        old_w = dict(node["weights"])
        node["weights"] = _drift(
            node["weights"], node["ewma_ic"],
            confidence={f: d.get("t_stat") for f, d in ic_detail.items()},
        )
        changed = {f: (old_w.get(f), node["weights"].get(f))
                   for f in node["weights"] if old_w.get(f) != node["weights"].get(f)}
        report["updated_regimes"].append(regime)
        report["weight_changes"][regime] = changed
        log.info("  %s: %d obs, IC=%s, weight changes=%s", regime, len(obs_r), ics, changed)

    # 3. Export current regime's weights to the flat file scoring.py reads
    try:
        from pipeline import regime as regime_module
        current = regime_module.run().get("regime", "neutral")
    except Exception:
        current = "neutral"
    export = store["regimes"][current]["weights"]
    report["exported"] = {"regime": current, "weights": export}

    store["history"].append({
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "obs_total": len(observations),
        "updated": report["updated_regimes"],
        "exported_regime": current,
    })
    store["history"] = store["history"][-100:]

    if dry_run:
        log.info("[DRY RUN] Would save v2 store and export %s weights: %s", current, export)
        return report

    V2_FILE.parent.mkdir(parents=True, exist_ok=True)
    V2_FILE.write_text(json.dumps(store, indent=2), encoding="utf-8")
    FLAT_FILE.parent.mkdir(parents=True, exist_ok=True)
    FLAT_FILE.write_text(json.dumps(export, indent=2), encoding="utf-8")
    log.info("Saved v2 store; exported %s weights -> %s", current.upper(), FLAT_FILE)
    return report


def post_to_discord(report: dict, dry_run: bool = False) -> None:
    """
    Post a weight-change summary to Discord.

    Audit finding F6 (2026-07-27): this module rewrites the factor weights that
    decide which stocks get bought, every week, and used to do so with zero
    notification. Silent model mutation is exactly the kind of change you want
    to see happen — especially with real money on the line.
    """
    try:
        from broker import discord_notify as dn
    except Exception as exc:
        log.warning("Discord notify unavailable (%s) — skipping post", exc)
        return

    exported = report.get("exported") or {}
    regime   = (exported.get("regime") or "neutral").upper()
    changes  = report.get("weight_changes") or {}
    skipped  = report.get("skipped_regimes") or {}

    fields = []
    for reg, changed in changes.items():
        if not changed:
            fields.append({"name": f"{reg.upper()}", "value": "No weight changes", "inline": False})
            continue
        lines = []
        for factor, (old, new) in sorted(changed.items()):
            if old is None or new is None:
                continue
            arrow = "▲" if new > old else "▼"
            lines.append(f"{arrow} **{factor}** {old*100:.1f}% → {new*100:.1f}%")
        fields.append({"name": f"{reg.upper()} — weights updated",
                       "value": "\n".join(lines)[:1024] or "—", "inline": False})

    # ── What the market is actually rewarding ────────────────────────────
    # THE point of this card. A weight change is a consequence; the finding is
    # which factors ranked stocks correctly, how consistently, and whether that
    # is separable from luck. Shown whether or not anything moved.
    store_now = _load_v2()
    for reg in ("bull", "neutral", "bear"):
        detail = (store_now["regimes"].get(reg) or {}).get("ic_detail") or {}
        if not detail:
            continue
        rows = []
        for f, d in sorted(detail.items(), key=lambda kv: -abs(kv[1].get("mean_ic") or 0)):
            t = d.get("t_stat")
            verdict = ("**REAL**" if d.get("significant")
                       else "noise" if t is not None else "too few periods")
            rows.append(f"`{f:<10}` IC {d.get('mean_ic', 0):+.3f} over "
                        f"{d.get('periods', 0)}w · t={t if t is not None else 'n/a'} · {verdict}")
        fields.append({
            "name": f"📈 {reg.upper()} — how well each factor ranked stocks",
            "value": "\n".join(rows)[:1024],
            "inline": False,
        })

    if skipped:
        # Report PERIODS, not rows. "30 obs" sounded like plenty; it was ten
        # stocks from each of three weeks — three independent facts.
        def _fmt(r, info):
            if isinstance(info, dict):
                if info.get("reason"):
                    return f"{r.upper()}: {info['reason']} ({info.get('periods', 0)}w)"
                return (f"{r.upper()} {info.get('periods', 0)}/"
                        f"{info.get('periods_required', MIN_PERIODS)} weeks "
                        f"({info.get('observations', 0)} rows)")
            return f"{r.upper()} {info} obs"
        fields.append({
            "name": "🔒 Weights held",
            "value": "\n".join(_fmt(r, n) for r, n in skipped.items())[:1024],
            "inline": False,
        })

    active = exported.get("weights") or {}
    if active:
        fields.append({
            "name": f"Active weights now ({regime})",
            "value": " · ".join(f"{k} {v*100:.0f}%" for k, v in
                                sorted(active.items(), key=lambda x: -x[1]))[:1024],
            "inline": False,
        })

    title = "🧠 Weekly Learning" + (" — DRY RUN (nothing saved)" if dry_run else "")
    changed_any = any(changes.values())
    n_periods_all = sorted({o.get("date") for o in (report.get("_observations") or [])
                            if o.get("date")})
    desc = (
        f"Measured **{report.get('obs_total', 0)}** stock-observations across "
        f"**{len(n_periods_all)}** weekly snapshots.\n"
        + ("Weights moved — see which factor earned it below."
           if changed_any else
           f"**Weights unchanged.** A factor must rank stocks correctly across "
           f"≥{MIN_PERIODS} weeks with |t| ≥ {IC_TSTAT_MIN:.0f} before its weight "
           "moves. One good week is weather, not skill.")
    )
    color = 0x9B59B6 if changed_any else 0x95A5A6

    try:
        dn.post_message([{
            "title": title,
            "description": desc,
            "color": color,
            "fields": fields,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "footer": {"text": "Investment Alpha · weekly learning loop"},
        }])
        log.info("Learning summary posted to Discord ✓")
    except Exception as exc:
        log.warning("Discord post failed: %s", exc)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Weekly learning update")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--no-discord", action="store_true",
                        help="Skip the Discord summary post")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    result = run(dry_run=args.dry_run)
    if not args.no_discord:
        post_to_discord(result, dry_run=args.dry_run)
    print(json.dumps(result, indent=2))
