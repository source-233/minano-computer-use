"""Isolated, read-only window pixel capture using PrintWindow or optional WGC."""

from __future__ import annotations

import ctypes
import multiprocessing as mp
import tempfile
import time
from pathlib import Path
from typing import Any

from .isolated_spawn import process as isolated_process

MAX_BITMAP_BYTES = 128 * 1024 * 1024


class WindowCaptureError(RuntimeError):
    def __init__(self, message: str, code: str = "WINDOW_CAPTURE_UNAVAILABLE") -> None:
        super().__init__(message)
        self.code = code


def _physical_dpi_context() -> bool:
    """Establish physical pixel APIs in this isolated thread, not the caller."""
    try:
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        user32.SetThreadDpiAwarenessContext.argtypes = [ctypes.c_void_p]
        user32.SetThreadDpiAwarenessContext.restype = ctypes.c_void_p
        user32.GetThreadDpiAwarenessContext.argtypes = []
        user32.GetThreadDpiAwarenessContext.restype = ctypes.c_void_p
        user32.GetAwarenessFromDpiAwarenessContext.argtypes = [ctypes.c_void_p]
        user32.GetAwarenessFromDpiAwarenessContext.restype = ctypes.c_int
        user32.SetThreadDpiAwarenessContext(ctypes.c_void_p(-4))
        return (
            user32.GetAwarenessFromDpiAwarenessContext(user32.GetThreadDpiAwarenessContext()) == 2
        )
    except (AttributeError, OSError):
        return False


def _validate(hwnd: int, pid: int) -> dict[str, Any]:
    from ctypes import wintypes

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    user32.IsWindow.argtypes = [wintypes.HWND]
    user32.IsWindow.restype = wintypes.BOOL
    user32.IsWindowVisible.argtypes = [wintypes.HWND]
    user32.IsWindowVisible.restype = wintypes.BOOL
    user32.IsIconic.argtypes = [wintypes.HWND]
    user32.IsIconic.restype = wintypes.BOOL
    user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
    user32.GetWindowRect.restype = wintypes.BOOL
    user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    user32.GetWindowThreadProcessId.restype = wintypes.DWORD
    handle = wintypes.HWND(hwnd)
    actual_pid = wintypes.DWORD()
    user32.GetWindowThreadProcessId(handle, ctypes.byref(actual_pid))
    rect = wintypes.RECT()
    if (
        not user32.IsWindow(handle)
        or not user32.IsWindowVisible(handle)
        or user32.IsIconic(handle)
        or actual_pid.value != pid
        or not user32.GetWindowRect(handle, ctypes.byref(rect))
    ):
        raise RuntimeError("window is invalid, hidden, minimized, or PID changed")
    width, height = rect.right - rect.left, rect.bottom - rect.top
    if width <= 0 or height <= 0 or width * height * 4 > MAX_BITMAP_BYTES:
        raise ValueError("window bitmap dimensions are invalid or exceed 128 MiB")
    return {"rect": [rect.left, rect.top, rect.right, rect.bottom], "size": [width, height]}


def _printwindow(hwnd: int, width: int, height: int) -> bytes:
    from ctypes import wintypes

    if width <= 0 or height <= 0 or width * height * 4 > MAX_BITMAP_BYTES:
        raise ValueError("window bitmap dimensions are invalid or exceed 128 MiB")
    gdi = ctypes.WinDLL("gdi32", use_last_error=True)
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    user32.GetWindowDC.argtypes = [wintypes.HWND]
    user32.GetWindowDC.restype = wintypes.HDC
    user32.ReleaseDC.argtypes = [wintypes.HWND, wintypes.HDC]
    user32.ReleaseDC.restype = ctypes.c_int
    user32.PrintWindow.argtypes = [wintypes.HWND, wintypes.HDC, wintypes.UINT]
    user32.PrintWindow.restype = wintypes.BOOL
    gdi.CreateCompatibleDC.argtypes = [wintypes.HDC]
    gdi.CreateCompatibleDC.restype = wintypes.HDC
    gdi.CreateCompatibleBitmap.argtypes = [wintypes.HDC, ctypes.c_int, ctypes.c_int]
    gdi.CreateCompatibleBitmap.restype = wintypes.HBITMAP
    gdi.SelectObject.argtypes = [wintypes.HDC, wintypes.HGDIOBJ]
    gdi.SelectObject.restype = wintypes.HGDIOBJ
    gdi.DeleteObject.argtypes = [wintypes.HGDIOBJ]
    gdi.DeleteObject.restype = wintypes.BOOL
    gdi.DeleteDC.argtypes = [wintypes.HDC]
    gdi.DeleteDC.restype = wintypes.BOOL

    class BitmapInfoHeader(ctypes.Structure):
        _fields_ = [
            ("biSize", wintypes.DWORD),
            ("biWidth", ctypes.c_long),
            ("biHeight", ctypes.c_long),
            ("biPlanes", wintypes.WORD),
            ("biBitCount", wintypes.WORD),
            ("biCompression", wintypes.DWORD),
            ("biSizeImage", wintypes.DWORD),
            ("biXPelsPerMeter", ctypes.c_long),
            ("biYPelsPerMeter", ctypes.c_long),
            ("biClrUsed", wintypes.DWORD),
            ("biClrImportant", wintypes.DWORD),
        ]

    class BitmapInfo(ctypes.Structure):
        _fields_ = [("bmiHeader", BitmapInfoHeader), ("bmiColors", wintypes.DWORD * 3)]

    gdi.GetDIBits.argtypes = [
        wintypes.HDC,
        wintypes.HBITMAP,
        wintypes.UINT,
        wintypes.UINT,
        ctypes.c_void_p,
        ctypes.POINTER(BitmapInfo),
        wintypes.UINT,
    ]
    gdi.GetDIBits.restype = ctypes.c_int
    hwnd_t = wintypes.HWND(hwnd)
    source = user32.GetWindowDC(hwnd_t)
    if not source:
        raise OSError("GetWindowDC failed")
    memory = bitmap = previous = None
    try:
        memory = gdi.CreateCompatibleDC(source)
        bitmap = gdi.CreateCompatibleBitmap(source, width, height)
        if not memory or not bitmap:
            raise OSError("GDI bitmap allocation failed")
        previous = gdi.SelectObject(memory, bitmap)
        if not previous or previous == ctypes.c_void_p(-1).value:
            raise OSError("SelectObject failed")
        if not user32.PrintWindow(hwnd_t, memory, 2):
            raise RuntimeError("PrintWindow failed")
        # GetDIBits requires the bitmap not to be selected into a device context.
        gdi.SelectObject(memory, previous)
        previous = None
        info = BitmapInfo()
        info.bmiHeader = BitmapInfoHeader(
            ctypes.sizeof(BitmapInfoHeader),
            width,
            -height,
            1,
            32,
            0,
            width * height * 4,
            0,
            0,
            0,
            0,
        )
        pixels = ctypes.create_string_buffer(width * height * 4)
        if gdi.GetDIBits(memory, bitmap, 0, height, pixels, ctypes.byref(info), 0) != height:
            raise OSError("GetDIBits failed")
        return pixels.raw
    finally:
        if previous and memory:
            gdi.SelectObject(memory, previous)
        if bitmap:
            gdi.DeleteObject(bitmap)
        if memory:
            gdi.DeleteDC(memory)
        user32.ReleaseDC(hwnd_t, source)


def _wgc(hwnd: int) -> tuple[bytes, int, int]:
    import threading

    from windows_capture import WindowsCapture

    result: dict[str, Any] = {}
    completed = threading.Event()
    capture = WindowsCapture(
        window_hwnd=hwnd, cursor_capture=False, draw_border=None, secondary_window=False
    )

    @capture.event
    def on_frame_arrived(frame, control):
        try:
            width, height = int(frame.width), int(frame.height)
            if width <= 0 or height <= 0 or width * height * 4 > MAX_BITMAP_BYTES:
                raise ValueError("WGC frame dimensions exceed 128 MiB")
            buffer = frame.frame_buffer
            if tuple(buffer.shape) != (height, width, 4):
                raise ValueError("WGC frame buffer is not height x width x 4 BGRA")
            result["buffer"] = buffer.copy().tobytes()
            result["width"], result["height"] = width, height
        except Exception as exc:
            result["error"] = str(exc)
        finally:
            control.stop()
            completed.set()

    @capture.event
    def on_closed():
        completed.set()

    try:
        control = capture.start_free_threaded()
    except Exception as exc:
        message = str(exc)
        if "Failed to convert item to GraphicsCaptureItem" in message.replace("`", ""):
            raise WindowCaptureError(
                "WGC_ITEM_CONVERSION_FAILED: windows-capture 2.0.1 could not create a "
                "GraphicsCaptureItem for the validated HWND; the OS WinRT interop rejected "
                f"this window or is unavailable. Native error: {message}",
                code="WGC_ITEM_CONVERSION_FAILED",
            ) from exc
        raise
    if not completed.wait(30):
        control.stop()
        raise TimeoutError("WGC did not produce a frame")
    control.stop()
    control.wait()
    if "error" in result:
        raise RuntimeError(result["error"])
    if "buffer" not in result:
        raise RuntimeError("WGC closed without producing a frame")
    return result["buffer"], result["width"], result["height"]


def _child(conn, hwnd: int, pid: int, backend: str, output_path: str) -> None:
    try:
        physical = _physical_dpi_context()
        before = _validate(hwnd, pid)
        if backend == "printwindow":
            width, height = before["size"]
            data = _printwindow(hwnd, width, height)
        else:
            data, width, height = _wgc(hwnd)
            if width <= 0 or height <= 0 or width * height * 4 > MAX_BITMAP_BYTES:
                raise ValueError("WGC frame dimensions exceed 128 MiB")
        after = _validate(hwnd, pid)
        if backend == "printwindow" and before["rect"] != after["rect"]:
            raise RuntimeError("window geometry changed during PrintWindow capture")
        if len(data) != width * height * 4:
            raise ValueError("window capture returned an invalid pixel buffer")
        Path(output_path).write_bytes(data)
        # Only a tiny bounded reply travels through IPC: a large Pipe.recv can
        # block forever after poll() sees the first chunk of an incomplete frame.
        # A physical-DPI-aware caller does not prove PrintWindow's target
        # renders at that same scale. Legacy providers can draw logical-size
        # pixels into a physical-size bitmap. Never advertise a guessed click map.
        conn.send(
            {
                "size": [width, height],
                "rect": after["rect"],
                "physical_origin": None,
                "physical_dpi_context": physical,
            }
        )
    except BaseException as exc:
        try:
            conn.send(
                {
                    "error": f"{type(exc).__name__}: {exc}"[:2000],
                    "code": getattr(exc, "code", "WINDOW_CAPTURE_UNAVAILABLE"),
                }
            )
        except (BrokenPipeError, EOFError, OSError):
            pass
    finally:
        conn.close()


def capture_window(
    hwnd: int, pid: int, backend: str, timeout: float, cancel_event: Any = None
) -> dict[str, Any]:
    """Run native capture in a spawned process, including startup in the deadline."""
    if backend not in {"printwindow", "wgc"}:
        raise ValueError("backend must be printwindow or wgc")
    started = time.monotonic()
    with tempfile.TemporaryDirectory(prefix="minano-window-") as directory:
        output = Path(directory) / "frame.bgra"
        ctx = mp.get_context("spawn")
        parent, child = ctx.Pipe(duplex=False)
        process = isolated_process(
            ctx, target=_child, args=(child, int(hwnd), int(pid), backend, str(output)), daemon=True
        )
        launched = False
        try:
            process.start()
            launched = True
            child.close()
            while True:
                if cancel_event is not None and cancel_event.is_set():
                    raise InterruptedError("capture cancelled")
                remaining = timeout - (time.monotonic() - started)
                if remaining <= 0:
                    raise TimeoutError("window capture timed out")
                if parent.poll(min(0.025, remaining)):
                    response = parent.recv()
                    if "error" in response:
                        raise WindowCaptureError(response["error"], response.get("code"))
                    response["data"] = output.read_bytes()
                    if cancel_event is not None and cancel_event.is_set():
                        raise InterruptedError("capture cancelled")
                    if time.monotonic() - started >= timeout:
                        raise TimeoutError("window capture timed out")
                    response["duration"] = time.monotonic() - started
                    return response
                if not process.is_alive():
                    raise RuntimeError("window capture worker exited without a result")
        finally:
            parent.close()
            child.close()
            if launched:
                if process.is_alive():
                    process.terminate()
                process.join(timeout=1)
                if process.is_alive() and hasattr(process, "kill"):
                    process.kill()
                    process.join(timeout=1)
                if process.is_alive():
                    raise RuntimeError("window capture worker cleanup failed")
            process.close()
