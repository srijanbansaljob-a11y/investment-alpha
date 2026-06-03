"""
api/server.py — FastAPI REST Server for Investment Alpha Pipeline

Start:
    uvicorn api.server:app --host 0.0.0.0 --port 8000 --reload

Endpoints:
    POST /run_workflow          — Execute full pipeline, return JSON output
    GET  /portfolio             — Return latest saved portfolio
    GET  /signals               — Return latest trade signals
    GET  /history               — List all prior run output files
    GET  /health                — Health check
    GET  /config                — Return current pipeline configuration

All responses follow the API-spec JSON format defined in Stage 8.
"""

import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Optional

# ── Bootstrap path ─────────────────────────────────────────────────────────
sys.path.insert(0, str(Path(__file__).parent.parent))
import config

try:
    from fastapi import BackgroundTasks, FastAPI, HTTPException, Query
    from fastapi.middleware.cors import CORSMiddleware
    from fastapi.responses import FileResponse, JSONResponse
    from pydantic import BaseModel
except ImportError:
    raise ImportError("FastAPI not installed. Run: pip install fastapi uvicorn")

import main as pipeline_main

# ── App Setup ──────────────────────────────────────────────────────────────
app = FastAPI(
    title="Investment Alpha API",
    description="Quantitative stock screening and portfolio construction pipeline",
    version="1.0.0",
    docs_url="/docs",      # Swagger UI at /docs
    redoc_url="/redoc",    # ReDoc at /redoc
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── In-memory cache for latest run ─────────────────────────────────────────
_latest_run: dict = {}


# ── Request / Response Models ──────────────────────────────────────────────

class WorkflowRequest(BaseModel):
    tickers:       Optional[list[str]] = None
    top_n:         Optional[int]       = None
    force_refresh: Optional[bool]      = False
    dry_run:       Optional[bool]      = False

    model_config = {"json_schema_extra": {
        "example": {
            "tickers":       ["AAPL", "MSFT", "NVDA", "GOOGL", "AMZN"],
            "top_n":         3,
            "force_refresh": False,
        }
    }}


# ── Helpers ────────────────────────────────────────────────────────────────

def _load_latest_json() -> dict | None:
    """Load the most recently created portfolio JSON from outputs/."""
    json_files = sorted(config.OUTPUT_DIR.glob("portfolio_*.json"), reverse=True)
    if not json_files:
        return None
    try:
        with open(json_files[0]) as f:
            return json.load(f)
    except Exception:
        return None


# ── Routes ────────────────────────────────────────────────────────────────

@app.get("/health", tags=["System"])
def health_check():
    """Quick health check — confirms API is running."""
    return {
        "status":    "ok",
        "timestamp": datetime.now().isoformat(),
        "version":   "1.0.0",
        "outputs_dir_exists": config.OUTPUT_DIR.exists(),
        "cache_dir_exists":   config.CACHE_DIR.exists(),
    }


@app.get("/config", tags=["System"])
def get_config():
    """Return current pipeline configuration (non-sensitive parameters)."""
    return {
        "universe_size":          len(config.ALL_TICKERS),
        "sp500_count":            len(config.SP500_TICKERS),
        "midcap400_count":        len(config.MIDCAP400_TICKERS),
        "factor_weights":         config.FACTOR_WEIGHTS,
        "top_n_stocks":           config.TOP_N_STOCKS,
        "rebalance_frequency":    config.REBALANCE_FREQUENCY,
        "min_history_days":       config.MIN_HISTORY_DAYS,
        "volatility_cutoff_pct":  int((1 - config.VOLATILITY_PERCENTILE_CUTOFF) * 100),
        "min_daily_volume_usd":   config.MIN_AVG_DAILY_VOLUME_USD,
        "cache_max_age_hours":    config.CACHE_MAX_AGE_HOURS,
        "sma_short":              config.SMA_SHORT,
        "sma_long":               config.SMA_LONG,
        "rsi_period":             config.RSI_PERIOD,
    }


@app.post("/run_workflow", tags=["Pipeline"])
def run_workflow(request: WorkflowRequest):
    """
    Execute the full 8-stage pipeline.

    - Stage 1: Data ingestion (yfinance, cached)
    - Stage 2: Feature engineering (SMA, RSI, MACD, momentum, fundamentals)
    - Stage 3: Scoring (4-factor composite)
    - Stage 4: Filtering (MA200, volatility, liquidity)
    - Stage 5: Ranking & selection (top N)
    - Stage 6: Portfolio construction (equal-weight)
    - Stage 7: Trade signal generation (BUY/HOLD/EXIT)
    - Stage 8: Output (JSON + Excel + Dashboard)

    Returns the final API-spec JSON output.
    """
    global _latest_run

    pipeline_main.setup_logging(debug=False)

    try:
        result = pipeline_main.run_pipeline(
            tickers=request.tickers,
            top_n=request.top_n or config.TOP_N_STOCKS,
            force_refresh=request.force_refresh or False,
            dry_run=request.dry_run or False,
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Pipeline error: {str(e)}")

    if "error" in result:
        raise HTTPException(status_code=500, detail=result["error"])

    _latest_run = result
    final = result.get("final_output") or {}

    return JSONResponse(content={
        "pipeline_status":  result.get("pipeline_status"),
        "elapsed_seconds":  result.get("elapsed_seconds"),
        "output_files":     result.get("output_files", {}),
        **final,
    })


@app.get("/portfolio", tags=["Portfolio"])
def get_portfolio():
    """
    Return the latest portfolio allocation.

    Reads from the most recently saved run JSON.
    Returns 404 if no run has been executed yet.
    """
    data = _load_latest_json()
    if not data:
        raise HTTPException(status_code=404, detail="No portfolio found. Run /run_workflow first.")

    return {
        "timestamp":    data.get("timestamp"),
        "run_label":    data.get("run_label"),
        "market_regime": data.get("market_regime"),
        "portfolio":    data.get("portfolio", []),
        "risk_summary": data.get("risk_summary", {}),
    }


@app.get("/signals", tags=["Portfolio"])
def get_signals():
    """
    Return the latest trade signals (BUY / HOLD / EXIT).

    Reads from the most recently saved run JSON.
    Returns 404 if no run has been executed yet.
    """
    data = _load_latest_json()
    if not data:
        raise HTTPException(status_code=404, detail="No signals found. Run /run_workflow first.")

    return {
        "timestamp":      data.get("timestamp"),
        "run_label":      data.get("run_label"),
        "signal_summary": data.get("signal_summary", {}),
        "trade_signals":  data.get("trade_signals", []),
    }


@app.get("/top_stocks", tags=["Portfolio"])
def get_top_stocks():
    """Return the ranked top 10 stocks from the latest run."""
    data = _load_latest_json()
    if not data:
        raise HTTPException(status_code=404, detail="No data found. Run /run_workflow first.")

    return {
        "timestamp":    data.get("timestamp"),
        "market_regime": data.get("market_regime"),
        "top_10_stocks": data.get("top_10_stocks", []),
    }


@app.get("/history", tags=["History"])
def get_history(limit: int = Query(default=10, ge=1, le=100)):
    """
    List all previous pipeline run output files.

    Returns metadata for the N most recent runs, sorted newest first.
    """
    json_files = sorted(config.OUTPUT_DIR.glob("portfolio_*.json"), reverse=True)[:limit]
    runs = []
    for f in json_files:
        try:
            with open(f) as fp:
                d = json.load(fp)
            runs.append({
                "run_label":     d.get("run_label"),
                "timestamp":     d.get("timestamp"),
                "market_regime": d.get("market_regime"),
                "stocks":        [s["ticker"] for s in d.get("top_10_stocks", [])],
                "signal_summary": d.get("signal_summary", {}),
                "file":          f.name,
            })
        except Exception:
            runs.append({"file": f.name, "error": "Could not parse"})

    return {
        "total_runs": len(json_files),
        "shown":      len(runs),
        "runs":       runs,
    }


@app.get("/history/{run_label}", tags=["History"])
def get_run(run_label: str):
    """Return the full output JSON for a specific historical run."""
    path = config.OUTPUT_DIR / f"portfolio_{run_label}.json"
    if not path.exists():
        raise HTTPException(status_code=404, detail=f"Run '{run_label}' not found")
    with open(path) as f:
        return json.load(f)


@app.get("/download/excel/{run_label}", tags=["Downloads"])
def download_excel(run_label: str):
    """Download the Excel report for a specific run."""
    path = config.OUTPUT_DIR / f"portfolio_{run_label}.xlsx"
    if not path.exists():
        raise HTTPException(status_code=404, detail=f"Excel report for run '{run_label}' not found")
    return FileResponse(
        path=str(path),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        filename=f"investment_alpha_{run_label}.xlsx",
    )


@app.get("/download/dashboard/{run_label}", tags=["Downloads"])
def download_dashboard(run_label: str):
    """Download the HTML dashboard for a specific run."""
    path = config.OUTPUT_DIR / f"dashboard_{run_label}.html"
    if not path.exists():
        raise HTTPException(status_code=404, detail=f"Dashboard for run '{run_label}' not found")
    return FileResponse(path=str(path), media_type="text/html")


# ── Run directly for testing ───────────────────────────────────────────────
if __name__ == "__main__":
    import uvicorn
    print("Starting Investment Alpha API server...")
    print(f"  Swagger docs : http://localhost:{config.API_PORT}/docs")
    print(f"  Health check : http://localhost:{config.API_PORT}/health")
    uvicorn.run(app, host=config.API_HOST, port=config.API_PORT, log_level="info")
