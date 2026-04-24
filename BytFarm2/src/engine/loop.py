"""
engine/loop.py — ExecutionEngine (Dual-Loop)
=============================================
Fast Loop (10-20 Hz): GhostController, OCController, SafetyWatchdog
Slow Loop (1-5 Hz):   HardwareScanner, SchedulingOffice, Scheduler,
                       StorageController, ProcessGuard, Logging

Injects dt into metrics each tick.
Manages idle freeze: only Watchdog runs when frozen.
"""

from __future__ import annotations
import logging
import threading
import time
from typing import Dict, Any, List, Optional

log = logging.getLogger(__name__)


class ExecutionEngine:
    """
    Dual-loop engine. Starts two daemon threads.
    All controllers are registered on init; loops start with .start().
    """

    def __init__(self, policy_bus, budget_governor,
                 fast_controllers, slow_controllers,
                 watchdog, scanner, tray=None) -> None:
        self._bus            = policy_bus
        self._budget         = budget_governor
        self._fast_ctrls     = fast_controllers   # list: [ghost, oc, watchdog]
        self._slow_ctrls     = slow_controllers   # list: [office, scheduler, storage, guard]
        self._watchdog       = watchdog
        self._scanner        = scanner
        self._tray           = tray

        self.idle_freeze     = False
        self._idle_counter   = 0
        self._running        = False

        self._last_fast_tick = time.monotonic()
        self._last_slow_tick = time.monotonic()

        # Shared metrics dict — written by scanner + controller results
        self._metrics: Dict[str, Any] = {}

        # Temp history for slope (last 5s)
        self._temp_history: List[tuple] = []

    def start(self) -> None:
        self._running = True
        threading.Thread(target=self._fast_loop, daemon=True,
                         name='BytFarm-FastLoop').start()
        threading.Thread(target=self._slow_loop, daemon=True,
                         name='BytFarm-SlowLoop').start()
        log.info('[Engine] Loops started')

    def stop(self) -> None:
        self._running = False

    def request_mode(self, mode: str) -> None:
        """Called by TrayUI or UI to manually request a mode change."""
        log.info(f'[Engine] Manual mode request: {mode}')
        # Inject a hint into metrics for the Office to pick up next tick
        self._metrics['manual_mode_request'] = mode

    # ── Fast Loop ─────────────────────────────────────────────────────────────

    def _fast_loop(self) -> None:
        while self._running:
            sleep = self._compute_sleep(self._metrics)
            time.sleep(sleep)

            now = time.monotonic()
            dt  = now - self._last_fast_tick
            self._last_fast_tick = now

            # Get latest cached metrics and inject dt
            metrics = self._scanner.get_cached()
            metrics['dt'] = dt
            metrics['cpu_temp_slope'] = self._scanner.compute_temp_slope(
                metrics.get('cpu_temp', 0.0))

            # Get current policy (may be None briefly at startup)
            policy = self._bus.latest
            budget = self._budget.tick(
                self._watchdog.current_veto if hasattr(self._watchdog, 'current_veto')
                else None)

            if self.idle_freeze:
                # Only watchdog runs during freeze
                result = self._watchdog.run(metrics, policy, budget)
                self._apply_result(result, metrics)
            else:
                for ctrl in self._fast_ctrls:
                    try:
                        if ctrl.should_run(metrics, policy):
                            result = ctrl.run(metrics, policy, budget)
                            self._apply_result(result, metrics)
                    except Exception as e:
                        log.error(f'[FastLoop] {ctrl.__class__.__name__} error: {e}')

            # Handle burst requests from results
            self._process_burst_requests(metrics)

            # Update tray icon
            if self._tray:
                try:
                    self._tray.update_from_metrics(metrics)
                except Exception:
                    pass

            self._metrics = metrics

    # ── Slow Loop ─────────────────────────────────────────────────────────────

    def _slow_loop(self) -> None:
        while self._running:
            time.sleep(0.25)   # base 4 Hz; office may slow this further

            now = time.monotonic()
            dt  = now - self._last_slow_tick
            self._last_slow_tick = now

            # Full hardware scan
            try:
                metrics = self._scanner.scan()
            except Exception as e:
                log.error(f'[SlowLoop] Scanner error: {e}')
                continue

            metrics['dt'] = dt
            metrics.update({k: v for k, v in self._metrics.items()
                            if k not in metrics})

            policy = self._bus.latest
            budget = self._budget.tick(
                self._watchdog.current_veto if hasattr(self._watchdog, 'current_veto')
                else None)

            for ctrl in self._slow_ctrls:
                try:
                    if hasattr(ctrl, 'should_run') and not ctrl.should_run(metrics, policy):
                        continue
                    if hasattr(ctrl, 'tick'):
                        # SchedulingOffice uses .tick() not .run()
                        ctrl.tick(metrics, policy, budget)
                    elif hasattr(ctrl, 'run'):
                        result = ctrl.run(metrics, policy, budget)
                        self._apply_result(result, metrics)
                except Exception as e:
                    log.error(f'[SlowLoop] {ctrl.__class__.__name__} error: {e}')

            self._metrics = metrics

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _compute_sleep(self, metrics: dict) -> float:
        cpu          = metrics.get('cpu_total',   0)
        ghost_active = metrics.get('ghost_active', False)
        io_load      = metrics.get('io_load',      0)

        idle_now = cpu < 10 and io_load < 2 and not ghost_active
        if idle_now:
            self._idle_counter += 1
        else:
            self._idle_counter = 0

        if self._idle_counter >= 28:   # ~7s at 4 Hz
            self.idle_freeze = True
            return 0.50   # 2 Hz — watchdog only

        self.idle_freeze = False
        if cpu > 70 or ghost_active: return 0.05   # 20 Hz
        elif cpu > 30:               return 0.10   # 10 Hz
        else:                        return 0.25   #  4 Hz

    def _apply_result(self, result, metrics: dict) -> None:
        """Merge controller result metrics into shared metrics dict."""
        if result and result.metrics:
            metrics.update(result.metrics)

    def _process_burst_requests(self, metrics: dict) -> None:
        """Check if any controller flagged a burst request via result."""
        # Controllers can set burst_request=True in their ControllerResult
        # This is checked here; direct calls to budget.request_burst() also work
        pass  # Placeholder — burst requests handled inline in controllers
