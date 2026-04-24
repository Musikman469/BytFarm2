"""
controllers/ghost.py — GhostController
========================================
Manages ghost memory allocation with mode-dependent decay and debt tracking.
Ghost Pressure (0.0–1.0) is a first-class output metric consumed by
StorageController and SafetyWatchdog.
"""

from __future__ import annotations
import logging

from controllers.base import BaseController, ControllerResult

log = logging.getLogger(__name__)

DECAY_RATES = {
    'Frame-Tight':   0.08,   # moderate — balanced responsiveness
    'Build-Storm':   0.04,   # slow — preserve ghost during long builds
    'Battery-Guard': 0.20,   # aggressive — reclaim resources fast
    'Stream-Smooth': 0.10,   # medium — steady throughput
}


class GhostController(BaseController):
    """
    Fast-loop controller. Runs only when ghost is active or stored > 0.

    Outputs (via ControllerResult.metrics):
        ghost_stored:    current stored ghost MB
        ghost_debt:      accumulated demand shortfall MB
        ghost_pressure:  0.0–1.0 pressure metric
    """

    def __init__(self) -> None:
        self.ghost_stored   = 0.0
        self.ghost_debt     = 0.0
        self.ghost_pressure = 0.0

    def should_run(self, metrics: dict, policy) -> bool:
        return metrics.get('ghost_active', False) or self.ghost_stored > 0

    def run(self, metrics: dict, policy, budget) -> ControllerResult:
        dt     = metrics.get('dt', 0.016)
        decay  = DECAY_RATES.get(policy.mode, 0.10)
        demand = metrics.get('ghost_demand', 0.0)
        supply = metrics.get('ghost_supply', 0.0)

        # Clamp by watchdog ghost cap if veto active
        veto = metrics.get('veto_state')
        if veto and veto.active:
            ghost_cap = veto.caps.get('ghost_max', 1.0)
            limit_abs = policy.ghost.get('max_stored', 800.0) * ghost_cap
        else:
            limit_abs = policy.ghost.get('max_stored', 800.0)

        # Decay
        if self.ghost_stored > 0:
            self.ghost_stored = max(self.ghost_stored - (decay * dt), 0.0)

        # Debt vs supply
        if demand > supply:
            self.ghost_debt += (demand - supply)
            gain = supply * 0.5   # efficiency penalty under pressure
        else:
            self.ghost_debt = max(
                self.ghost_debt - (supply - demand) * 0.5, 0.0)
            gain = supply

        self.ghost_stored = min(self.ghost_stored + gain, limit_abs)

        # Ghost Pressure: 0.0–1.0 combining stored + debt
        self.ghost_pressure = min(
            (self.ghost_stored + self.ghost_debt * 0.5) / max(limit_abs, 1.0),
            1.0,
        )

        result = ControllerResult(metrics={
            'ghost_stored':   self.ghost_stored,
            'ghost_debt':     self.ghost_debt,
            'ghost_pressure': self.ghost_pressure,
            'ghost_active':   self.ghost_stored > 0 or demand > 0,
        })

        if self.ghost_pressure > 0.85:
            result.log('GhostController', 'pressure_high',
                       {'pressure': self.ghost_pressure,
                        'stored': self.ghost_stored,
                        'debt': self.ghost_debt}, level='warning')

        return result
