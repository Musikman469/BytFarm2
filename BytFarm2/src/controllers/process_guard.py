"""
controllers/process_guard.py — ProcessGuard
=============================================
Monitors running processes and terminates duplicate instances that appear
within the misclick window (default 10 seconds).
Runs on the Slow Loop.
"""

from __future__ import annotations
import logging
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set

import psutil

from controllers.base import BaseController, ControllerResult

log = logging.getLogger(__name__)


@dataclass
class ProcessRecord:
    """Tracks a known running process instance."""
    exe:          str    # full executable path
    pid:          int
    name:         str    # just the filename e.g. 'chrome.exe'
    first_seen:   float  = field(default_factory=time.monotonic)
    launch_count: int    = 1   # incremented for each intentional new instance


class ProcessGuard(BaseController):
    """
    Detects and terminates accidental duplicate process launches.

    Rules:
    - Second instance within MISCLICK_WINDOW_S → terminate (misclick)
    - Second instance after MISCLICK_WINDOW_S  → intentional, track normally
    - Processes in excluded_exes              → never touch
    - System/elevated processes (AccessDenied) → log warning, skip silently
    """

    MISCLICK_WINDOW_S = 10.0

    def __init__(self, excluded_exes: Optional[List[str]] = None,
                 misclick_window_s: float = 10.0) -> None:
        self._known: Dict[str, List[ProcessRecord]] = {}
        self._excluded: Set[str] = set(excluded_exes or DEFAULT_EXCLUDED_EXES)
        self.MISCLICK_WINDOW_S = misclick_window_s
        self._termination_log: List[dict] = []   # for UI PROCESSES tab

    def should_run(self, metrics: dict, policy) -> bool:
        return True  # runs every slow-loop tick

    def run(self, metrics: dict, policy, budget) -> ControllerResult:
        result   = ControllerResult()
        live_pids = {p.pid for p in psutil.process_iter(['pid'])}

        # Prune dead processes from known list
        for exe in list(self._known):
            self._known[exe] = [
                r for r in self._known[exe] if r.pid in live_pids]
            if not self._known[exe]:
                del self._known[exe]

        # Evaluate all live processes
        for proc in psutil.process_iter(['pid', 'exe', 'name']):
            try:
                exe  = proc.info.get('exe')
                name = proc.info.get('name', '')
                pid  = proc.info.get('pid')
                if not exe or not pid:
                    continue
                if name in self._excluded or exe in self._excluded:
                    continue
                event = self._evaluate(exe, pid, name)
                if event:
                    result.log('ProcessGuard', event['event'],
                               event, level='info')
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue

        result.metrics['process_guard_known_count'] = sum(
            len(v) for v in self._known.values())
        return result

    def get_termination_log(self) -> List[dict]:
        """Returns termination history for the PROCESSES tab."""
        return list(self._termination_log)

    # ── Internal ──────────────────────────────────────────────────────────────

    def _evaluate(self, exe: str, pid: int, name: str) -> Optional[dict]:
        records = self._known.setdefault(exe, [])

        # Already tracking this PID
        if any(r.pid == pid for r in records):
            return None

        now = time.monotonic()

        if records:
            gap = now - records[0].first_seen

            if gap < self.MISCLICK_WINDOW_S:
                # Misclick — terminate the newcomer, keep the original
                self._terminate(pid, exe, name, gap)
                return {
                    'event': 'misclick_terminated',
                    'pid':   pid,
                    'exe':   exe,
                    'gap_s': round(gap, 2),
                }
            else:
                # Intentional — increment launch count on the first record
                records[0].launch_count += 1

        records.append(ProcessRecord(exe=exe, pid=pid, name=name))
        return None

    def _terminate(self, pid: int, exe: str,
                   name: str, gap_s: float) -> None:
        entry = {
            'pid':       pid,
            'exe':       exe,
            'name':      name,
            'gap_s':     round(gap_s, 2),
            'timestamp': time.time(),
        }
        try:
            psutil.Process(pid).terminate()
            log.info(f'[ProcessGuard] Terminated PID {pid} ({name}) '
                     f'gap={gap_s:.2f}s (misclick)')
            entry['result'] = 'terminated'
        except psutil.NoSuchProcess:
            entry['result'] = 'already_gone'
        except psutil.AccessDenied:
            log.warning(f'[ProcessGuard] Access denied terminating '
                        f'PID {pid} ({name}) — skipped')
            entry['result'] = 'access_denied'

        self._termination_log.append(entry)
        # Keep last 100 entries
        if len(self._termination_log) > 100:
            self._termination_log.pop(0)


# Default exclusion list — processes that legitimately run many instances
DEFAULT_EXCLUDED_EXES = {
    # Browsers
    'chrome.exe', 'firefox.exe', 'msedge.exe', 'brave.exe',
    'opera.exe', 'vivaldi.exe',
    # Terminals & shells
    'WindowsTerminal.exe', 'wt.exe', 'cmd.exe',
    'powershell.exe', 'pwsh.exe', 'ConEmu64.exe',
    # Editors & IDEs
    'Code.exe', 'notepad++.exe', 'notepad.exe',
    'sublime_text.exe', 'idea64.exe', 'devenv.exe',
    # System shell processes
    'explorer.exe', 'SearchHost.exe', 'ShellExperienceHost.exe',
    'ApplicationFrameHost.exe', 'svchost.exe', 'RuntimeBroker.exe',
    'sihost.exe', 'ctfmon.exe',
    # BytFarm itself
    'BytFarm2.exe', 'python.exe', 'pythonw.exe',
}
