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
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from database import Base, engine, SessionLocal
from routers import nodes, ports, settings
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
    yield

    # --- Shutdown ----------------------------------------------------------
    logger.info("Shutting down – stopping all Xray processes …")
    await process_manager.stop_all()
    logger.info("All Xray processes stopped. Goodbye.")


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
