"""
controllers/watchdog.py — SafetyWatchdog & VetoState
======================================================
Highest priority controller. Unconditional veto over all others.
Always runs on the Fast Loop regardless of should_run().
Cannot be overridden by mode policy, config, or any other controller.
"""

from __future__ import annotations
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Dict, Optional

from controllers.base import BaseController, ControllerResult

log = logging.getLogger(__name__)


@dataclass
class VetoState:
    """
    Published each fast-loop tick by SafetyWatchdog.
    All controllers and BudgetGovernor must check this before acting.
    Ignoring an active veto must log a VETO_VIOLATION event.
    """
    active:    bool
    reason:    str          # 'thermal_slope' | 'ghost_oc_combined' | 'cleared'
    caps:      Dict[str, Any] = field(default_factory=dict)
    # e.g. {'oc_limit': 0.0, 'ghost_max': 0.5, 'storage_ops': False}
    timestamp: float = field(default_factory=time.time)


class SafetyWatchdog(BaseController):
    """
    Monitors thermal slope and ghost/OC combined risk.
    Emits VetoState each tick. Hysteresis prevents rapid toggling.

    Triggers:
        - cpu_temp_slope > TEMP_SLOPE_THRESHOLD (deg C/s)
        - ghost_pressure > 0.85 AND oc_risk > 0.7 simultaneously

    Exit condition:
        - Temperature drops HYSTERESIS_EXIT_C below peak veto temperature
    """

    TEMP_SLOPE_THRESHOLD = 2.0   # deg C per second
    HYSTERESIS_EXIT_C    = 5.0   # must cool by this much to exit veto

    def __init__(self) -> None:
        self._veto_active    = False
        self._veto_peak_temp = 0.0
        self._veto: VetoState = VetoState(active=False, reason='init', caps={})

    @property
    def current_veto(self) -> VetoState:
        return self._veto

    def should_run(self, metrics: dict, policy) -> bool:
        return True  # ALWAYS runs on fast loop — no exceptions

    def run(self, metrics: dict, policy, budget) -> ControllerResult:
        temp_slope = metrics.get('cpu_temp_slope', 0.0)
        ghost_p    = metrics.get('ghost_pressure',  0.0)
        oc_risk    = metrics.get('oc_risk',          0.0)
        temp       = metrics.get('cpu_temp',         0.0)

        trigger = (
            temp_slope > self.TEMP_SLOPE_THRESHOLD or
            (ghost_p > 0.85 and oc_risk > 0.7)
        )

        result = ControllerResult()

        if trigger and not self._veto_active:
            self._veto_active    = True
            self._veto_peak_temp = temp
            reason = (
                'thermal_slope' if temp_slope > self.TEMP_SLOPE_THRESHOLD
                else 'ghost_oc_combined'
            )
            self._veto = VetoState(
                active=True,
                reason=reason,
                caps={
                    'oc_limit':    0.0,
                    'ghost_max':   0.5,
                    'storage_ops': False,
                },
            )
            log.warning(f'[Watchdog] VETO engaged: {reason} '
                        f'(temp={temp:.1f}C slope={temp_slope:.2f}C/s '
                        f'ghost={ghost_p:.2f} oc_risk={oc_risk:.2f})')
            result.log('SafetyWatchdog', 'veto_engaged', {
                'reason': reason, 'temp': temp,
                'slope': temp_slope, 'ghost': ghost_p, 'oc_risk': oc_risk,
            }, level='warning')

        elif self._veto_active:
            # Hysteresis: only exit when temp drops enough below peak
            if temp <= self._veto_peak_temp - self.HYSTERESIS_EXIT_C:
                self._veto_active = False
                self._veto = VetoState(active=False, reason='cleared', caps={})
                log.info(f'[Watchdog] Veto cleared (temp={temp:.1f}C)')
                result.log('SafetyWatchdog', 'veto_cleared',
                           {'temp': temp}, level='info')

        result.veto = self._veto
        result.metrics['veto_state']  = self._veto
        result.metrics['veto_active'] = self._veto.active
        return result
