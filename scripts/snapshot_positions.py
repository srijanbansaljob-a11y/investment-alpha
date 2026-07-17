"""
scripts/snapshot_positions.py — Save daily position state for outcome tracking

Runs 3x daily (every screener run) to capture the current positions
for both Screener and Pipeline accounts.

trade_outcome_logger.py diffs yesterday's snapshot vs today's to
detect exits and log their outcomes.

Snapshots stored in: data/position_snapshots/positions_YYYY-MM-DD.json
Only the last 30 days are kept (auto-pruned).
"""

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from broker.alpaca_client import get_client

_SNAP_DIR = Path(__file__).parent.parent / "data" / "position_snapshots"


def _snapshot_portfolio(portfolio: str) -> dict:
    try:
        client    = get_client(portfolio)
        positions = client.get_all_positions()
        return {
            p.symbol: {
                "qty":             float(p.qty),
                "avg_entry_price": float(p.avg_entry_price),
                "market_value":    float(p.market_value),
                "current_price":   float(p.current_price),
                "unrealized_pl":   float(p.unrealized_pl),
                "unrealized_plpc": float(p.unrealized_plpc),
                "cost_basis":      float(p.cost_basis),
            }
            for p in positions
        }
    except Exception as e:
        print(f"  ⚠  Could not snapshot {portfolio}: {e}")
        return {}


def main():
    today = datetime.now(timezone.utc).date().isoformat()
    _SNAP_DIR.mkdir(parents=True, exist_ok=True)

    sc = _snapshot_portfolio("screener")
    pc = _snapshot_portfolio("pipeline")

    snapshot = {
        "date":      today,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "screener":  sc,
        "pipeline":  pc,
    }

    path = _SNAP_DIR / f"positions_{today}.json"
    path.write_text(json.dumps(snapshot, indent=2, default=str), encoding="utf-8")
    print(
        f"✅ Snapshot saved: {path.name} — "
        f"screener {len(sc)} pos, pipeline {len(pc)} pos"
    )

    # Prune — keep only last 30 days
    all_snaps = sorted(_SNAP_DIR.glob("positions_*.json"))
    for old in all_snaps[:-30]:
        old.unlink()
        print(f"  Pruned: {old.name}")


if __name__ == "__main__":
    main()
