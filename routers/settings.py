"""
routers/settings.py – Singleton settings endpoints.

Endpoints
---------
GET /api/settings   → retrieve current application settings
PUT /api/settings   → update one or more settings fields (partial update)

Design notes
------------
The settings table is a singleton: it always has exactly one row (id=1).
On first GET the row is auto-created with sensible defaults so the frontend
never receives a 404 on initial startup.
"""

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

import models
import schemas
from database import get_db

router = APIRouter(prefix="/api/settings", tags=["Settings"])


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _get_or_create_settings(db: Session) -> models.Settings:
    """
    Return the singleton settings row, creating it with defaults if absent.
    This ensures the frontend always gets a valid response on first launch.
    """
    settings = db.get(models.Settings, 1)
    if settings is None:
        settings = models.Settings(id=1)
        db.add(settings)
        db.commit()
        db.refresh(settings)
    return settings


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@router.get(
    "/",
    response_model=schemas.SettingsResponse,
    summary="Get application settings",
)
def get_settings(db: Session = Depends(get_db)):
    """
    Retrieve the current application settings.
    Auto-initialises the row with defaults on first call.
    """
    return _get_or_create_settings(db)


@router.put(
    "/",
    response_model=schemas.SettingsResponse,
    summary="Update application settings",
)
def update_settings(payload: schemas.SettingsUpdate, db: Session = Depends(get_db)):
    """
    Perform a partial update on the settings row.

    Only fields present in the request body (and not None) are written to
    the database, so the frontend can send a single changed field without
    clearing the rest.
    """
    settings = _get_or_create_settings(db)

    # Apply only the non-None fields from the payload
    update_data = payload.model_dump(exclude_none=True)
    for field, value in update_data.items():
        setattr(settings, field, value)

    db.commit()
    db.refresh(settings)
    return settings
