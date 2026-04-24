"""
BytFarm 2.1 — Main Entry Point
================================
Run this file to start BytFarm. PyInstaller uses this as the entry point.
"""

import sys
import os

# Ensure src/ is on the path when running from source
sys.path.insert(0, os.path.join(os.path.dirname(__file__), 'src'))

from utils.instance_lock import acquire_instance_lock
from utils.startup import startup


def main():
    # Step 1: Instance lock — must be first, before anything else
    if not acquire_instance_lock():
        sys.exit(0)  # Silent — existing instance was focused

    # Steps 2–11: Splash-driven startup sequence
    success = startup()
    if not success:
        sys.exit(1)


if __name__ == '__main__':
    main()
