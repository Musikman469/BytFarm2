# BytFarm 2.1 — Cursor Handoff Instructions
# ===========================================
# This document tells Cursor exactly what to do with this project.
# Read this first before touching any file.

## What this project is

BytFarm 2.1 is a Windows performance engine that runs as a system tray
application. It manages CPU/GPU OC, process scheduling, ghost memory,
and storage staging — all from a lightweight background process.

Target: Windows 10+ x64, Python 3.11, output = BytFarm2.exe

---

## Project structure

    bytfarm/
    ├── main.py                      Entry point (PyInstaller target)
    ├── BytFarm2.spec                PyInstaller build spec → BytFarm2.exe
    ├── version_info.txt             Windows version metadata for the exe
    ├── requirements.txt             All pip dependencies
    ├── vendor/
    │   ├── WINRING0_INSTRUCTIONS.md  Read before building
    │   ├── WinRing0x64.dll           Place here before building (see above)
    │   └── WinRing0x64.sys           Place here before building (see above)
    ├── assets/icons/
    │   ├── bytfarm.ico              Main app icon (used by exe)
    │   ├── bytfarm_logo.png         Used by splash screen
    │   ├── idle.ico                 Tray icon states
    │   ├── cpu_active.ico
    │   ├── gpu_active.ico
    │   └── heavy_load.ico
    └── src/
        ├── main.py → calls utils/startup.py
        ├── policy/
        │   ├── snapshot.py          PolicySnapshot + PolicyBus (core data flow)
        │   └── transition.py        TransitionEngine + _lerp_dict
        ├── controllers/
        │   ├── base.py              BaseController + ControllerResult
        │   ├── watchdog.py          SafetyWatchdog + VetoState (fast loop)
        │   ├── ghost.py             GhostController (fast loop)
        │   ├── hardware_scanner.py  HardwareScanner — all hardware reads
        │   ├── scheduler.py         Thread affinity + priority (slow loop)
        │   └── process_guard.py     Misclick deduplication (slow loop)
        ├── engine/
        │   ├── loop.py              ExecutionEngine — dual fast/slow loops
        │   ├── budget.py            BudgetState + BudgetGovernor
        │   └── scheduling_office.py WorkloadClassifier + SchedulingOffice
        ├── oc/
        │   └── oc_controller.py     OCController — PL1/PL2, XTU, Ryzen Master
        ├── storage/
        │   └── flow_director.py     FlowDirector + StorageHealth
        ├── ui/
        │   ├── splash.py            Win32 native splash screen
        │   └── tray.py              System tray icon + menu
        └── utils/
            ├── config.py            TOML config + hot-reload
            ├── instance_lock.py     Named mutex single-instance enforcement
            └── startup.py           10-step splash startup sequence

---

## Build instructions

### 1. Install dependencies

    pip install -r requirements.txt
    pip install pyinstaller

### 2. Place WinRing0 (optional but recommended)

Read vendor/WINRING0_INSTRUCTIONS.md.
Download from: https://github.com/GermanAizek/WinRing0/releases/latest
Place WinRing0x64.dll and WinRing0x64.sys in vendor/.
If skipped, BytFarm runs in monitor_only OC mode.

### 3. Build the exe

    pyinstaller BytFarm2.spec

Output: dist/BytFarm2.exe

### 4. Run

    dist/BytFarm2.exe   (run as Administrator for full OC capability)

---

## What Cursor needs to implement / complete

The architecture, data flow, and all core stubs are written.
The following areas need implementation work:

### HIGH PRIORITY — needed for basic functionality

1. **src/ui/splash.py** — `_run()` / `_message_loop()` / `_create_window()`
   The Win32 window creation loop is scaffolded but the full RegisterClassEx
   + message pump needs to be wired. The `_paint()` method is complete.
   See spec Section 21.1 for the full pseudocode.

2. **src/oc/oc_controller.py** — `_apply_pl1pl2()`
   The MSR encoding logic for Intel (0x610) and AMD (0xC0010299) is
   scaffolded. The actual WinRing0 WriteMsr call needs the correct
   RAPL encoding. Reference: Intel SDM Vol 3, Section 14.7.3.

3. **src/engine/scheduling_office.py** — manual mode request handling
   `engine.request_mode(mode)` sets `metrics['manual_mode_request']`
   but SchedulingOffice.tick() doesn't yet read it. Add a check:
   if 'manual_mode_request' in metrics, override classifier output.

4. **src/storage/flow_director.py** — Phase 2 (Ghost Sweeper) and
   Phase 3 (Micro-Defragger) are not yet implemented.
   Implement per spec Sections 13 (Phase 2) and 13 (Phase 3).
   Phase 3 hard rule: MAX_BURST_MS = 50, never configurable.

### MEDIUM PRIORITY — polish and robustness

5. **src/ui/tray.py** — cockpit window
   Currently tray-only. A full cockpit window (MODES, STORAGE, PROCESSES
   tabs) showing live metrics is specified but not yet built.
   Metrics to expose: cpu_total, gpu_util, cpu_temp, ghost_pressure,
   oc_capability, idle_freeze, tick_rate, veto_active, mode + confidence.

6. **src/controllers/hardware_scanner.py** — IO load delta
   `io_load` currently records raw bytes, not a percentage delta.
   Fix: store previous counter value, compute MB/s delta per tick,
   normalise to 0–100 against a sensible peak (e.g. drive rated speed).

7. **src/utils/config.py** — `_split_key()` has a simplified implementation.
   The dot-split logic for keys like `modes.Frame-Tight.ghost_max_stored_mb`
   needs to correctly handle the hyphenated mode name as a single dict key.
   Current workaround: use `key.split('.', maxsplit=2)` for 3-part keys.

### LOW PRIORITY — nice to have

8. **tests/** — skeleton test files are present (empty).
   Add at minimum:
   - PolicySnapshot roundtrip + replay test
   - TransitionEngine mid-transition restart test
   - ProcessGuard misclick window test
   - BudgetGovernor veto cancellation test
   Use pytest. Run with: `pytest tests/`

9. **Logging subsystem** — `ControllerResult.log_entries` are generated
   but not yet aggregated by ExecutionEngine into a persistent audit log.
   Wire up a LoggingSubsystem on the slow loop that writes JSON events
   to %APPDATA%/BytFarm/logs/audit.jsonl (append-only).

---

## Architecture rules — do not break these

- Controllers NEVER read from each other directly.
  All shared state flows through PolicyBus (read) or ControllerResult (write).

- SafetyWatchdog veto is UNCONDITIONAL.
  Nothing overrides it. BudgetGovernor.request_burst() checks veto first.
  If you add new logic that bypasses veto, it is a bug.

- StorageController Phase 3 (Micro-Defragger): MAX_BURST_MS = 50.
  This value must never be made configurable.

- PolicySnapshot is frozen=True.
  Never attempt to modify a snapshot in place. Always create a new one.

- dt is injected by ExecutionEngine.
  Controllers must never call time.monotonic() themselves to compute dt.

- HardwareScanner owns all hardware reads.
  No controller may call psutil, wmi, or ctypes hardware APIs in run().

---

## Key constants at a glance

    MUTEX_NAME          = 'Global\\BytFarm_SingleInstance_v2'
    MISCLICK_WINDOW_S   = 10.0
    IDLE_GRACE_TICKS    = 28        (~7 seconds at 4 Hz)
    MAX_DEFRAG_BURST_MS = 50        (hard cap, non-configurable)
    BURST_DURATION_S    = 5.0
    TRANSITION_MS       = 300
    TEMP_SLOPE_TRIGGER  = 2.0       (deg C/s)
    HYSTERESIS_EXIT_C   = 5.0       (deg C below veto peak to clear)
    CONFIG_PATH         = %APPDATA%\BytFarm\config.toml
    STAGING_DIR         = C:\BytFarm\staging\  (default)
    WINRING0_DLL        = vendor\WinRing0x64.dll

---

## Spec document

The full 35-page developer specification (BytFarm_2.1_Spec_Revised.pdf)
is the authoritative reference for all architecture decisions, pseudocode,
and implementation details. When in doubt, consult the spec.
