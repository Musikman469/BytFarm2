"""
utils/startup.py — Startup Sequence
=====================================
Orchestrates the 10-step splash-driven initialisation.
Fatal failures: PolicyBus, ExecutionEngine, HardwareScanner (total),
               StorageController (no write access).
All others degrade gracefully with UI warnings.
"""

from __future__ import annotations
import logging
import sys
from typing import Optional

log = logging.getLogger(__name__)

# Fatal step labels — any exception here exits BytFarm
FATAL_STEPS = {
    'Scanning hardware...',
    'Initialising policy bus...',
    'Starting engine loops...',
    'Preparing storage...',
}


def _is_fatal(label: str) -> bool:
    return label in FATAL_STEPS


def startup() -> bool:
    """
    Runs the full startup sequence behind a splash screen.
    Returns True on success, False on fatal failure.
    """
    # Configure logging first (before any other imports that might log)
    _configure_logging()

    from ui.splash import SplashScreen

    splash = SplashScreen(total_steps=10)
    splash.show()

    components = {}

    def step(n: int, label: str, fn):
        splash.update(n, label)
        try:
            result = fn()
            components[label] = result
            log.info(f'[Startup] Step {n}: {label} — OK')
            return result
        except Exception as e:
            log.error(f'[Startup] Step {n}: {label} — FAILED: {e}')
            if _is_fatal(label):
                splash.show_error(label, e)
                return None
            else:
                log.warning(f'[Startup] Non-fatal: {label} degraded')
                components[label] = None
                return None

    # ── Step 1: Config ────────────────────────────────────────────────────────
    from utils.config import ConfigManager
    config = step(1, 'Loading config...', ConfigManager)
    if config is None:
        config = ConfigManager()  # fallback to defaults

    # ── Step 2: Hardware Scanner ──────────────────────────────────────────────
    from controllers.hardware_scanner import HardwareScanner
    scanner = step(2, 'Scanning hardware...', HardwareScanner)
    if scanner is None:
        splash.finish()
        return False  # Fatal

    # ── Step 3: OC Controller ─────────────────────────────────────────────────
    from oc.oc_controller import OCController
    oc_ctrl = step(3, 'Detecting OC capability...', OCController)

    # ── Step 4: Scheduler / Core Topology ─────────────────────────────────────
    from controllers.scheduler import Scheduler
    scheduler = step(4, 'Mapping CPU topology...', Scheduler)

    # ── Step 5: Storage ───────────────────────────────────────────────────────
    from storage.flow_director import FlowDirector, get_staging_dir
    def init_storage():
        staging_dir = get_staging_dir(config)
        batch_mb = config.get('modes.Frame-Tight.batch_flush_mb', 4.0) if config else 4.0
        return FlowDirector(staging_dir, batch_flush_mb=batch_mb)

    storage = step(5, 'Preparing storage...', init_storage)
    if storage is None:
        splash.finish()
        return False  # Fatal

    # ── Step 6: Policy Bus ────────────────────────────────────────────────────
    from policy.snapshot import PolicyBus, default_snapshot
    def init_bus():
        bus = PolicyBus()
        bus.emit(default_snapshot())
        return bus

    bus = step(6, 'Initialising policy bus...', init_bus)
    if bus is None:
        splash.finish()
        return False  # Fatal

    # ── Step 7: Scheduling Office ─────────────────────────────────────────────
    from policy.transition import TransitionEngine
    from engine.scheduling_office import SchedulingOffice
    def init_office():
        transition = TransitionEngine(duration_ms=300)
        return SchedulingOffice(bus, transition, config=config)

    office = step(7, 'Starting scheduling office...', init_office)

    # ── Step 8: Engine ────────────────────────────────────────────────────────
    from controllers.ghost import GhostController
    from controllers.watchdog import SafetyWatchdog
    from engine.budget import BudgetGovernor

    def init_engine():
        watchdog = SafetyWatchdog()
        ghost    = GhostController()
        budget   = BudgetGovernor(
            cpu_pct=config.get('budget.cpu_pct', 5.0) if config else 5.0,
            io_pct= config.get('budget.io_pct',  5.0) if config else 5.0,
        )

        fast_ctrls = [ghost, oc_ctrl or _NoopController(),
                      watchdog]
        slow_ctrls = [ctrl for ctrl in
                      [office, scheduler, _StorageAdapter(storage)]
                      if ctrl is not None]

        from engine.loop import ExecutionEngine
        engine = ExecutionEngine(
            policy_bus=bus,
            budget_governor=budget,
            fast_controllers=fast_ctrls,
            slow_controllers=slow_ctrls,
            watchdog=watchdog,
            scanner=scanner,
        )
        engine.start()
        return engine

    engine = step(8, 'Starting engine loops...', init_engine)
    if engine is None:
        splash.finish()
        return False  # Fatal

    # ── Step 9: Process Guard ─────────────────────────────────────────────────
    from controllers.process_guard import ProcessGuard
    def init_guard():
        excluded = (config.get('process_guard.excluded_exes', [])
                    if config else [])
        window   = (config.get('process_guard.misclick_window_s', 10.0)
                    if config else 10.0)
        return ProcessGuard(excluded_exes=excluded, misclick_window_s=window)

    guard = step(9, 'Starting process guard...', init_guard)

    # ── Step 10: Tray UI ──────────────────────────────────────────────────────
    from ui.tray import TrayUI
    def init_tray():
        tray = TrayUI(engine=engine)
        tray.start()
        return tray

    tray = step(10, 'Ready.', init_tray)

    splash.finish()
    log.info('[Startup] BytFarm started successfully')

    # Keep main thread alive — tray runs in daemon thread
    _keep_alive(tray)
    return True


def _keep_alive(tray) -> None:
    """Block main thread until tray exits."""
    import time
    try:
        while True:
            time.sleep(1.0)
    except (KeyboardInterrupt, SystemExit):
        log.info('[BytFarm] Shutting down')
        if tray:
            tray.stop()


def _configure_logging() -> None:
    import os, pathlib
    log_dir = pathlib.Path(os.environ.get('APPDATA', '.')) / 'BytFarm' / 'logs'
    log_dir.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s [%(levelname)s] %(name)s: %(message)s',
        handlers=[
            logging.FileHandler(log_dir / 'bytfarm.log', encoding='utf-8'),
            logging.StreamHandler(),
        ],
    )


class _NoopController:
    """Placeholder for controllers that failed to init."""
    def should_run(self, *_): return False
    def run(self, *_):
        from controllers.base import ControllerResult
        return ControllerResult()


class _StorageAdapter:
    """Wraps FlowDirector for slow-loop integration."""
    def __init__(self, fd): self._fd = fd
    def should_run(self, *_): return True
    def run(self, metrics, policy, budget):
        from controllers.base import ControllerResult
        self._fd.flush()
        return ControllerResult()
