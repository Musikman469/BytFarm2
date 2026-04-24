"""
policy/transition.py — TransitionEngine
=========================================
Handles smooth interpolation between mode changes.
Mid-transition restarts blend from the current midpoint — no snap-back.
"""

from __future__ import annotations
import time
from typing import Any, Optional

from policy.snapshot import PolicySnapshot


def _lerp_dict(old: dict, new: dict, weight: float) -> dict:
    """
    Recursively interpolates two policy dicts.
    - Numerics:      linear interpolation
    - Lists:         always take new immediately
    - Nested dicts:  recurse
    - Everything else: snap at weight > 0.5
    """
    result = {}
    all_keys = set(old) | set(new)
    for key in all_keys:
        ov = old.get(key)
        nv = new.get(key, ov)
        if isinstance(ov, dict) and isinstance(nv, dict):
            result[key] = _lerp_dict(ov, nv, weight)               # recurse
        elif isinstance(ov, list) or isinstance(nv, list):
            result[key] = nv                                         # always new
        elif isinstance(ov, (int, float)) and isinstance(nv, (int, float)):
            result[key] = ov + (nv - ov) * weight                   # interpolate
        else:
            result[key] = nv if weight > 0.5 else ov                # snap at midpoint
    return result


class TransitionEngine:
    """
    Manages blended mode transitions over a fixed duration.

    Usage:
        engine.start(current_snap, target_snap)
        # each slow-loop tick:
        blended = engine.tick()
        if blended: bus.emit(blended)
        # blended is None once transition completes
    """

    def __init__(self, duration_ms: float = 300) -> None:
        self.duration_ms = duration_ms
        self._active: Optional[dict] = None  # {from, to, start_time}

    @property
    def in_progress(self) -> bool:
        return self._active is not None

    def start(self, from_snap: PolicySnapshot, to_snap: PolicySnapshot) -> None:
        """
        Begin a transition. If one is already active, restart from the
        current blended midpoint — never snap back to from_snap.
        """
        if self._active:
            progress = self._progress()
            from_snap = self._blend(self._active['from'], self._active['to'], progress)
        self._active = {
            'from':       from_snap,
            'to':         to_snap,
            'start_time': time.monotonic(),
        }

    def tick(self) -> Optional[PolicySnapshot]:
        """
        Returns the current blended snapshot, or None if no transition active.
        Clears _active once progress reaches 1.0.
        """
        if not self._active:
            return None
        p = self._progress()
        blended = self._blend(self._active['from'], self._active['to'], p)
        if p >= 1.0:
            self._active = None
        return blended

    def _progress(self) -> float:
        if not self._active:
            return 0.0
        elapsed_ms = (time.monotonic() - self._active['start_time']) * 1000
        return min(elapsed_ms / self.duration_ms, 1.0)

    def _blend(self, old: PolicySnapshot, new: PolicySnapshot,
               weight: float) -> PolicySnapshot:
        return PolicySnapshot(
            mode           = new.mode if weight > 0.5 else old.mode,
            workload_class = new.workload_class,
            confidence     = old.confidence + (new.confidence - old.confidence) * weight,
            transition     = {'from': old.mode, 'to': new.mode, 'progress': weight},
            ghost          = _lerp_dict(old.ghost,     new.ghost,     weight),
            oc             = _lerp_dict(old.oc,        new.oc,        weight),
            scheduler      = _lerp_dict(old.scheduler, new.scheduler, weight),
            storage        = _lerp_dict(old.storage,   new.storage,   weight),
            budgets        = old.budgets,   # budgets never interpolated
        )
