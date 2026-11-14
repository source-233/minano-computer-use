"""Owned cross-process native child for opt-in UIA verification (no input injection)."""

import ctypes
import json
import os
import sys
import time
from ctypes import wintypes as w


def main():
    user = ctypes.WinDLL("user32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
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
    user.IsWindow.argtypes, user.IsWindow.restype = [w.HWND], w.BOOL
    user.PeekMessageW.argtypes = [ctypes.POINTER(w.MSG), w.HWND, w.UINT, w.UINT, w.UINT]
    user.PeekMessageW.restype = w.BOOL
    user.TranslateMessage.argtypes = [ctypes.POINTER(w.MSG)]
    user.DispatchMessageW.argtypes = [ctypes.POINTER(w.MSG)]
    user.DispatchMessageW.restype = ctypes.c_ssize_t
    user.DestroyWindow.argtypes, user.DestroyWindow.restype = [w.HWND], w.BOOL
    kernel.GetModuleHandleW.argtypes, kernel.GetModuleHandleW.restype = [w.LPCWSTR], w.HMODULE
    host = int(sys.argv[1])
    child = user.CreateWindowExW(
        0x08000000,
        "EDIT",
        "hosted initial",
        0x50810080,
        20,
        290,
        350,
        25,
        host,
        w.HMENU(2001),
        kernel.GetModuleHandleW(None),
        None,
    )
    if not child:
        raise ctypes.WinError(ctypes.get_last_error())
    print(json.dumps({"hwnd": int(child), "pid": os.getpid(), "host": host}), flush=True)
    try:
        end = time.monotonic() + 45
        msg = w.MSG()
        while user.IsWindow(child) and time.monotonic() < end:
            while user.PeekMessageW(ctypes.byref(msg), None, 0, 0, 1):
                user.TranslateMessage(ctypes.byref(msg))
                user.DispatchMessageW(ctypes.byref(msg))
            time.sleep(0.01)
    finally:
        if user.IsWindow(child):
            user.DestroyWindow(child)


if __name__ == "__main__":
    main()
