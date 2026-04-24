"""
controllers/hardware_scanner.py — HardwareScanner
===================================================
Owns ALL hardware reads. Uses psutil for CPU/RAM/disk/process data
and WMI for temperatures and GPU utilisation.

Runs on the Slow Loop. Caches results thread-safely.
Fast-loop controllers call get_cached() — never platform APIs directly.

WMI requires OpenHardwareMonitor or LibreHardwareMonitor to be running
for full sensor data. Falls back to CIMV2 thermal zones if not available,
logging a startup warning.
"""

from __future__ import annotations
import ctypes
import logging
import threading
import time
from typing import Dict, Any, List, Optional, Tuple

import psutil

log = logging.getLogger(__name__)


class HardwareScanner:
    """
    Slow-loop hardware data collector with fast-loop safe cache.

    All keys it populates match the Metrics Schema in the spec (Section 4).
    Keys injected by ExecutionEngine (dt, cpu_temp_slope) are handled
    separately in engine/loop.py.
    """

    WMI_NAMESPACE_OHM   = 'root/OpenHardwareMonitor'
    WMI_NAMESPACE_CIMV2 = 'root/CIMV2'
    TEMP_SLOPE_WINDOW_S = 5.0  # rolling window for slope calculation

    def __init__(self) -> None:
        self._cache: Dict[str, Any] = self._empty_metrics()
        self._lock  = threading.Lock()
        self._temp_history: List[Tuple[float, float]] = []  # (monotonic, temp)
        # self._wmi   = None
        # self._wmi_source = 'none'
        # self._init_wmi()

        # Prime psutil CPU percent (first call always returns 0.0)
        psutil.cpu_percent(interval=None)
        psutil.cpu_percent(interval=None, percpu=True)

    # ── Public API ────────────────────────────────────────────────────────────

    def scan(self) -> Dict[str, Any]:
        """
        Full hardware scan. Called once per slow-loop tick.
        Updates internal cache. Returns the new metrics dict.
        """
        data = self._empty_metrics()

        # psutil reads
        data['cpu_total']    = psutil.cpu_percent(interval=None)
        data['cpu_per_core'] = psutil.cpu_percent(interval=None, percpu=True)

        vm = psutil.virtual_memory()
        data['ram_used']  = vm.used  / 1024 ** 2
        data['ram_total'] = vm.total / 1024 ** 2

        try:
            io = psutil.disk_io_counters()
            data['io_load'] = (io.read_bytes + io.write_bytes) / 1024 ** 2
        except Exception:
            data['io_load'] = 0.0

        try:
            data['thread_count'] = sum(
                p.info.get('num_threads', 0)
                for p in psutil.process_iter(['num_threads'])
            )
        except Exception:
            data['thread_count'] = 0

        data['foreground_process'] = self._get_foreground_exe()

        # WMI reads (temperatures, GPU)
        data['cpu_temp'], data['gpu_util'] = self._read_wmi_sensors()

        with self._lock:
            self._cache = data

        return data

    def get_cached(self) -> Dict[str, Any]:
        """
        Fast-loop safe. Returns a shallow copy of the last scanned values.
        Never calls platform APIs.
        """
        with self._lock:
            return dict(self._cache)

    def compute_temp_slope(self, temp: float) -> float:
        """
        Computes deg C per second over a 5-second rolling window.
        Called by ExecutionEngine after scan() to inject into metrics.
        """
        now = time.monotonic()
        self._temp_history.append((now, temp))
        cutoff = now - self.TEMP_SLOPE_WINDOW_S
        self._temp_history = [(t, v) for t, v in self._temp_history if t > cutoff]
        if len(self._temp_history) < 2:
            return 0.0
        dt = self._temp_history[-1][0] - self._temp_history[0][0]
        dv = self._temp_history[-1][1] - self._temp_history[0][1]
        return dv / dt if dt > 0 else 0.0

    # ── WMI init ──────────────────────────────────────────────────────────────

    def _init_wmi(self) -> None:
        try:
            import wmi
            # Try OpenHardwareMonitor namespace first (full sensor data)
            try:
                self._wmi = wmi.WMI(namespace=self.WMI_NAMESPACE_OHM)
                # Test query to confirm sensors exist
                _ = self._wmi.Sensor()
                self._wmi_source = 'ohm'
                log.info('[HardwareScanner] WMI: using OpenHardwareMonitor sensors')
            except Exception:
                # Fall back to CIMV2 thermal zones (less accurate)
                self._wmi = wmi.WMI(namespace=self.WMI_NAMESPACE_CIMV2)
                self._wmi_source = 'cimv2'
                log.warning(
                    '[HardwareScanner] OpenHardwareMonitor not found. '
                    'Falling back to CIMV2 thermal zones — temperature accuracy '
                    'will be reduced. Install LibreHardwareMonitor for full data: '
                    'https://github.com/LibreHardwareMonitor/LibreHardwareMonitor/releases'
                )
        except ImportError:
            log.error('[HardwareScanner] wmi package not installed. '
                      'Run: pip install wmi')
            self._wmi = None
            self._wmi_source = 'none'
        except Exception as e:
            log.error(f'[HardwareScanner] WMI init failed: {e}')
            self._wmi = None
            self._wmi_source = 'none'

    def _read_wmi_sensors(self) -> Tuple[float, float]:
        """Returns (cpu_temp, gpu_util). Both default to 0.0 on failure."""
        cpu_temp = 0.0
        gpu_util = 0.0

        if not self._wmi:
            return cpu_temp, gpu_util

        try:
            if self._wmi_source == 'ohm':
                for s in self._wmi.Sensor():
                    if s.SensorType == 'Temperature' and 'CPU' in s.Name:
                        cpu_temp = float(s.Value)
                    if s.SensorType == 'Load' and 'GPU' in s.Name:
                        gpu_util = float(s.Value)

            elif self._wmi_source == 'cimv2':
                # CIMV2 thermal zones report in tenths of Kelvin
                for zone in self._wmi.MSAcpi_ThermalZoneTemperature():
                    kelvin = zone.CurrentTemperature / 10.0
                    cpu_temp = kelvin - 273.15
                    break  # take first zone

        except Exception as e:
            log.debug(f'[HardwareScanner] WMI sensor read failed: {e}')

        return cpu_temp, gpu_util

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _get_foreground_exe(self) -> str:
        """Returns the exe name of the currently focused window."""
        try:
            hwnd = ctypes.windll.user32.GetForegroundWindow()
            pid  = ctypes.c_ulong()
            ctypes.windll.user32.GetWindowThreadProcessId(
                hwnd, ctypes.byref(pid))
            return psutil.Process(pid.value).name()
        except Exception:
            return ''

    @staticmethod
    def _empty_metrics() -> Dict[str, Any]:
        return {
            'cpu_total':         0.0,
            'cpu_per_core':      [],
            'cpu_temp':          0.0,
            'gpu_util':          0.0,
            'io_load':           0.0,
            'ram_used':          0.0,
            'ram_total':         0.0,
            'ghost_demand':      0.0,
            'ghost_supply':      0.0,
            'ghost_active':      False,
            'ghost_pressure':    0.0,
            'oc_risk':           0.0,
            'oc_capability':     'monitor_only',
            'thread_count':      0,
            'foreground_process':'',
            'veto_active':       False,
            'veto_state':        None,
            'dt':                0.0,
            'cpu_temp_slope':    0.0,
        }
