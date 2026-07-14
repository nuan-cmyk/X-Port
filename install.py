#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
install.py – Cross-platform service installer for the Xray Manager API backend.

Usage
-----
    python install.py               # full install (venv + db + service)
    python install.py --no-service  # venv + db only (dev/testing)
    python install.py --uninstall   # remove the background service

Supported platforms
-------------------
    Linux   → systemd unit in /etc/systemd/system/   (requires root)
    Windows → NSSM service   (preferred, requires nssm.exe on PATH or in dir)
              Task Scheduler  (automatic fallback, always available)
    macOS   → launchd plist in ~/Library/LaunchAgents/ (user-level, no sudo)

After a successful install the API is available at:
    http://localhost:8000
    http://localhost:8000/docs  (Swagger UI)
"""

from __future__ import annotations

import argparse
import ctypes
import json
import os
import platform
import shutil
import subprocess
import sys
import textwrap
import time
from pathlib import Path

# ---------------------------------------------------------------------------
# Configuration – change these if you move the project
# ---------------------------------------------------------------------------

SERVICE_NAME   = "xray-manager"          # systemd / launchd / NSSM identifier
TASK_NAME      = "XrayManagerAPI"        # Windows Task Scheduler name
APP_DISPLAY    = "Xray Manager API"
APP_DESC       = "Xray proxy manager – FastAPI/Uvicorn backend"
HOST           = "0.0.0.0"
PORT           = 8000
UVICORN_APP    = "main:app"

# Resolve paths relative to this script's directory
BACKEND_DIR    = Path(__file__).parent.resolve()
VENV_DIR       = BACKEND_DIR / "venv"
REQUIREMENTS   = BACKEND_DIR / "requirements.txt"
LOG_DIR        = BACKEND_DIR / "logs"
SERVICE_UNIT   = Path(f"/etc/systemd/system/{SERVICE_NAME}.service")
LAUNCHD_PLIST  = Path.home() / "Library" / "LaunchAgents" / f"com.{SERVICE_NAME}.plist"


# ---------------------------------------------------------------------------
# ANSI colour helpers (disabled on Windows < Win10 or when not a TTY)
# ---------------------------------------------------------------------------

def _supports_ansi() -> bool:
    if not sys.stdout.isatty():
        return False
    if platform.system() == "Windows":
        # Enable VT100 on Windows 10+ via kernel32
        try:
            kernel32 = ctypes.windll.kernel32
            kernel32.SetConsoleMode(kernel32.GetStdHandle(-11), 7)
            return True
        except Exception:
            return False
    return True


_ANSI = _supports_ansi()


def _c(text: str, code: str) -> str:
    return f"\033[{code}m{text}\033[0m" if _ANSI else text


def ok(msg: str)    -> None: print(_c(f"  [OK] {msg}", "32"))
def info(msg: str)  -> None: print(_c(f"  -->  {msg}", "36"))
def warn(msg: str)  -> None: print(_c(f"  [!]  {msg}", "33"))
def err(msg: str)   -> None: print(_c(f"  [X]  {msg}", "31"), file=sys.stderr)
def head(msg: str)  -> None: print(_c(f"\n{'-'*60}\n  {msg}\n{'-'*60}", "1;35"))


# ---------------------------------------------------------------------------
# OS / privilege detection
# ---------------------------------------------------------------------------

SYSTEM = platform.system()   # "Linux" | "Windows" | "Darwin"


def _is_windows() -> bool: return SYSTEM == "Windows"
def _is_linux()   -> bool: return SYSTEM == "Linux"
def _is_macos()   -> bool: return SYSTEM == "Darwin"


def _check_privileges() -> None:
    """
    Abort with a helpful message if the required OS-level privileges are absent.
      Linux  → must be root (euid == 0)
      Windows → must be Administrator
      macOS  → no root needed (launchd user-level)
    """
    if _is_linux():
        if os.geteuid() != 0:
            err("Linux service installation requires root.")
            print("  Please re-run with:  sudo python install.py")
            sys.exit(1)
    elif _is_windows():
        try:
            is_admin = ctypes.windll.shell32.IsUserAnAdmin()
        except Exception:
            is_admin = False
        if not is_admin:
            err("Windows service installation requires Administrator privileges.")
            print("  Right-click your terminal and choose 'Run as Administrator', then retry.")
            sys.exit(1)


# ---------------------------------------------------------------------------
# Step 1 – Python version check
# ---------------------------------------------------------------------------

def step_check_python() -> None:
    head("Step 1 – Python version check")
    major, minor = sys.version_info[:2]
    if (major, minor) < (3, 10):
        err(f"Python 3.10+ is required (found {major}.{minor}).")
        sys.exit(1)
    ok(f"Python {major}.{minor} – OK")


# ---------------------------------------------------------------------------
# Step 2 – Virtual environment
# ---------------------------------------------------------------------------

def _python_in_venv() -> Path:
    """Return the path to the Python executable inside the venv."""
    if _is_windows():
        return VENV_DIR / "Scripts" / "python.exe"
    return VENV_DIR / "bin" / "python"


def _uvicorn_in_venv() -> Path:
    """Return the path to the uvicorn executable inside the venv."""
    if _is_windows():
        return VENV_DIR / "Scripts" / "uvicorn.exe"
    return VENV_DIR / "bin" / "uvicorn"


def step_setup_venv() -> None:
    head("Step 2 – Virtual environment")
    if VENV_DIR.exists():
        warn(f"venv already exists at {VENV_DIR} – skipping creation.")
    else:
        info(f"Creating venv at {VENV_DIR} …")
        subprocess.run([sys.executable, "-m", "venv", str(VENV_DIR)], check=True)
        ok("Virtual environment created.")

    # Always upgrade pip silently
    info("Upgrading pip …")
    subprocess.run(
        [str(_python_in_venv()), "-m", "pip", "install", "--upgrade", "pip", "-q"],
        check=True,
    )
    ok("pip is up to date.")


# ---------------------------------------------------------------------------
# Step 3 – Install dependencies
# ---------------------------------------------------------------------------

def step_install_requirements() -> None:
    head("Step 3 – Installing Python dependencies")
    if not REQUIREMENTS.exists():
        err(f"requirements.txt not found at {REQUIREMENTS}")
        sys.exit(1)

    info(f"Running pip install -r {REQUIREMENTS.name} …")
    subprocess.run(
        [
            str(_python_in_venv()), "-m", "pip", "install",
            "-r", str(REQUIREMENTS), "-q",
        ],
        check=True,
        cwd=str(BACKEND_DIR),
    )
    ok("All dependencies installed.")


# ---------------------------------------------------------------------------
# Step 4 – Initialise the SQLite database
# ---------------------------------------------------------------------------

def step_init_database() -> None:
    head("Step 4 – Initialising SQLite database")
    init_script = textwrap.dedent("""
        import sys
        sys.path.insert(0, r'{backend}')
        from database import Base, engine
        import models   # registers all ORM models
        Base.metadata.create_all(bind=engine)
        print("Database tables created (or already exist).")
    """).format(backend=str(BACKEND_DIR))

    result = subprocess.run(
        [str(_python_in_venv()), "-c", init_script],
        capture_output=True, text=True, cwd=str(BACKEND_DIR),
    )
    if result.returncode != 0:
        err("Database initialisation failed:")
        print(result.stderr)
        sys.exit(1)
    ok(result.stdout.strip())

    # Create log directory
    LOG_DIR.mkdir(exist_ok=True)
    ok(f"Log directory ready: {LOG_DIR}")


# ===========================================================================
# Step 5 – Service installation (platform-specific)
# ===========================================================================

# ---------------------------------------------------------------------------
# Linux – systemd
# ---------------------------------------------------------------------------

def _linux_service_unit() -> str:
    """Generate the content of the systemd .service unit file."""
    uvicorn     = _uvicorn_in_venv()
    access_log  = LOG_DIR / "access.log"
    error_log   = LOG_DIR / "error.log"

    return textwrap.dedent(f"""\
        [Unit]
        Description={APP_DESC}
        Documentation=https://github.com/encode/uvicorn
        After=network-online.target
        Wants=network-online.target

        [Service]
        Type=simple
        User={os.environ.get('SUDO_USER', os.environ.get('USER', 'root'))}
        WorkingDirectory={BACKEND_DIR}
        ExecStart={uvicorn} {UVICORN_APP} \\
            --host {HOST} \\
            --port {PORT} \\
            --log-level info \\
            --access-log
        Restart=on-failure
        RestartSec=5
        StandardOutput=append:{access_log}
        StandardError=append:{error_log}
        Environment=PYTHONUNBUFFERED=1
        # Resource limits – tune as needed
        LimitNOFILE=65536

        [Install]
        WantedBy=multi-user.target
    """)


def _step_install_linux() -> None:
    head("Step 5 – Installing systemd service")

    unit_content = _linux_service_unit()
    info(f"Writing unit file → {SERVICE_UNIT}")

    try:
        SERVICE_UNIT.write_text(unit_content, encoding="utf-8")
    except PermissionError:
        err(f"Cannot write to {SERVICE_UNIT} – are you running as root?")
        sys.exit(1)

    ok(f"Unit file written: {SERVICE_UNIT}")

    # Pretty-print the unit for user inspection
    print()
    print(_c("  Generated unit file:", "90"))
    for line in unit_content.splitlines():
        print("    " + _c(line, "90"))
    print()

    _run("systemctl daemon-reload")
    ok("systemd daemon reloaded.")

    _run(f"systemctl enable {SERVICE_NAME}")
    ok(f"Service '{SERVICE_NAME}' enabled (will start on boot).")

    _run(f"systemctl restart {SERVICE_NAME}")
    ok(f"Service '{SERVICE_NAME}' started.")


# ---------------------------------------------------------------------------
# macOS – launchd
# ---------------------------------------------------------------------------

def _macos_plist() -> str:
    """Generate a launchd plist for user-level persistent service."""
    uvicorn = _uvicorn_in_venv()
    return textwrap.dedent(f"""\
        <?xml version="1.0" encoding="UTF-8"?>
        <!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
          "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
        <plist version="1.0">
        <dict>
            <key>Label</key>
            <string>com.{SERVICE_NAME}</string>

            <key>ProgramArguments</key>
            <array>
                <string>{uvicorn}</string>
                <string>{UVICORN_APP}</string>
                <string>--host</string><string>{HOST}</string>
                <string>--port</string><string>{PORT}</string>
                <string>--log-level</string><string>info</string>
            </array>

            <key>WorkingDirectory</key>
            <string>{BACKEND_DIR}</string>

            <key>RunAtLoad</key>
            <true/>

            <key>KeepAlive</key>
            <true/>

            <key>StandardOutPath</key>
            <string>{LOG_DIR}/access.log</string>

            <key>StandardErrorPath</key>
            <string>{LOG_DIR}/error.log</string>

            <key>EnvironmentVariables</key>
            <dict>
                <key>PYTHONUNBUFFERED</key>
                <string>1</string>
            </dict>
        </dict>
        </plist>
    """)


def _step_install_macos() -> None:
    head("Step 5 – Installing launchd service (user-level)")

    plist_content = _macos_plist()
    LAUNCHD_PLIST.parent.mkdir(parents=True, exist_ok=True)

    info(f"Writing plist → {LAUNCHD_PLIST}")
    LAUNCHD_PLIST.write_text(plist_content, encoding="utf-8")
    ok("Plist written.")

    # Unload first in case an old version is running
    subprocess.run(["launchctl", "unload", str(LAUNCHD_PLIST)],
                   capture_output=True)

    _run(f"launchctl load -w {LAUNCHD_PLIST}")
    ok(f"Service loaded and will start on login.")
    info("To start immediately:  launchctl start com.xray-manager")


# ---------------------------------------------------------------------------
# Windows – NSSM (primary)
# ---------------------------------------------------------------------------

def _find_nssm() -> Path | None:
    """Search for nssm.exe in PATH and the backend directory."""
    # Check backend dir first (user may have dropped nssm.exe there)
    local = BACKEND_DIR / "nssm.exe"
    if local.exists():
        return local
    found = shutil.which("nssm")
    return Path(found) if found else None


def _step_install_windows_nssm(nssm: Path) -> None:
    """Install the service using NSSM (Non-Sucking Service Manager)."""
    head("Step 5 – Installing Windows service via NSSM")
    info(f"Using NSSM at: {nssm}")

    uvicorn = _uvicorn_in_venv()
    args    = f"{UVICORN_APP} --host {HOST} --port {PORT} --log-level info"

    # Remove stale installation silently
    subprocess.run([str(nssm), "remove", SERVICE_NAME, "confirm"],
                   capture_output=True)

    cmds = [
        [str(nssm), "install",   SERVICE_NAME, str(uvicorn), args],
        [str(nssm), "set",       SERVICE_NAME, "AppDirectory",   str(BACKEND_DIR)],
        [str(nssm), "set",       SERVICE_NAME, "DisplayName",    APP_DISPLAY],
        [str(nssm), "set",       SERVICE_NAME, "Description",    APP_DESC],
        [str(nssm), "set",       SERVICE_NAME, "Start",          "SERVICE_AUTO_START"],
        [str(nssm), "set",       SERVICE_NAME, "AppStdout",      str(LOG_DIR / "access.log")],
        [str(nssm), "set",       SERVICE_NAME, "AppStderr",      str(LOG_DIR / "error.log")],
        [str(nssm), "set",       SERVICE_NAME, "AppRotateFiles", "1"],
        [str(nssm), "set",       SERVICE_NAME, "AppRotateBytes", "10485760"],  # 10 MB
        [str(nssm), "set",       SERVICE_NAME, "AppEnvironmentExtra",
         "PYTHONUNBUFFERED=1"],
    ]

    for cmd in cmds:
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            err(f"NSSM command failed: {' '.join(cmd[1:3])}")
            print("  STDOUT:", result.stdout.strip())
            print("  STDERR:", result.stderr.strip())
            sys.exit(1)

    ok("NSSM service configured.")
    _run(f'sc start "{SERVICE_NAME}"', shell=True)
    ok(f"Service '{APP_DISPLAY}' started.")
    info("The service is set to AUTO-START on every Windows boot.")


# ---------------------------------------------------------------------------
# Windows – Task Scheduler (fallback, no extra tools required)
# ---------------------------------------------------------------------------

def _write_vbs_launcher() -> Path:
    """
    Write a VBScript that launches uvicorn completely invisibly (no console
    window).  This is used as the Task Scheduler action target.
    """
    vbs_path = BACKEND_DIR / "_launch_service.vbs"
    uvicorn  = _uvicorn_in_venv()
    cmd_line = (
        f'"{uvicorn}" {UVICORN_APP} '
        f"--host {HOST} --port {PORT} --log-level info "
        f'>> "{LOG_DIR}\\access.log" 2>> "{LOG_DIR}\\error.log"'
    )
    dq = '"'   # double-quote alias keeps the template readable
    vbs_lines = [
        "' _launch_service.vbs - auto-generated by install.py",
        "' Launches uvicorn as a completely hidden background process.",
        "Option Explicit",
        "Dim oShell",
        f'Set oShell = CreateObject({dq}WScript.Shell{dq})',
        f'oShell.CurrentDirectory = {dq}{BACKEND_DIR}{dq}',
        f'oShell.Run {dq}cmd /c {dq}{dq}{cmd_line}{dq}{dq}{dq}, 0, False',
        "Set oShell = Nothing",
    ]
    vbs_content = "\r\n".join(vbs_lines) + "\r\n"
    vbs_path.write_text(vbs_content, encoding="utf-8")
    ok(f"VBScript launcher written: {vbs_path.name}")
    return vbs_path



def _step_install_windows_task_scheduler() -> None:
    """Install the service via Windows Task Scheduler + hidden VBScript."""
    head("Step 5 – Installing Windows service via Task Scheduler")
    warn("NSSM not found – falling back to Task Scheduler.")
    info("  (For a true Windows Service, place nssm.exe in the backend directory and re-run.)")
    print()

    vbs_path = _write_vbs_launcher()

    # Build the PowerShell command that creates the scheduled task
    ps_script = textwrap.dedent(f"""
        $ErrorActionPreference = 'Stop'

        # Remove old task if exists
        Unregister-ScheduledTask -TaskName '{TASK_NAME}' -Confirm:$false -ErrorAction SilentlyContinue

        $action  = New-ScheduledTaskAction `
            -Execute 'wscript.exe' `
            -Argument '"{vbs_path}" //nologo' `
            -WorkingDirectory '{BACKEND_DIR}'

        $trigger = New-ScheduledTaskTrigger -AtStartup

        $settings = New-ScheduledTaskSettingsSet `
            -ExecutionTimeLimit 0 `
            -RestartCount 3 `
            -RestartInterval (New-TimeSpan -Minutes 1) `
            -StartWhenAvailable

        $principal = New-ScheduledTaskPrincipal `
            -UserId ([System.Security.Principal.WindowsIdentity]::GetCurrent().Name) `
            -RunLevel Highest `
            -LogonType Interactive

        Register-ScheduledTask `
            -TaskName '{TASK_NAME}' `
            -Description '{APP_DESC}' `
            -Action $action `
            -Trigger $trigger `
            -Settings $settings `
            -Principal $principal `
            -Force | Out-Null

        Write-Output 'Task registered.'
        Start-ScheduledTask -TaskName '{TASK_NAME}'
        Write-Output 'Task started.'
    """).strip()

    info("Registering Task Scheduler entry via PowerShell …")
    result = subprocess.run(
        ["powershell", "-NonInteractive", "-Command", ps_script],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        err("PowerShell task registration failed:")
        print(result.stdout)
        print(result.stderr)
        sys.exit(1)

    for line in result.stdout.strip().splitlines():
        ok(line)

    info(f"Task '{TASK_NAME}' is set to run at every Windows startup.")
    info(f"Logs:  {LOG_DIR}\\access.log  /  {LOG_DIR}\\error.log")


# ---------------------------------------------------------------------------
# Windows – dispatcher
# ---------------------------------------------------------------------------

def _step_install_windows() -> None:
    nssm = _find_nssm()
    if nssm:
        _step_install_windows_nssm(nssm)
    else:
        _step_install_windows_task_scheduler()

    # Print NSSM install hint if we used the fallback
    if not nssm:
        print()
        info("─── Optional: install as a true Windows Service ─────────────────")
        info("  1. Download nssm.exe from https://nssm.cc/download")
        info(f"  2. Place nssm.exe in: {BACKEND_DIR}")
        info("  3. Re-run:  python install.py")
        print()


# ---------------------------------------------------------------------------
# Step 5 dispatcher
# ---------------------------------------------------------------------------

def step_install_service() -> None:
    if _is_linux():
        _step_install_linux()
    elif _is_windows():
        _step_install_windows()
    elif _is_macos():
        _step_install_macos()
    else:
        warn(f"Unknown platform '{SYSTEM}' – skipping service installation.")
        warn("Start manually with:")
        warn(f"  {_uvicorn_in_venv()} {UVICORN_APP} --host {HOST} --port {PORT}")


# ===========================================================================
# Step 6 – Health check
# ===========================================================================

def step_health_check() -> None:
    head("Step 6 – Health check")
    url = f"http://127.0.0.1:{PORT}/health"
    info(f"Waiting for the API to respond at {url} …")

    # Give the service 8 seconds to come up
    for attempt in range(1, 9):
        time.sleep(1)
        try:
            import urllib.request
            with urllib.request.urlopen(url, timeout=2) as resp:
                body = json.loads(resp.read())
            ok(f"API responded: {body}")
            print()
            ok(f"Installation complete!")
            info(f"Swagger UI: http://localhost:{PORT}/docs")
            return
        except Exception:
            print(f"  … waiting ({attempt}/8)", end="\r")

    print()
    warn("API did not respond within 8 seconds.")
    warn("Check logs for errors:")
    warn(f"  {LOG_DIR}")
    if _is_linux():
        warn(f"  journalctl -u {SERVICE_NAME} --no-pager -n 30")


# ===========================================================================
# Uninstall
# ===========================================================================

def _run(cmd: str | list, shell: bool = False) -> subprocess.CompletedProcess:
    """Run a shell command, exit on failure."""
    if isinstance(cmd, str) and not shell and _is_linux():
        import shlex
        cmd = shlex.split(cmd)
        
    result = subprocess.run(
        cmd, shell=shell, capture_output=True, text=True,
    )
    if result.returncode != 0:
        err(f"Command failed: {cmd if isinstance(cmd, str) else ' '.join(cmd)}")
        if result.stdout.strip(): print("  stdout:", result.stdout.strip())
        if result.stderr.strip(): print("  stderr:", result.stderr.strip())
        sys.exit(1)
    return result


def uninstall() -> None:
    head("Uninstalling Xray Manager service")

    if _is_linux():
        for cmd in [
            f"systemctl stop {SERVICE_NAME}",
            f"systemctl disable {SERVICE_NAME}",
        ]:
            subprocess.run(cmd.split(), capture_output=True)
        if SERVICE_UNIT.exists():
            SERVICE_UNIT.unlink()
            ok(f"Removed: {SERVICE_UNIT}")
        subprocess.run(["systemctl", "daemon-reload"], capture_output=True)
        ok("systemd daemon reloaded.")

    elif _is_windows():
        # Try NSSM first
        nssm = _find_nssm()
        if nssm:
            subprocess.run([str(nssm), "stop", SERVICE_NAME], capture_output=True)
            subprocess.run([str(nssm), "remove", SERVICE_NAME, "confirm"], capture_output=True)
            ok(f"NSSM service '{SERVICE_NAME}' removed.")
        # Also remove Task Scheduler entry (in case both were installed)
        ps = f"Unregister-ScheduledTask -TaskName '{TASK_NAME}' -Confirm:$false -ErrorAction SilentlyContinue"
        subprocess.run(["powershell", "-NonInteractive", "-Command", ps], capture_output=True)
        ok(f"Scheduled task '{TASK_NAME}' removed (if it existed).")

        vbs = BACKEND_DIR / "_launch_service.vbs"
        if vbs.exists():
            vbs.unlink()
            ok("VBScript launcher removed.")

    elif _is_macos():
        subprocess.run(["launchctl", "unload", str(LAUNCHD_PLIST)], capture_output=True)
        if LAUNCHD_PLIST.exists():
            LAUNCHD_PLIST.unlink()
            ok(f"Removed: {LAUNCHD_PLIST}")

    ok("Service uninstalled.  Your data (database, configs) was not deleted.")
    info("To also remove the venv:  Remove-Item -Recurse venv  (Windows)")
    info("                          rm -rf venv               (Linux/macOS)")


# ===========================================================================
# Entry point
# ===========================================================================

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Xray Manager API – cross-platform service installer",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=textwrap.dedent(f"""\
            Examples
            --------
              python install.py               Full install + service registration
              python install.py --no-service  Setup venv/db only (no service)
              python install.py --uninstall   Remove the background service
        """),
    )
    parser.add_argument(
        "--no-service",
        action="store_true",
        help="Skip service registration (venv + DB setup only)",
    )
    parser.add_argument(
        "--uninstall",
        action="store_true",
        help="Remove the installed background service",
    )
    args = parser.parse_args()

    print(_c(r"""
  __  ____  ___   ____  __  __  __  __  ____  ____
 \ \/ /  \/  \ \/ /  \/  \/  \/  \/  \/  __\/  _ \
  >  <| /\ /\ \\  / /\ /\ /\ /\ /\ /\ / (__/ / \ |
 /_/\_\_\/ \/ \//  \_\/ \_\/ \_\/ \_\/ \____/\____/
  Installer  |  platform: {plat}  |  port: {port}
    """.format(plat=SYSTEM, port=PORT), "1;36"))

    if args.uninstall:
        _check_privileges()
        uninstall()
        return

    if not args.no_service:
        _check_privileges()

    step_check_python()
    step_setup_venv()
    step_install_requirements()
    step_init_database()

    if not args.no_service:
        step_install_service()
        step_health_check()
    else:
        print()
        ok("Environment ready.  Start manually with:")
        info(f"  {_uvicorn_in_venv()} {UVICORN_APP} --host {HOST} --port {PORT} --reload")


if __name__ == "__main__":
    main()
