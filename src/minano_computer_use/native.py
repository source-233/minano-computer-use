"""Explicitly opted-in Win32 input backend."""

from __future__ import annotations

import ctypes
import threading
import time
from contextlib import contextmanager, nullcontext
from ctypes import wintypes
from math import ceil as math_ceil
from typing import Any

from .contracts import result
from .control import OperationCancelled
from .input_plan import EXTENDED_KEYS, key_code, normalize_native_action

INPUT_MOUSE = 0
INPUT_KEYBOARD = 1
KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_SCANCODE = 0x0008
KEYEVENTF_EXTENDEDKEY = 0x0001
KEYEVENTF_UNICODE = 0x0004
MOUSEEVENTF_MOVE = 0x0001
MOUSEEVENTF_WHEEL = 0x0800
MOUSEEVENTF_ABSOLUTE = 0x8000
MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP = 0x0004
MOUSEEVENTF_HWHEEL = 0x1000
_BUTTON_FLAGS = {
    "left": (0x0002, 0x0004, 0),
    "right": (0x0008, 0x0010, 0),
    "middle": (0x0020, 0x0040, 0),
    "x1": (0x0080, 0x0100, 1),
    "x2": (0x0080, 0x0100, 2),
}
_BROWSERS = {"chrome.exe", "msedge.exe", "firefox.exe", "brave.exe", "opera.exe", "vivaldi.exe"}
SW_SHOW = 5
SW_RESTORE = 9

_CLICK_SETTLE_SECONDS = 0.020
_CLICK_PRESS_SECONDS = 0.030


class _PhysicalCoordinatesError(RuntimeError):
    """The owner thread could not establish/restore physical coordinate mapping."""


class _MouseInput(ctypes.Structure):
    _fields_ = [
        ("dx", wintypes.LONG),
        ("dy", wintypes.LONG),
        ("mouseData", wintypes.DWORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ctypes.c_size_t),
    ]


class _KeyboardInput(ctypes.Structure):
    _fields_ = [
        ("wVk", wintypes.WORD),
        ("wScan", wintypes.WORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ctypes.c_size_t),
    ]


class _InputUnion(ctypes.Union):
    _fields_ = [("mi", _MouseInput), ("ki", _KeyboardInput)]


class _Input(ctypes.Structure):
    _anonymous_ = ("payload",)
    _fields_ = [("type", wintypes.DWORD), ("payload", _InputUnion)]


class _Win32Adapter:
    def __init__(self) -> None:
        if not hasattr(ctypes, "windll"):
            raise OSError("Win32 input is only available on Windows")
        self.user32 = ctypes.windll.user32
        self.user32.GetForegroundWindow.argtypes = []
        self.user32.GetForegroundWindow.restype = wintypes.HWND
        self.user32.IsWindow.argtypes = [wintypes.HWND]
        self.user32.IsWindow.restype = wintypes.BOOL
        self.user32.SendInput.argtypes = [wintypes.UINT, ctypes.POINTER(_Input), ctypes.c_int]
        self.user32.SendInput.restype = wintypes.UINT
        self.user32.SetCursorPos.argtypes = [ctypes.c_int, ctypes.c_int]
        self.user32.SetCursorPos.restype = wintypes.BOOL
        self.user32.SetForegroundWindow.argtypes = [wintypes.HWND]
        self.user32.SetForegroundWindow.restype = wintypes.BOOL
        self.user32.AttachThreadInput.argtypes = [wintypes.DWORD, wintypes.DWORD, wintypes.BOOL]
        self.user32.AttachThreadInput.restype = wintypes.BOOL
        self.user32.BringWindowToTop.argtypes = [wintypes.HWND]
        self.user32.BringWindowToTop.restype = wintypes.BOOL
        self.user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
        self.user32.ShowWindow.restype = wintypes.BOOL
        self.user32.GetWindowThreadProcessId.argtypes = [
            wintypes.HWND,
            ctypes.POINTER(wintypes.DWORD),
        ]
        self.user32.GetWindowThreadProcessId.restype = wintypes.DWORD
        self.user32.IsIconic.argtypes = [wintypes.HWND]
        self.user32.IsIconic.restype = wintypes.BOOL
        self.user32.MapVirtualKeyW.argtypes = [wintypes.UINT, wintypes.UINT]
        self.user32.MapVirtualKeyW.restype = wintypes.UINT
        self.user32.WindowFromPoint.argtypes = [wintypes.POINT]
        self.user32.WindowFromPoint.restype = wintypes.HWND
        self.user32.GetAncestor.argtypes = [wintypes.HWND, wintypes.UINT]
        self.user32.GetAncestor.restype = wintypes.HWND
        self.user32.GetSystemMetrics.argtypes = [ctypes.c_int]
        self.user32.GetSystemMetrics.restype = ctypes.c_int
        self.kernel32 = ctypes.windll.kernel32
        self.kernel32.GetCurrentThreadId.argtypes = []
        self.kernel32.GetCurrentThreadId.restype = wintypes.DWORD
        self.user32.PostMessageW.argtypes = [
            wintypes.HWND,
            wintypes.UINT,
            wintypes.WPARAM,
            wintypes.LPARAM,
        ]
        self.user32.PostMessageW.restype = wintypes.BOOL

    def foreground(self) -> int:
        return int(self.user32.GetForegroundWindow() or 0)

    @contextmanager
    def physical_coordinates(self):
        """Scope per-monitor-v2 awareness to this thread, never process-wide."""
        try:
            setter = self.user32.SetThreadDpiAwarenessContext
            getter = self.user32.GetThreadDpiAwarenessContext
            awareness = self.user32.GetAwarenessFromDpiAwarenessContext
        except AttributeError as exc:
            raise _PhysicalCoordinatesError("Thread DPI awareness APIs are unavailable") from exc
        setter.argtypes = [ctypes.c_void_p]
        setter.restype = ctypes.c_void_p
        getter.argtypes = []
        getter.restype = ctypes.c_void_p
        awareness.argtypes = [ctypes.c_void_p]
        awareness.restype = ctypes.c_int
        previous = setter(ctypes.c_void_p(-4))
        if not previous:
            raise _PhysicalCoordinatesError("Cannot establish physical thread DPI coordinates")
        try:
            if awareness(getter()) != 2:
                raise _PhysicalCoordinatesError("Physical thread DPI coordinates were not verified")
            yield
        finally:
            if not setter(previous):
                raise _PhysicalCoordinatesError("Cannot restore the previous thread DPI context")

    def send(self, inputs: list[_Input]) -> int:
        array = (_Input * len(inputs))(*inputs)
        return int(self.user32.SendInput(len(inputs), array, ctypes.sizeof(_Input)))

    def set_cursor(self, x: int, y: int) -> bool:
        return bool(self.user32.SetCursorPos(x, y))

    def cursor_position(self) -> tuple[int, int]:
        self.user32.GetCursorPos.argtypes = [ctypes.POINTER(wintypes.POINT)]
        self.user32.GetCursorPos.restype = wintypes.BOOL
        point = wintypes.POINT()
        if not self.user32.GetCursorPos(ctypes.byref(point)):
            raise OSError("GetCursorPos failed")
        return point.x, point.y

    def activate(self, hwnd: int) -> bool:
        return bool(self.user32.SetForegroundWindow(hwnd))

    def is_window(self, hwnd: int) -> bool:
        return bool(self.user32.IsWindow(wintypes.HWND(hwnd)))

    def window_pid(self, hwnd: int) -> int | None:
        pid = wintypes.DWORD()
        thread_id = self.user32.GetWindowThreadProcessId(wintypes.HWND(hwnd), ctypes.byref(pid))
        return int(pid.value) if thread_id else None

    def window_thread_id(self, hwnd: int) -> int:
        pid = wintypes.DWORD()
        return int(self.user32.GetWindowThreadProcessId(wintypes.HWND(hwnd), ctypes.byref(pid)))

    def current_thread_id(self) -> int:
        return int(self.kernel32.GetCurrentThreadId())

    def is_iconic(self, hwnd: int) -> bool:
        return bool(self.user32.IsIconic(wintypes.HWND(hwnd)))

    def attach_thread_input(self, source: int, target: int, attach: bool) -> bool:
        return bool(self.user32.AttachThreadInput(source, target, attach))

    def bring_to_top(self, hwnd: int) -> bool:
        return bool(self.user32.BringWindowToTop(wintypes.HWND(hwnd)))

    def show_window(self, hwnd: int, command: int) -> bool:
        return bool(self.user32.ShowWindow(wintypes.HWND(hwnd), command))

    def key_event(self, vk: int, up: bool = False) -> _Input:
        scan = int(self.user32.MapVirtualKeyW(vk, 0))
        if not scan:
            return _key(vk, up)
        extended = vk in EXTENDED_KEYS
        flags = KEYEVENTF_SCANCODE | (KEYEVENTF_KEYUP if up else 0)
        if extended:
            flags |= KEYEVENTF_EXTENDEDKEY
        item = _Input(type=INPUT_KEYBOARD)
        item.ki = _KeyboardInput(0, scan, flags, 0, 0)
        return item

    def point_hits(self, x: int, y: int, hwnd: int) -> bool:
        point = wintypes.POINT(x, y)
        hit = self.user32.WindowFromPoint(point)
        root = self.user32.GetAncestor(hit, 2) if hit else 0
        return int(hit or 0) == hwnd or int(root or 0) == hwnd

    def virtual_bounds(self) -> tuple[int, int, int, int]:
        left, top = self.user32.GetSystemMetrics(76), self.user32.GetSystemMetrics(77)
        return left, top, self.user32.GetSystemMetrics(78), self.user32.GetSystemMetrics(79)

    def map_key(self, vk: int) -> int:
        return int(self.user32.MapVirtualKeyW(vk, 0))

    def process_name(self, pid: int) -> str:
        self.kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        self.kernel32.OpenProcess.restype = wintypes.HANDLE
        self.kernel32.QueryFullProcessImageNameW.argtypes = [
            wintypes.HANDLE,
            wintypes.DWORD,
            wintypes.LPWSTR,
            ctypes.POINTER(wintypes.DWORD),
        ]
        self.kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL
        self.kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        self.kernel32.CloseHandle.restype = wintypes.BOOL
        handle = self.kernel32.OpenProcess(0x1000, False, pid)
        if not handle:
            raise OSError("Cannot verify target process identity")
        try:
            size = wintypes.DWORD(32768)
            path = ctypes.create_unicode_buffer(size.value)
            if not self.kernel32.QueryFullProcessImageNameW(handle, 0, path, ctypes.byref(size)):
                raise OSError("Cannot query target process identity")
            return path.value.replace("\\", "/").rsplit("/", 1)[-1]
        finally:
            self.kernel32.CloseHandle(handle)

    def post_message(self, hwnd: int, message: int, wparam: int, lparam: int) -> bool:
        return bool(self.user32.PostMessageW(hwnd, message, wparam, lparam))

    def render_children(self, hwnd: int) -> list[int]:
        callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
        children = []
        self.user32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
        self.user32.GetClassNameW.restype = ctypes.c_int

        @callback_type
        def callback(child, _):
            name = ctypes.create_unicode_buffer(256)
            self.user32.GetClassNameW(child, name, len(name))
            if name.value in {"Chrome_RenderWidgetHostHWND", "MozillaWindowClass"}:
                children.append(int(child))
            return True

        self.user32.EnumChildWindows.argtypes = [wintypes.HWND, callback_type, wintypes.LPARAM]
        self.user32.EnumChildWindows.restype = wintypes.BOOL
        self.user32.EnumChildWindows(hwnd, callback, 0)
        return children


def _key(vk: int, up: bool = False) -> _Input:
    item = _Input(type=INPUT_KEYBOARD)
    item.ki = _KeyboardInput(vk, 0, KEYEVENTF_KEYUP if up else 0, 0, 0)
    return item


def _key_event(adapter: Any, vk: int, up: bool = False) -> _Input:
    return adapter.key_event(vk, up) if hasattr(adapter, "key_event") else _key(vk, up)


def _unicode(char: str, up: bool = False) -> _Input:
    item = _Input(type=INPUT_KEYBOARD)
    item.ki = _KeyboardInput(0, ord(char), KEYEVENTF_UNICODE | (KEYEVENTF_KEYUP if up else 0), 0, 0)
    return item


def _mouse(flags: int, dx: int = 0, dy: int = 0, data: int = 0) -> _Input:
    item = _Input(type=INPUT_MOUSE)
    item.mi = _MouseInput(dx, dy, data, flags, 0, 0)
    return item


class NativeInput:
    def __init__(self, enabled: bool = False, adapter: Any = None, services: Any = None) -> None:
        self.enabled = enabled
        self._adapter = adapter
        self._held_keys: set[int] = set()
        self._services = services
        self._held_buttons: set[str] = set()
        self._message_keys: dict[int, tuple[int, int]] = {}
        self._message_hwnd: int | None = None
        self._message_pid: int | None = None
        self._expected_pid: int | None = None
        self._held_unicode: list[int] = []
        self._target_rect: tuple[int, int, int, int] | None = None
        self._closed = False
        self._cancel_event: threading.Event | None = None
        self._cleanup_required = False
        self._owner_thread_id: int | None = None
        self._action_acted = False
        self._clipboard_cleanup: dict | None = None
        self._coordinate_context_depth = 0

    @property
    def _held_mouse(self) -> bool:
        """Compatibility view; ownership is tracked separately for every button."""
        return bool(self._held_buttons)

    @_held_mouse.setter
    def _held_mouse(self, held: bool) -> None:
        if held:
            self._held_buttons.add("left")
        else:
            self._held_buttons.clear()

    def set_cancel_event(self, event: threading.Event | None) -> None:
        """Set the cooperative cancellation event used by the next execute."""
        if event is not None and not hasattr(event, "is_set"):
            raise TypeError("cancel event must provide is_set()")
        self._cancel_event = event

    def _cancel_checkpoint(self) -> None:
        if self._cancel_event is not None and self._cancel_event.is_set():
            raise OperationCancelled()

    def _win(self) -> Any:
        if self._adapter is None:
            self._adapter = _Win32Adapter()
        return self._adapter

    def _target_info(self, target: str) -> dict[str, Any]:
        if self._adapter is not None and hasattr(self._adapter, "resolve_window"):
            resolved = self._adapter.resolve_window(target)
            if isinstance(resolved, dict):
                rect = resolved.get("rect")
                self._target_rect = tuple(rect) if rect else None
                return dict(resolved)
            self._target_rect = None
            return {"hwnd": int(resolved)}
        from . import win32

        resolved = win32.resolve_window(target)
        self._target_rect = tuple(resolved["rect"])
        return dict(resolved)

    def _target_hwnd(self, target: str) -> int:
        return int(self._target_info(target)["hwnd"])

    def _force_target_live(self, hwnd: int, expected_pid: int | None) -> bool:
        adapter = self._win()
        return bool(
            hwnd > 0
            and expected_pid
            and adapter.is_window(hwnd)
            and adapter.window_pid(hwnd) == expected_pid
        )

    def _force_focus(self, name: str, target: str) -> dict:
        before: int | None = None
        after: int | None = None
        binding: dict = {}

        def outcome(status, code=None, message=None, *, foreground=False, **extra):
            return result(
                status,
                name,
                method="AttachThreadInput+SetForegroundWindow",
                code=code,
                message=message,
                delivery="native",
                target_binding=binding,
                focus_changed=(
                    before != after if before is not None and after is not None else False
                ),
                target_foreground=foreground,
                may_have_acted=self._action_acted,
                **extra,
            )

        try:
            info = self._target_info(target)
        except Exception as exc:
            return outcome(
                "refused",
                getattr(exc, "code", "TARGET_NOT_FOUND"),
                str(exc),
            )
        hwnd = int(info["hwnd"])
        adapter = self._win()
        expected_pid = info.get("pid")
        if expected_pid is not None:
            if (
                isinstance(expected_pid, bool)
                or not isinstance(expected_pid, int)
                or expected_pid <= 0
            ):
                expected_pid = None
        if expected_pid is None:
            expected_pid = adapter.window_pid(hwnd)
        binding = {"hwnd": hwnd, "pid": expected_pid}
        if not self._force_target_live(hwnd, expected_pid):
            return outcome(
                "refused", "STALE_TARGET", "Target window disappeared or changed process"
            )

        before = adapter.foreground()
        minimized = adapter.is_iconic(hwnd)
        if before == hwnd and not minimized:
            after = adapter.foreground()
            if not self._force_target_live(hwnd, expected_pid):
                return outcome(
                    "refused", "STALE_TARGET", "Target window disappeared or changed process"
                )
            if after == hwnd:
                return outcome("verified", foreground=True, no_op=True)
            return outcome("error", "FOCUS_NOT_VERIFIED", "Foreground changed during verification")

        attached: list[int] = []
        attempted: set[int] = set()
        detach_failed: list[int] = []
        operation_error: Exception | None = None
        current_thread = 0
        try:
            self._cancel_checkpoint()
            if minimized:
                if not self._force_target_live(hwnd, expected_pid):
                    return outcome(
                        "refused", "STALE_TARGET", "Target window disappeared or changed process"
                    )
                self._action_acted = True
                adapter.show_window(hwnd, SW_RESTORE)
            current_thread = adapter.current_thread_id()
            current_foreground = adapter.foreground()
            foreground_thread = (
                adapter.window_thread_id(current_foreground) if current_foreground else 0
            )
            target_thread = adapter.window_thread_id(hwnd)
            tids = (foreground_thread, target_thread)
            for thread_id in tids:
                if not current_thread or not thread_id or thread_id == current_thread:
                    continue
                if thread_id in attempted:
                    continue
                attempted.add(thread_id)
                self._cancel_checkpoint()
                try:
                    if adapter.attach_thread_input(current_thread, thread_id, True):
                        attached.append(thread_id)
                except Exception:
                    # AttachThreadInput is an aid, not evidence of focus. Continue and
                    # require the exact postcondition below.
                    continue
            for activate in (
                lambda: adapter.bring_to_top(hwnd),
                lambda: adapter.show_window(hwnd, SW_SHOW),
                lambda: adapter.activate(hwnd),
            ):
                self._cancel_checkpoint()
                if not self._force_target_live(hwnd, expected_pid):
                    break
                self._action_acted = True
                activate()
        except OperationCancelled:
            raise
        except Exception as exc:
            operation_error = exc
        finally:
            for thread_id in reversed(attached):
                try:
                    if not adapter.attach_thread_input(current_thread, thread_id, False):
                        detach_failed.append(thread_id)
                except Exception:
                    detach_failed.append(thread_id)

        try:
            after = adapter.foreground()
            target_live = self._force_target_live(hwnd, expected_pid)
        except Exception as exc:
            return outcome("error", "FOCUS_NOT_VERIFIED", str(exc))
        if not target_live:
            return outcome(
                "refused", "STALE_TARGET", "Target window disappeared or changed process"
            )
        if after != hwnd:
            detail = "Foreground window did not become the exact target"
            if operation_error is not None:
                detail = f"{detail}: {operation_error}"
            return outcome("error", "FOCUS_NOT_VERIFIED", detail)
        if detach_failed:
            return outcome(
                "error", "FOCUS_FAILED", "Thread input detachment failed", foreground=True
            )
        return outcome("verified", foreground=True)

    def _send(self, events: list[_Input]) -> None:
        if not events:
            return
        # Even a failed SendInput/adapter call can have taken effect.
        self._action_acted = True
        try:
            sent = self._win().send(events)
            if sent != len(events):
                raise RuntimeError(f"SendInput accepted {sent} of {len(events)} input events")
        except Exception:
            if any(
                event.type == INPUT_KEYBOARD
                and event.ki.dwFlags & KEYEVENTF_KEYUP
                or event.type == INPUT_MOUSE
                and event.mi.dwFlags & (0x0004 | 0x0010 | 0x0040 | 0x0100)
                for event in events
            ):
                self._cleanup_required = True
            raise

    @staticmethod
    def _key_code(value: Any) -> int:
        return key_code(value)

    @staticmethod
    def _integer(action: dict, key: str) -> int:
        value = action.get(key)
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError(f"{key} must be an integer")
        return value

    @classmethod
    def _required_integer(cls, action: dict, key: str) -> int:
        if key not in action:
            raise ValueError(f"{key} is required")
        return cls._integer(action, key)

    def _validate_click_target(self, x: int, y: int, hwnd: int) -> None:
        adapter = self._win()
        if hasattr(adapter, "virtual_bounds"):
            left, top, width, height = adapter.virtual_bounds()
            if not (left <= x < left + width and top <= y < top + height):
                raise ValueError("click coordinates are outside the virtual desktop")
        if hasattr(adapter, "point_hits") and not adapter.point_hits(x, y, hwnd):
            raise ValueError("click point is covered by another window or not in the target")
        if hasattr(adapter, "window_rect"):
            left, top, right, bottom = adapter.window_rect(hwnd)
            if not (left <= x < right and top <= y < bottom):
                raise ValueError("click coordinates are outside the target window")
        elif self._target_rect is not None:
            left, top, right, bottom = self._target_rect
            if not (left <= x < right and top <= y < bottom):
                raise ValueError("click coordinates are outside the target window")

    def _checkpoint(self, hwnd: int) -> None:
        self._cancel_checkpoint()
        if self._win().foreground() != hwnd:
            raise PermissionError("target is no longer the foreground window")
        if self._expected_pid is not None:
            if self._win().window_pid(hwnd) != self._expected_pid:
                raise PermissionError("target window changed process")

    def _wait_checked(self, hwnd: int, seconds: float) -> None:
        """Wait on the owner thread, checking cancellation and focus throughout."""
        end = time.monotonic() + seconds
        self._checkpoint(hwnd)
        while (remaining := end - time.monotonic()) > 0:
            interval = min(0.010, remaining)
            if self._cancel_event is None:
                time.sleep(interval)
            elif self._cancel_event.wait(interval):
                raise OperationCancelled()
            self._checkpoint(hwnd)

    def _send_checked(self, hwnd: int, events: list[_Input]) -> None:
        """Check the target before each injected event, not just each action."""
        for event in events:
            self._checkpoint(hwnd)
            button = None
            up = False
            if event.type == INPUT_MOUSE:
                for name, (down_flag, up_flag, data) in _BUTTON_FLAGS.items():
                    if data and event.mi.mouseData != data:
                        continue
                    if event.mi.dwFlags & (down_flag | up_flag):
                        button, up = name, bool(event.mi.dwFlags & up_flag)
                        if not up:
                            self._held_buttons.add(name)
                        break
            self._send([event])
            if button and up:
                self._held_buttons.discard(button)

    def _press_key(self, hwnd: int, key: int) -> None:
        self._checkpoint(hwnd)
        if self._message_hwnd is not None:
            child = self._message_hwnd
            pid = self._message_pid
            if not pid or self._win().window_pid(child) != pid:
                raise PermissionError("message target disappeared")
            self._message_keys[key] = (child, pid)
            self._post_key(hwnd, child, pid, key)
            return
        event = _key_event(self._win(), key)
        # Track the requested VK, not wVk: scan-code events set wVk to zero.
        self._held_keys.add(key)
        self._send([event])

    def _post_key(self, root: int, child: int, pid: int, key: int, up=False, cleanup=False):
        adapter = self._win()
        if not cleanup:
            self._checkpoint(root)
        if adapter.window_pid(child) != pid or not adapter.is_window(child):
            raise PermissionError("message target changed process or disappeared")
        scan = adapter.map_key(key) & 0xFF
        lparam = 1 | (scan << 16) | ((1 << 24) if key in EXTENDED_KEYS else 0)
        if up:
            lparam |= (1 << 30) | (1 << 31)
        self._action_acted = True
        if not adapter.post_message(child, 0x101 if up else 0x100, key, lparam):
            if up:
                self._cleanup_required = True
            raise RuntimeError("PostMessage rejected keyboard event")

    def _release_key(self, hwnd: int, key: int, cleanup=False) -> None:
        if key in self._message_keys:
            child, pid = self._message_keys[key]
            self._post_key(hwnd, child, pid, key, up=True, cleanup=cleanup)
            del self._message_keys[key]
        elif key in self._held_keys:
            if not cleanup:
                self._checkpoint(hwnd)
            self._send([_key_event(self._win(), key, True)])
            self._held_keys.discard(key)

    def _choose_message_target(self, info: dict, key_method: str) -> None:
        self._message_hwnd = None
        self._message_pid = None
        process = str(info.get("process", info.get("process_name", ""))).replace("\\", "/")
        if not process and key_method != "scancode":
            # Process identity, never title/class-name guessing, chooses auto mode.
            pid = self._win().window_pid(int(info["hwnd"]))
            process = self._win().process_name(pid) if pid else ""
        browser = process.rsplit("/", 1)[-1].lower() in _BROWSERS
        if key_method == "scancode" or key_method == "auto" and not browser:
            return
        hwnd = int(info["hwnd"])
        children = self._win().render_children(hwnd) if browser else [hwnd]
        if len(children) != 1:
            raise ValueError("message mode requires exactly one qualifying browser render child")
        child = children[0]
        if not self._win().is_window(child) or not self._win().window_pid(child):
            raise ValueError("message target is not a live window")
        self._message_hwnd = child
        self._message_pid = self._win().window_pid(child)

    def _chord(self, hwnd: int, keys: list[int], seconds: float = 0) -> None:
        if any(key in self._held_keys or key in self._message_keys for key in keys):
            raise ValueError("key is already held by this backend")
        try:
            for key in keys:
                self._press_key(hwnd, key)
            if seconds:
                self._wait_checked(hwnd, seconds)
        finally:
            failures = []
            for key in reversed(keys):
                try:
                    self._release_key(hwnd, key, cleanup=True)
                except Exception as exc:
                    failures.append(str(exc))
            if failures:
                self._cleanup_required = True
                raise RuntimeError("key release failed")

    def _cursor(self, hwnd: int, x: int, y: int, validate=False) -> None:
        self._checkpoint(hwnd)
        if validate:
            self._validate_click_target(x, y, hwnd)
        self._checkpoint(hwnd)
        self._action_acted = True
        if not self._win().set_cursor(x, y):
            raise RuntimeError("SetCursorPos failed")

    @staticmethod
    def _button_event(button: str, up=False) -> _Input:
        down_flag, up_flag, data = _BUTTON_FLAGS[button]
        return _mouse(up_flag if up else down_flag, data=data)

    def _cancelled_result(self, name: str) -> dict:
        return result(
            "unknown" if self._action_acted else "refused",
            name,
            code="CANCELLED",
            message="Operation was cancelled",
            cancelled=True,
            may_have_acted=self._action_acted,
        )

    @contextmanager
    def _physical_coordinates(self):
        if self._coordinate_context_depth:
            yield
            return
        adapter = self._win()
        context = (
            adapter.physical_coordinates()
            if hasattr(adapter, "physical_coordinates")
            else nullcontext()
        )
        with context:
            self._coordinate_context_depth += 1
            try:
                yield
            finally:
                self._coordinate_context_depth -= 1

    def execute(self, action: dict, target: str, key_method: str = "scancode") -> dict:
        """Resolve, inject and clean up under owner-thread physical DPI mapping."""
        if (
            not isinstance(action, dict)
            or not self.enabled
            or self._closed
            or self._cleanup_required
            or (
                self._owner_thread_id is not None
                and self._owner_thread_id != threading.get_ident()
                and (
                    self._held_keys or self._held_unicode or self._held_mouse or self._message_keys
                )
            )
        ):
            return self._execute_scoped(action, target, key_method)
        self._action_acted = False
        try:
            with self._physical_coordinates():
                return self._execute_scoped(action, target, key_method)
        except _PhysicalCoordinatesError as exc:
            acted = self._action_acted
            # Releases carry no positions and must not depend on DPI setup.
            cleanup = self._release_all()
            response = result(
                "unknown" if acted else "refused",
                str(action.get("action", "")),
                code="PHYSICAL_COORDINATES_UNAVAILABLE",
                message=str(exc),
                may_have_acted=acted,
            )
            if cleanup.get("status") == "unknown":
                response["cleanup"] = cleanup
            return response

    def _execute_scoped(self, action: dict, target: str, key_method: str = "scancode") -> dict:
        """Execute on the caller's owner thread; cancellation never injects."""
        if not isinstance(action, dict):
            return result("refused", "", code="INVALID_ACTION", message="action must be an object")
        if (
            self._owner_thread_id is not None
            and self._owner_thread_id != threading.get_ident()
            and (self._held_keys or self._held_unicode or self._held_mouse or self._message_keys)
        ):
            return result(
                "refused",
                str(action.get("action", "")),
                code="CLEANUP_REQUIRED",
                message="Held native input belongs to another thread",
            )
        response = self._execute(action, target, key_method)
        if (
            response.get("status") not in {"invoked", "verified", "ok"}
            and response.get("error", {}).get("code") != "CLEANUP_REQUIRED"
            and (self._held_keys or self._held_buttons or self._message_keys or self._held_unicode)
        ):
            cleanup = self.release_all()
            if cleanup.get("status") == "unknown":
                response["cleanup"] = cleanup
        if (
            self.enabled
            and not self._closed
            and self._cancel_event is not None
            and self._cancel_event.is_set()
            and response.get("error", {}).get("code") != "CLEANUP_REQUIRED"
        ):
            cleanup = self.release_all()
            response = self._cancelled_result(str(action.get("action", "")))
            if cleanup.get("status") == "unknown":
                response["cleanup"] = cleanup
        if self._clipboard_cleanup is not None:
            response["clipboard_cleanup"] = self._clipboard_cleanup
            if self._clipboard_cleanup["restore_error"]:
                response["status"] = "unknown"
                response["error"] = {
                    "code": "CLIPBOARD_RESTORE_FAILED",
                    "message": "Clipboard text restoration failed; paste was not retried",
                }
                response["may_have_acted"] = self._action_acted
                response["consumption_verified"] = False
        return response

    def _execute(self, action: dict, target: str, key_method: str) -> dict:
        self._clipboard_cleanup = None
        name = str(action.get("action", ""))
        if not self.enabled:
            return result(
                "refused", name, code="NATIVE_DISABLED", message="Native input is disabled"
            )
        if self._closed:
            return result("refused", name, code="CLOSED", message="Native input backend is closed")
        if self._cleanup_required:
            return result(
                "refused", name, code="CLEANUP_REQUIRED", message="Native input cleanup is required"
            )
        try:
            action = normalize_native_action(action)
            name = action["action"]
            if not isinstance(key_method, str) or key_method not in {"scancode", "auto", "message"}:
                raise ValueError("key_method must be auto, scancode or message")
        except ValueError as exc:
            if self._held_keys or self._held_buttons or self._message_keys or self._held_unicode:
                self.release_all()
            return result("refused", name, code="INVALID_ACTION", message=str(exc))
        self._owner_thread_id = threading.get_ident()
        self._action_acted = False
        self._expected_pid = None
        self._message_hwnd = None
        hwnd: int | None = None
        if name not in {"focus", "force_focus"}:
            try:
                info = self._target_info(target)
                hwnd = int(info["hwnd"])
                if hasattr(self._win(), "window_pid"):
                    actual_pid = self._win().window_pid(hwnd)
                    self._expected_pid = info.get("pid", actual_pid)
                    if not actual_pid or actual_pid != self._expected_pid:
                        raise PermissionError("target window changed process")
                self._checkpoint(hwnd)
                if name in {"key", "hold", "key_down", "key_up", "text", "paste"}:
                    self._choose_message_target(info, key_method)
            except Exception as exc:
                if (
                    self._held_keys
                    or self._held_buttons
                    or self._message_keys
                    or self._held_unicode
                ):
                    self.release_all()
                return result("refused", name, code="FOREGROUND", message=str(exc))
        try:
            if name == "focus":
                self._cancel_checkpoint()
                info = self._target_info(target)
                hwnd = int(info["hwnd"])
                if hasattr(self._win(), "window_pid"):
                    pid = self._win().window_pid(hwnd)
                    if not pid or info.get("pid", pid) != pid:
                        raise PermissionError("target window changed process")
                self._cancel_checkpoint()
                self._action_acted = True
                ok = self._win().activate(hwnd)
                return result(
                    "invoked" if ok else "error",
                    name,
                    method="SetForegroundWindow",
                    code=None if ok else "FOCUS_FAILED",
                    message=None if ok else "Activation was rejected",
                )
            if name == "force_focus":
                self._cancel_checkpoint()
                return self._force_focus(name, target)
            if name in {"key", "hold"}:
                keycodes = action["key"]
                self._chord(hwnd, keycodes, action["seconds"])
                if self._cancel_event is not None and self._cancel_event.is_set():
                    raise OperationCancelled()
                return result(
                    "invoked",
                    name,
                    method="PostMessage" if self._message_hwnd else "SendInput",
                    data={"sent": len(keycodes)},
                    consumption_verified=False,
                )
            if name == "paste":
                if self._services is None:
                    from .automation_services import AutomationServices

                    self._services = AutomationServices()
                self._checkpoint(hwnd)
                # A clipboard write itself is an effect, even when subsequent
                # keyboard delivery is cancelled before its first event.
                self._action_acted = True
                token = None
                try:
                    with self._services.clipboard_session(
                        action["text"], cancel_event=self._cancel_event
                    ) as token:
                        self._checkpoint(hwnd)
                        self._chord(hwnd, [17, 86])
                        # A bounded opportunity for asynchronous paste handling,
                        # not evidence that the application consumed the text.
                        self._wait_checked(hwnd, 0.020)
                finally:
                    restore_error = getattr(token, "restore_error", None)
                    self._clipboard_cleanup = {
                        "restored": getattr(token, "restored", None),
                        "restore_skipped": getattr(token, "restore_skipped", None),
                        # Do not echo arbitrary service exception text/clipboard.
                        "restore_error": ("CLIPBOARD_RESTORE_FAILED" if restore_error else None),
                    }
                return result(
                    "invoked", name, method="clipboard+keyboard", consumption_verified=False
                )
            if name == "text":
                text_value = action.get("text", action.get("value"))
                if text_value is None or not isinstance(text_value, str):
                    return result(
                        "refused", name, code="INVALID_ACTION", message="text or value is required"
                    )
                text = text_value
                if len(text) > 10000:
                    return result(
                        "refused", name, code="BOUNDS", message="text exceeds 10000 characters"
                    )
                # Treat CRLF as one Enter unless literal Unicode was requested.
                typed_text = text if action["literal"] else text.replace("\r\n", "\n")
                for char in typed_text:
                    self._checkpoint(hwnd)
                    if not action["literal"] and char in "\n\r\t":
                        self._chord(hwnd, [9 if char == "\t" else 13])
                        continue
                    encoded = char.encode("utf-16-le")
                    units = [
                        int.from_bytes(encoded[i : i + 2], "little")
                        for i in range(0, len(encoded), 2)
                    ]
                    for unit in units:
                        self._checkpoint(hwnd)
                        if self._message_hwnd is not None:
                            child = self._message_hwnd
                            pid = self._message_pid
                            self._checkpoint(hwnd)
                            if (
                                not pid
                                or self._win().window_pid(child) != pid
                                or not self._win().is_window(child)
                            ):
                                raise PermissionError("message target disappeared")
                            self._action_acted = True
                            if not self._win().post_message(child, 0x102, unit, 1):
                                raise RuntimeError("PostMessage rejected character")
                            continue
                        self._held_unicode.append(unit)
                        input_error: Exception | None = None
                        try:
                            self._send([_unicode(chr(unit))])
                        except Exception as exc:
                            input_error = exc
                        finally:
                            try:
                                self._send([_unicode(chr(unit), True)])
                                self._held_unicode.remove(unit)
                            except Exception as exc:
                                input_error = input_error or exc
                        if input_error is not None:
                            raise RuntimeError(f"Unicode input/release failed: {input_error}")
                return result(
                    "invoked",
                    name,
                    method="PostMessage" if self._message_hwnd else "SendInput",
                    data={"characters": len(text)},
                    consumption_verified=False,
                )
            if name in {"move", "click"}:
                x, y = (
                    (action["x"], action["y"]) if "x" in action else self._win().cursor_position()
                )
                button = action.get("button", "left")
                if name == "click" and button in self._held_buttons:
                    raise ValueError("mouse button is already held")
                modifiers = action.get("modifiers", [])
                if any(key in self._held_keys or key in self._message_keys for key in modifiers):
                    raise ValueError("modifier already held")
                if "x" in action:
                    self._cursor(hwnd, x, y, validate=name == "click")
                else:
                    self._validate_click_target(x, y, hwnd)
                if name == "click":
                    try:
                        for key in modifiers:
                            self._press_key(hwnd, key)
                        for _ in range(action["clicks"]):
                            self._wait_checked(hwnd, _CLICK_SETTLE_SECONDS)
                            if "x" not in action and self._win().cursor_position() != (x, y):
                                raise ValueError("cursor moved during target validation")
                            self._validate_click_target(x, y, hwnd)
                            self._send_checked(hwnd, [self._button_event(button)])
                            self._wait_checked(hwnd, _CLICK_PRESS_SECONDS)
                            self._send_checked(hwnd, [self._button_event(button, True)])
                    finally:
                        for key in reversed(modifiers):
                            self._release_key(hwnd, key, cleanup=True)
                return result(
                    "invoked", name, method="SetCursorPos" if name == "move" else "SendInput"
                )
            if name in {"mouse_down", "mouse_up"}:
                button = action["button"]
                if name == "mouse_up" and button not in self._held_buttons:
                    return result(
                        "refused",
                        name,
                        code="NOT_HELD",
                        message="Mouse button is not held by this backend",
                    )
                if name == "mouse_down" and button in self._held_buttons:
                    raise ValueError("mouse button is already held")
                if "x" in action:
                    self._cursor(hwnd, action["x"], action["y"], validate=True)
                elif name == "mouse_down":
                    x, y = self._win().cursor_position()
                    self._validate_click_target(x, y, hwnd)
                self._send_checked(hwnd, [self._button_event(button, name == "mouse_up")])
            elif name == "drag":
                button = action["button"]
                if button in self._held_buttons:
                    raise ValueError("mouse button already held")
                points = action.get("path", [action.get("to")])
                # Validate all points before moving or pressing the pointer.
                for point in [{"x": action["x"], "y": action["y"]}, *points]:
                    self._validate_click_target(point["x"], point["y"], hwnd)
                self._cursor(hwnd, action["x"], action["y"], validate=True)
                started = time.monotonic()
                self._send_checked(hwnd, [self._button_event(button)])
                try:
                    dwell = action["press_dwell_ms"] / 1000
                    self._wait_checked(hwnd, max(0, started + dwell - time.monotonic()))
                    interval = (action["seconds"] - dwell) / len(points)
                    previous = {"x": action["x"], "y": action["y"]}
                    for index, point in enumerate(points):
                        steps = max(1, math_ceil(interval / 0.010))
                        for step in range(1, steps + 1):
                            due = started + dwell + interval * (index + step / steps)
                            self._wait_checked(hwnd, max(0, due - time.monotonic()))
                            x = round(previous["x"] + (point["x"] - previous["x"]) * step / steps)
                            y = round(previous["y"] + (point["y"] - previous["y"]) * step / steps)
                            self._cursor(hwnd, x, y, validate=True)
                        previous = point
                    self._send_checked(hwnd, [self._button_event(button, True)])
                finally:
                    if button in self._held_buttons:
                        self._send([self._button_event(button, True)])
                        self._held_buttons.discard(button)
            elif name == "look":
                if "dx" not in action or "dy" not in action:
                    return result(
                        "refused", name, code="INVALID_ACTION", message="dx and dy are required"
                    )
                self._checkpoint(hwnd)
                dx = self._required_integer(action, "dx")
                dy = self._required_integer(action, "dy")
                self._send_checked(hwnd, [_mouse(MOUSEEVENTF_MOVE, dx, dy)])
            elif name == "wheel":
                if "amount" not in action and "delta" not in action:
                    return result(
                        "refused", name, code="INVALID_ACTION", message="amount is required"
                    )
                self._checkpoint(hwnd)
                amount = action.get("amount", action.get("delta"))
                if isinstance(amount, bool) or not isinstance(amount, int):
                    raise ValueError("amount must be an integer")
                flag = MOUSEEVENTF_HWHEEL if action["horizontal"] else MOUSEEVENTF_WHEEL
                self._send_checked(hwnd, [_mouse(flag, data=amount)])
            elif name == "key_down":
                if "key" not in action:
                    return result("refused", name, code="INVALID_ACTION", message="key is required")
                keys = action["key"] if isinstance(action["key"], list) else [action["key"]]
                if any(key in self._held_keys or key in self._message_keys for key in keys):
                    raise ValueError("key is already held")
                for key in keys:
                    self._press_key(hwnd, key)
            elif name == "key_up":
                if "key" not in action:
                    return result("refused", name, code="INVALID_ACTION", message="key is required")
                keys = action["key"] if isinstance(action["key"], list) else [action["key"]]
                if any(
                    key not in self._held_keys and key not in self._message_keys for key in keys
                ):
                    return result(
                        "refused", name, code="NOT_HELD", message="Key is not held by this backend"
                    )
                for key in reversed(keys):
                    self._release_key(hwnd, key)
            return result(
                "invoked",
                name,
                method="PostMessage" if self._message_hwnd else "SendInput",
                consumption_verified=False,
            )
        except OperationCancelled:
            self.release_all()
            return self._cancelled_result(name)
        except ValueError as exc:
            self.release_all()
            return result(
                "unknown" if self._action_acted else "refused",
                name,
                code="INVALID_ACTION",
                message="Clipboard paste failed" if name == "paste" else str(exc),
                may_have_acted=self._action_acted,
            )
        except PermissionError as exc:
            self.release_all()
            return result(
                "unknown",
                name,
                code="FOREGROUND_LOST",
                message="Clipboard paste failed" if name == "paste" else str(exc),
                may_have_acted=True,
            )
        except Exception as exc:
            self.release_all()
            return result(
                "unknown",
                name,
                code="INPUT_FAILED",
                message="Clipboard paste failed" if name == "paste" else str(exc),
                may_have_acted=True,
            )

    def release_all(self) -> dict:
        if not (
            self._held_keys or self._held_unicode or self._held_mouse or self._message_keys
        ) or (self._owner_thread_id is not None and self._owner_thread_id != threading.get_ident()):
            return self._release_all()
        try:
            with self._physical_coordinates():
                return self._release_all()
        except _PhysicalCoordinatesError:
            # Cleanup contains only owned up-events, no coordinates. Do not strand
            # held input when the OS refuses an awareness transition.
            return self._release_all()

    def _release_all(self) -> dict:
        # Cleanup only concerns our own recorded holds and deliberately ignores
        # both foreground and cancellation.  Never inject from a cancel thread.
        if (
            self._owner_thread_id is not None
            and self._owner_thread_id != threading.get_ident()
            and (self._held_keys or self._held_unicode or self._held_mouse or self._message_keys)
        ):
            self._cleanup_required = True
            return result(
                "unknown",
                "release_all",
                code="CLEANUP_FAILED",
                message="Held native input must be released by its owner thread",
                may_have_acted=True,
            )
        if not (self._held_keys or self._held_unicode or self._held_mouse or self._message_keys):
            self._cleanup_required = False
            return result("ok", "release_all", method="SendInput", data={"released": 0})
        released = 0
        failures: list[str] = []
        for key in list(self._message_keys):
            try:
                self._release_key(0, key, cleanup=True)
                released += 1
            except Exception:
                failures.append("message key release failed")
        for key in sorted(self._held_keys):
            try:
                self._send([_key_event(self._win(), key, True)])
                self._held_keys.discard(key)
                released += 1
            except Exception as exc:
                failures.append(str(exc))
        for unit in list(self._held_unicode):
            try:
                self._send([_unicode(chr(unit), True)])
                self._held_unicode.remove(unit)
                released += 1
            except Exception as exc:
                failures.append(str(exc))
        for button in sorted(self._held_buttons):
            try:
                self._send([self._button_event(button, True)])
                self._held_buttons.discard(button)
                released += 1
            except Exception as exc:
                failures.append(str(exc))
        if self._held_keys or self._held_unicode or self._held_mouse or self._message_keys:
            self._cleanup_required = True
            return result(
                "unknown",
                "release_all",
                code="CLEANUP_FAILED",
                message="; ".join(failures) or "one or more releases were not accepted",
                may_have_acted=True,
            )
        self._cleanup_required = False
        return result("invoked", "release_all", method="SendInput", data={"released": released})

    def close(self) -> None:
        self.release_all()
        self._closed = True
