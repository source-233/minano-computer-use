"""Explicit background HWND messages; never a physical-input fallback.

The injectable adapter is for unit tests. Production Win32 work is exclusively
performed by the disposable, fixed message worker.
"""

from __future__ import annotations

import ctypes
import math
import time
from contextlib import nullcontext

from .contracts import result
from .input_plan import EXTENDED_KEYS, bounded_seconds, normalize_native_action

_MOUSE = {"move", "click", "mouse_down", "mouse_up", "drag", "wheel"}
_BUTTONS = {
    "left": (0x201, 0x202, 0x203, 1, 0),
    "right": (0x204, 0x205, 0x206, 2, 0),
    "middle": (0x207, 0x208, 0x209, 16, 0),
    "x1": (0x20B, 0x20C, 0x20D, 32, 1),
    "x2": (0x20B, 0x20C, 0x20D, 64, 2),
}


def _hwnd(value):
    if isinstance(value, str) and value.startswith("hwnd:"):
        try:
            value = int(value[5:], 0)
        except ValueError:
            raise ValueError("recipient must be an HWND") from None
    if isinstance(value, bool) or not isinstance(value, int) or not 0 < value < 2**64:
        raise ValueError("recipient must be a positive pointer-sized HWND")
    if value >= 1 << (ctypes.sizeof(ctypes.c_void_p) * 8):
        raise ValueError("recipient exceeds pointer size")
    return value


def normalize_message_action(action: dict) -> dict:
    """Pure whole-plan validation, with native-compatible aliases and envelopes."""
    if not isinstance(action, dict) or any(not isinstance(k, str) for k in action):
        raise ValueError("action must be an object with string field names")
    data = dict(action)
    envelope = {k: data.pop(k) for k in ("at", "timeout", "target") if k in data}
    if "at" in envelope:
        envelope["at"] = bounded_seconds(envelope["at"], "at", 120)
    if "timeout" in envelope:
        envelope["timeout"] = bounded_seconds(envelope["timeout"], "timeout", 120)
        if not envelope["timeout"]:
            raise ValueError("timeout must be positive")
    if "target" in envelope and (
        not isinstance(envelope["target"], str) or not envelope["target"].strip()
    ):
        raise ValueError("target must be a nonempty string")
    recipient = _hwnd(data.pop("recipient")) if "recipient" in data else None
    name = data.get("action", data.get("type"))
    if name == "set_value":
        if "action" in data and "type" in data:
            raise ValueError("conflicting action aliases")
        if set(data) - {"action", "type", "value"}:
            raise ValueError("set_value accepts only value and recipient")
        value = data.get("value")
        if not isinstance(value, str) or len(value) > 10000:
            raise ValueError("value must be a string of at most 10000 characters")
        data = {"action": "set_value", "value": value}
    else:
        # Native wheel does not carry a point; message wheel explicitly does.
        point = {}
        if name in {"wheel", "scroll"}:
            point_fields = {
                k: data.pop(k) for k in ("x", "y", "coordinate", "coordinates") if k in data
            }
            point = normalize_native_action({"action": "move", **point_fields})
            point.pop("action")
        data = normalize_native_action(data)
        data.update(point)
        if data["action"] not in {"key", "hold", "key_down", "key_up", "text", *_MOUSE}:
            raise ValueError("unsupported background message action")
        if data["action"] in _MOUSE - {"mouse_up"} and "x" not in data:
            raise ValueError("message mouse actions require physical screen x/y")
        if data["action"] == "wheel" and not -32768 <= data["amount"] <= 32767:
            raise ValueError("wheel amount must fit signed16")
    text = data.get("value", data.get("text"))
    if text is not None:
        if "\0" in text:
            raise ValueError("message text must not contain NUL")
        try:
            text.encode("utf-16-le")
        except UnicodeError:
            raise ValueError("message text contains invalid Unicode") from None
    if recipient is not None:
        data["recipient"] = recipient
    data.update(envelope)
    return data


class _MessageError(Exception):
    def __init__(self, code):
        self.code = code


def _packed(x, y):
    if not -32768 <= x <= 32767 or not -32768 <= y <= 32767:
        raise _MessageError("COORDINATE_OVERFLOW")
    return (x & 0xFFFF) | ((y & 0xFFFF) << 16)


class BackgroundInput:
    def __init__(self, adapter=None):
        self._adapter = adapter
        self._broker = None
        self._cancel_event = None
        self._closed = False
        self._lost_holds = False
        self._possible_holds = False
        self._cleanup_required = False
        self._bindings = {}
        self._keys = {}
        self._buttons = {}
        self._deadline = 0.0
        self._acted = False

    def set_cancel_event(self, event):
        if event is not None and not hasattr(event, "is_set"):
            raise TypeError("cancel event must provide is_set()")
        self._cancel_event = event

    def _response(self, status, name, **extra):
        extra.setdefault("target_binding", None)
        return result(
            status,
            name,
            method="message",
            delivery="message",
            dispatch="SendMessageTimeoutW",
            consumption_verified=False,
            **extra,
        )

    def _remote(self, action, target=None, timeout=5.0):
        name = action["action"]
        if self._lost_holds:
            return self._response(
                "unknown",
                name,
                code="CLEANUP_UNCONFIRMED",
                may_have_acted=True,
                cleanup_confirmed=False,
                target_binding=self._bindings.get(target),
            )
        if self._broker is None:
            from .broker import UIABroker
            from .message_worker import worker_main

            self._broker = UIABroker(worker_target=worker_main)
        deadline = time.monotonic() + timeout
        if name != "message_release" and target not in self._bindings:
            # Read-only handshake puts the original pin in the parent BEFORE any
            # mutation. Even a subsequently killed worker cannot lose this evidence.
            bound = self._broker.call(
                {"action": "inspect", "message_bind": True, "target": target},
                timeout=timeout,
                cancel_event=self._cancel_event,
            )
            if bound.get("status") != "ok" or not bound.get("target_binding"):
                bound.pop("_message_worker", None)
                bound.pop("_message_holds", None)
                bound.update(
                    operation=name, method="message", delivery="message", consumption_verified=False
                )
                if "error" in bound:
                    bound["error"]["message"] = "Message target binding was not confirmed"
                if self._possible_holds and self._broker._process is None:
                    self._lost_holds = True
                    bound.update(status="unknown", may_have_acted=True, cleanup_confirmed=False)
                return bound
            self._bindings[target] = bound["target_binding"]
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return self._response(
                "refused",
                name,
                code="TIMEOUT",
                may_have_acted=False,
                target_binding=self._bindings.get(target),
            )
        response = self._broker.call(
            {
                # UIABroker's UIA READ_ACTIONS contains 'text'. A message text
                # action is a WRITE, so never use its canonical verb for the
                # broker's uncertainty classification.
                "action": "message_release" if name == "message_release" else "message_execute",
                "item": action,
                "target": target,
                "timeout": remaining,
                "binding": self._bindings.get(target),
            },
            timeout=remaining,
            cancel_event=None if name == "message_release" else self._cancel_event,
        )
        worker_reply = response.pop("_message_worker", False)
        response["operation"] = name
        if not worker_reply and (
            response.get("may_have_acted")
            or (self._possible_holds and self._broker._process is None)
        ):
            if self._possible_holds or name in {
                "key",
                "hold",
                "key_down",
                "click",
                "mouse_down",
                "drag",
                "message_release",
            }:
                self._lost_holds = True
                response.update(
                    status="unknown",
                    may_have_acted=True,
                    cleanup_confirmed=False,
                    cleanup_unconfirmed=True,
                )
        if worker_reply:
            self._possible_holds = response.pop("_message_holds", self._possible_holds)
        response.setdefault("method", "message")
        response.setdefault("dispatch", "SendMessageTimeoutW")
        response.update(delivery="message")
        response.setdefault("consumption_verified", False)
        if name != "message_release":
            if response.get("target_binding") is None:
                response["target_binding"] = self._bindings.get(target)
            response.setdefault(
                "observations",
                {"before": None, "after": None, "unavailable": "WORKER_RESULT_UNAVAILABLE"},
            )
        # Broker diagnostics can contain arbitrary exception text: do not expose
        # entered text through a transport failure. Codes retain useful evidence.
        if not worker_reply and "error" in response:
            response["error"]["message"] = "Message worker did not return a confirmed result"
        return response

    def _checkpoint(self, binding, recipient=None, cleanup=False):
        if not cleanup and self._cancel_event is not None and self._cancel_event.is_set():
            raise _MessageError("CANCELLED")
        if time.monotonic() >= self._deadline:
            raise _MessageError("TIMEOUT")
        actual = self._adapter.identity(binding["hwnd"], binding["pid"])
        if actual != binding or not actual.get("process_creation_time"):
            raise _MessageError("STALE_TARGET")
        if recipient is not None and (
            not self._adapter.is_window(recipient)
            or self._adapter.window_pid(recipient) != binding["pid"]
            or (
                recipient != binding["hwnd"]
                and not self._adapter.is_child(binding["hwnd"], recipient)
            )
        ):
            raise _MessageError("INVALID_RECIPIENT")

    def _bind(self, target):
        if not isinstance(target, str) or not target.strip():
            raise _MessageError("INVALID_TARGET")
        if target not in self._bindings:
            info = self._adapter.resolve_window(target)
            binding = self._adapter.identity(info["hwnd"], info["pid"])
            if not binding.get("process_creation_time"):
                raise _MessageError("IDENTITY_UNAVAILABLE")
            for field in ("hwnd", "pid", "process_creation_time"):
                if field in info and info[field] != binding[field]:
                    raise _MessageError("STALE_TARGET")
            self._bindings[target] = binding
        binding = self._bindings[target]
        self._checkpoint(binding)
        return binding

    def _recipient(self, action, binding):
        if "recipient" in action:
            recipient = action["recipient"]
        elif action["action"] in _MOUSE:
            recipient = binding["hwnd"]
            x, y = action["x"], action["y"]
            for _ in range(32):
                self._checkpoint(binding, recipient)
                point = self._adapter.screen_to_client(recipient, x, y)
                child = self._adapter.child_from_point(recipient, *point)
                if not child or child == recipient:
                    break
                self._checkpoint(binding, child)
                recipient = child
            else:
                raise _MessageError("CHILD_DEPTH_EXCEEDED")
        else:
            recipient = self._adapter.thread_focus(binding["hwnd"]) or binding["hwnd"]
            try:
                self._checkpoint(binding, recipient)
            except _MessageError as exc:
                if exc.code != "INVALID_RECIPIENT":
                    raise
                recipient = binding["hwnd"]
        self._checkpoint(binding, recipient)
        return recipient

    def _dispatch(self, binding, recipient, message, wparam, lparam, cleanup=False):
        self._checkpoint(binding, recipient, cleanup)
        milliseconds = max(1, min(500, int((self._deadline - time.monotonic()) * 1000)))
        # All payloads use the same synchronous send mechanism as set_value.
        # Queuing WM_CHAR then sending WM_SETTEXT could reorder our own writes.
        # Window-procedure completion still does not prove application consumption.
        self._acted = True
        if not self._adapter.dispatch_message(recipient, message, wparam, lparam, milliseconds):
            raise _MessageError("MESSAGE_DISPATCH_FAILED")

    def _wait(self, binding, recipient, seconds):
        end = time.monotonic() + seconds
        while True:
            self._checkpoint(binding, recipient)
            remaining = end - time.monotonic()
            if remaining <= 0:
                return
            time.sleep(min(0.01, remaining))

    def _key(self, binding, recipient, key, up=False, cleanup=False):
        if up:
            if key not in self._keys:
                raise _MessageError("NOT_HELD")
            old_binding, old_recipient = self._keys[key]
            if old_binding != binding or (not cleanup and old_recipient != recipient):
                raise _MessageError("HELD_RECIPIENT_MISMATCH")
            recipient = old_recipient
        elif key in self._keys:
            raise _MessageError("ALREADY_HELD")
        scan = self._adapter.map_key(key) & 255
        lparam = 1 | scan << 16 | (int(key in EXTENDED_KEYS) << 24)
        if up:
            lparam |= 3 << 30
        else:
            self._keys[key] = (dict(binding), recipient)
        self._dispatch(binding, recipient, 0x101 if up else 0x100, key, lparam, cleanup)
        if up:
            del self._keys[key]

    def _flags(self, recipient):
        flags = 0
        for button, (_, hwnd, _) in self._buttons.items():
            if hwnd == recipient:
                flags |= _BUTTONS[button][3]
        if any(key in self._keys and self._keys[key][1] == recipient for key in (16, 160, 161)):
            flags |= 4
        if any(key in self._keys and self._keys[key][1] == recipient for key in (17, 162, 163)):
            flags |= 8
        return flags

    def _point(self, binding, recipient, x, y, cleanup=False):
        self._checkpoint(binding, recipient, cleanup)
        if not cleanup and (
            self._adapter.is_iconic(binding["hwnd"])
            or not self._adapter.is_visible(binding["hwnd"])
            or not self._adapter.is_visible(recipient)
        ):
            raise _MessageError("MOUSE_TARGET_NOT_VISIBLE")
        point = self._adapter.screen_to_client(recipient, x, y)
        left, top, right, bottom = self._adapter.client_rect(recipient)
        if not cleanup and not (left <= point[0] < right and top <= point[1] < bottom):
            raise _MessageError("POINT_OUTSIDE_RECIPIENT")
        return _packed(*point)

    def _button(self, binding, recipient, button, x, y, up=False, double=False, cleanup=False):
        if up:
            if button not in self._buttons:
                raise _MessageError("NOT_HELD")
            old_binding, old_recipient, _ = self._buttons[button]
            if old_binding != binding or old_recipient != recipient:
                raise _MessageError("HELD_RECIPIENT_MISMATCH")
        elif button in self._buttons:
            raise _MessageError("ALREADY_HELD")
        lparam = self._point(binding, recipient, x, y, cleanup)
        down, release, dbl, _, extra = _BUTTONS[button]
        if not up:
            self._buttons[button] = (dict(binding), recipient, (x, y))
        flags = self._flags(recipient)
        if up:
            flags &= ~_BUTTONS[button][3]
        self._dispatch(
            binding,
            recipient,
            release if up else dbl if double else down,
            flags | extra << 16,
            lparam,
            cleanup,
        )
        if up:
            del self._buttons[button]

    def _sync(self, binding, recipient, message, wparam, payload):
        self._checkpoint(binding, recipient)
        if message == 0xC:
            self._acted = True
        milliseconds = max(1, min(500, int((self._deadline - time.monotonic()) * 1000)))
        ok, value = self._adapter.send_timeout(recipient, message, wparam, payload, milliseconds)
        if not ok:
            raise _MessageError("SEND_MESSAGE_TIMEOUT")
        return value

    def _perform(self, action, binding, recipient):
        name = action["action"]
        if name == "text":
            encoded = action["text"].encode("utf-16-le")
            for index in range(0, len(encoded), 2):
                unit = int.from_bytes(encoded[index : index + 2], "little")
                self._dispatch(binding, recipient, 0x102, unit, 1)
        elif name == "set_value":
            if self._adapter.class_name(recipient).casefold() not in {
                "edit",
                "richedit",
                "richedit20w",
                "richedit50w",
                "richedit20a",
            }:
                raise _MessageError("UNSUPPORTED_CONTROL")
            if not self._sync(binding, recipient, 0xC, 0, action["value"]):
                raise _MessageError("SET_VALUE_REJECTED")
            length = self._sync(binding, recipient, 0xE, 0, 0)
            if not 0 <= length <= 20000:
                raise _MessageError("READBACK_BOUNDS")
            # One extra readable unit detects growth instead of falsely verifying
            # a truncated prefix when the control changes between length and read.
            buffer = ctypes.create_unicode_buffer(length + 2)
            copied = self._sync(binding, recipient, 0xD, length + 2, buffer)
            if not 0 <= copied <= length or buffer.value != action["value"]:
                raise _MessageError("VALUE_NOT_VERIFIED")
            return True
        elif name in {"key", "hold", "key_down", "key_up"}:
            keys = action["key"] if isinstance(action["key"], list) else [action["key"]]
            if any((key in self._keys) != (name == "key_up") for key in keys):
                raise _MessageError("NOT_HELD" if name == "key_up" else "ALREADY_HELD")
            for key in reversed(keys) if name == "key_up" else keys:
                self._key(binding, recipient, key, up=name == "key_up")
            if name in {"key", "hold"}:
                self._wait(binding, recipient, action["seconds"])
                for key in reversed(keys):
                    self._key(binding, recipient, key, up=True)
        elif name in _MOUSE:
            x, y = action["x"], action["y"]
            lparam = self._point(binding, recipient, x, y)
            if name == "wheel":
                self._dispatch(
                    binding,
                    recipient,
                    0x20E if action["horizontal"] else 0x20A,
                    self._flags(recipient) | (action["amount"] & 0xFFFF) << 16,
                    _packed(x, y),
                )
            elif name == "move":
                self._dispatch(binding, recipient, 0x200, self._flags(recipient), lparam)
            elif name in {"mouse_down", "mouse_up"}:
                self._button(binding, recipient, action["button"], x, y, up=name == "mouse_up")
            elif name == "click":
                if action["button"] in self._buttons or any(
                    key in self._keys for key in action["modifiers"]
                ):
                    raise _MessageError("ALREADY_HELD")
                for key in action["modifiers"]:
                    self._key(binding, recipient, key)
                for index in range(action["clicks"]):
                    self._wait(binding, recipient, 0.02)
                    self._button(binding, recipient, action["button"], x, y, double=index == 1)
                    self._wait(binding, recipient, 0.03)
                    self._button(binding, recipient, action["button"], x, y, up=True)
                for key in reversed(action["modifiers"]):
                    self._key(binding, recipient, key, up=True)
            else:
                points = action.get("path", [action.get("to")])
                # Validate the complete drag against the pinned recipient before down.
                for point in points:
                    self._point(binding, recipient, point["x"], point["y"])
                self._button(binding, recipient, action["button"], x, y)
                started = time.monotonic()
                dwell = action["press_dwell_ms"] / 1000
                self._wait(binding, recipient, dwell)
                previous = (x, y)
                interval = (action["seconds"] - dwell) / len(points)
                for index, point in enumerate(points):
                    steps = max(1, math.ceil(interval / 0.01))
                    for step in range(1, steps + 1):
                        due = started + dwell + interval * (index + step / steps)
                        self._wait(binding, recipient, max(0, due - time.monotonic()))
                        x = round(previous[0] + (point["x"] - previous[0]) * step / steps)
                        y = round(previous[1] + (point["y"] - previous[1]) * step / steps)
                        self._dispatch(
                            binding,
                            recipient,
                            0x200,
                            self._flags(recipient),
                            self._point(binding, recipient, x, y),
                        )
                        self._buttons[action["button"]] = (dict(binding), recipient, (x, y))
                    previous = (point["x"], point["y"])
                self._button(binding, recipient, action["button"], x, y, up=True)
        return False

    def execute(self, action, target, timeout=5.0):
        name = action.get("action", "") if isinstance(action, dict) else ""
        try:
            action = normalize_message_action(action)
            name = action["action"]
            timeout = bounded_seconds(timeout, "timeout", 120)
            if not timeout:
                raise ValueError("timeout must be positive")
            if not isinstance(target, str) or not target.strip():
                raise ValueError("target must be a nonempty string")
        except (ValueError, TypeError):
            return self._response("refused", name, code="INVALID_ACTION")
        if self._closed:
            return self._response("refused", name, code="CLOSED")
        if self._adapter is None:
            return self._remote(action, target, timeout)
        if self._cleanup_required:
            return self._response("refused", name, code="CLEANUP_REQUIRED", cleanup_confirmed=False)
        self._deadline = time.monotonic() + timeout
        self._acted = False
        before, after, binding, recipient = None, None, self._bindings.get(target), None
        try:
            before = self._adapter.observations()
            binding = self._bind(target)
            # Releases use their original recipient even if target thread focus changed.
            if name == "key_up":
                keys = action["key"] if isinstance(action["key"], list) else [action["key"]]
                held = [self._keys[k] for k in keys if k in self._keys]
                if len(held) != len(keys):
                    raise _MessageError("NOT_HELD")
                if any(item != held[0] for item in held) or held[0][0] != binding:
                    raise _MessageError("HELD_RECIPIENT_MISMATCH")
                recipient = held[0][1]
            elif name == "mouse_up":
                held = self._buttons.get(action["button"])
                if held is None:
                    raise _MessageError("NOT_HELD")
                if held[0] != binding:
                    raise _MessageError("HELD_RECIPIENT_MISMATCH")
                recipient = held[1]
                action.setdefault("x", held[2][0])
                action.setdefault("y", held[2][1])
            if recipient is not None and action.get("recipient", recipient) != recipient:
                raise _MessageError("HELD_RECIPIENT_MISMATCH")
            context = self._adapter.physical_coordinates() if name in _MOUSE else nullcontext()
            with context:
                if recipient is None:
                    recipient = self._recipient(action, binding)
                verified = self._perform(action, binding, recipient)
            response = self._response(
                "verified" if verified else "invoked", name, may_have_acted=self._acted
            )
            response["consumption_verified"] = verified
        except Exception as exc:
            code = exc.code if isinstance(exc, _MessageError) else "MESSAGE_FAILED"
            response = self._response(
                "unknown" if self._acted else "refused",
                name,
                code=code,
                may_have_acted=self._acted,
                cancelled=code == "CANCELLED",
            )
            if self._keys or self._buttons:
                response["cleanup"] = self.release_all()
        try:
            after = self._adapter.observations()
        except Exception:
            pass
        response.update(
            target_binding=binding,
            recipient=recipient,
            observations={"before": before, "after": after},
        )
        if before is not None and after is not None:
            response.update(
                foreground_changed=before.get("foreground_hwnd") != after.get("foreground_hwnd"),
                cursor_changed=before.get("cursor") != after.get("cursor"),
            )
        return response

    def release_all(self):
        if self._adapter is None:
            if (self._broker is None or self._broker._process is None) and not (
                self._lost_holds or self._possible_holds
            ):
                return self._response("ok", "message_release", cleanup_confirmed=True, released=0)
            return self._remote({"action": "message_release"}, timeout=2.0)
        self._deadline = time.monotonic() + 1.0
        failures = 0
        released = 0
        for key, (binding, recipient) in list(self._keys.items())[::-1]:
            try:
                self._key(binding, recipient, key, up=True, cleanup=True)
                released += 1
            except Exception:
                failures += 1
        for button, (binding, recipient, (x, y)) in list(self._buttons.items())[::-1]:
            try:
                with self._adapter.physical_coordinates():
                    self._button(binding, recipient, button, x, y, up=True, cleanup=True)
                released += 1
            except Exception:
                failures += 1
        self._cleanup_required = bool(failures)
        return self._response(
            "unknown" if failures else "ok",
            "message_release",
            cleanup_confirmed=not failures,
            released=released,
            may_have_acted=bool(failures or released),
            **({"code": "CLEANUP_UNCONFIRMED"} if failures else {}),
        )

    def close(self):
        if self._closed:
            return
        self.release_all()
        self._closed = True
        if self._broker is not None:
            self._broker.close()
