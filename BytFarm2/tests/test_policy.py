"""
tests/test_policy.py — Policy System Tests
"""
import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).parent.parent / 'src'))

import time
from policy.snapshot import PolicySnapshot, PolicyBus, default_snapshot


def test_snapshot_is_frozen():
    snap = default_snapshot()
    try:
        snap.mode = 'Build-Storm'
        assert False, "Should have raised FrozenInstanceError"
    except Exception:
        pass


def test_policy_bus_emit_and_latest():
    bus = PolicyBus()
    assert bus.latest is None
    snap = default_snapshot()
    bus.emit(snap)
    assert bus.latest is snap


def test_policy_bus_history_limit():
    bus = PolicyBus()
    for _ in range(150):
        bus.emit(default_snapshot())
    assert len(bus.history()) <= 100


def test_replay():
    bus = PolicyBus()
    snaps = [default_snapshot() for _ in range(5)]
    bus.replay(snaps)
    assert len(bus.history()) == 5
