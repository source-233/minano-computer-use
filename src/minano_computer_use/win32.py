"""Read-only Win32 window targeting and desktop observations."""

from __future__ import annotations

import ctypes
from ctypes import wintypes


class TargetError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def _user32():
    if not hasattr(ctypes, "windll"):
        raise OSError("Win32 APIs are only available on Windows")
    user32 = ctypes.windll.user32
    callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    user32.EnumWindows.argtypes = [callback_type, wintypes.LPARAM]
    user32.EnumWindows.restype = wintypes.BOOL
    user32.IsWindow.argtypes = [wintypes.HWND]
    user32.IsWindow.restype = wintypes.BOOL
    user32.IsWindowVisible.argtypes = [wintypes.HWND]
    user32.IsWindowVisible.restype = wintypes.BOOL
    user32.IsIconic.argtypes = [wintypes.HWND]
    user32.IsIconic.restype = wintypes.BOOL
    user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
    user32.GetWindowTextLengthW.restype = ctypes.c_int
    user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    user32.GetWindowTextW.restype = ctypes.c_int
    user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    user32.GetWindowThreadProcessId.restype = wintypes.DWORD
    user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
    user32.GetWindowRect.restype = wintypes.BOOL
    user32.GetForegroundWindow.argtypes = []
    user32.GetForegroundWindow.restype = wintypes.HWND
    user32.AttachThreadInput.argtypes = [wintypes.DWORD, wintypes.DWORD, wintypes.BOOL]
    user32.AttachThreadInput.restype = wintypes.BOOL
    user32.BringWindowToTop.argtypes = [wintypes.HWND]
    user32.BringWindowToTop.restype = wintypes.BOOL
    user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
    user32.ShowWindow.restype = wintypes.BOOL
    user32.GetCursorPos.argtypes = [ctypes.POINTER(wintypes.POINT)]
    user32.GetCursorPos.restype = wintypes.BOOL
    user32.GetPhysicalCursorPos.argtypes = [ctypes.POINTER(wintypes.POINT)]
    user32.GetPhysicalCursorPos.restype = wintypes.BOOL
    return user32


def rect_coordinate_space(user32) -> str:
    """Observe caller DPI context; never alter it or infer a DIP conversion."""
    try:
        context = user32.GetThreadDpiAwarenessContext
        context.argtypes, context.restype = [], ctypes.c_void_p
        awareness = user32.GetAwarenessFromDpiAwarenessContext
        awareness.argtypes, awareness.restype = [ctypes.c_void_p], ctypes.c_int
        current = context()
        if not current:
            return "win32_screen_unknown"
        value = awareness(current)
        return {2: "physical_screen", 0: "dpi_virtualized_screen", 1: "system_dpi_screen"}.get(
            value, "win32_screen_unknown"
        )
    except (AttributeError, OSError):
        return "win32_screen_unknown"


def _window_entry(hwnd: int, user32) -> dict:
    handle = wintypes.HWND(hwnd)
    if not user32.IsWindow(handle):
        return {}
    length = user32.GetWindowTextLengthW(handle)
    title = ctypes.create_unicode_buffer(length + 1)
    user32.GetWindowTextW(handle, title, len(title))
    pid = wintypes.DWORD()
    user32.GetWindowThreadProcessId(handle, ctypes.byref(pid))
    rect = wintypes.RECT()
    before_space = rect_coordinate_space(user32)
    if not user32.GetWindowRect(handle, ctypes.byref(rect)):
        rect_values = [0, 0, 0, 0]
        rect_space = "unavailable"
    else:
        rect_values = [rect.left, rect.top, rect.right, rect.bottom]
        after_space = rect_coordinate_space(user32)
        rect_space = before_space if before_space == after_space else "win32_screen_unknown"
    return {
        "hwnd": int(hwnd),
        "pid": int(pid.value),
        "title": title.value,
        "rect": rect_values,
        "rect_format": "left_top_right_bottom",
        "rect_space": rect_space,
        "rect_source": "GetWindowRect",
        "visible": bool(user32.IsWindowVisible(handle)),
        "minimized": bool(user32.IsIconic(handle)),
    }


def list_windows() -> list[dict]:
    user32 = _user32()
    found: list[dict] = []
    callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    @callback_type
    def visit(hwnd, _):
        entry = _window_entry(int(hwnd), user32)
        if entry:
            found.append(entry)
        return True

    if not user32.EnumWindows(visit, 0):
        raise OSError("EnumWindows failed")
    return found


def resolve_window(target: str) -> dict:
    if not isinstance(target, str) or not target.strip():
        raise TargetError("INVALID_TARGET", "An explicit nonempty window target is required")
    windows = list_windows()
    if target == "foreground":
        hwnd = observations()["foreground_hwnd"]
        matches = [item for item in windows if item["hwnd"] == hwnd]
    elif target.startswith("hwnd:"):
        try:
            hwnd = int(target[5:], 0)
        except ValueError as exc:
            raise TargetError("INVALID_TARGET", "Invalid HWND") from exc
        if hwnd <= 0 or hwnd >= 1 << (ctypes.sizeof(ctypes.c_void_p) * 8):
            raise TargetError("INVALID_TARGET", "HWND is outside the pointer-sized range")
        matches = [item for item in windows if item["hwnd"] == hwnd]
    elif target.startswith("pid:"):
        try:
            pid = int(target[4:], 10)
        except ValueError as exc:
            raise TargetError("INVALID_TARGET", "Invalid PID") from exc
        if not 0 < pid <= 0xFFFFFFFF:
            raise TargetError("INVALID_TARGET", "PID is outside the DWORD range")
        matches = [item for item in windows if item["pid"] == pid]
    else:
        exact = [item for item in windows if item["title"].casefold() == target.casefold()]
        matches = exact or [
            item for item in windows if target.casefold() in item["title"].casefold()
        ]
    if not matches:
        raise TargetError("TARGET_NOT_FOUND", f"No window matches {target!r}")
    if len(matches) != 1:
        raise TargetError("AMBIGUOUS_TARGET", f"{len(matches)} windows match {target!r}")
    return matches[0]


def observations() -> dict:
    user32 = _user32()
    point = wintypes.POINT()
    # Read physical coordinates directly. Loading MSS/UIA can change a caller's
    # DPI context; GetCursorPos would otherwise manufacture apparent motion.
    cursor = [point.x, point.y] if user32.GetPhysicalCursorPos(ctypes.byref(point)) else None
    foreground = user32.GetForegroundWindow()
    return {
        "foreground_hwnd": int(foreground or 0),
        "cursor": cursor,
        "cursor_available": cursor is not None,
        "cursor_coordinate_space": "physical_screen",
    }


def process_creation_time(pid: int) -> int | None:
    """Read a process creation FILETIME; access restrictions are reported as unavailable.

    PID/HWND reuse is not disproved by a missing timestamp. No process handles escape.
    """
    kernel32 = ctypes.windll.kernel32
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4
    kernel32.GetProcessTimes.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not handle:
        return None
    try:
        creation, exit_time, kernel, user = (wintypes.FILETIME() for _ in range(4))
        if not kernel32.GetProcessTimes(
            handle,
            ctypes.byref(creation),
            ctypes.byref(exit_time),
            ctypes.byref(kernel),
            ctypes.byref(user),
        ):
            return None
        return (creation.dwHighDateTime << 32) | creation.dwLowDateTime
    finally:
        kernel32.CloseHandle(handle)


def window_identity(hwnd: int, expected_pid: int) -> dict:
    """Validate the live handle before and after reading optional process identity."""
    user32 = _user32()

    def check():
        pid = wintypes.DWORD()
        if (
            not user32.IsWindow(hwnd)
            or not user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            or pid.value != expected_pid
        ):
            raise TargetError("STALE_TARGET", "Window handle no longer belongs to target process")

    check()
    creation = process_creation_time(expected_pid)
    check()
    return {"hwnd": int(hwnd), "pid": expected_pid, "process_creation_time": creation}


def child_window_identity(host_hwnd: int, host_pid: int, child_hwnd: int, child_pid: int) -> dict:
    """Bound a native descendant to its OWN process, not necessarily the host PID."""
    user = _user32()
    user.IsChild.argtypes = [wintypes.HWND, wintypes.HWND]
    user.IsChild.restype = wintypes.BOOL

    def relation():
        host = wintypes.DWORD()
        if (
            not user.IsWindow(host_hwnd)
            or not user.GetWindowThreadProcessId(host_hwnd, ctypes.byref(host))
            or host.value != host_pid
            or not user.IsChild(host_hwnd, child_hwnd)
        ):
            raise TargetError("STALE_TARGET", "Native descendant membership changed")

    relation()
    identity = window_identity(child_hwnd, child_pid)
    relation()
    if identity["process_creation_time"] is None:
        raise TargetError("IDENTITY_UNAVAILABLE", "Element process identity unavailable")
    return identity
