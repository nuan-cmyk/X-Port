"""
main.py – FastAPI application entry point.

Responsibilities
----------------
1. Create database tables on startup (if they don't already exist).
2. Register the three API routers (nodes, ports, settings).
3. Configure CORS so the React dev server can call the API.
4. Expose a simple health-check endpoint.

Run with:
    uvicorn main:app --reload --host 0.0.0.0 --port 8000
"""

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from database import Base, engine
from routers import nodes, ports, settings


# ---------------------------------------------------------------------------
# Lifespan – create DB tables before accepting requests
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    FastAPI lifespan handler.

    The `startup` block (before `yield`) runs once when the server starts.
    The `shutdown` block (after `yield`) runs once when the server stops.
    """
    # Create all tables that don't exist yet; no-ops for existing tables.
    Base.metadata.create_all(bind=engine)
    yield
    # Shutdown logic can go here in future steps (e.g. stop all Xray processes)


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
