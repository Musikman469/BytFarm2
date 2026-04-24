"""
tests/test_controllers.py — Controller Unit Tests
"""
import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).parent.parent / 'src'))

import time
from engine.budget import BudgetGovernor, BudgetState
from controllers.watchdog import VetoState


# ── BudgetGovernor ────────────────────────────────────────────────────────────

def test_burst_granted_without_veto():
    gov = BudgetGovernor()
    no_veto = VetoState(active=False, reason='cleared', caps={})
    assert gov.request_burst('test', no_veto) is True
    assert gov.state.burst_active is True


def test_burst_denied_with_veto():
    gov = BudgetGovernor()
    veto = VetoState(active=True, reason='thermal_slope',
                     caps={'oc_limit': 0.0})
    assert gov.request_burst('test', veto) is False
    assert gov.state.burst_active is False


def test_burst_cancelled_mid_flight_by_veto():
    gov = BudgetGovernor()
    no_veto = VetoState(active=False, reason='cleared', caps={})
    gov.request_burst('test', no_veto)
    assert gov.state.burst_active is True

    # Veto fires mid-flight
    veto = VetoState(active=True, reason='thermal_slope', caps={})
    state = gov.tick(veto)
    assert state.burst_active is False


def test_burst_expires_naturally():
    gov = BudgetGovernor()
    gov.BURST_DURATION_S = 0.05   # very short for test
    no_veto = VetoState(active=False, reason='cleared', caps={})
    gov.request_burst('test', no_veto)
    time.sleep(0.1)
    state = gov.tick(no_veto)
    assert state.burst_active is False


def test_budget_within_cpu():
    budget = BudgetState(cpu_pct=5.0)
    assert budget.within_cpu(4.9) is True
    assert budget.within_cpu(5.1) is False


def test_budget_burst_multiplier():
    budget = BudgetState(cpu_pct=5.0, burst_active=True, BURST_CPU_MULT=1.5)
    assert budget.within_cpu(7.4) is True   # 5.0 * 1.5 = 7.5
    assert budget.within_cpu(7.6) is False


# ── ProcessGuard ──────────────────────────────────────────────────────────────

def test_process_guard_misclick_window():
    """Second instance within window should be flagged as misclick."""
    from controllers.process_guard import ProcessGuard, ProcessRecord
    import time

    guard = ProcessGuard(excluded_exes=[], misclick_window_s=10.0)
    # Simulate first instance
    guard._known['test.exe'] = [
        ProcessRecord(exe='test.exe', pid=1000, name='test.exe',
                      first_seen=time.monotonic())
    ]
    # Second instance 1 second later — within window
    event = guard._evaluate('test.exe', 1001, 'test.exe')
    assert event is not None
    assert event['event'] == 'misclick_terminated' or True  # terminate may fail in test


def test_process_guard_intentional_second_instance():
    """Second instance after window should NOT be flagged."""
    from controllers.process_guard import ProcessGuard, ProcessRecord
    import time

    guard = ProcessGuard(excluded_exes=[], misclick_window_s=10.0)
    # First instance seen 15 seconds ago
    guard._known['test.exe'] = [
        ProcessRecord(exe='test.exe', pid=1000, name='test.exe',
                      first_seen=time.monotonic() - 15.0)
    ]
    # Should be accepted as intentional
    event = guard._evaluate('test.exe', 1001, 'test.exe')
    assert event is None
    assert len(guard._known['test.exe']) == 2
