"""
xray_manager.py – Async Xray process manager.

Overview
--------
`XrayProcessManager` is a module-level singleton that owns all running Xray
child processes.  It is responsible for:

  start(port_id, db)  → generate config → launch xray → capture logs
  stop(port_id)       → terminate process → clean up config file
  restart(port_id, db)→ stop then start
  stop_all()          → graceful shutdown of every managed process
  is_running(port_id) → predicate for status queries
  status_summary()    → dict of port_id → {"pid", "running"}

Concurrency model
-----------------
FastAPI runs on an asyncio event loop.  All public methods are `async` and
use `asyncio.create_subprocess_exec` so they never block the request thread.

Log capture is done via two background asyncio Tasks per process (one for
stdout, one for stderr).  They drain the pipe continuously and write lines
to the standard `logging` module with a `port:<id>` prefix.  The tasks are
cancelled automatically when the process is stopped.

Cross-platform binary resolution
---------------------------------
The binary path comes from `Settings.xray_path`.  If that path is a bare
name like "xray" or "xray.exe" we resolve it against PATH; otherwise the
literal path is used.  On Windows the `.exe` suffix is appended when the
stored path has no extension.

Error handling
--------------
- `XrayStartError`  → raised when the process exits within 1 second of launch
                       (indicates a bad config or missing binary).
- `XrayStopError`   → raised when SIGTERM + 3-second grace period fails to
                       stop the process (SIGKILL is then sent as a fallback).
- Both errors inherit from `XrayManagerError` so callers can catch the base.
"""

from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path
from typing import NamedTuple

from sqlalchemy.orm import Session

import models
from xray_config import generate_xray_config, write_config_file, remove_config_file

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Custom exceptions
# ---------------------------------------------------------------------------

class XrayManagerError(RuntimeError):
    """Base class for process-manager errors."""

class XrayStartError(XrayManagerError):
    """Raised when an Xray process fails to stay alive after launch."""

class XrayStopError(XrayManagerError):
    """Raised when a running Xray process cannot be terminated gracefully."""


# ---------------------------------------------------------------------------
# Internal state container
# ---------------------------------------------------------------------------

class _ProcessEntry(NamedTuple):
    process:    asyncio.subprocess.Process
    log_tasks:  list[asyncio.Task]  # [stdout_drain, stderr_drain]
    port_id:    int


# ---------------------------------------------------------------------------
# Log-drain coroutines
# ---------------------------------------------------------------------------

async def _drain_stream(
    stream: asyncio.StreamReader,
    level: int,
    prefix: str,
) -> None:
    """
    Continuously read lines from an asyncio StreamReader and emit them via
    the standard logging module.  Exits cleanly when the stream closes.
    """
    try:
        async for raw_line in stream:
            line = raw_line.decode(errors="replace").rstrip()
            if line:
                logger.log(level, "[%s] %s", prefix, line)
    except asyncio.CancelledError:
        pass   # normal shutdown path
    except Exception as exc:
        logger.warning("[%s] log drain error: %s", prefix, exc)


# ---------------------------------------------------------------------------
# Binary resolution
# ---------------------------------------------------------------------------

def _resolve_binary(xray_path: str) -> str:
    """
    Resolve the xray binary path.

    Rules
    -----
    1. If the stored path is an absolute file that exists → use as-is.
    2. If it looks like a relative path starting with ./ → resolve relative
       to the backend directory.
    3. Otherwise treat it as a bare executable name and let the OS find it
       on PATH (e.g. "xray").
    4. On Windows, append ".exe" if no suffix is present.
    """
    p = Path(xray_path)

    # On Windows add .exe if missing
    if sys.platform == "win32" and not p.suffix:
        p = p.with_suffix(".exe")

    if p.is_absolute() and p.exists():
        return str(p)

    if str(xray_path).startswith("./") or str(xray_path).startswith(".\\"):
        resolved = (Path(__file__).parent / p).resolve()
        return str(resolved)

    # bare name → rely on PATH
    return str(p)


# ---------------------------------------------------------------------------
# XrayProcessManager
# ---------------------------------------------------------------------------

class XrayProcessManager:
    """
    Async singleton manager for Xray child processes.

    Thread-safety note: all mutating operations are synchronised via a single
    asyncio.Lock so concurrent toggle requests for the same port are serialised.
    """

    def __init__(self) -> None:
        self._entries:  dict[int, _ProcessEntry] = {}   # port_id → entry
        self._lock:     asyncio.Lock | None = None       # created lazily

    @property
    def _alock(self) -> asyncio.Lock:
        """Lazily create the asyncio.Lock on the running event loop."""
        if self._lock is None:
            self._lock = asyncio.Lock()
        return self._lock

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def start(self, port_id: int, db: Session) -> None:
        """
        Start an Xray instance for the given port.

        Steps
        -----
        1. Load Port + Node + Settings from the database.
        2. Generate the Xray JSON config and write it to disk.
        3. Spawn the xray process with asyncio.create_subprocess_exec.
        4. Launch background log-drain tasks for stdout and stderr.
        5. Wait 1 second; if the process has already exited → raise XrayStartError.
        6. Update port.status = "running" in the database.
        """
        async with self._alock:
            if self.is_running(port_id):
                logger.info("Port %d is already running – skipping start.", port_id)
                return

            # --- Load DB state -------------------------------------------
            port:     models.Port     = db.get(models.Port,     port_id)
            settings: models.Settings = db.get(models.Settings, 1)

            if port is None:
                raise XrayStartError(f"Port id={port_id} not found in database.")
            if port.node is None:
                raise XrayStartError(f"Port id={port_id} has no associated node.")
            if settings is None:
                # Auto-create defaults
                settings = models.Settings(id=1)
                db.add(settings)
                db.commit()
                db.refresh(settings)

            node: models.Node = port.node

            # --- Generate and write config --------------------------------
            try:
                config = generate_xray_config(port, node, settings)
                config_path = write_config_file(port_id, config)
            except Exception as exc:
                raise XrayStartError(f"Config generation failed: {exc}") from exc

            # --- Resolve xray binary path ---------------------------------
            binary = _resolve_binary(settings.xray_path)
            logger.info(
                "Starting Xray for port %d (local %d → %s:%d) using binary '%s'",
                port_id, port.local_port, node.address, node.port, binary,
            )

            # --- Spawn the process ----------------------------------------
            try:
                process = await asyncio.create_subprocess_exec(
                    binary, "run", "-c", str(config_path),
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
            except FileNotFoundError:
                remove_config_file(port_id)
                raise XrayStartError(
                    f"Xray binary not found: '{binary}'. "
                    "Update xray_path in Settings."
                )
            except Exception as exc:
                remove_config_file(port_id)
                raise XrayStartError(f"Failed to spawn Xray: {exc}") from exc

            # --- Launch log-drain background tasks -----------------------
            prefix = f"port:{port_id}"
            stdout_task = asyncio.create_task(
                _drain_stream(process.stdout, logging.INFO,    prefix),
                name=f"xray-stdout-{port_id}",
            )
            stderr_task = asyncio.create_task(
                _drain_stream(process.stderr, logging.WARNING, prefix),
                name=f"xray-stderr-{port_id}",
            )

            # --- Wait 1 s to catch immediate startup failures -----------
            try:
                await asyncio.wait_for(process.wait(), timeout=1.0)
                # Process exited within 1 second → it crashed
                stdout_task.cancel()
                stderr_task.cancel()
                remove_config_file(port_id)
                raise XrayStartError(
                    f"Xray exited immediately with code {process.returncode}. "
                    "Check the config or binary path."
                )
            except asyncio.TimeoutError:
                # Still running after 1 second → good
                pass

            # --- Register entry ------------------------------------------
            self._entries[port_id] = _ProcessEntry(
                process=process,
                log_tasks=[stdout_task, stderr_task],
                port_id=port_id,
            )

            # --- Persist status -------------------------------------------
            port.status = "running"
            db.commit()
            logger.info("Xray started for port %d (PID %d)", port_id, process.pid)

    async def stop(self, port_id: int, db: Session | None = None) -> None:
        """
        Stop the Xray process for the given port.

        Steps
        -----
        1. Cancel log-drain tasks.
        2. Send SIGTERM (terminate on Windows) and wait up to 3 seconds.
        3. If still alive after 3 seconds → send SIGKILL.
        4. Remove the config file.
        5. If a DB session is provided, update port.status = "stopped".
        """
        async with self._alock:
            entry = self._entries.pop(port_id, None)
            if entry is None:
                logger.info("Port %d is not managed – nothing to stop.", port_id)
                if db:
                    port = db.get(models.Port, port_id)
                    if port and port.status != "stopped":
                        port.status = "stopped"
                        db.commit()
                return

            # Cancel log drains
            for task in entry.log_tasks:
                task.cancel()

            # Graceful termination
            proc = entry.process
            if proc.returncode is None:   # still alive
                try:
                    proc.terminate()
                    await asyncio.wait_for(proc.wait(), timeout=3.0)
                    logger.info("Xray stopped for port %d (SIGTERM)", port_id)
                except asyncio.TimeoutError:
                    logger.warning(
                        "Xray port %d did not stop gracefully – sending SIGKILL", port_id
                    )
                    try:
                        proc.kill()
                        await proc.wait()
                    except Exception as exc:
                        logger.error("SIGKILL failed for port %d: %s", port_id, exc)

            remove_config_file(port_id)

            if db:
                port = db.get(models.Port, port_id)
                if port:
                    port.status = "stopped"
                    db.commit()
            logger.info("Xray process cleaned up for port %d", port_id)

    async def restart(self, port_id: int, db: Session) -> None:
        """Stop then start the Xray process for the given port."""
        logger.info("Restarting Xray for port %d …", port_id)
        await self.stop(port_id, db=None)   # don't flip DB status yet
        await self.start(port_id, db)       # start will flip it to "running"

    async def stop_all(self) -> None:
        """
        Gracefully stop every managed Xray process.
        Called during FastAPI shutdown so no orphan processes are left.
        Does NOT update DB status (DB may be closing too).
        """
        port_ids = list(self._entries.keys())
        if not port_ids:
            return
        logger.info("Stopping all %d managed Xray process(es) …", len(port_ids))
        await asyncio.gather(
            *(self.stop(pid) for pid in port_ids),
            return_exceptions=True,
        )

    # ------------------------------------------------------------------
    # Status helpers
    # ------------------------------------------------------------------

    def is_running(self, port_id: int) -> bool:
        """
        Return True if an Xray process for this port is tracked and alive.
        A process may have crashed externally; `.returncode is None` guards that.
        """
        entry = self._entries.get(port_id)
        if entry is None:
            return False
        return entry.process.returncode is None

    def status_summary(self) -> dict[int, dict]:
        """
        Return a dict mapping port_id → {pid, running} for all tracked ports.
        Useful for the health-check endpoint or admin UI.
        """
        return {
            pid: {
                "pid":     entry.process.pid,
                "running": entry.process.returncode is None,
            }
            for pid, entry in self._entries.items()
        }


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------

#: Single shared instance imported by routers and main.py
process_manager = XrayProcessManager()
