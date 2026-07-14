"""
routers/ports.py – CRUD + toggle endpoints for local listener ports.

Endpoints
---------
GET    /api/ports              → list all ports (with nested node info)
POST   /api/ports              → create a new port mapping
PUT    /api/ports/{id}/toggle  → flip the port status running ↔ stopped
DELETE /api/ports/{id}         → remove a port mapping
"""

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session
from typing import List

import models
import schemas
from database import get_db

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
def toggle_port(port_id: int, db: Session = Depends(get_db)):
    """
    Flip the status of a port between 'running' and 'stopped'.

    Note: In Step 2 (process management), this endpoint will also start or
    stop the corresponding Xray inbound process. For now it only updates the
    persisted status flag.
    """
    port = _get_port_or_404(port_id, db)
    port.status = "running" if port.status == "stopped" else "stopped"
    db.commit()
    db.refresh(port)
    return port


@router.delete(
    "/{port_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete a port mapping",
)
def delete_port(port_id: int, db: Session = Depends(get_db)):
    """
    Remove a port mapping from the database.

    Note: If the port is currently 'running', the caller should toggle it
    first to stop the associated process (will be enforced in Step 2).
    """
    port = _get_port_or_404(port_id, db)
    db.delete(port)
    db.commit()
