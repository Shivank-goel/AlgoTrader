"""FastAPI web dashboard for live monitoring and engine control."""

from __future__ import annotations

import logging
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

logger = logging.getLogger(__name__)

DASHBOARD_DIR = Path(__file__).parent

_products_cache: dict[str, Any] = {"data": [], "fetched_at": 0.0}
PRODUCTS_CACHE_TTL = 60

# Read-only endpoints used to call engine.sync_exchange_state() on every poll.
# With a 5s browser refresh that hammered the exchange and, during an outage,
# drove TradingEngine._consecutive_sync_failures up by ~12/min per open tab.
_last_dashboard_sync: dict[str, float] = {"at": 0.0}
DASHBOARD_SYNC_MIN_INTERVAL = 30.0


async def _maybe_sync(engine: Any) -> None:
    """Refresh exchange state for read-only endpoints, throttled and never fatal.

    No-op while the engine loop is running — run_loop already syncs on its own
    cadence, so the dashboard should just read what it published.
    """
    if engine is None or getattr(engine, "_running", False):
        return

    now = time.monotonic()
    if now - _last_dashboard_sync["at"] < DASHBOARD_SYNC_MIN_INTERVAL:
        return
    _last_dashboard_sync["at"] = now

    try:
        await engine.sync_exchange_state()
    except Exception:
        logger.debug("Dashboard sync failed", exc_info=True)


class PairRequest(BaseModel):
    symbol: str
    timeframes: Optional[dict[str, str]] = None


class ModeRequest(BaseModel):
    mode: str  # "demo" or "live"


def _positions_payload(engine: Any) -> list[dict[str, Any]]:
    """Return dashboard-ready positions; never raises on bad rows."""
    if engine is None:
        return []
    return engine.get_positions_view()


def create_app(engine: Optional[Any] = None) -> FastAPI:
    app = FastAPI(title="Crypto Trader Dashboard", version="2.0.0")
    templates = Jinja2Templates(directory=str(DASHBOARD_DIR / "templates"))

    static_dir = DASHBOARD_DIR / "static"
    if static_dir.exists():
        app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

    def require_engine():
        if engine is None:
            raise HTTPException(status_code=503, detail="Engine not initialized")
        return engine

    @app.get("/", response_class=HTMLResponse)
    async def index(request: Request):
        return templates.TemplateResponse("index.html", {"request": request})

    @app.post("/api/engine/start")
    async def engine_start():
        eng = require_engine()
        return await eng.start_background()

    @app.post("/api/engine/stop")
    async def engine_stop():
        eng = require_engine()
        return await eng.stop_background()

    @app.get("/api/heartbeat")
    async def heartbeat():
        if engine is None:
            return {
                "state": "offline",
                "mode": "demo",
                "loop_count": 0,
                "last_scan_at": None,
                "last_scan_symbols": [],
                "started_at": None,
                "errors": 0,
                "uptime_seconds": None,
                "active_pairs": [],
                "stale": False,
            }
        return engine.get_heartbeat()

    @app.get("/api/mode")
    async def get_mode():
        if engine is None:
            return {"mode": "demo"}
        return {"mode": engine.mode}

    @app.post("/api/mode")
    async def set_mode(body: ModeRequest):
        eng = require_engine()
        result = await eng.set_mode(body.mode)
        if result.get("status") == "error":
            raise HTTPException(status_code=400, detail=result.get("message"))
        _products_cache["data"] = []
        _products_cache["fetched_at"] = 0.0
        return result

    @app.get("/api/universe")
    async def get_universe():
        if engine is None:
            return {"top_active": [], "scoreboard": [], "last_scan_at": None}
        return engine.get_universe_snapshot()

    @app.post("/api/universe/rescan")
    async def rescan_universe():
        eng = require_engine()
        try:
            return await eng.rescan_universe()
        except Exception as e:
            logger.exception("Universe rescan failed")
            raise HTTPException(status_code=500, detail=str(e))

    @app.get("/api/products")
    async def products():
        eng = require_engine()
        now = time.time()
        if now - _products_cache["fetched_at"] < PRODUCTS_CACHE_TTL and _products_cache["data"]:
            return {"products": _products_cache["data"]}

        try:
            if not eng._exchange_connected:
                await eng.exchange.connect()
                eng._exchange_connected = True

            raw = await eng.exchange.get_products()
            perpetuals = [
                {
                    "symbol": p.get("symbol", ""),
                    "id": p.get("id"),
                    "contract_type": p.get("contract_type", ""),
                    "tick_size": p.get("tick_size"),
                    "min_size": p.get("min_size"),
                }
                for p in raw
                if p.get("contract_type") == "perpetual_futures" and p.get("symbol")
            ]
            perpetuals.sort(key=lambda x: x["symbol"])
            _products_cache["data"] = perpetuals
            _products_cache["fetched_at"] = now
            return {"products": perpetuals}
        except Exception as e:
            logger.warning("Failed to fetch products from exchange: %s", e)
            fallback = [
                {"symbol": p["symbol"], "id": None, "contract_type": "perpetual_futures"}
                for p in eng.get_active_pairs()
            ]
            if not fallback:
                from src.market.universe import DEFAULT_PERPETUAL_SYMBOLS
                fallback = [
                    {"symbol": s, "id": None, "contract_type": "perpetual_futures"}
                    for s in DEFAULT_PERPETUAL_SYMBOLS
                ]
            return {"products": fallback, "cached": False, "error": str(e)}

    @app.get("/api/pairs")
    async def get_pairs():
        if engine is None:
            return {"pairs": []}
        pairs = engine.get_active_pairs()
        enriched = []
        for pair in pairs:
            symbol = pair["symbol"]
            snap = engine.get_symbol_snapshot(symbol)
            enriched.append({
                **pair,
                "price": snap.get("price") if snap else None,
                "regime": snap.get("regime") if snap else None,
                "regime_confidence": snap.get("regime_confidence") if snap else None,
            })
        return {"pairs": enriched}

    @app.post("/api/pairs")
    async def add_pair(body: PairRequest):
        eng = require_engine()
        added = eng.add_pair(body.symbol, body.timeframes)
        if not added:
            raise HTTPException(status_code=409, detail=f"Pair {body.symbol} already active")
        return {"status": "ok", "pairs": eng.get_active_pairs()}

    @app.delete("/api/pairs/{symbol}")
    async def remove_pair(symbol: str):
        eng = require_engine()
        removed = eng.remove_pair(symbol)
        if not removed:
            raise HTTPException(status_code=404, detail=f"Pair {symbol} not found")
        return {"status": "ok", "pairs": eng.get_active_pairs()}

    @app.get("/api/candles/{symbol}")
    async def candles(symbol: str, resolution: str = "15m", limit: int = 200):
        eng = require_engine()
        try:
            if not eng._exchange_connected:
                await eng.exchange.connect()
                eng._exchange_connected = True

            candle_list = await eng.exchange.get_candles(symbol.upper(), resolution, limit=limit)
            return {
                "symbol": symbol.upper(),
                "resolution": resolution,
                "candles": [
                    {
                        "time": int(c.timestamp.timestamp()),
                        "open": c.open,
                        "high": c.high,
                        "low": c.low,
                        "close": c.close,
                        "volume": c.volume,
                    }
                    for c in candle_list
                ],
            }
        except Exception as e:
            logger.warning("Failed to fetch candles for %s: %s", symbol, e)
            return {"symbol": symbol.upper(), "resolution": resolution, "candles": [], "error": str(e)}

    @app.get("/api/signals")
    async def signals(limit: int = 50):
        if engine is None:
            return {"signals": []}
        return {"signals": engine.get_signal_log(limit=limit)}

    @app.get("/api/regime/{symbol}")
    async def regime(symbol: str):
        if engine is None:
            raise HTTPException(status_code=503, detail="Engine not initialized")
        snap = engine.get_symbol_snapshot(symbol.upper())
        if not snap:
            return {
                "symbol": symbol.upper(),
                "regime": "unknown",
                "regime_confidence": 0,
                "price": None,
                "indicators": {},
                "updated_at": None,
            }
        return snap

    @app.get("/api/status")
    async def status():
        if engine is None:
            return {"status": "offline", "timestamp": datetime.utcnow().isoformat()}

        await _maybe_sync(engine)

        portfolio = engine.portfolio.get_snapshot()
        hb = engine.get_heartbeat()
        regime = engine.regime_detector.current_regime.value
        return {
            "status": hb["state"],
            "mode": engine.mode,
            "equity": portfolio.equity,
            "unrealized_pnl": portfolio.unrealized_pnl,
            "daily_pnl": portfolio.realized_pnl_today,
            "drawdown_pct": portfolio.drawdown_pct,
            "regime": regime,
            "positions": _positions_payload(engine),
            "timestamp": datetime.utcnow().isoformat(),
        }

    @app.get("/api/errors")
    async def errors(limit: int = 20):
        if engine is None:
            return {"errors": []}
        log = engine.get_signal_log(limit=100)
        err_entries = [e for e in log if e.get("status") in ("error", "rejected")]
        return {"errors": err_entries[:limit], "count": len(err_entries)}

    @app.get("/api/positions")
    async def positions():
        if engine is None:
            return {"count": 0, "positions": []}
        await _maybe_sync(engine)
        pos_list = _positions_payload(engine)
        return {"count": len(pos_list), "positions": pos_list}

    @app.get("/api/trades")
    async def trades(limit: int = 50):
        if engine is None:
            return {"trades": []}
        records = engine.journal.get_trades(limit=limit)
        return {
            "trades": [
                {
                    "symbol": t.symbol,
                    "side": t.side.value,
                    "strategy": t.strategy_name,
                    "pnl": t.pnl,
                    "pnl_pct": t.pnl_pct,
                    "entry_time": t.entry_time.isoformat() if t.entry_time else None,
                    "exit_time": t.exit_time.isoformat() if t.exit_time else None,
                }
                for t in records
            ]
        }

    @app.get("/api/strategies")
    async def strategies():
        if engine is None:
            return {"strategies": []}
        scores = engine.strategy_selector.performance_scores
        return {
            "strategies": [
                {"name": name, "score": score}
                for name, score in scores.items()
            ]
        }

    return app
