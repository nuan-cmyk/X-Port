"""
scheduler.py – APScheduler-powered background subscription refresh.

Overview
--------
``SubscriptionScheduler`` wraps an APScheduler ``AsyncIOScheduler`` and owns a
single recurring job: ``_refresh_all_subscriptions``.

The job runs every ``settings.update_interval`` seconds (default 300 s).
Each run:
  1. Reads every ``Subscription`` row with ``auto_update=True`` from the DB.
  2. Calls ``fetch_and_parse(sub.url)`` for each one.
  3. Upserts the parsed nodes into the ``nodes`` table.
  4. Updates ``subscription.last_fetched`` and ``subscription.node_count``.

The scheduler interval is dynamically reconfigured by calling
``reschedule(new_interval)`` – the settings router calls this whenever a
``PUT /api/settings`` request changes ``update_interval``.

Upsert strategy
---------------
Nodes are uniquely identified by the composite key ``(uuid, address, port)``.
If a matching row already exists its fields are refreshed (update); otherwise
a new row is inserted.  ``raw_link`` is always overwritten so the latest
share link (with any updated params) is persisted.

Module-level singleton
----------------------
``subscription_scheduler`` is the single shared instance imported by
``main.py`` and ``routers/settings.py``.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger
from sqlalchemy.orm import Session

import models
from database import SessionLocal
from subscription_parser import ParsedNode, SubscriptionFetchError, fetch_and_parse

logger = logging.getLogger(__name__)

# Stable job ID so we can reschedule it by reference
_JOB_ID = "subscription_refresh"


# ---------------------------------------------------------------------------
# Node upsert helper
# ---------------------------------------------------------------------------

def _upsert_node(db: Session, node: ParsedNode) -> tuple[bool, bool]:
    """
    Insert or update a ``Node`` row based on (uuid, address, port).

    Returns
    -------
    (inserted, updated) : tuple[bool, bool]
        Exactly one of these will be True.
    """
    existing = (
        db.query(models.Node)
        .filter(
            models.Node.uuid    == node.uuid,
            models.Node.address == node.address,
            models.Node.port    == node.port,
        )
        .first()
    )

    if existing:
        # Refresh mutable fields; preserve id, latency (measured separately)
        existing.name     = node.name
        existing.protocol = node.protocol
        existing.network  = node.network
        existing.security = node.security
        existing.raw_link = node.raw_link
        return False, True   # not inserted, was updated
    else:
        new_node = models.Node(
            name     = node.name,
            protocol = node.protocol,
            address  = node.address,
            port     = node.port,
            uuid     = node.uuid,
            network  = node.network,
            security = node.security,
            raw_link = node.raw_link,
            latency  = None,
        )
        db.add(new_node)
        return True, False   # was inserted, not updated


# ---------------------------------------------------------------------------
# Core refresh job
# ---------------------------------------------------------------------------

async def _refresh_all_subscriptions() -> None:
    """
    APScheduler job: iterate every auto_update subscription and upsert nodes.

    This function is intentionally stand-alone (no ``self``) so APScheduler
    can serialise and call it without pickling a class instance.
    """
    logger.info("Subscription refresh job triggered.")

    with SessionLocal() as db:
        subs = (
            db.query(models.Subscription)
            .filter(models.Subscription.auto_update.is_(True))
            .all()
        )

        if not subs:
            logger.info("No auto_update subscriptions found – skipping.")
            return

        for sub in subs:
            logger.info("Refreshing subscription id=%d url=%s", sub.id, sub.url)
            try:
                parsed_nodes, errors = await fetch_and_parse(sub.url)
            except SubscriptionFetchError as exc:
                logger.error("Fetch failed for sub id=%d: %s", sub.id, exc)
                continue

            total_inserted = total_updated = 0

            for node in parsed_nodes:
                inserted, updated = _upsert_node(db, node)
                total_inserted += inserted
                total_updated  += updated

            # Persist the batch and update subscription metadata
            sub.last_fetched = datetime.now(timezone.utc).replace(tzinfo=None)
            sub.node_count   = len(parsed_nodes)

            try:
                db.commit()
                logger.info(
                    "Sub id=%d: +%d inserted, ~%d updated, %d errors.",
                    sub.id, total_inserted, total_updated, len(errors),
                )
            except Exception as exc:
                db.rollback()
                logger.error("DB commit failed for sub id=%d: %s", sub.id, exc)


# ---------------------------------------------------------------------------
# SubscriptionScheduler
# ---------------------------------------------------------------------------

class SubscriptionScheduler:
    """
    Thin wrapper around APScheduler's ``AsyncIOScheduler``.

    Usage in main.py lifespan
    -------------------------
    ::
        scheduler = SubscriptionScheduler()
        # startup:
        await scheduler.start(initial_interval_seconds=settings.update_interval)
        # shutdown:
        await scheduler.stop()

    Usage in settings router
    ------------------------
    ::
        subscription_scheduler.reschedule(new_interval)
    """

    def __init__(self) -> None:
        self._scheduler: AsyncIOScheduler = AsyncIOScheduler(
            job_defaults={"coalesce": True, "max_instances": 1},
            timezone="UTC",
        )

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self, initial_interval_seconds: int = 300) -> None:
        """
        Start the scheduler and register the refresh job.

        Parameters
        ----------
        initial_interval_seconds : int
            Interval read from ``settings.update_interval`` at startup.
            Can be changed later via ``reschedule()``.
        """
        self._scheduler.add_job(
            _refresh_all_subscriptions,
            trigger=IntervalTrigger(seconds=initial_interval_seconds),
            id=_JOB_ID,
            replace_existing=True,
        )
        self._scheduler.start()
        logger.info(
            "Subscription scheduler started (interval=%ds).",
            initial_interval_seconds,
        )

    def stop(self) -> None:
        """Shut down the scheduler, cancelling any pending job executions."""
        if self._scheduler.running:
            self._scheduler.shutdown(wait=False)
            logger.info("Subscription scheduler stopped.")

    # ------------------------------------------------------------------
    # Dynamic reconfiguration
    # ------------------------------------------------------------------

    def reschedule(self, new_interval_seconds: int) -> None:
        """
        Change the refresh interval without restarting the scheduler.

        Called by the settings router when ``update_interval`` is updated.
        The next run is rescheduled from now + new_interval_seconds.
        """
        if not self._scheduler.running:
            logger.warning(
                "reschedule() called before scheduler.start() – ignoring."
            )
            return

        self._scheduler.reschedule_job(
            _JOB_ID,
            trigger=IntervalTrigger(seconds=new_interval_seconds),
        )
        logger.info(
            "Subscription scheduler rescheduled (new interval=%ds).",
            new_interval_seconds,
        )

    # ------------------------------------------------------------------
    # Manual trigger
    # ------------------------------------------------------------------

    async def run_now(self) -> None:
        """
        Immediately execute the refresh job outside of its schedule.

        Called by ``POST /api/subscriptions/{id}/refresh`` so the user can
        force a manual sync from the UI without waiting for the next cycle.
        """
        await _refresh_all_subscriptions()

    # ------------------------------------------------------------------
    # Status
    # ------------------------------------------------------------------

    @property
    def is_running(self) -> bool:
        return self._scheduler.running

    def next_run_time(self) -> Optional[str]:
        """Return the ISO-8601 next-run timestamp string, or None."""
        job = self._scheduler.get_job(_JOB_ID)
        if job and job.next_run_time:
            return job.next_run_time.isoformat()
        return None


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------

#: Single shared instance imported by main.py and routers/settings.py
subscription_scheduler = SubscriptionScheduler()
