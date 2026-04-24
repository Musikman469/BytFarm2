"""
engine/budget.py — BudgetState & BudgetGovernor
=================================================
Cross-controller budget enforcement.
Controllers check budget before acting; never write to it directly.
"""

from __future__ import annotations
import logging
import time
from dataclasses import dataclass, field
from typing import Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from controllers.watchdog import VetoState

log = logging.getLogger(__name__)


@dataclass
class BudgetState:
    """
    Mutable budget state owned exclusively by BudgetGovernor.
    Controllers receive this as a read-only argument.
    """
    cpu_pct:          float = 5.0   # max % CPU the engine may consume
    io_pct:           float = 5.0   # max % I/O the engine may consume
    thermal_headroom: float = 0.0   # deg C below thermal limit
    burst_active:     bool  = False
    burst_expires_at: float = 0.0   # monotonic timestamp
    BURST_CPU_MULT:   float = 1.5   # multiplier during burst

    def within_cpu(self, requested: float) -> bool:
        limit = self.cpu_pct * (self.BURST_CPU_MULT if self.burst_active else 1.0)
        return requested <= limit

    def within_io(self, requested: float) -> bool:
        return requested <= self.io_pct


class BudgetGovernor:
    """
    Manages global engine budgets and burst overrides.
    Burst grants are subject to unconditional SafetyWatchdog veto.
    The Watchdog can cancel an active burst mid-flight.
    """

    BURST_DURATION_S = 5.0

    def __init__(self, cpu_pct: float = 5.0, io_pct: float = 5.0) -> None:
        self._state = BudgetState(cpu_pct=cpu_pct, io_pct=io_pct)

    @property
    def state(self) -> BudgetState:
        return self._state

    def update_from_config(self, cpu_pct: float, io_pct: float) -> None:
        """Hot-reload safe — called when config changes."""
        self._state.cpu_pct = cpu_pct
        self._state.io_pct  = io_pct

    def request_burst(self, reason: str,
                      veto: Optional['VetoState']) -> bool:
        """
        Request a temporary burst budget.
        Returns True if granted, False if denied (veto active).
        Burst allows BURST_CPU_MULT x normal cpu_pct for BURST_DURATION_S seconds.
        """
        if veto and veto.active:
            self._log_burst_denied(reason, veto.reason)
            return False
        self._state.burst_active     = True
        self._state.burst_expires_at = time.monotonic() + self.BURST_DURATION_S
        log.info(f'[BudgetGovernor] Burst granted: {reason}')
        return True

    def tick(self, veto: Optional['VetoState']) -> BudgetState:
        """
        Called each engine tick. Applies veto cancellation and expiry.
        Returns current BudgetState.
        """
        # Veto unconditionally cancels active burst mid-flight
        if veto and veto.active and self._state.burst_active:
            self._state.burst_active = False
            log.info('[BudgetGovernor] Burst cancelled by watchdog veto')

        # Natural expiry
        if self._state.burst_active:
            if time.monotonic() > self._state.burst_expires_at:
                self._state.burst_active = False

        return self._state

    def _log_burst_denied(self, reason: str, veto_reason: str) -> None:
        log.warning(
            f'[BudgetGovernor] Burst denied: {reason} '
            f'(veto active: {veto_reason})'
        )
