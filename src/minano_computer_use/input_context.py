"""Bounded read-only observations; never acquire cross-process IMM contexts."""

from __future__ import annotations

import ctypes
import time
from ctypes import wintypes
from typing import Any


class _GuiThreadInfo(ctypes.Structure):
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


class _Win32Context:
    """Only local queries with no provider/window-message round trips."""

    def __init__(self, adapter: Any) -> None:
        self.user32 = adapter.user32

    def focus_hwnd(self, thread_id: int) -> int:
        fn = self.user32.GetGUIThreadInfo
        fn.argtypes = [wintypes.DWORD, ctypes.POINTER(_GuiThreadInfo)]
        fn.restype = wintypes.BOOL
        info = _GuiThreadInfo(cbSize=ctypes.sizeof(_GuiThreadInfo))
        if not fn(thread_id, ctypes.byref(info)):
            raise OSError("GetGUIThreadInfo failed")
        return int(info.hwndFocus or 0)

    def keyboard_layout(self, thread_id: int) -> int:
        fn = self.user32.GetKeyboardLayout
        fn.argtypes = [wintypes.DWORD]
        fn.restype = ctypes.c_void_p
        hkl = int(fn(thread_id) or 0)
        if not hkl:
            raise OSError("GetKeyboardLayout failed")
        return hkl

    def is_ime(self, hkl: int) -> bool:
        fn = ctypes.windll.imm32.ImmIsIME
        fn.argtypes = [ctypes.c_void_p]
        fn.restype = wintypes.BOOL
        return bool(fn(ctypes.c_void_p(hkl)))

    def modifier_down(self, vk: int) -> bool | None:
        fn = self.user32.GetAsyncKeyState
        fn.argtypes = [ctypes.c_int]
        fn.restype = ctypes.c_short
        state = int(fn(vk))
        # Zero also indicates an inaccessible/inactive desktop. This API has no
        # reliable error discriminator, so zero cannot certify that a key is up.
        # Only the high bit describes current state; the unreliable low bit is
        # ignored and is not evidence of a recent key event.
        return bool(state & 0x8000) if state else None


_SOURCES = {
    "foreground": "GetForegroundWindow+GetWindowThreadProcessId",
    "focus_hwnd": "GetGUIThreadInfo",
    "keyboard_layout": "GetKeyboardLayout",
    "is_ime": "ImmIsIME",
    "ime_open": "cross_process_context_unavailable",
    "ime_conversion": "cross_process_context_unavailable",
    "modifiers": "GetAsyncKeyState",
}


def read_input_context(
    adapter: Any,
    target_binding: dict[str, Any],
    *,
    source: Any = None,
    cancel_event: Any = None,
    budget_seconds: float = 0.050,
) -> dict[str, Any]:
    """Observe on the owner thread with checkpoints between a fixed set of calls.

    The injected source implements focus_hwnd, keyboard_layout, is_ime and
    modifier_down: each is a cheap local query, never an arbitrary provider call.
    A budget limits subsequent queries, not the duration of an individual OS API.
    """
    started = time.monotonic()
    deadline = started + min(0.250, max(0.001, budget_seconds))
    fields = {
        name: {
            "status": "unknown",
            "value": None,
            "source": api,
            "observed_at": None,
            "consistency": "unknown",
        }
        for name, api in _SOURCES.items()
    }
    response: dict[str, Any] = {
        "status": "unknown",
        "non_atomic": True,
        "started_at": started,
        "finished_at": None,
        "fields": fields,
    }

    def checkpoint() -> None:
        if cancel_event is not None and cancel_event.is_set():
            raise InterruptedError("cancelled")
        if time.monotonic() >= deadline:
            raise TimeoutError("budget_exceeded")

    def query(name: str, function):
        checkpoint()
        try:
            value = function()
        except (InterruptedError, TimeoutError):
            raise
        except Exception:
            fields[name]["reason"] = "unavailable"
            return None
        checkpoint()
        fields[name].update(
            status="known", value=value, observed_at=time.monotonic(), consistency="non_atomic"
        )
        return value

    def binding_snapshot() -> dict:
        checkpoint()
        hwnd = int(adapter.foreground())
        checkpoint()
        pid = adapter.window_pid(hwnd) if hwnd else None
        checkpoint()
        target_hwnd = int(target_binding["hwnd"])
        target_pid = adapter.window_pid(target_hwnd)
        checkpoint()
        thread_id = int(adapter.window_thread_id(target_hwnd))
        checkpoint()
        identity = {"hwnd": target_hwnd, "pid": target_pid, "process_creation_time": None}
        if target_pid:
            if hasattr(adapter, "window_identity"):
                identity = adapter.window_identity(target_hwnd, target_pid)
            elif hasattr(adapter, "process_creation_time"):
                identity["process_creation_time"] = adapter.process_creation_time(target_pid)
            elif hasattr(adapter, "user32"):
                from .win32 import window_identity

                identity = window_identity(target_hwnd, target_pid)
        checkpoint()
        return {
            "hwnd": hwnd,
            "pid": pid,
            "target_pid": target_pid,
            "target_thread_id": thread_id,
            "target_identity": identity,
        }

    def binding_matches(snapshot: dict) -> bool:
        identity = snapshot["target_identity"]
        expected_creation = target_binding.get("process_creation_time")
        return (
            snapshot["target_pid"] == target_binding.get("pid")
            and identity.get("hwnd") == target_binding["hwnd"]
            and identity.get("pid") == target_binding.get("pid")
            and (
                expected_creation is None
                or identity.get("process_creation_time") == expected_creation
            )
        )

    try:
        before = binding_snapshot()
        response["binding_before"] = before
        expected_pid = target_binding.get("pid")
        if expected_pid is None or (
            target_binding.get("process_creation_time") is not None
            and before["target_identity"].get("process_creation_time") is None
        ):
            response["reason"] = "target_binding_unverified"
            return response
        if not binding_matches(before):
            response["reason"] = "binding_changed"
            return response
        query(
            "foreground",
            lambda: {
                "hwnd": before["hwnd"],
                "pid": before["pid"],
                "matches_target": before["hwnd"] == target_binding["hwnd"]
                and before["pid"] == expected_pid,
            },
        )
        checkpoint()
        thread_id = before["target_thread_id"]
        if not thread_id:
            raise OSError("target thread unavailable")
        if source is None:
            source = _Win32Context(adapter)
        query("focus_hwnd", lambda: int(source.focus_hwnd(thread_id)))
        hkl = query("keyboard_layout", lambda: int(source.keyboard_layout(thread_id)))
        if hkl:
            fields["keyboard_layout"]["value"] = {"hkl": hkl, "language_id": hkl & 0xFFFF}
            query("is_ime", lambda: bool(source.is_ime(hkl)))
        # Open/conversion stay explicitly unknown: ImmGetContext on another
        # process is not a reliable context or timeout boundary.
        modifiers = {}
        for name, vks in {
            "shift": (0x10,),
            "ctrl": (0x11,),
            "alt": (0x12,),
            "win": (0x5B, 0x5C),
        }.items():
            states = []
            for vk in vks:
                checkpoint()
                state = source.modifier_down(vk)
                states.append(state if isinstance(state, bool) else None)
                checkpoint()
            modifiers[name] = (
                True
                if any(state is True for state in states)
                else None
                if any(state is None for state in states)
                else False
            )
        query("modifiers", lambda: modifiers)
        unknown_modifiers = [name for name, state in modifiers.items() if state is None]
        if unknown_modifiers:
            all_unknown = len(unknown_modifiers) == len(modifiers)
            fields["modifiers"].update(
                status="unknown" if all_unknown else "partial",
                consistency="unknown" if all_unknown else "partial_unknown",
                reason="ambiguous_or_unavailable",
                unknown=unknown_modifiers,
            )
        after = binding_snapshot()
        response["binding_after"] = after
        stable = before == after and binding_matches(after)
        if not stable:
            for value in fields.values():
                value.update(status="unknown", value=None, consistency="changed")
            response["reason"] = "binding_changed"
        else:
            for value in fields.values():
                if value["status"] == "known":
                    value["consistency"] = "consistent_non_atomic"
            response["status"] = "partial"  # IME open/conversion are unknown.
    except InterruptedError:
        response["reason"] = "cancelled"
    except TimeoutError:
        response["reason"] = "budget_exceeded"
    except Exception as exc:
        response["reason"] = (
            "binding_changed" if getattr(exc, "code", None) == "STALE_TARGET" else "unavailable"
        )
    finally:
        response["finished_at"] = time.monotonic()
        # An unfinished binding check cannot certify the intervening values.
        if response["status"] == "unknown":
            for value in fields.values():
                value.update(status="unknown", value=None)
    return response
