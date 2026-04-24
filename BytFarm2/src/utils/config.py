"""
utils/config.py — ConfigManager
=================================
TOML-based configuration. Built-in tomllib (Python 3.11+) for reads,
tomli-w for writes. Lives at %APPDATA%\\BytFarm\\config.toml.
Hot-reloads non-critical settings when file changes.
Critical settings (loop timing, staging path) require restart.
"""

from __future__ import annotations
import logging
import os
import pathlib
import threading
import tomllib
from typing import Any, Optional

log = logging.getLogger(__name__)

CONFIG_PATH = pathlib.Path(os.environ.get('APPDATA', '.')) / 'BytFarm' / 'config.toml'

# Settings that require a restart to take effect
COLD_SECTIONS = {'engine', 'storage.staging_path', 'instance'}

DEFAULT_CONFIG = """\
# BytFarm 2.1 Configuration
# Edit this file to tune BytFarm's behaviour.
# Changes to most settings apply live without restarting.
# Settings marked [restart required] need a full restart.

[engine]
# [restart required]
fast_loop_hz     = 20
slow_loop_hz     = 5
idle_grace_ticks = 28

[budget]
cpu_pct          = 5.0
io_pct           = 5.0
burst_duration_s = 5.0
burst_cpu_mult   = 1.5

[storage]
# [restart required]
staging_path     = ""   # empty = C:\\BytFarm\\staging

[process_guard]
misclick_window_s   = 10.0
poll_interval_s     = 1.0
excluded_exes = [
    "chrome.exe", "firefox.exe", "msedge.exe", "brave.exe",
    "WindowsTerminal.exe", "wt.exe", "cmd.exe", "powershell.exe",
    "pwsh.exe", "Code.exe", "notepad++.exe", "notepad.exe",
    "explorer.exe", "SearchHost.exe", "ShellExperienceHost.exe",
    "ApplicationFrameHost.exe", "svchost.exe",
]

[ui]
debounce_ms        = 500
cockpit_refresh_hz = 4

[modes.Frame-Tight]
ghost_max_stored_mb = 800
oc_headroom_pct     = 15
vram_runway_mb      = 512
batch_flush_mb      = 4

[modes.Build-Storm]
ghost_max_stored_mb = 1200
oc_headroom_pct     = 10
vram_runway_mb      = 1024
batch_flush_mb      = 8

[modes.Battery-Guard]
ghost_max_stored_mb = 200
oc_headroom_pct     = 0
vram_runway_mb      = 128
batch_flush_mb      = 2

[modes.Stream-Smooth]
ghost_max_stored_mb = 600
oc_headroom_pct     = 5
vram_runway_mb      = 512
batch_flush_mb      = 4
"""


class ConfigManager:
    """
    Thread-safe TOML config manager with hot-reload.
    Usage:
        config = ConfigManager()
        value  = config.get('modes.Frame-Tight.ghost_max_stored_mb', 800)
    """

    def __init__(self) -> None:
        self._data: dict = {}
        self._lock = threading.Lock()
        self._needs_restart_keys: set = set()
        self._load()
        self._start_watcher()

    def get(self, key: str, default: Any = None) -> Any:
        """
        Dot-notation key access.
        e.g. config.get('modes.Frame-Tight.ghost_max_stored_mb', 800)
        Handles keys with hyphens in section names (e.g. 'Frame-Tight').
        """
        with self._lock:
            parts = self._split_key(key)
            node  = self._data
            for p in parts:
                if not isinstance(node, dict) or p not in node:
                    return default
                node = node[p]
            return node

    def needs_restart(self) -> set:
        """Returns set of changed cold-section keys pending restart."""
        return set(self._needs_restart_keys)

    # ── Internal ──────────────────────────────────────────────────────────────

    def _load(self) -> None:
        CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        if not CONFIG_PATH.exists():
            CONFIG_PATH.write_text(DEFAULT_CONFIG, encoding='utf-8')
            log.info(f'[Config] Created default config at {CONFIG_PATH}')
        try:
            with open(CONFIG_PATH, 'rb') as f:
                with self._lock:
                    self._data = tomllib.load(f)
        except Exception as e:
            log.error(f'[Config] Failed to load {CONFIG_PATH}: {e}')
            with self._lock:
                self._data = tomllib.loads(DEFAULT_CONFIG)

    def _reload_non_critical(self) -> None:
        """Reloads only hot-reloadable sections."""
        HOT_SECTIONS = {'budget', 'process_guard', 'ui', 'modes'}
        try:
            with open(CONFIG_PATH, 'rb') as f:
                fresh = tomllib.load(f)
        except Exception as e:
            log.warning(f'[Config] Hot-reload failed: {e}')
            return

        with self._lock:
            for section in HOT_SECTIONS:
                if section in fresh:
                    self._data[section] = fresh[section]

        # Check if any cold sections changed
        for section in COLD_SECTIONS:
            parts = self._split_key(section)
            old = self._data
            new = fresh
            for p in parts:
                old = old.get(p, {}) if isinstance(old, dict) else {}
                new = new.get(p, {}) if isinstance(new, dict) else {}
            if old != new:
                self._needs_restart_keys.add(section)
                log.info(f'[Config] {section} changed — restart required')

        log.debug('[Config] Hot-reload applied')

    def _start_watcher(self) -> None:
        try:
            from watchdog.observers import Observer
            from watchdog.events import FileSystemEventHandler

            class _Handler(FileSystemEventHandler):
                def __init__(self, callback):
                    self._cb = callback
                def on_modified(self, event):
                    if pathlib.Path(event.src_path).name == CONFIG_PATH.name:
                        self._cb()

            observer = Observer()
            observer.schedule(
                _Handler(self._reload_non_critical),
                str(CONFIG_PATH.parent),
                recursive=False,
            )
            observer.daemon = True
            observer.start()
            log.info('[Config] File watcher started')
        except ImportError:
            log.warning('[Config] watchdog not installed — hot-reload disabled')
        except Exception as e:
            log.warning(f'[Config] Watcher start failed: {e}')

    @staticmethod
    def _split_key(key: str) -> list:
        """
        Split dot-notation key, preserving hyphenated section names.
        'modes.Frame-Tight.ghost_max_stored_mb' →
        ['modes', 'Frame-Tight', 'ghost_max_stored_mb']
        Strategy: split on '.' but rejoin parts that are known mode names.
        """
        parts = key.split('.')
        # Rejoin 'Frame' + '-Tight' style splits
        merged = []
        i = 0
        while i < len(parts):
            if (i + 1 < len(parts) and
                    not parts[i + 1][0].islower() and
                    '-' in parts[i + 1]):
                merged.append(parts[i] + '.' + parts[i + 1])
                i += 2
            else:
                merged.append(parts[i])
                i += 1
        # Simpler: just split on first dot for section, rest as subkeys
        return key.split('.', key.count('.'))
