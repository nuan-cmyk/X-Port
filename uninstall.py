#!/usr/bin/env python3
"""
uninstall.py – Companion uninstaller for the Xray Manager service.

Delegates to install.py --uninstall so there is a single authoritative
removal code-path.  Run this script directly when you want a one-command
clean removal:

    python uninstall.py
"""

import subprocess
import sys
from pathlib import Path

if __name__ == "__main__":
    install_script = Path(__file__).parent / "install.py"
    result = subprocess.run(
        [sys.executable, str(install_script), "--uninstall"] + sys.argv[1:],
    )
    sys.exit(result.returncode)
