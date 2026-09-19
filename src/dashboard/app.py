"""FYERS/NSE operational dashboard; monitoring and safe controls only."""

from __future__ import annotations

import asyncio
import os
import secrets
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from src.fyers.dashboard import dashboard_snapshot
from src.fyers.models import ROOT, RuntimeConfig
from src.fyers.supervisor import RecorderSupervisor
from src.shadow.continuous import ContinuousStrategyLab

DASHBOARD_DIR = Path(__file__).parent


def create_app(engine=None, *, supervisor: RecorderSupervisor | None = None,
               strategy_lab: ContinuousStrategyLab | None = None) -> FastAPI:
    """Create the FYERS app. `engine` is ignored for source compatibility."""
    recorder = supervisor or RecorderSupervisor()
    lab_error = None
    try:
        lab = strategy_lab or ContinuousStrategyLab()
    except (OSError, ValueError) as exc:
        lab = None
        lab_error = f"strategy lab configuration unavailable: {type(exc).__name__}"
    tasks: list[asyncio.Task] = []

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        tasks.append(asyncio.create_task(recorder.schedule()))
        if lab is not None:
            tasks.append(asyncio.create_task(lab.schedule()))
        yield
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await recorder.stop()

    app = FastAPI(title="FYERS NSE Operations", version="3.0.0", lifespan=lifespan)
    token = os.environ.get("DASHBOARD_CONTROL_TOKEN", "")
    templates = Jinja2Templates(directory=str(DASHBOARD_DIR / "templates"))
    app.mount("/static", StaticFiles(directory=str(DASHBOARD_DIR / "static")), name="static")

    @app.middleware("http")
    async def authorize_controls(request: Request, call_next):
        if request.method not in {"GET", "HEAD", "OPTIONS"}:
            if len(token) < 32:
                return JSONResponse({"error": "Dashboard controls disabled: configure a strong control token"}, status_code=503)
            expected = f"Bearer {token}"
            if not secrets.compare_digest(request.headers.get("Authorization", "").encode(), expected.encode()):
                return JSONResponse({"error": "Unauthorized dashboard control"}, status_code=401)
        return await call_next(request)

    @app.get("/", response_class=HTMLResponse)
    async def index(request: Request):
        return templates.TemplateResponse(request=request, name="index.html", context={})

    @app.get("/api/fyers/dashboard")
    async def snapshot():
        payload = dashboard_snapshot()
        payload["recorder"] = recorder.status()
        if lab_error:
            payload.setdefault("sections", {})["strategy_lab"] = {
                "enabled": False, "execution": "observation_only", "live_enabled": False,
                "reason": lab_error, "candidates": []}
        return payload

    @app.get("/api/fyers/readiness")
    async def readiness_alias():
        return dashboard_snapshot()["readiness"]

    @app.post("/api/fyers/recorder/start")
    async def recorder_start():
        try:
            return await recorder.start()
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=409)

    @app.post("/api/fyers/recorder/stop")
    async def recorder_stop():
        return await recorder.stop(manual=True)

    @app.post("/api/fyers/halt")
    async def halt(request: Request):
        from src.fyers.journal import Journal
        body = await request.json()
        reason = body.get("reason") if isinstance(body, dict) else None
        if not isinstance(reason, str) or not reason.strip() or len(reason) > 200:
            return JSONResponse({"error": "A halt reason of 1-200 characters is required"}, status_code=400)
        config = RuntimeConfig.load()
        journal = Journal(ROOT / config.database)
        try:
            journal.halt(reason.strip())
        finally:
            journal.close()
        return {"status": "halted", "live_enabled": False}

    @app.post("/api/fyers/backup")
    async def backup():
        from src.fyers.operations import backup_database, prune_backups, verify_backup
        config = RuntimeConfig.load()
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        destination = ROOT / config.backup_directory / f"runtime-{stamp}.sqlite3"
        try:
            backup_database(ROOT / config.database, destination)
            return {"path": str(destination), **verify_backup(destination),
                    "pruned": prune_backups(destination.parent, retain=config.backup_retention_count)}
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=409)

    return app
