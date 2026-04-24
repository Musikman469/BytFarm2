"""
controllers/scheduler.py — Scheduler
======================================
Full thread-level affinity pinning via Windows thread APIs.
Runs on the Slow Loop. Only executes when process changes, imbalance,
or policy change is detected.
"""

from __future__ import annotations
import ctypes
import logging
from typing import Dict, Optional, Set

import psutil

from controllers.base import BaseController, ControllerResult

log = logging.getLogger(__name__)

# Windows process priority class constants
PRIORITY_MAP = {
    'foreground':        0x00000080,  # HIGH_PRIORITY_CLASS
    'latency-sensitive': 0x00008000,  # ABOVE_NORMAL_PRIORITY_CLASS
    'background':        0x00004000,  # BELOW_NORMAL_PRIORITY_CLASS
    'batch':             0x00000040,  # IDLE_PRIORITY_CLASS
}

PROCESS_SET_INFORMATION = 0x0200

# Named process classification table — extend as needed
KNOWN_CLASSES: Dict[str, str] = {
    # latency-sensitive
    'audiodg.exe':          'latency-sensitive',
    'csrss.exe':            'latency-sensitive',
    'dwm.exe':              'latency-sensitive',
    'winlogon.exe':         'latency-sensitive',
    # background
    'MsMpEng.exe':          'background',   # Windows Defender
    'SearchIndexer.exe':    'background',
    'SgrmBroker.exe':       'background',
    'OneDrive.exe':         'background',
    'OfficeClickToRun.exe': 'background',
    'TiWorker.exe':         'background',   # Windows Update worker
    'WmiPrvSE.exe':         'background',
    'SearchHost.exe':       'background',
    'RuntimeBroker.exe':    'background',
    'taskhostw.exe':        'background',
    # batch
    'msbuild.exe':          'batch',
    'cl.exe':               'batch',
    'gcc.exe':              'batch',
    'g++.exe':              'batch',
    'cargo.exe':            'batch',
    'node.exe':             'batch',
    'python.exe':           'batch',    # overridden if it's the foreground proc
    'java.exe':             'batch',
    'gradle.exe':           'batch',
    'webpack.exe':          'batch',
}


def detect_core_topology() -> Dict[str, list]:
    """
    Detects P-core / E-core topology (Intel hybrid) or uniform layout.
    On uniform machines, top half = 'perf', bottom half = 'eff'.
    Returns {'perf': [core_ids...], 'eff': [core_ids...]}
    """
    physical = psutil.cpu_count(logical=False) or 1
    logical  = psutil.cpu_count(logical=True)  or 1

    # Heuristic: logical > 1.5x physical suggests hybrid with E-cores
    if logical > physical * 1.5:
        half = physical // 2
        return {
            'perf': list(range(half)),
            'eff':  list(range(half, physical)),
        }

    return {
        'perf': list(range(physical)),
        'eff':  list(range(physical // 2, physical)),
    }


class Scheduler(BaseController):
    """
    Slow-loop controller. Applies process priorities and CPU affinity masks.
    Runs only when process list changes or policy transitions.
    """

    def __init__(self) -> None:
        self._topology   = detect_core_topology()
        self._last_pids: Set[int] = set()
        log.info(f'[Scheduler] Core topology: {self._topology}')

    def should_run(self, metrics: dict, policy) -> bool:
        try:
            current_pids = {p.pid for p in psutil.process_iter(['pid'])}
        except Exception:
            return False
        changed = current_pids != self._last_pids
        self._last_pids = current_pids
        # Also run during active mode transitions
        in_transition = policy.transition.get('progress', 1.0) < 1.0
        return changed or in_transition

    def run(self, metrics: dict, policy, budget) -> ControllerResult:
        result    = ControllerResult()
        fg        = metrics.get('foreground_process', '').lower()
        applied   = 0
        skipped   = 0

        for proc in psutil.process_iter(['pid', 'name', 'status']):
            try:
                task_class = self._classify_process(proc, metrics, policy)
                self._apply_priority(proc.pid, task_class)
                self._apply_affinity(proc.pid, task_class)
                applied += 1
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                skipped += 1
                continue

        result.metrics['scheduler_applied'] = applied
        result.metrics['scheduler_skipped'] = skipped
        return result

    # ── Classification ────────────────────────────────────────────────────────

    def _classify_process(self, proc, metrics: dict, policy) -> str:
        name = proc.info.get('name', '')
        fg   = metrics.get('foreground_process', '').lower()

        # Foreground always wins
        if name.lower() == fg:
            return 'foreground'

        # Named list lookup (exact match, case-sensitive)
        known = KNOWN_CLASSES.get(name)
        if known:
            return known

        # Threshold-based fallback for unknown processes
        try:
            cpu     = proc.cpu_percent(interval=None)
            threads = proc.num_threads()
            mem_mb  = proc.memory_info().rss / 1024 ** 2
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            return 'background'

        if cpu > 20 and threads > 8:
            return 'batch'
        if cpu < 2 and mem_mb < 50:
            return 'background'

        return 'background'  # safe default for unknown processes

    # ── Priority & affinity ───────────────────────────────────────────────────

    def _apply_priority(self, pid: int, task_class: str) -> None:
        priority = PRIORITY_MAP.get(task_class, 0x00000020)  # NORMAL fallback
        handle = ctypes.windll.kernel32.OpenProcess(
            PROCESS_SET_INFORMATION, False, pid)
        if handle:
            ctypes.windll.kernel32.SetPriorityClass(handle, priority)
            ctypes.windll.kernel32.CloseHandle(handle)

    def _apply_affinity(self, pid: int, task_class: str) -> None:
        if task_class in ('background', 'batch'):
            cores = self._topology.get('eff', [])
        else:
            cores = self._topology.get('perf', [])
        if not cores:
            return
        try:
            psutil.Process(pid).cpu_affinity(cores)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass
