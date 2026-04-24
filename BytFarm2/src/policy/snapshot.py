"""
policy/snapshot.py — PolicySnapshot & PolicyBus
=================================================
The immutable source of truth consumed by all controllers.
All controllers read from PolicyBus.latest; none write to it.
"""

from __future__ import annotations
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass(frozen=True)
class PolicySnapshot:
    """
    Immutable policy state emitted each slow-loop tick.
    Once created, cannot be modified. Safe to share across threads.
    """
    mode:           str
    workload_class: str
    confidence:     float
    timestamp:      float = field(default_factory=time.time)

    # Transition metadata: {'from': str, 'to': str, 'progress': float 0-1}
    # Empty dict when no transition is active.
    transition: dict = field(default_factory=dict)

    # Per-controller policy dicts — populated by Scheduling Office
    ghost:     Dict[str, Any] = field(default_factory=dict)
    oc:        Dict[str, Any] = field(default_factory=dict)
    scheduler: Dict[str, Any] = field(default_factory=dict)
    storage:   Dict[str, Any] = field(default_factory=dict)
    budgets:   Dict[str, Any] = field(default_factory=dict)


# ── Default snapshot emitted at startup before Office is ready ────────────────
def default_snapshot() -> PolicySnapshot:
    return PolicySnapshot(
        mode='Frame-Tight',
        workload_class='interactive',
        confidence=1.0,
        ghost={
            'max_stored': 800.0,
            'decay_rate': 0.08,
        },
        oc={
            'headroom_pct': 15.0,
            'mode_cap': 'monitor_only',  # safe default until OC controller init
        },
        scheduler={
            'foreground_boost': 'HIGH',
            'background_suppression': 'strong',
        },
        storage={
            'vram_runway_mb': 512,
            'batch_flush_mb': 4,
            'aggressiveness': 'low',
        },
        budgets={
            'cpu_pct': 5.0,
            'io_pct': 5.0,
        },
    )


# ── Policy Bus ────────────────────────────────────────────────────────────────
class PolicyBus:
    """
    Thread-safe distribution hub for PolicySnapshots.

    Fast Loop (read): call bus.latest — acquires lock, returns frozen ref, releases.
    Slow Loop (write): call bus.emit(snapshot).

    Controllers must NOT hold the reference across their full run() call for
    mutation purposes — snapshots are frozen so this is safe, but the pattern
    to follow is: snap = bus.latest, then use snap locally.
    """

    MAX_HISTORY = 100

    def __init__(self) -> None:
        self._current: Optional[PolicySnapshot] = None
        self._history: List[PolicySnapshot] = []
        self._lock = threading.Lock()

    def emit(self, snapshot: PolicySnapshot) -> None:
        """Publish a new snapshot. Called by Scheduling Office on slow loop."""
        with self._lock:
            self._current = snapshot
            self._history.append(snapshot)
            if len(self._history) > self.MAX_HISTORY:
                self._history.pop(0)

    @property
    def latest(self) -> Optional[PolicySnapshot]:
        """
        Returns the current snapshot. Thread-safe.
        Returns None only before the first emit — callers should handle this.
        """
        with self._lock:
            return self._current  # frozen dataclass — safe to return reference

    def history(self) -> List[PolicySnapshot]:
        """Returns a copy of the snapshot history for audit/replay."""
        with self._lock:
            return list(self._history)

    def replay(self, snapshots: List[PolicySnapshot]) -> None:
        """
        Feed a recorded sequence back into the bus.
        Used for deterministic regression testing.
        """
        for snap in snapshots:
            self.emit(snap)
