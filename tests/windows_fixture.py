"""Owned standard Win32 controls for opt-in UIA verification; no input injection."""

import ctypes
import json
import os
from ctypes import wintypes as w


def main() -> None:
    user = ctypes.WinDLL("user32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    callback = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, w.HWND, w.UINT, w.WPARAM, w.LPARAM)

    class WindowClass(ctypes.Structure):
        _fields_ = [
            ("style", w.UINT),
            ("wndproc", callback),
            ("extra_class", ctypes.c_int),
            ("extra_window", ctypes.c_int),
            ("instance", w.HINSTANCE),
            ("icon", w.HICON),
            ("cursor", w.HANDLE),
            ("background", w.HBRUSH),
            ("menu", w.LPCWSTR),
            ("name", w.LPCWSTR),
        ]

    kernel.GetModuleHandleW.argtypes = [w.LPCWSTR]
    kernel.GetModuleHandleW.restype = w.HMODULE
    user.DefWindowProcW.argtypes = [w.HWND, w.UINT, w.WPARAM, w.LPARAM]
    user.DefWindowProcW.restype = ctypes.c_ssize_t
    user.RegisterClassW.argtypes = [ctypes.POINTER(WindowClass)]
    user.RegisterClassW.restype = w.ATOM
    user.CreateWindowExW.argtypes = [
        w.DWORD,
        w.LPCWSTR,
        w.LPCWSTR,
        w.DWORD,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_int,
        w.HWND,
        w.HMENU,
        w.HINSTANCE,
        w.LPVOID,
    ]
    user.CreateWindowExW.restype = w.HWND
    user.SetWindowTextW.argtypes = [w.HWND, w.LPCWSTR]
    user.SetWindowTextW.restype = w.BOOL
    user.DestroyWindow.argtypes = [w.HWND]
    user.DestroyWindow.restype = w.BOOL
    user.ShowWindow.argtypes = [w.HWND, ctypes.c_int]
    user.ShowWindow.restype = w.BOOL
    user.GetMessageW.argtypes = [ctypes.POINTER(w.MSG), w.HWND, w.UINT, w.UINT]
    user.GetMessageW.restype = w.BOOL
    user.TranslateMessage.argtypes = [ctypes.POINTER(w.MSG)]
    user.DispatchMessageW.argtypes = [ctypes.POINTER(w.MSG)]
    user.DispatchMessageW.restype = ctypes.c_ssize_t
    user.PostQuitMessage.argtypes = [ctypes.c_int]
    user.SetTimer.argtypes = [w.HWND, ctypes.c_size_t, w.UINT, w.LPVOID]
    user.SetTimer.restype = ctypes.c_size_t
    user.KillTimer.argtypes = [w.HWND, ctypes.c_size_t]
    user.KillTimer.restype = w.BOOL

    status_handle = 0
    invocation_count = 0
    delayed_handle = 0

    @callback
    def wndproc(hwnd: int, message: int, wp: int, lp: int) -> int:
        nonlocal invocation_count
        if message == 0x0111 and (wp & 0xFFFF) == 1002:
            invocation_count += 1
            user.SetWindowTextW(status_handle, f"Invocations:{invocation_count}")
            return 0
        if message == 0x0111 and (wp & 0xFFFF) == 1007:
            user.SetTimer(hwnd, 1, 150, None)
            return 0
        if message == 0x0113 and wp == 1:
            user.KillTimer(hwnd, 1)
            user.SetWindowTextW(delayed_handle, "Delayed ready")
            return 0
        if message == 0x0010:
            user.DestroyWindow(hwnd)
            return 0
        if message == 0x0002:
            user.PostQuitMessage(0)
            return 0
        return int(user.DefWindowProcW(hwnd, message, wp, lp))

    instance = kernel.GetModuleHandleW(None)
    class_name = f"MinanoFixtureClass{os.getpid()}"
    wc = WindowClass(0, wndproc, 0, 0, instance, None, None, w.HBRUSH(6), None, class_name)
    if not user.RegisterClassW(ctypes.byref(wc)):
        raise ctypes.WinError(ctypes.get_last_error())
    # NOACTIVATE + SW_SHOWNOACTIVATE: never intentionally foreground this fixture.
    title = f"Minano UIA verification {os.getpid()}"
    window = user.CreateWindowExW(
        0x08000000,
        class_name,
        title,
        0x00CF0000,
        50,
        50,
        420,
        360,
        None,
        None,
        instance,
        None,
    )
    if not window:
        raise ctypes.WinError(ctypes.get_last_error())

    def control(kind: str, label: str, identifier: int, y: int, style: int = 0) -> int:
        handle = user.CreateWindowExW(
            0,
            kind,
            label,
            0x50000000 | style,
            20,
            y,
            350,
            25,
            window,
            w.HMENU(identifier),
            instance,
            None,
        )
        if not handle:
            raise ctypes.WinError(ctypes.get_last_error())
        return int(handle)

    edit = control("EDIT", "initial", 1001, 15, 0x00810080)
    button = control("BUTTON", "Apply", 1002, 50)
    status_handle = control("STATIC", "Invocations:0", 1003, 85)
    checkbox = control("BUTTON", "Enabled", 1006, 120, 0x00000003)
    control("BUTTON", "Duplicate", 1004, 155)
    control("BUTTON", "Duplicate", 1005, 185)
    control("BUTTON", "Delayed apply", 1007, 220)
    delayed_handle = control("STATIC", "Pending", 1008, 255)
    user.ShowWindow(window, 4)
    print(
        json.dumps(
            {
                "hwnd": int(window),
                "pid": os.getpid(),
                "title": title,
                "edit": edit,
                "button": button,
                "status": status_handle,
                "checkbox": checkbox,
            }
        ),
        flush=True,
    )
    msg = w.MSG()
    while user.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
        user.TranslateMessage(ctypes.byref(msg))
        user.DispatchMessageW(ctypes.byref(msg))


if __name__ == "__main__":
    main()
