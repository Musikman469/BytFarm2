"""
ui/splash.py — Win32 Native Splash Screen
==========================================
Borderless Win32 window shown during startup.
No Qt dependency. Pure ctypes.
Shows BytFarm logo, status label, and progress bar.
"""

from __future__ import annotations
import ctypes
import ctypes.wintypes
import logging
import pathlib
import threading
import time
from typing import Optional

log = logging.getLogger(__name__)

# GDI / User32 constants
WS_POPUP          = 0x80000000
WS_VISIBLE        = 0x10000000
COLOR_WINDOW      = 5
WM_PAINT          = 0x000F
WM_DESTROY        = 0x0002
WM_CLOSE          = 0x0010
CS_HREDRAW        = 0x0002
CS_VREDRAW        = 0x0001
DT_CENTER         = 0x00000001
DT_VCENTER        = 0x00000004
DT_SINGLELINE     = 0x00000020
IDC_ARROW         = 32512

# Colours (COLORREF = 0x00BBGGRR)
CLR_BACKGROUND = 0x00190F1A   # deep navy/purple
CLR_BAR_BG     = 0x00302040   # dark bar track
CLR_BAR_FILL   = 0x00C050FF   # purple progress fill
CLR_TEXT_MAIN  = 0x00E0D0FF   # light lavender
CLR_TEXT_SUB   = 0x00907090   # muted


class SplashScreen:
    """
    Win32 native splash screen.

    Usage:
        splash = SplashScreen(total_steps=10)
        splash.show()
        splash.update(1, 'Loading config...')
        ...
        splash.finish()
    """

    WIDTH  = 480
    HEIGHT = 260

    def __init__(self, total_steps: int = 10) -> None:
        self._total       = total_steps
        self._step        = 0
        self._label       = 'Initialising...'
        self._error       = False
        self._hwnd: Optional[int] = None
        self._thread      = threading.Thread(
            target=self._message_loop, daemon=True, name='SplashThread')
        self._ready       = threading.Event()
        self._logo_path   = (pathlib.Path(__file__).parent.parent.parent
                             / 'assets' / 'icons' / 'bytfarm_logo.png')

    def show(self) -> None:
        """Start splash window. Blocks briefly until window is created."""
        self._thread.start()
        self._ready.wait(timeout=2.0)

    def update(self, step: int, label: str) -> None:
        self._step  = step
        self._label = label
        if self._hwnd:
            ctypes.windll.user32.InvalidateRect(self._hwnd, None, True)
            ctypes.windll.user32.UpdateWindow(self._hwnd)

    def show_error(self, label: str, exc: Exception) -> None:
        self._error = True
        self._label = f'Fatal error in: {label}\n{type(exc).__name__}: {exc}'
        if self._hwnd:
            ctypes.windll.user32.InvalidateRect(self._hwnd, None, True)
            ctypes.windll.user32.UpdateWindow(self._hwnd)
        time.sleep(3.0)  # Show error briefly before exit

    def finish(self) -> None:
        """Destroy the splash window."""
        if self._hwnd:
            ctypes.windll.user32.PostMessageW(self._hwnd, WM_CLOSE, 0, 0)
        self._hwnd = None

    # ── Win32 message loop (runs in dedicated thread) ─────────────────────────

    def _message_loop(self) -> None:
        try:
            self._create_window()
            msg = ctypes.wintypes.MSG()
            while ctypes.windll.user32.GetMessageW(ctypes.byref(msg), None, 0, 0):
                ctypes.windll.user32.TranslateMessage(ctypes.byref(msg))
                ctypes.windll.user32.DispatchMessageW(ctypes.byref(msg))
        except Exception as e:
            log.error(f'[Splash] Message loop error: {e}')

    def _create_window(self) -> None:
        hinstance = ctypes.windll.kernel32.GetModuleHandleW(None)

        WNDPROC = ctypes.WINFUNCTYPE(
            ctypes.c_long, ctypes.wintypes.HWND, ctypes.c_uint,
            ctypes.wintypes.WPARAM, ctypes.wintypes.LPARAM)

        wnd_proc = WNDPROC(self._wnd_proc)
        self._wnd_proc_ref = wnd_proc  # prevent GC

        wc = ctypes.create_string_buffer(b'\x00' * 80)  # WNDCLASSEX placeholder
        # Use RegisterClassEx via a simpler approach
        class WNDCLASSEXW(ctypes.Structure):
            _fields_ = [
                ('cbSize',        ctypes.c_uint),
                ('style',         ctypes.c_uint),
                ('lpfnWndProc',   WNDPROC),
                ('cbClsExtra',    ctypes.c_int),
                ('cbWndExtra',    ctypes.c_int),
                ('hInstance',     ctypes.wintypes.HINSTANCE),
                ('hIcon',         ctypes.wintypes.HICON),
                ('hCursor',       ctypes.wintypes.HANDLE),
                ('hbrBackground', ctypes.wintypes.HBRUSH),
                ('lpszMenuName',  ctypes.wintypes.LPCWSTR),
                ('lpszClassName', ctypes.wintypes.LPCWSTR),
                ('hIconSm',       ctypes.wintypes.HICON),
            ]

        cursor = ctypes.windll.user32.LoadCursorW(None, IDC_ARROW)
        wc_obj = WNDCLASSEXW(
            cbSize=ctypes.sizeof(WNDCLASSEXW),
            style=CS_HREDRAW | CS_VREDRAW,
            lpfnWndProc=wnd_proc,
            hInstance=hinstance,
            hCursor=cursor,
            hbrBackground=ctypes.windll.gdi32.CreateSolidBrush(CLR_BACKGROUND),
            lpszClassName='BytFarmSplash',
        )
        ctypes.windll.user32.RegisterClassExW(ctypes.byref(wc_obj))

        # Centre on screen
        sw = ctypes.windll.user32.GetSystemMetrics(0)
        sh = ctypes.windll.user32.GetSystemMetrics(1)
        x  = (sw - self.WIDTH)  // 2
        y  = (sh - self.HEIGHT) // 2

        self._hwnd = ctypes.windll.user32.CreateWindowExW(
            0, 'BytFarmSplash', 'BytFarm',
            WS_POPUP | WS_VISIBLE,
            x, y, self.WIDTH, self.HEIGHT,
            None, None, hinstance, None,
        )
        self._ready.set()

    def _wnd_proc(self, hwnd, msg, wparam, lparam) -> int:
        if msg == WM_PAINT:
            self._paint(hwnd)
            return 0
        if msg in (WM_DESTROY, WM_CLOSE):
            ctypes.windll.user32.PostQuitMessage(0)
            return 0
        return ctypes.windll.user32.DefWindowProcW(hwnd, msg, wparam, lparam)

    def _paint(self, hwnd: int) -> None:
        class PAINTSTRUCT(ctypes.Structure):
            _fields_ = [
                ('hdc',         ctypes.wintypes.HDC),
                ('fErase',      ctypes.wintypes.BOOL),
                ('rcPaint',     ctypes.wintypes.RECT),
                ('fRestore',    ctypes.wintypes.BOOL),
                ('fIncUpdate',  ctypes.wintypes.BOOL),
                ('rgbReserved', ctypes.c_byte * 32),
            ]

        ps  = PAINTSTRUCT()
        hdc = ctypes.windll.user32.BeginPaint(hwnd, ctypes.byref(ps))

        # Background
        bg_brush = ctypes.windll.gdi32.CreateSolidBrush(CLR_BACKGROUND)
        rc = ctypes.wintypes.RECT(0, 0, self.WIDTH, self.HEIGHT)
        ctypes.windll.user32.FillRect(hdc, ctypes.byref(rc), bg_brush)
        ctypes.windll.gdi32.DeleteObject(bg_brush)

        # Title text
        ctypes.windll.gdi32.SetTextColor(hdc, CLR_TEXT_MAIN)
        ctypes.windll.gdi32.SetBkMode(hdc, 1)  # TRANSPARENT
        title_font = ctypes.windll.gdi32.CreateFontW(
            32, 0, 0, 0, 700, 0, 0, 0, 0, 0, 0, 0, 0, 'Segoe UI')
        ctypes.windll.gdi32.SelectObject(hdc, title_font)
        rc_title = ctypes.wintypes.RECT(0, 40, self.WIDTH, 90)
        ctypes.windll.user32.DrawTextW(
            hdc, 'BytFarm 2.1', -1, ctypes.byref(rc_title),
            DT_CENTER | DT_SINGLELINE)
        ctypes.windll.gdi32.DeleteObject(title_font)

        # Status label
        sub_font = ctypes.windll.gdi32.CreateFontW(
            16, 0, 0, 0, 400, 0, 0, 0, 0, 0, 0, 0, 0, 'Segoe UI')
        ctypes.windll.gdi32.SelectObject(hdc, sub_font)
        ctypes.windll.gdi32.SetTextColor(hdc, CLR_TEXT_SUB)
        rc_label = ctypes.wintypes.RECT(20, 160, self.WIDTH - 20, 195)
        label = self._label.split('\n')[0][:60]  # first line, max 60 chars
        ctypes.windll.user32.DrawTextW(
            hdc, label, -1, ctypes.byref(rc_label),
            DT_CENTER | DT_SINGLELINE)
        ctypes.windll.gdi32.DeleteObject(sub_font)

        # Progress bar track
        bar_x, bar_y = 40, 200
        bar_w, bar_h = self.WIDTH - 80, 14
        track_brush = ctypes.windll.gdi32.CreateSolidBrush(CLR_BAR_BG)
        rc_track = ctypes.wintypes.RECT(bar_x, bar_y, bar_x + bar_w, bar_y + bar_h)
        ctypes.windll.user32.FillRect(hdc, ctypes.byref(rc_track), track_brush)
        ctypes.windll.gdi32.DeleteObject(track_brush)

        # Progress bar fill
        progress = self._step / max(self._total, 1)
        fill_w   = int(bar_w * progress)
        if fill_w > 0:
            fill_color  = 0x004040FF if self._error else CLR_BAR_FILL
            fill_brush  = ctypes.windll.gdi32.CreateSolidBrush(fill_color)
            rc_fill = ctypes.wintypes.RECT(bar_x, bar_y, bar_x + fill_w, bar_y + bar_h)
            ctypes.windll.user32.FillRect(hdc, ctypes.byref(rc_fill), fill_brush)
            ctypes.windll.gdi32.DeleteObject(fill_brush)

        ctypes.windll.user32.EndPaint(hwnd, ctypes.byref(ps))
