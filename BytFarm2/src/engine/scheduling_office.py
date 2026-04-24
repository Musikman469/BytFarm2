"""
engine/scheduling_office.py — Scheduling Office
=================================================
THREE responsibilities only:
    1. Classify workload
    2. Select mode
    3. Emit PolicySnapshot via PolicyBus

Does NOT enforce budgets (BudgetGovernor).
Does NOT write audit logs (LoggingSubsystem).
"""

from __future__ import annotations
import logging
import time
from collections import Counter
from typing import Dict, List, Optional, Tuple

from policy.snapshot import PolicySnapshot, default_snapshot
from policy.transition import TransitionEngine
from engine.budget import BudgetGovernor

log = logging.getLogger(__name__)

# ── Mode policy templates — all values tunable via config ────────────────────
MODE_DEFAULTS: Dict[str, dict] = {
    'Frame-Tight': {
        'ghost':     {'max_stored': 800.0,  'decay_rate': 0.08},
        'oc':        {'headroom_pct': 15.0, 'mode_cap': 'pl1pl2'},
        'scheduler': {'foreground_boost': 'HIGH', 'background_suppression': 'strong'},
        'storage':   {'vram_runway_mb': 512, 'batch_flush_mb': 4, 'aggressiveness': 'low'},
        'budgets':   {'cpu_pct': 5.0, 'io_pct': 5.0},
    },
    'Build-Storm': {
        'ghost':     {'max_stored': 1200.0, 'decay_rate': 0.04},
        'oc':        {'headroom_pct': 10.0, 'mode_cap': 'external_tool'},
        'scheduler': {'foreground_boost': 'ABOVE_NORMAL', 'background_suppression': 'moderate'},
        'storage':   {'vram_runway_mb': 1024, 'batch_flush_mb': 8, 'aggressiveness': 'medium'},
        'budgets':   {'cpu_pct': 5.0, 'io_pct': 5.0},
    },
    'Battery-Guard': {
        'ghost':     {'max_stored': 200.0,  'decay_rate': 0.20},
        'oc':        {'headroom_pct': 0.0,  'mode_cap': 'monitor_only'},
        'scheduler': {'foreground_boost': 'NORMAL', 'background_suppression': 'strong'},
        'storage':   {'vram_runway_mb': 128, 'batch_flush_mb': 2, 'aggressiveness': 'minimal'},
        'budgets':   {'cpu_pct': 3.0, 'io_pct': 3.0},
    },
    'Stream-Smooth': {
        'ghost':     {'max_stored': 600.0,  'decay_rate': 0.10},
        'oc':        {'headroom_pct': 5.0,  'mode_cap': 'external_tool'},
        'scheduler': {'foreground_boost': 'ABOVE_NORMAL', 'background_suppression': 'moderate'},
        'storage':   {'vram_runway_mb': 512, 'batch_flush_mb': 4, 'aggressiveness': 'medium'},
        'budgets':   {'cpu_pct': 5.0, 'io_pct': 5.0},
    },
}


class WorkloadClassifier:
    """
    Classifies current workload combining process hints, CPU, GPU,
    thread count, and I/O. History booster prevents flip-flopping.
    """

    HISTORY_WINDOW_S     = 30.0
    HYSTERESIS_THRESHOLD = 0.6

    GAME_HINTS  = {'game', 'unity', 'unreal', 'd3d', 'vulkan', 'dxgi',
                   'steam', 'epicgames', 'battlenet'}
    BUILD_HINTS = {'msbuild', 'cl.exe', 'gcc', 'g++', 'cargo', 'gradle',
                   'webpack', 'tsc', 'dotnet', 'ninja', 'cmake'}

    def __init__(self) -> None:
        self._history: List[Tuple[float, str]] = []

    def classify(self, metrics: dict) -> dict:
        raw_class, raw_conf = self._raw_classify(metrics)
        boosted = self._apply_history_boost(raw_class, raw_conf)
        final   = (raw_class
                   if boosted >= self.HYSTERESIS_THRESHOLD
                   else self._dominant_history())
        self._record(final)
        return {'class': final, 'confidence': round(boosted, 3)}

    def _raw_classify(self, m: dict) -> Tuple[str, float]:
        cpu     = m.get('cpu_total',  0)
        gpu     = m.get('gpu_util',   0)
        threads = m.get('thread_count', 0)
        io      = m.get('io_load',    0)
        fg      = m.get('foreground_process', '').lower()

        if any(h in fg for h in self.GAME_HINTS):
            return 'Frame-Tight', 0.85
        if any(h in fg for h in self.BUILD_HINTS):
            return 'Build-Storm', 0.90

        if cpu > 80 or (cpu > 60 and threads > 16):
            return 'Build-Storm', 0.80
        if gpu > 60 or (cpu > 40 and gpu > 30):
            return 'Frame-Tight', 0.75
        if io > 40:
            return 'Stream-Smooth', 0.70

        return 'Frame-Tight', 0.55

    def _apply_history_boost(self, cls: str, conf: float) -> float:
        now    = time.monotonic()
        recent = [c for t, c in self._history
                  if now - t < self.HISTORY_WINDOW_S]
        if not recent:
            return conf
        match_ratio = recent.count(cls) / len(recent)
        return min(conf + 0.15 * match_ratio, 1.0)

    def _dominant_history(self) -> str:
        if not self._history:
            return 'Frame-Tight'
        return Counter(c for _, c in self._history[-10:]).most_common(1)[0][0]

    def _record(self, cls: str) -> None:
        self._history.append((time.monotonic(), cls))
        cutoff = time.monotonic() - self.HISTORY_WINDOW_S * 2
        self._history = [(t, c) for t, c in self._history if t > cutoff]


class SchedulingOffice:
    """
    Meta-scheduler. Runs on the Slow Loop via should_run() delta gating.
    """

    CPU_DELTA  = 5.0
    TEMP_DELTA = 2.0
    RAM_DELTA  = 3.0

    def __init__(self, bus, transition: TransitionEngine,
                 classifier: Optional[WorkloadClassifier] = None,
                 config=None) -> None:
        self._bus        = bus
        self._transition = transition
        self._classifier = classifier or WorkloadClassifier()
        self._config     = config
        self._last_metrics: dict = {}
        self._current_mode = 'Frame-Tight'

    def should_run(self, metrics: dict, policy: Optional[PolicySnapshot]) -> bool:
        if not self._last_metrics:
            return True
        return (
            abs(metrics.get('cpu_total', 0) - self._last_metrics.get('cpu_total', 0)) > self.CPU_DELTA  or
            abs(metrics.get('cpu_temp',  0) - self._last_metrics.get('cpu_temp',  0)) > self.TEMP_DELTA or
            abs(metrics.get('ram_used',  0) - self._last_metrics.get('ram_used',  0)) > self.RAM_DELTA
        )

    def tick(self, metrics: dict,
             policy: Optional[PolicySnapshot],
             budget) -> PolicySnapshot:
        """Called by slow loop. Returns new or blended snapshot."""
        self._last_metrics = dict(metrics)

        result     = self._classifier.classify(metrics)
        new_mode   = result['class']
        confidence = result['confidence']

        # Start a transition if mode changed
        if policy and new_mode != self._current_mode:
            log.info(f'[Office] Mode change: {self._current_mode} → {new_mode} '
                     f'(confidence={confidence})')
            target = self._build_snapshot(new_mode, confidence, metrics)
            self._transition.start(policy, target)
            self._current_mode = new_mode

        # Emit blended or final snapshot
        snap = (self._transition.tick()
                or self._build_snapshot(new_mode, confidence, metrics))
        self._bus.emit(snap)
        return snap

    def _build_snapshot(self, mode: str, confidence: float,
                        metrics: dict) -> PolicySnapshot:
        """Build a full snapshot from mode defaults + config overrides."""
        defaults = MODE_DEFAULTS.get(mode, MODE_DEFAULTS['Frame-Tight'])

        def cfg(key: str, fallback):
            if self._config:
                return self._config.get(f'modes.{mode}.{key}', fallback)
            return fallback

        ghost = dict(defaults['ghost'])
        ghost['max_stored'] = cfg('ghost_max_stored_mb', ghost['max_stored'])

        storage = dict(defaults['storage'])
        storage['vram_runway_mb'] = cfg('vram_runway_mb', storage['vram_runway_mb'])
        storage['batch_flush_mb'] = cfg('batch_flush_mb', storage['batch_flush_mb'])

        oc = dict(defaults['oc'])
        oc['headroom_pct'] = cfg('oc_headroom_pct', oc['headroom_pct'])

        budgets = dict(defaults['budgets'])
        if self._config:
            budgets['cpu_pct'] = self._config.get('budget.cpu_pct', budgets['cpu_pct'])
            budgets['io_pct']  = self._config.get('budget.io_pct',  budgets['io_pct'])

        return PolicySnapshot(
            mode=mode,
            workload_class=mode,
            confidence=confidence,
            ghost=ghost,
            oc=oc,
            scheduler=dict(defaults['scheduler']),
            storage=storage,
            budgets=budgets,
        )
