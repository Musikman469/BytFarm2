# WinRing0 — Required for PL1/PL2 OC (Optional)
# ================================================
# BytFarm uses WinRing0 to write CPU power limits directly (MSR writes).
# Without it, BytFarm falls back to external_tool or monitor_only mode.
# All other features work normally without WinRing0.

## What you need

Place these two files in this directory (vendor/):

    WinRing0x64.dll
    WinRing0x64.sys

## Where to download

Official GermanAizek fork (maintained, Windows 10/11 compatible):
    https://github.com/GermanAizek/WinRing0/releases

Direct link to latest release assets:
    https://github.com/GermanAizek/WinRing0/releases/latest

Download the zip, extract, and copy:
    WinRing0x64.dll  →  vendor/WinRing0x64.dll
    WinRing0x64.sys  →  vendor/WinRing0x64.sys

## Requirements

- Windows 10 or 11 (x64)
- BytFarm must be run as Administrator (the .spec requests UAC elevation)
- Secure Boot must allow unsigned kernel drivers, OR use the signed variant
  from the release page

## Verification

After placing the files, BytFarm will log at startup:
    [OCController] capability=pl1pl2

If it logs:
    [OCController] capability=external_tool  →  WinRing0 not found or no admin rights
    [OCController] capability=monitor_only   →  No tools available

## Without WinRing0

BytFarm works fully without it. OC capability degrades to:
    1. external_tool  (if Intel XTU or AMD Ryzen Master is installed)
    2. monitor_only   (safe read-only mode on any machine)

BytFarm will show a one-time notification suggesting the appropriate tool
for your CPU if WinRing0 is not present.
