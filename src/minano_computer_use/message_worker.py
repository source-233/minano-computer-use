"""Fixed JSON-only worker and pointer-width-correct background Win32 adapter."""

from __future__ import annotations

import contextlib
import ctypes
import json
import sys
import time
from ctypes import wintypes

from . import win32
from .message_backend import BackgroundInput, _MessageError


class _GUIThreadInfo(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("flags", wintypes.DWORD),
        ("hwndActive", wintypes.HWND),
        ("hwndFocus", wintypes.HWND),
        ("hwndCapture", wintypes.HWND),
        ("hwndMenuOwner", wintypes.HWND),
        ("hwndMoveSize", wintypes.HWND),
        ("hwndCaret", wintypes.HWND),
        ("rcCaret", wintypes.RECT),
    ]


class _Win32MessageAdapter:
    def __init__(self):
        self.user32 = ctypes.WinDLL("user32", use_last_error=True)
        signatures = {
            "IsWindow": ([wintypes.HWND], wintypes.BOOL),
            "IsWindowVisible": ([wintypes.HWND], wintypes.BOOL),
            "IsIconic": ([wintypes.HWND], wintypes.BOOL),
            "IsChild": ([wintypes.HWND, wintypes.HWND], wintypes.BOOL),
            "GetWindowThreadProcessId": (
                [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)],
                wintypes.DWORD,
            ),
            "GetGUIThreadInfo": ([wintypes.DWORD, ctypes.POINTER(_GUIThreadInfo)], wintypes.BOOL),
            "SendMessageTimeoutW": (
                [
                    wintypes.HWND,
                    wintypes.UINT,
                    wintypes.WPARAM,
                    wintypes.LPARAM,
                    wintypes.UINT,
                    wintypes.UINT,
                    ctypes.POINTER(ctypes.c_size_t),
                ],
                ctypes.c_ssize_t,
            ),
            "GetClassNameW": ([wintypes.HWND, wintypes.LPWSTR, ctypes.c_int], ctypes.c_int),
            "GetClientRect": ([wintypes.HWND, ctypes.POINTER(wintypes.RECT)], wintypes.BOOL),
            "ScreenToClient": ([wintypes.HWND, ctypes.POINTER(wintypes.POINT)], wintypes.BOOL),
            "ChildWindowFromPointEx": (
                [wintypes.HWND, wintypes.POINT, wintypes.UINT],
                wintypes.HWND,
            ),
            "MapVirtualKeyW": ([wintypes.UINT, wintypes.UINT], wintypes.UINT),
            "SetThreadDpiAwarenessContext": ([ctypes.c_void_p], ctypes.c_void_p),
            "GetThreadDpiAwarenessContext": ([], ctypes.c_void_p),
            "GetAwarenessFromDpiAwarenessContext": ([ctypes.c_void_p], ctypes.c_int),
        }
        for name, (args, restype) in signatures.items():
            function = getattr(self.user32, name)
            function.argtypes, function.restype = args, restype

    def resolve_window(self, target):
        return win32.resolve_window(target)

    def identity(self, hwnd, pid):
        return win32.window_identity(hwnd, pid)

    def observations(self):
        return win32.observations()

    def is_window(self, hwnd):
        return bool(self.user32.IsWindow(hwnd))

    def window_pid(self, hwnd):
        pid = wintypes.DWORD()
        return pid.value if self.user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid)) else None

    def is_child(self, root, child):
        return bool(self.user32.IsChild(root, child))

    def is_visible(self, hwnd):
        return bool(self.user32.IsWindowVisible(hwnd))

    def is_iconic(self, hwnd):
        return bool(self.user32.IsIconic(hwnd))

    def thread_focus(self, root):
        pid = wintypes.DWORD()
        thread = self.user32.GetWindowThreadProcessId(root, ctypes.byref(pid))
        if not thread:
            raise OSError("target GUI thread unavailable")
        info = _GUIThreadInfo(cbSize=ctypes.sizeof(_GUIThreadInfo))
        if not self.user32.GetGUIThreadInfo(thread, ctypes.byref(info)):
            return 0
        return int(info.hwndFocus or 0)

    def dispatch_message(self, hwnd, message, wparam, lparam, milliseconds):
        # Pointer-free, fixed payloads only. No asynchronous queue posts: mixing
        # posted WM_CHAR with synchronous WM_SETTEXT can reverse caller ordering.
        if message not in {
            0x100,
            0x101,
            0x102,
            0x200,
            0x201,
            0x202,
            0x203,
            0x204,
            0x205,
            0x206,
            0x207,
            0x208,
            0x209,
            0x20A,
            0x20B,
            0x20C,
            0x20D,
            0x20E,
        }:
            raise ValueError("unsupported payload message")
        value = ctypes.c_size_t()
        ok = self.user32.SendMessageTimeoutW(
            hwnd, message, wparam, lparam, 0x2 | 0x20, milliseconds, ctypes.byref(value)
        )
        # A window procedure may legitimately return zero. The API's nonzero
        # transport return, not lpdwResult, indicates synchronous dispatch.
        return bool(ok)

    def send_timeout(self, hwnd, message, wparam, payload, milliseconds):
        # Only system-marshaled WM_SETTEXT/GETTEXT/GETTEXTLENGTH are exposed.
        if message not in {0xC, 0xD, 0xE}:
            raise ValueError("unsupported message")
        if isinstance(payload, str):
            payload = ctypes.create_unicode_buffer(payload)
        pointer = payload if isinstance(payload, int) else ctypes.addressof(payload)
        value = ctypes.c_size_t()
        ok = self.user32.SendMessageTimeoutW(
            hwnd, message, wparam, pointer, 0x2 | 0x20, milliseconds, ctypes.byref(value)
        )  # SMTO_ABORTIFHUNG | SMTO_ERRORONEXIT; never SMTO_NOTIMEOUTIFNOTHUNG.
        return bool(ok), value.value

    def class_name(self, hwnd):
        buffer = ctypes.create_unicode_buffer(256)
        if not self.user32.GetClassNameW(hwnd, buffer, len(buffer)):
            raise OSError("class unavailable")
        return buffer.value

    def client_rect(self, hwnd):
        rect = wintypes.RECT()
        if not self.user32.GetClientRect(hwnd, ctypes.byref(rect)):
            raise OSError("client rect unavailable")
        return rect.left, rect.top, rect.right, rect.bottom

    def screen_to_client(self, hwnd, x, y):
        point = wintypes.POINT(x, y)
        if not self.user32.ScreenToClient(hwnd, ctypes.byref(point)):
            raise OSError("coordinate conversion failed")
        return point.x, point.y

    def child_from_point(self, hwnd, x, y):
        return int(self.user32.ChildWindowFromPointEx(hwnd, wintypes.POINT(x, y), 1 | 2 | 4) or 0)

    def map_key(self, key):
        return int(self.user32.MapVirtualKeyW(key, 0))

    @contextlib.contextmanager
    def physical_coordinates(self):
        previous = self.user32.SetThreadDpiAwarenessContext(ctypes.c_void_p(-4))
        if not previous:
            raise OSError("physical DPI scope unavailable")
        try:
            context = self.user32.GetThreadDpiAwarenessContext()
            if self.user32.GetAwarenessFromDpiAwarenessContext(context) != 2:
                raise OSError("physical DPI scope unverified")
            yield
        finally:
            if not self.user32.SetThreadDpiAwarenessContext(previous):
                raise OSError("physical DPI scope restore failed")


def worker_main(connection):
    with contextlib.redirect_stdout(sys.stderr):
        backend = None
        try:
            while True:
                request = json.loads(connection.recv_bytes(16 * 1024 * 1024).decode("utf-8"))
                if request is None:
                    break
                if not isinstance(request, dict):
                    break
                if backend is None:
                    backend = BackgroundInput(adapter=_Win32MessageAdapter())
                if request.get("message_bind") is True and request["action"] == "inspect":
                    backend._deadline = request["_deadline_monotonic"]
                    try:
                        binding = backend._bind(request["target"])
                        response = backend._response("ok", "inspect", target_binding=binding)
                    except Exception as exc:
                        code = (
                            exc.code if isinstance(exc, _MessageError) else "IDENTITY_UNAVAILABLE"
                        )
                        response = backend._response(
                            "refused", "inspect", code=code, may_have_acted=False
                        )
                elif request["action"] == "message_release":
                    response = backend.release_all()
                else:
                    # Never rediscover a title after the parent received its pin,
                    # including after a non-holding worker was discarded.
                    backend._bindings[request["target"]] = request["binding"]
                    remaining = min(
                        request["timeout"], request["_deadline_monotonic"] - time.monotonic()
                    )
                    if remaining <= 0:
                        response = backend._response("refused", request["action"], code="TIMEOUT")
                    else:
                        response = backend.execute(request["item"], request["target"], remaining)
                response["_message_worker"] = True
                response["_message_holds"] = bool(backend._keys or backend._buttons)
                response["operation"] = request["action"]
                connection.send_bytes(json.dumps(response, allow_nan=False).encode("utf-8"))
        except (EOFError, OSError, ValueError, TypeError):
            pass
        finally:
            try:
                if backend is not None:
                    backend.close()
            finally:
                connection.close()
