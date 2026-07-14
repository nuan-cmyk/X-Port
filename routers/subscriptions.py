"""
routers/subscriptions.py – CRUD + fetch/import endpoints for subscriptions.

Endpoints
---------
GET    /api/subscriptions             → list all saved subscription sources
POST   /api/subscriptions             → add a subscription, fetch it immediately
GET    /api/subscriptions/{id}        → get one subscription
PATCH  /api/subscriptions/{id}        → update name / url / auto_update flag
DELETE /api/subscriptions/{id}        → remove a subscription (nodes are kept)
POST   /api/subscriptions/{id}/refresh → force an immediate re-fetch of one sub
GET    /api/subscriptions/scheduler/status → next scheduled run time + state
"""

import logging
from datetime import datetime, timezone
from typing import List

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, status
from sqlalchemy.orm import Session

import models
import schemas
from database import get_db
from scheduler import _upsert_node, subscription_scheduler
from subscription_parser import SubscriptionFetchError, fetch_and_parse

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/subscriptions", tags=["Subscriptions"])


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _get_sub_or_404(sub_id: int, db: Session) -> models.Subscription:
    sub = db.get(models.Subscription, sub_id)
    if sub is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Subscription id={sub_id} not found.",
        )
    return sub


async def _do_fetch_and_import(
    sub: models.Subscription,
    db: Session,
) -> schemas.ImportResult:
    """
    Core fetch+import logic shared by POST and the manual-refresh endpoint.

    Fetches the subscription URL, parses every vless:// link, upserts nodes,
    and updates the subscription metadata in one DB commit.
    """
    try:
        parsed_nodes, errors = await fetch_and_parse(sub.url)
    except SubscriptionFetchError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Failed to fetch subscription: {exc}",
        )

    total_inserted = total_updated = 0

    for node in parsed_nodes:
        inserted, updated = _upsert_node(db, node)
        total_inserted += inserted
        total_updated  += updated

    # Update subscription metadata
    sub.last_fetched = datetime.now(timezone.utc).replace(tzinfo=None)
    sub.node_count   = len(parsed_nodes)

    try:
        db.commit()
    except Exception as exc:
        db.rollback()
        logger.error("DB commit failed for sub id=%d: %s", sub.id, exc)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Database error while saving nodes: {exc}",
        )

    return schemas.ImportResult(
        subscription_id = sub.id,
        total_parsed    = len(parsed_nodes),
        inserted        = total_inserted,
        updated         = total_updated,
        skipped         = len(errors),
        errors          = errors,
    )


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@router.get(
    "/",
    response_model=List[schemas.SubscriptionResponse],
    summary="List all subscription sources",
)
def list_subscriptions(db: Session = Depends(get_db)):
    """Return all subscription sources ordered by id."""
    return db.query(models.Subscription).order_by(models.Subscription.id).all()


@router.post(
    "/",
    response_model=schemas.ImportResult,
    status_code=status.HTTP_201_CREATED,
    summary="Add a subscription and import its nodes",
)
async def create_subscription(
    payload: schemas.SubscriptionCreate,
    db: Session = Depends(get_db),
):
    """
    Register a new subscription URL, immediately fetch its content, and
    import all parsed vless:// nodes into the database.

    If the URL already exists a 409 Conflict is returned.
    The response is an ``ImportResult`` showing how many nodes were added or
    updated (not a SubscriptionResponse), since the primary use-case is the
    first bulk import.
    """
    # Guard: reject duplicate URLs
    existing = (
        db.query(models.Subscription)
        .filter(models.Subscription.url == payload.url)
        .first()
    )
    if existing:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Subscription with this URL already exists (id={existing.id}).",
        )

    # Persist the subscription row first so we have an id for the result
    sub = models.Subscription(
        name        = payload.name,
        url         = payload.url,
        auto_update = payload.auto_update,
    )
    db.add(sub)
    db.commit()
    db.refresh(sub)

    logger.info("New subscription id=%d created, starting import …", sub.id)
    return await _do_fetch_and_import(sub, db)


@router.get(
    "/scheduler/status",
    summary="Background scheduler status",
    tags=["Subscriptions"],
)
def scheduler_status():
    """
    Return whether the background scheduler is running and when the next
    refresh cycle is due.
    """
    return {
        "running":       subscription_scheduler.is_running,
        "next_run_time": subscription_scheduler.next_run_time(),
    }


@router.get(
    "/{sub_id}",
    response_model=schemas.SubscriptionResponse,
    summary="Get a single subscription",
)
def get_subscription(sub_id: int, db: Session = Depends(get_db)):
    """Retrieve one subscription source by its primary key."""
    return _get_sub_or_404(sub_id, db)


@router.patch(
    "/{sub_id}",
    response_model=schemas.SubscriptionResponse,
    summary="Update subscription metadata",
)
def update_subscription(
    sub_id: int,
    payload: schemas.SubscriptionUpdate,
    db: Session = Depends(get_db),
):
    """
    Partially update a subscription's name, url, or auto_update flag.

    Only supplied (non-None) fields are written.
    Changing ``url`` does NOT automatically re-fetch; use the
    ``/{id}/refresh`` endpoint for that.
    """
    sub = _get_sub_or_404(sub_id, db)
    update_data = payload.model_dump(exclude_none=True)
    for field_name, value in update_data.items():
        setattr(sub, field_name, value)
    db.commit()
    db.refresh(sub)
    return sub


@router.delete(
    "/{sub_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete a subscription source",
)
def delete_subscription(sub_id: int, db: Session = Depends(get_db)):
    """
    Remove a subscription source.

    The nodes that were imported from this subscription are **not** deleted
    automatically; they remain in the ``nodes`` table and can be managed
    individually via ``DELETE /api/nodes/{id}``.
    """
    sub = _get_sub_or_404(sub_id, db)
    db.delete(sub)
    db.commit()


@router.post(
    "/{sub_id}/refresh",
    response_model=schemas.ImportResult,
    summary="Force an immediate re-fetch of a subscription",
)
async def refresh_subscription(sub_id: int, db: Session = Depends(get_db)):
    """
    Re-fetch the subscription URL right now, outside of the normal schedule.

    Useful after updating a subscription URL or when the user wants to pull
    the latest node list immediately.
    """
    sub = _get_sub_or_404(sub_id, db)
    logger.info("Manual refresh triggered for subscription id=%d", sub_id)
    return await _do_fetch_and_import(sub, db)
