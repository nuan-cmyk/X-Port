"""
routers/ports.py – CRUD + toggle endpoints for local listener ports.

Endpoints
---------
GET    /api/ports              → list all port mappings
POST   /api/ports              → create a new port mapping
PUT    /api/ports/{id}/toggle  → start or stop the Xray process for this port
DELETE /api/ports/{id}         → stop (if running) then remove a port mapping
"""

import logging

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session
from typing import List

import models
import schemas
from database import get_db
from xray_manager import XrayManagerError, process_manager

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/ports", tags=["Ports"])


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _get_port_or_404(port_id: int, db: Session) -> models.Port:
    """Return a Port ORM object or raise a 404 HTTP exception."""
    port = db.get(models.Port, port_id)
    if port is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Port with id={port_id} not found.",
        )
    return port


def _assert_node_exists(node_id: int, db: Session) -> None:
    """Raise 404 if the referenced node does not exist."""
    if db.get(models.Node, node_id) is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Node with id={node_id} not found.",
        )


def _assert_port_not_taken(local_port: int, db: Session, exclude_id: int | None = None) -> None:
    """
    Raise 409 if `local_port` is already registered to another port entry.
    Pass `exclude_id` when updating so the current row does not trigger a conflict.
    """
    query = db.query(models.Port).filter(models.Port.local_port == local_port)
    if exclude_id is not None:
        query = query.filter(models.Port.id != exclude_id)
    if query.first() is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Local port {local_port} is already in use.",
        )


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@router.get(
    "/",
    response_model=List[schemas.PortResponse],
    summary="List all port mappings",
)
def list_ports(db: Session = Depends(get_db)):
    """Return every port mapping ordered by local_port ascending."""
    return db.query(models.Port).order_by(models.Port.local_port).all()


@router.post(
    "/",
    response_model=schemas.PortResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create a new port mapping",
)
def create_port(payload: schemas.PortCreate, db: Session = Depends(get_db)):
    """
    Register a new local listener port and associate it with a node.

    Validates that:
    - The referenced node_id exists.
    - The local_port is not already registered.
    """
    _assert_node_exists(payload.node_id, db)
    _assert_port_not_taken(payload.local_port, db)

    port = models.Port(**payload.model_dump(), status="stopped")
    db.add(port)
    db.commit()
    db.refresh(port)
    return port


@router.put(
    "/{port_id}/toggle",
    response_model=schemas.PortResponse,
    summary="Toggle port status (running ↔ stopped)",
)
async def toggle_port(port_id: int, db: Session = Depends(get_db)):
    """
    Start or stop the Xray process for this port.

    Behaviour
    ---------
    - stopped → running : generates an Xray config, writes it to disk, and
      spawns an Xray child process.  The port's status is updated to
      'running' only after the process survives its 1-second health check.
    - running → stopped : sends SIGTERM to the Xray process, waits up to
      3 seconds for a clean exit (falls back to SIGKILL), removes the config
      file, and updates the port status to 'stopped'.

    Error responses
    ---------------
    502 Bad Gateway  – Xray binary missing or crashed on startup.
    """
    port = _get_port_or_404(port_id, db)

    try:
        if port.status == "stopped":
            await process_manager.start(port_id, db)
        else:
            await process_manager.stop(port_id, db)
    except XrayManagerError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=str(exc),
        )

    db.refresh(port)
    return port


@router.delete(
    "/{port_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete a port mapping",
)
async def delete_port(port_id: int, db: Session = Depends(get_db)):
    """
    Stop the Xray process (if running) then permanently remove the port mapping.

    The endpoint is safe to call regardless of the current port status; it
    will gracefully stop the process before deleting the DB row.
    """
    port = _get_port_or_404(port_id, db)

    # Stop the process if it is currently running (best-effort, swallow errors)
    if process_manager.is_running(port_id):
        try:
            await process_manager.stop(port_id, db=None)   # don't update status – row is being deleted
        except XrayManagerError as exc:
            logger.warning("Could not stop port %d before delete: %s", port_id, exc)

    db.delete(port)
    db.commit()
