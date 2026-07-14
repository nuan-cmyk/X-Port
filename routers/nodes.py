"""
routers/nodes.py – CRUD endpoints for proxy nodes.

Endpoints
---------
GET    /api/nodes          → list all nodes
POST   /api/nodes          → create a new node
GET    /api/nodes/{id}     → retrieve a single node
DELETE /api/nodes/{id}     → permanently remove a node
"""

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session
from typing import List

import models
import schemas
from database import get_db

router = APIRouter(prefix="/api/nodes", tags=["Nodes"])


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _get_node_or_404(node_id: int, db: Session) -> models.Node:
    """Return a Node ORM object or raise a 404 HTTP exception."""
    node = db.get(models.Node, node_id)
    if node is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Node with id={node_id} not found.",
        )
    return node


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@router.get(
    "/",
    response_model=List[schemas.NodeResponse],
    summary="List all nodes",
)
def list_nodes(db: Session = Depends(get_db)):
    """Return every node stored in the database, ordered by id ascending."""
    return db.query(models.Node).order_by(models.Node.id).all()


@router.post(
    "/",
    response_model=schemas.NodeResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create a new node",
)
def create_node(payload: schemas.NodeCreate, db: Session = Depends(get_db)):
    """
    Persist a new proxy node from the supplied payload.

    The `raw_link` field may carry the original share-link string for future
    re-parsing; it is optional.
    """
    node = models.Node(**payload.model_dump())
    db.add(node)
    db.commit()
    db.refresh(node)
    return node


@router.get(
    "/{node_id}",
    response_model=schemas.NodeResponse,
    summary="Get a single node",
)
def get_node(node_id: int, db: Session = Depends(get_db)):
    """Retrieve one node by its primary key."""
    return _get_node_or_404(node_id, db)


@router.delete(
    "/{node_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete a node",
)
def delete_node(node_id: int, db: Session = Depends(get_db)):
    """
    Permanently remove a node and cascade-delete all associated ports
    (enforced via the SQLAlchemy `cascade='all, delete-orphan'` relationship).
    """
    node = _get_node_or_404(node_id, db)
    db.delete(node)
    db.commit()
