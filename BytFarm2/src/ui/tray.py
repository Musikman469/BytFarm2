"""
ui/tray.py — System Tray UI
=============================
Manages the BytFarm system tray icon.
Switches between 4 state icons: idle, cpu_active, gpu_active, heavy_load.
Provides right-click menu for mode switching and exit.
"""

from __future__ import annotations
import logging
import pathlib
import sys
import threading
from typing import Optional, Dict

log = logging.getLogger(__name__)

def _resource_root() -> pathlib.Path:
    """
    Resolve resources for both source runs and PyInstaller one-file builds.
    """
    if getattr(sys, 'frozen', False):
        meipass = getattr(sys, '_MEIPASS', None)
        if meipass:
            return pathlib.Path(meipass)
        return pathlib.Path(sys.executable).resolve().parent
    return pathlib.Path(__file__).resolve().parent.parent.parent


ASSETS_DIR = _resource_root() / 'assets' / 'icons'

ICON_MAP = {
    'idle':        ASSETS_DIR / 'idle.ico',
    'cpu_active':  ASSETS_DIR / 'cpu_active.ico',
    'gpu_active':  ASSETS_DIR / 'gpu_active.ico',
    'heavy_load':  ASSETS_DIR / 'heavy_load.ico',
}

_notified_once: Dict[str, bool] = {}


class TrayUI:
    """
    System tray icon with state-based icon switching.
    Runs in its own thread via pystray.
    """

    _instance: Optional['TrayUI'] = None

    def __init__(self, engine=None) -> None:
        TrayUI._instance = self
        self._engine     = engine
        self._icon       = None
        self._current_state = 'idle'
        self._thread     = threading.Thread(
            target=self._run, daemon=True, name='TrayThread')

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        if self._icon:
            self._icon.stop()

    def set_state(self, state: str) -> None:
        """
        Update tray icon to reflect current system state.
        state: 'idle' | 'cpu_active' | 'gpu_active' | 'heavy_load'
        """
        if state == self._current_state:
            return
        self._current_state = state
        if self._icon:
            try:
                from PIL import Image
                ico_path = ICON_MAP.get(state, ICON_MAP['idle'])
                self._icon.icon = Image.open(ico_path)
                self._icon.title = f'BytFarm — {state.replace("_", " ").title()}'
            except Exception as e:
                log.debug(f'[Tray] Icon update failed: {e}')

    def update_from_metrics(self, metrics: dict) -> None:
        """Determine icon state from current metrics."""
        cpu  = metrics.get('cpu_total',  0)
        gpu  = metrics.get('gpu_util',   0)
        temp = metrics.get('cpu_temp',   0)

        if cpu > 70 or temp > 85:
            state = 'heavy_load'
        elif gpu > 50:
            state = 'gpu_active'
        elif cpu > 20:
            state = 'cpu_active'
        else:
            state = 'idle'

        self.set_state(state)

    @classmethod
    def notify_once(cls, key: str, message: str) -> None:
        """Show a balloon notification at most once per session per key."""
        if _notified_once.get(key):
            return
        _notified_once[key] = True
        if cls._instance and cls._instance._icon:
            try:
                cls._instance._icon.notify(message, 'BytFarm')
            except Exception:
                log.info(f'[Tray] Notice ({key}): {message}')
        else:
            log.info(f'[Tray] Notice ({key}): {message}')

    # ── pystray loop ──────────────────────────────────────────────────────────

    def _run(self) -> None:
        try:
            import pystray
            from PIL import Image

            icon_img = Image.open(ICON_MAP['idle'])

            menu = pystray.Menu(
                pystray.MenuItem('BytFarm 2.1', None, enabled=False),
                pystray.Menu.SEPARATOR,
                pystray.MenuItem('Mode: Frame-Tight',
                                 lambda: self._set_mode('Frame-Tight')),
                pystray.MenuItem('Mode: Build-Storm',
                                 lambda: self._set_mode('Build-Storm')),
                pystray.MenuItem('Mode: Battery-Guard',
                                 lambda: self._set_mode('Battery-Guard')),
                pystray.MenuItem('Mode: Stream-Smooth',
                                 lambda: self._set_mode('Stream-Smooth')),
                pystray.Menu.SEPARATOR,
                pystray.MenuItem('Exit', self._exit),
            )

            self._icon = pystray.Icon(
                name='BytFarm',
                icon=icon_img,
                title='BytFarm',
                menu=menu,
            )
            self._icon.run()

        except ImportError:
            log.warning('[Tray] pystray not installed — tray icon disabled')
        except Exception as e:
            log.error(f'[Tray] Tray error: {e}')

    def _set_mode(self, mode: str) -> None:
        log.info(f'[Tray] User requested mode: {mode}')
        if self._engine:
            try:
                self._engine.request_mode(mode)
            except Exception as e:
                log.warning(f'[Tray] Mode request failed: {e}')

    def _exit(self) -> None:
        log.info('[Tray] Exit requested')
        if self._icon:
            self._icon.stop()
        import sys
        sys.exit(0)
