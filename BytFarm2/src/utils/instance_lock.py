"""
utils/instance_lock.py — Single Instance Enforcement
======================================================
Uses a Windows named mutex to ensure only one BytFarm instance runs.
If another instance exists, focuses its window and returns False.
"""

from __future__ import annotations
import ctypes
import ctypes.wintypes
import logging

log = logging.getLogger(__name__)

MUTEX_NAME       = 'Global\\BytFarm_SingleInstance_v2'
WM_BYTFARM_FOCUS = None   # registered lazily

# Module-level handle to keep mutex alive for process lifetime
_mutex_handle = None


def acquire_instance_lock() -> bool:
    """
    Returns True if this is the first instance (lock acquired).
    Returns False if another instance is already running.
    On False: focuses the existing window, then caller should sys.exit(0).
    """
    global _mutex_handle, WM_BYTFARM_FOCUS

    handle = ctypes.windll.kernel32.CreateMutexW(None, True, MUTEX_NAME)
    err    = ctypes.windll.kernel32.GetLastError()
    ERROR_ALREADY_EXISTS = 183

    if err == ERROR_ALREADY_EXISTS:
        log.info('[InstanceLock] Another instance detected — focusing it')
        _focus_existing_window()
        if handle:
            ctypes.windll.kernel32.CloseHandle(handle)
        return False

    # Keep handle alive — releasing it would allow another instance to start
    _mutex_handle = handle
    log.info('[InstanceLock] Mutex acquired — first instance')
    return True


def _focus_existing_window() -> None:
    """
    Broadcasts a registered window message to the running BytFarm instance.
    The main window listens for this and calls showNormal() + activateWindow().
    """
    global WM_BYTFARM_FOCUS
    if WM_BYTFARM_FOCUS is None:
        WM_BYTFARM_FOCUS = ctypes.windll.user32.RegisterWindowMessageW(
            'BytFarmFocusRequest')

    HWND_BROADCAST = 0xFFFF
    ctypes.windll.user32.PostMessageW(
        HWND_BROADCAST, WM_BYTFARM_FOCUS, 0, 0)
