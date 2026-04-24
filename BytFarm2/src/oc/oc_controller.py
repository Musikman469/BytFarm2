"""
controllers/oc_controller.py — OCController
=============================================
Detects available OC capability at startup and operates at the highest
safe level the machine supports. Never exceeds policy headroom limits.
Defers unconditionally to SafetyWatchdog veto.

Capability tiers (highest to lowest):
    pl1pl2        — Direct MSR writes via WinRing0 (bundled in vendor/)
    external_tool — Subprocess call to XTU (Intel) or Ryzen Master (AMD)
    monitor_only  — Read-only: tracks headroom, caps on risk threshold
"""

from __future__ import annotations
import ctypes
import logging
import os
import pathlib
import subprocess
import sys
import winreg
from typing import Optional

import psutil

from controllers.base import BaseController, ControllerResult

log = logging.getLogger(__name__)

def _runtime_root() -> pathlib.Path:
    if getattr(sys, 'frozen', False):
        meipass = getattr(sys, '_MEIPASS', None)
        if meipass:
            return pathlib.Path(meipass)
        return pathlib.Path(sys.executable).resolve().parent
    return pathlib.Path(__file__).resolve().parent.parent.parent


def _winring_candidates() -> list[pathlib.Path]:
    root = _runtime_root()
    candidates = [root / 'vendor' / 'WinRing0x64.dll']
    if getattr(sys, 'frozen', False):
        candidates.append(pathlib.Path(sys.executable).resolve().parent / 'vendor' / 'WinRing0x64.dll')
    return candidates

CAPABILITY_RANK = {'pl1pl2': 2, 'external_tool': 1, 'monitor_only': 0}

# MSR addresses for power limits
MSR_PKG_POWER_LIMIT_INTEL = 0x610
MSR_PKG_POWER_LIMIT_AMD   = 0xC0010299


def min_capability(hardware: str, policy_cap: str) -> str:
    """
    Returns the lower of two OC capability tiers.
    Mode cap acts as ceiling; hardware capability is hard limit.
    If policy wants more than hardware can deliver, triggers one-time UI notice.
    """
    hw_rank  = CAPABILITY_RANK.get(hardware,    0)
    pol_rank = CAPABILITY_RANK.get(policy_cap,  0)

    if pol_rank > hw_rank:
        _notify_oc_upgrade(hardware, policy_cap)

    result_rank = min(hw_rank, pol_rank)
    rank_to_name = {v: k for k, v in CAPABILITY_RANK.items()}
    return rank_to_name[result_rank]


def _notify_oc_upgrade(current: str, requested: str) -> None:
    """Show a one-time UI notice suggesting the appropriate tool install."""
    vendor = _detect_cpu_vendor()
    tool   = 'Intel XTU' if vendor == 'intel' else 'AMD Ryzen Master'
    url    = (
        'https://www.intel.com/content/www/us/en/download/17881/intel-extreme-tuning-utility-intel-xtu.html'
        if vendor == 'intel'
        else 'https://www.amd.com/en/technologies/ryzen-master'
    )
    # Import deferred to avoid circular dependency at module load time
    try:
        from ui.tray import TrayUI
        TrayUI.notify_once(
            'oc_upgrade',
            f'Install {tool} to unlock better OC control.\n{url}'
        )
    except Exception:
        log.info(f'[OCController] OC upgrade available: install {tool} ({url})')


def _detect_cpu_vendor() -> str:
    """Returns 'intel' or 'amd'."""
    try:
        # Avoid cpuinfo's WMIC subprocess path in frozen apps.
        brand = (
            os.environ.get('PROCESSOR_IDENTIFIER', '')
            or os.environ.get('PROCESSOR_ARCHITECTURE', '')
        ).lower()
        return 'amd' if 'amd' in brand else 'intel'
    except Exception:
        return 'intel'  # safe default


class OCController(BaseController):
    """
    Fast-loop controller. Always runs.
    Detects capability once at init; applies it every tick.
    """

    def __init__(self) -> None:
        self._vendor     = _detect_cpu_vendor()
        self._capability = self._detect_capability()
        self._tool       = self._tool_available()
        self.oc_risk     = 0.0
        log.info(f'[OCController] vendor={self._vendor} '
                 f'capability={self._capability} tool={self._tool}')

    def should_run(self, metrics: dict, policy) -> bool:
        return True  # always runs on fast loop

    def run(self, metrics: dict, policy, budget) -> ControllerResult:
        veto        = metrics.get('veto_state')
        headroom    = policy.oc.get('headroom_pct', 0.0)
        policy_cap  = policy.oc.get('mode_cap', 'monitor_only')

        # Watchdog veto: immediately drop to monitor_only
        if veto and veto.active:
            effective_cap = 'monitor_only'
        else:
            effective_cap = min_capability(self._capability, policy_cap)

        self.oc_risk = self._compute_risk(metrics, headroom)

        if effective_cap == 'pl1pl2':
            self._apply_pl1pl2(headroom, metrics)
        elif effective_cap == 'external_tool':
            self._call_external_tool(headroom)
        # monitor_only: no writes, just update oc_risk

        result = ControllerResult(metrics={
            'oc_risk':       self.oc_risk,
            'oc_capability': self._capability,
        })

        if self.oc_risk > 0.8:
            result.log('OCController', 'risk_high',
                       {'oc_risk': self.oc_risk, 'headroom': headroom},
                       level='warning')
        return result

    # ── Capability detection ──────────────────────────────────────────────────

    def _detect_capability(self) -> str:
        if self._can_write_msr():
            return 'pl1pl2'
        if self._tool_available():
            return 'external_tool'
        return 'monitor_only'

    def _can_write_msr(self) -> bool:
        """Admin rights + WinRing0 DLL present."""
        is_admin = ctypes.windll.shell32.IsUserAnAdmin() != 0
        dll_path = next((p for p in _winring_candidates() if p.exists()), None)
        has_dll = dll_path is not None
        self._winring_path = dll_path
        if not is_admin:
            log.debug('[OCController] No admin rights — cannot use pl1pl2')
        if not has_dll:
            log.debug('[OCController] WinRing0 not found in runtime vendor directory')
        return is_admin and has_dll

    def _tool_available(self) -> Optional[str]:
        """
        Returns 'xtu', 'ryzenmaster', or None.
        Checks primary vendor first, falls back to other vendor's tool.
        """
        def check_xtu() -> bool:
            try:
                winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                               r'SOFTWARE\Intel\XTU')
                return True
            except FileNotFoundError:
                return False

        def check_ryzenmaster() -> bool:
            return pathlib.Path(
                r'C:\Program Files\AMD\RyzenMaster').exists()

        if self._vendor == 'intel':
            if check_xtu():          return 'xtu'
            if check_ryzenmaster():  return 'ryzenmaster'
        else:
            if check_ryzenmaster():  return 'ryzenmaster'
            if check_xtu():          return 'xtu'

        return None

    # ── Risk calculation ──────────────────────────────────────────────────────

    def _compute_risk(self, metrics: dict, headroom: float) -> float:
        temp_factor  = min(metrics.get('cpu_temp', 0.0) / 100.0, 1.0)
        ghost_factor = metrics.get('ghost_pressure', 0.0)
        raw = (temp_factor * 0.6 + ghost_factor * 0.4) / max(headroom / 100.0, 0.01)
        return min(raw, 1.0)

    # ── Hardware writes ───────────────────────────────────────────────────────

    def _apply_pl1pl2(self, headroom_pct: float, metrics: dict) -> None:
        """Write PL1/PL2 power limits via WinRing0."""
        try:
            dll_path = getattr(self, '_winring_path', None)
            if not dll_path:
                return
            ring0    = ctypes.WinDLL(str(dll_path))
            tdp_w    = metrics.get('cpu_tdp_w', 65)
            pl1      = int(tdp_w * (1.0 + headroom_pct / 100.0))
            pl2      = int(pl1 * 1.25)
            msr_addr = (MSR_PKG_POWER_LIMIT_INTEL
                        if self._vendor == 'intel'
                        else MSR_PKG_POWER_LIMIT_AMD)
            # Encode: bits[14:0]=PL1/8W, bit15=enable, bits[23:17]=tau,
            #         bits[46:32]=PL2/8W, bit47=enable
            pl1_encoded = (int(pl1 * 8) & 0x7FFF) | (1 << 15)
            pl2_encoded = (int(pl2 * 8) & 0x7FFF) | (1 << 15)
            value = pl1_encoded | (pl2_encoded << 32)
            ring0.WriteMsr(msr_addr, value)
        except Exception as e:
            log.warning(f'[OCController] pl1pl2 write failed: {e}')

    def _call_external_tool(self, headroom_pct: float) -> None:
        """Invoke XTU or Ryzen Master CLI to apply power limit."""
        try:
            if self._tool == 'xtu':
                subprocess.run(
                    [r'C:\Program Files\Intel\XTU\xtu.exe',
                     '-t', str(int(headroom_pct))],
                    timeout=5, capture_output=True,
                )
            elif self._tool == 'ryzenmaster':
                subprocess.run(
                    [r'C:\Program Files\AMD\RyzenMaster\RyzenMaster.exe',
                     '--set-tdp', str(int(headroom_pct))],
                    timeout=5, capture_output=True,
                )
        except Exception as e:
            log.warning(f'[OCController] external tool call failed: {e}')
