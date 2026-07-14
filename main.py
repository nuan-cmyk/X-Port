"""
main.py – FastAPI application entry point.

Responsibilities
----------------
1. Create database tables on startup (if they don't already exist).
2. Reconcile port statuses: any port marked 'running' at startup (stale from a
   previous crash) is reset to 'stopped' since no process is managing it yet.
3. Register the three API routers (nodes, ports, settings).
4. Configure CORS so the React dev server can call the API.
5. Expose a simple health-check and process-status endpoint.
6. On shutdown: gracefully stop every managed Xray child process.

Run with:
    uvicorn main:app --reload --host 0.0.0.0 --port 8000
"""

import logging
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, Depends, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy.orm import Session

from database import Base, engine, SessionLocal, get_db
from routers import nodes, ports, settings
from routers import subscriptions
from scheduler import subscription_scheduler
from xray_manager import process_manager

logger = logging.getLogger(__name__)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)


# ---------------------------------------------------------------------------
# Lifespan – create DB tables before accepting requests
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    FastAPI lifespan handler.

    Startup
    -------
    1. Create all DB tables (no-op if they already exist).
    2. Reset any port rows stuck in 'running' state from a previous crash
       so the UI correctly shows them as stopped on next launch.

    Shutdown
    --------
    Gracefully terminate every Xray child process managed by process_manager.
    """
    # --- Startup -----------------------------------------------------------
    Base.metadata.create_all(bind=engine)

    # Reconcile stale 'running' status left over from a previous unclean exit
    import models as _models
    with SessionLocal() as db:
        stale = (
            db.query(_models.Port)
            .filter(_models.Port.status == "running")
            .all()
        )
        for p in stale:
            p.status = "stopped"
        if stale:
            db.commit()
            logger.info(
                "Reset %d stale 'running' port(s) to 'stopped' on startup.",
                len(stale),
            )

    logger.info("Xray Manager API started. Visit /docs for the Swagger UI.")

    # --- Start background subscription scheduler --------------------------
    import models as _models2
    with SessionLocal() as _db:
        _settings = _db.get(_models2.Settings, 1)
        _interval = _settings.update_interval if _settings else 300
    subscription_scheduler.start(initial_interval_seconds=_interval)
    logger.info("Subscription scheduler started (interval=%ds).", _interval)

    yield

    # --- Shutdown ----------------------------------------------------------
    logger.info("Shutting down – stopping all Xray processes …")
    await process_manager.stop_all()
    subscription_scheduler.stop()
    logger.info("Shutdown complete.")


# ---------------------------------------------------------------------------
# Application instance
# ---------------------------------------------------------------------------

app = FastAPI(
    title="Xray Manager API",
    description=(
        "REST API for managing Xray-core proxy nodes, local listener ports, "
        "and application settings. Built with FastAPI + SQLAlchemy + SQLite."
    ),
    version="0.1.0",
    lifespan=lifespan,
)


# ---------------------------------------------------------------------------
# CORS
# ---------------------------------------------------------------------------
# Allow the React Vite dev server (default port 5173) and CRA (3000) to call
# the API without browser CORS errors.
# In production, replace these origins with your actual frontend URL.

ALLOWED_ORIGINS = [
    "http://localhost:3000",   # Create React App
    "http://localhost:5173",   # Vite
    "http://127.0.0.1:3000",
    "http://127.0.0.1:5173",
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ---------------------------------------------------------------------------
# Routers
# ---------------------------------------------------------------------------

app.include_router(nodes.router)
app.include_router(ports.router)
app.include_router(settings.router)
app.include_router(subscriptions.router)


# ---------------------------------------------------------------------------
# Health check
# ---------------------------------------------------------------------------

@app.get("/health", tags=["Health"], summary="Health check")
def health_check():
    """Returns 200 OK when the server is up and the database is reachable."""
    return {"status": "ok", "version": app.version}


@app.get("/health/processes", tags=["Health"], summary="Active Xray process list")
def process_status():
    """
    Return the PID and live-status of every Xray child process currently
    managed by the process manager.

    Response shape:
        {
          "<port_id>": { "pid": 12345, "running": true },
          ...
        }
    """
    return process_manager.status_summary()


# ---------------------------------------------------------------------------
# Frontend SPA & Extra Endpoints
# ---------------------------------------------------------------------------

import subprocess
import time
import re
from collections import deque

_TRAFFIC_HISTORY = deque(maxlen=20)
_PREV_STATS = {} # port_id -> {"down": total_down, "up": total_up, "time": float}

@app.get("/api/stats", tags=["Stats"], summary="Dashboard statistics")
def get_stats(db: Session = Depends(get_db)):
    """Fetch real statistics from running Xray cores."""
    import models
    settings = db.query(models.Settings).first()
    active_ports = db.query(models.Port).filter(models.Port.status == "running").all()
    
    total_down_rate = 0
    total_up_rate = 0
    now = time.time()
    
    total_down_bytes = 0
    total_up_bytes = 0
    
    for p in active_ports:
        api_port = 10000 + p.id
        try:
            res = subprocess.run(
                [settings.xray_path, "api", "stats", f"-server=127.0.0.1:{api_port}"],
                capture_output=True, text=True, timeout=2
            )
            if res.returncode == 0 and res.stdout:
                # Support both protobuf text format and JSON
                names = re.findall(r'"?name"?\s*[:]\s*"([^"]+)"', res.stdout)
                values = re.findall(r'"?value"?\s*[:]\s*"?(\d+)"?', res.stdout)
                
                p_down = 0
                p_up = 0
                for n, v in zip(names, values):
                    val = int(v)
                    # Exclude api traffic from the stats to prevent infinite loop
                    if "api" in n:
                        continue
                    if "downlink" in n: p_down += val
                    elif "uplink" in n: p_up += val
                
                total_down_bytes += p_down
                total_up_bytes += p_up
                
                if p.id in _PREV_STATS:
                    prev = _PREV_STATS[p.id]
                    dt = now - prev["time"]
                    if dt > 0:
                        total_down_rate += max(0, (p_down - prev["down"]) / dt)
                        total_up_rate += max(0, (p_up - prev["up"]) / dt)
                
                _PREV_STATS[p.id] = {"down": p_down, "up": p_up, "time": now}
        except Exception as e:
            pass # Xray not ready or not responding
            
    time_str = time.strftime("%H:%M:%S")
    _TRAFFIC_HISTORY.append({"time": time_str, "down": total_down_rate, "up": total_up_rate})
    
    return {
        "active_ports": len(active_ports),
        "total_download_bytes": total_down_bytes,
        "total_upload_bytes": total_up_bytes,
        "traffic_history": list(_TRAFFIC_HISTORY)
    }


@app.post("/api/core/restart", tags=["Core"], summary="Restart all running Xray processes")
async def restart_core(db: Session = Depends(get_db)):
    """Trigger the Process Manager to restart all running ports."""
    import models
    running_ports = db.query(models.Port).filter(models.Port.status == "running").all()
    restarted = 0
    for p in running_ports:
        try:
            await process_manager.restart(p.id, db)
            restarted += 1
        except Exception as e:
            logger.error("Failed to restart port %d: %s", p.id, e)
            p.status = "stopped"
            db.commit()
    return {"message": f"Restarted {restarted} running ports."}


# ---------------------------------------------------------------------------
# Frontend Multi-Page HTML Serving
# ---------------------------------------------------------------------------
# The frontend consists of a single "frontend" folder in the parent directory
# containing the html files. We mount it as a static directory so any
# local assets load, and map clean URLs to the HTML files.

_BASE_DIR = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.abspath(os.path.join(_BASE_DIR, ".."))
_FRONTEND_DIR = os.path.join(_PROJECT_ROOT, "frontend")

# Mount directory for static assets (images, external CSS/JS if any)
if os.path.isdir(_FRONTEND_DIR):
    app.mount("/static", StaticFiles(directory=_FRONTEND_DIR), name="static")


def _serve_html(filename: str):
    """Helper to safely serve an HTML file if it exists."""
    path = os.path.join(_FRONTEND_DIR, filename)
    if os.path.isfile(path):
        return FileResponse(path)
    return {"detail": f"Frontend file not found at {path}"}


@app.get("/", include_in_schema=False)
def serve_dashboard():
    return _serve_html("dashboard.html")


@app.get("/ports", include_in_schema=False)
def serve_ports():
    return _serve_html("ports.html")


@app.get("/nodes", include_in_schema=False)
def serve_nodes():
    return _serve_html("nodes.html")


@app.get("/settings", include_in_schema=False)
def serve_settings():
    return _serve_html("settings.html")

