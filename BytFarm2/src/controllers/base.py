"""
controllers/base.py — BaseController & ControllerResult
=========================================================
Every controller inherits BaseController and returns ControllerResult.
Controllers must never write to shared state directly.
"""

from __future__ import annotations
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from policy.snapshot import PolicySnapshot
    from engine.budget import BudgetState
    from controllers.watchdog import VetoState


@dataclass
class ControllerResult:
    """
    The only sanctioned output channel from a controller.

    metrics:       Keys merged into the shared metrics dict after this tick.
                   Available to other controllers on the NEXT tick.
    veto:          VetoState — only SafetyWatchdog should set this.
    burst_request: Controller requests a budget burst. Governor checks veto
                   before granting. Alternative: call governor.request_burst()
                   directly for immediate feedback.
    burst_reason:  Human-readable reason string for audit log.
    log_entries:   Structured audit entries appended this tick.
                   Schema: {'controller': str, 'event': str,
                            'value': Any, 'level': 'info'|'warning'|'error'}
    urgent:        If True, engine re-ticks this controller next cycle
                   regardless of should_run(). Use sparingly.
    """
    metrics:       Dict[str, Any]       = field(default_factory=dict)
    veto:          Optional[Any]        = None   # VetoState | None
    burst_request: bool                 = False
    burst_reason:  str                  = ''
    log_entries:   List[Dict[str, Any]] = field(default_factory=list)
    urgent:        bool                 = False

    def log(self, controller: str, event: str,
            value: Any = None, level: str = 'info') -> None:
        """Convenience helper to append a structured log entry."""
        self.log_entries.append({
            'controller': controller,
            'event':      event,
            'value':      value,
            'level':      level,
        })


class BaseController:
    """
    Abstract base for all BytFarm controllers.

    Subclass and implement:
        should_run(metrics, policy) -> bool
        run(metrics, policy, budget) -> ControllerResult
    """

    def should_run(self, metrics: Dict[str, Any],
                   policy: 'PolicySnapshot') -> bool:
        """
        Return True if this controller should execute this tick.
        Called by ExecutionEngine before run(). If False, run() is skipped.
        """
        raise NotImplementedError(
            f'{self.__class__.__name__} must implement should_run()')

    def run(self, metrics: Dict[str, Any],
            policy: 'PolicySnapshot',
            budget: 'BudgetState') -> ControllerResult:
        """
        Execute controller logic. Return a ControllerResult.
        Must not write to shared state directly.
        Must not call platform APIs (use cached metrics from HardwareScanner).
        """
        raise NotImplementedError(
            f'{self.__class__.__name__} must implement run()')
