"""
tests/test_transition.py — TransitionEngine Tests
"""
import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).parent.parent / 'src'))

import time
from policy.snapshot import PolicySnapshot, default_snapshot
from policy.transition import TransitionEngine, _lerp_dict


def _snap(mode):
    return PolicySnapshot(
        mode=mode, workload_class=mode, confidence=1.0,
        ghost={'max_stored': 800.0 if mode == 'Frame-Tight' else 1200.0},
    )


def test_lerp_dict_numeric():
    old = {'a': 0.0}
    new = {'a': 100.0}
    result = _lerp_dict(old, new, 0.5)
    assert result['a'] == 50.0


def test_lerp_dict_list_always_new():
    old = {'items': [1, 2, 3]}
    new = {'items': [4, 5, 6]}
    result = _lerp_dict(old, new, 0.1)   # even at low weight, list = new
    assert result['items'] == [4, 5, 6]


def test_lerp_dict_nested():
    old = {'inner': {'val': 0.0}}
    new = {'inner': {'val': 10.0}}
    result = _lerp_dict(old, new, 1.0)
    assert result['inner']['val'] == 10.0


def test_transition_completes():
    engine = TransitionEngine(duration_ms=1)  # 1ms — completes immediately
    a = _snap('Frame-Tight')
    b = _snap('Build-Storm')
    engine.start(a, b)
    time.sleep(0.01)
    result = engine.tick()
    assert result is not None
    assert result.mode == 'Build-Storm'
    # Should be done now
    assert engine.tick() is None


def test_mid_transition_restart_no_snapback():
    """Starting a new transition mid-flight must not snap back to original from."""
    engine = TransitionEngine(duration_ms=5000)  # long — won't complete during test
    a = _snap('Frame-Tight')
    b = _snap('Build-Storm')
    engine.start(a, b)

    # Tick once to get a midpoint
    mid = engine.tick()
    assert mid is not None
    assert mid.transition.get('progress', 0) < 1.0

    # Start a new transition — should blend from midpoint, not from 'a'
    c = _snap('Battery-Guard')
    engine.start(mid, c)
    assert engine._active['from'].mode == mid.mode
