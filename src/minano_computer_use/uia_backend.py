"""Strict, process-local semantic UIA; no focus or physical-input fallbacks."""

from __future__ import annotations

import contextlib
import ctypes
import math
import os
import sys
import threading
import time
import uuid
from collections import OrderedDict
from ctypes import wintypes

from . import win32 as default_win32
from .contracts import READ_ACTIONS, UIA_ACTIONS, result
from .output import bound_result, validate_budget


class Refusal(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


class UIABackend:
    def __init__(self, auto=None, win32=None, *, clock=None, sleep=None):
        self._owner = (os.getpid(), threading.get_ident())
        self._closed = False
        self._initialized = False
        self._clock = clock or time.monotonic
        self._sleep = sleep or time.sleep
        self._tokens = OrderedDict()
        self.win32 = win32 or default_win32
        if auto is None:
            if threading.current_thread() is not threading.main_thread():
                raise RuntimeError("UIA must be initialized on the child main thread")
            # comtypes initializes its importing thread. Set MTA before *either* import.
            if "comtypes" in sys.modules and getattr(sys, "coinit_flags", None) != 0:
                raise RuntimeError("comtypes was already imported without MTA configuration")
            sys.coinit_flags = 0
            with contextlib.redirect_stdout(sys.stderr):
                import uiautomation as auto

                auto.InitializeUIAutomationInCurrentThread()
            self._initialized = True
        self.auto = auto

    def _check_owner(self):
        if self._closed or self._owner != (os.getpid(), threading.get_ident()):
            raise RuntimeError("UIA backend is closed or used outside its owning process/thread")

    def close(self):
        if self._closed:
            return
        self._check_owner()
        if self._initialized:
            self.auto.UninitializeUIAutomationInCurrentThread()
        self._tokens.clear()
        self._closed = True

    def execute(self, request: dict) -> dict:
        self._check_owner()
        action = request.get("action", "inspect")
        before = None
        self._may_have_acted = False
        self._target_binding = None
        self._issued_this_call = 0
        budgets = (24000, 65536)
        try:
            try:
                budgets = validate_budget(
                    request.get("max_output_chars", 24000),
                    request.get("max_output_bytes", 65536),
                )
            except ValueError as exc:
                raise Refusal("INVALID_BUDGET", str(exc)) from exc
            before = self.win32.observations()
            # Some library error paths print diagnostics; never let them reach MCP stdout.
            with contextlib.redirect_stdout(sys.stderr):
                output = self._execute(request, action)
        except (Refusal, default_win32.TargetError) as exc:
            output = result("refused", action, method="uia", code=exc.code, message=str(exc))
        except Exception as exc:
            output = result(
                "unknown" if self._may_have_acted else "error",
                action,
                method="uia",
                code="UIA_MUTATION_UNCERTAIN" if self._may_have_acted else "UIA_ERROR",
                # Provider exceptions can contain entered/password values. Do not echo them.
                message=f"{type(exc).__name__}: UIA provider operation failed",
                may_have_acted=self._may_have_acted,
            )
        try:
            after = self.win32.observations()
            output["observations"] = {
                "before": before,
                "after": after,
                "foreground_changed": before is not None
                and (before["foreground_hwnd"] != after["foreground_hwnd"]),
                "cursor_changed": before is not None
                and before.get("cursor") is not None
                and after.get("cursor") is not None
                and before["cursor"] != after["cursor"],
            }
        except Exception as exc:
            output["observations"] = {
                "before": before,
                "after": None,
                "error": f"{type(exc).__name__}: Observation unavailable",
            }
        output["delivery"] = "read_only" if action in READ_ACTIONS else "accessibility"
        output["fallbacks"] = []
        if self._target_binding is not None:
            output["target_binding"] = self._target_binding
        return bound_result(output, max_output_chars=budgets[0], max_output_bytes=budgets[1])

    def _pattern(self, control, name):
        # GetPattern swallows COMError in uiautomation. Distinguish unsupported from
        # a provider that advertises support but fails to supply the promised pattern.
        property_ids = getattr(self.auto, "PropertyId", None)
        available_id = getattr(property_ids, "Is" + name + "PatternAvailableProperty", None)
        advertised = None
        if available_id is not None:
            advertised = control.GetPropertyValue(available_id)
            if type(advertised) is not bool:
                raise RuntimeError("Provider pattern availability is unavailable")
            if not advertised:
                return None
        pattern = control.GetPattern(getattr(self.auto.PatternId, name + "Pattern"))
        if pattern is None and advertised:
            raise RuntimeError(f"Provider advertised {name}Pattern but retrieval failed")
        return pattern

    @staticmethod
    def _metadata(control):
        strings = {
            "name": str(control.Name),
            "automation_id": str(control.AutomationId),
            "control_type": str(control.ControlTypeName),
        }
        data = {
            **{key: value if len(value) <= 4096 else None for key, value in strings.items()},
            "pid": int(control.ProcessId),
        }
        if any(len(value) > 4096 for value in strings.values()):
            data["metadata_truncated"] = True
            data["fields_omitted"] = [key for key, value in strings.items() if len(value) > 4096]
        return data

    @staticmethod
    def _unavailable(error=None, reason="Provider property unavailable"):
        evidence = {"status": "unavailable", "reason": reason}
        if error is not None:
            evidence["error_type"] = type(error).__name__
        return evidence

    @staticmethod
    def _password(control):
        try:
            value = control.IsPassword
            return value if type(value) is bool else None
        except Exception:
            return None

    def _rich_metadata(self, control, limit, *, allow_value=True):
        # Identity metadata is deliberately independent: tokens must not depend on
        # volatile focus/value/frame fields, nor force rich reads on basic discovery.
        data = self._metadata(control)
        unavailable = {}
        for field, prop, kind in (
            ("class_name", "ClassName", str),
            ("localized_control_type", "LocalizedControlType", str),
            ("native_window_handle", "NativeWindowHandle", int),
            ("enabled", "IsEnabled", bool),
            ("offscreen", "IsOffscreen", bool),
            ("has_keyboard_focus", "HasKeyboardFocus", bool),
            ("focusable", "IsKeyboardFocusable", bool),
            ("is_password", "IsPassword", bool),
        ):
            try:
                value = getattr(control, prop)
                if kind is bool and type(value) is not bool:
                    raise ValueError("Not a boolean property")
                data[field] = kind(value)[:4096] if kind is str else kind(value)
            except Exception as exc:
                data[field] = None
                unavailable[field] = self._unavailable(exc)
        try:
            rect = control.BoundingRectangle
            edges = {key: float(getattr(rect, key)) for key in ("left", "top", "right", "bottom")}
            if not all(math.isfinite(value) for value in edges.values()):
                raise ValueError("Nonfinite rectangle")
            data["frame"] = {**edges, "coordinate_space": "provider_screen"}
        except Exception as exc:
            data["frame"] = None
            unavailable["frame"] = self._unavailable(exc)
        actions = []
        for name, verbs in (
            ("Invoke", ("invoke",)),
            ("Value", ("set_value",)),
            ("Toggle", ("toggle",)),
            ("SelectionItem", ("select",)),
            ("ExpandCollapse", ("expand", "collapse")),
            ("ScrollItem", ("scroll_into_view",)),
            ("Scroll", ("scroll",)),
        ):
            try:
                pattern = self._pattern(control, name)
                if pattern is None:
                    continue
                if name == "Value":
                    try:
                        readonly = pattern.IsReadOnly
                        if type(readonly) is not bool:
                            raise ValueError("Not a boolean property")
                        if not readonly and callable(getattr(pattern, "SetValue", None)):
                            actions.extend(verbs)
                        elif not readonly:
                            unavailable["actions.set_value"] = self._unavailable(
                                reason="ValuePattern does not expose SetValue"
                            )
                    except Exception as exc:
                        unavailable["actions.set_value"] = self._unavailable(exc)
                    if data["is_password"] is not False or not allow_value:
                        data["value_redacted"] = True
                    else:
                        try:
                            value = str(pattern.Value)
                            data["value"] = value[:limit]
                            if len(value) > limit:
                                data["value_truncated"] = True
                        except Exception as exc:
                            data["value"] = None
                            unavailable["value"] = self._unavailable(exc)
                else:
                    methods = {
                        "invoke": "Invoke",
                        "toggle": "Toggle",
                        "select": "Select",
                        "expand": "Expand",
                        "collapse": "Collapse",
                        "scroll_into_view": "ScrollIntoView",
                        "scroll": "Scroll",
                    }
                    for verb in verbs:
                        if callable(getattr(pattern, methods[verb], None)):
                            actions.append(verb)
                        else:
                            unavailable["actions." + verb] = self._unavailable(
                                reason="Pattern method unavailable"
                            )
            except Exception as exc:
                unavailable["patterns." + name] = self._unavailable(exc)
        data["actions"] = actions
        if unavailable:
            data["unavailable"] = unavailable
        return data

    def _discovery_metadata(self, control, request, *, allow_value=True):
        if request.get("rich", request.get("action") == "state"):
            return self._rich_metadata(
                control, request.get("text_limit", 4096), allow_value=allow_value
            )
        return self._metadata(control)

    def _safe_text(self, control, entry, limit, *, allow_document=True):
        if self._password(control) is not False or not allow_document:
            entry["text_redacted"] = True
            return
        try:
            pattern = self._pattern(control, "Text")
            value_pattern = self._pattern(control, "Value") if pattern is None else None
            text = (
                pattern.DocumentRange.GetText(limit)
                if pattern is not None
                else value_pattern.Value
                if value_pattern is not None
                else control.Name
            )
            entry["text"] = str(text)[:limit]
            if len(str(text)) > limit:
                entry["text_truncated"] = True
        except Exception as exc:
            entry.setdefault("unavailable", {})["text"] = self._unavailable(exc)

    def _unsafe_text_nodes(self, nodes, truncated):
        # A TextPattern on an ancestor can include descendant password text. If the
        # traversal cannot establish the entire scope, redact rather than infer.
        if truncated:
            return set(range(len(nodes)))
        unsafe = set()
        for index, (node, _, _) in enumerate(nodes):
            if self._password(node) is False:
                continue
            while index is not None:
                unsafe.add(index)
                index = nodes[index][2]
        return unsafe

    def _aggregate_value_safe(self, control, depth, maximum):
        nodes, truncated = self._walk(control, depth, maximum)
        return not truncated and not self._unsafe_text_nodes(nodes, truncated)

    def _state_binding(self, request, window):
        expected = request.get("target_binding")
        if (
            not isinstance(expected, dict)
            or set(expected) != {"hwnd", "pid", "process_creation_time"}
            or any(type(value) is not int or value <= 0 for value in expected.values())
        ):
            raise Refusal(
                "INVALID_TARGET_BINDING", "state requires a complete parent target binding"
            )
        actual = self._window_identity(window)
        if actual.get("process_creation_time") is None or any(
            actual.get(key) != expected.get(key) for key in expected
        ):
            raise Refusal(
                "STALE_TARGET", "Resolved window does not match the parent target binding"
            )
        return dict(expected)

    def _state_live_candidate(self, control, original_id, root, root_id, expected):
        try:
            if self._runtime_id(root) != root_id or int(root.ProcessId) != expected["pid"]:
                return False
            if getattr(root, "NativeWindowHandle", expected["hwnd"]) != expected["hwnd"]:
                return False
            if (
                self._runtime_id(control) != original_id
                or int(control.ProcessId) != expected["pid"]
            ):
                return False
            ancestor = control
            for _ in range(64):
                ancestor_id = self._runtime_id(ancestor)
                if ancestor_id == root_id:
                    break
                ancestor = ancestor.GetParentControl()
                if ancestor is None:
                    return False
            else:
                return False
            hwnd = getattr(control, "NativeWindowHandle", 0)
            if hwnd in (None, 0, expected["hwnd"]):
                return True
            checker = getattr(self.win32, "is_child_window", None)
            if checker is not None:
                return bool(checker(expected["hwnd"], int(hwnd), expected["pid"]))
            user32_factory = getattr(self.win32, "_user32", None)
            if user32_factory is None:
                return False
            user32 = user32_factory()
            child_pid = wintypes.DWORD()
            user32.GetWindowThreadProcessId(int(hwnd), ctypes.byref(child_pid))
            user32.IsChild.argtypes = [wintypes.HWND, wintypes.HWND]
            user32.IsChild.restype = wintypes.BOOL
            return child_pid.value == expected["pid"] and bool(
                user32.IsChild(expected["hwnd"], int(hwnd))
            )
        except Exception:
            return False

    def _state(self, request, window, root, depth, maximum):
        expected = self._state_binding(request, window)
        if int(root.ProcessId) != expected["pid"]:
            raise Refusal(
                "STALE_TARGET", "UIA root process does not match the parent target binding"
            )
        try:
            root_hwnd = root.NativeWindowHandle
        except Exception:
            root_hwnd = None
        if root_hwnd not in (None, expected["hwnd"]):
            raise Refusal("STALE_TARGET", "UIA root window handle does not match the bound window")
        times = {}
        start = self._clock()
        root_id = self._runtime_id(root)
        nodes, truncated = self._walk(root, depth, maximum)
        times["traversal"] = {"start": start, "end": self._clock()}
        if root_id is None:
            raise Refusal("STALE_TARGET", "UIA root RuntimeId is unavailable")
        original_ids = [self._runtime_id(node) for node, _, _ in nodes]
        if any(node_id is None for node_id in original_ids):
            raise Refusal("STALE_TARGET", "State traversal contains an unbound UIA element")
        unsafe_text = self._unsafe_text_nodes(nodes, truncated)
        focus_start = self._clock()
        focused = []
        for index, (control, _, _) in enumerate(nodes):
            if not self._state_live_candidate(
                control, original_ids[index], root, root_id, expected
            ):
                raise Refusal(
                    "STALE_TARGET", "UIA element left the bound root during state collection"
                )
            try:
                if control.HasKeyboardFocus is True:
                    focused.append(index)
            except Exception:
                continue
            if not self._state_live_candidate(
                control, original_ids[index], root, root_id, expected
            ):
                raise Refusal("STALE_TARGET", "UIA focus candidate left the bound root")
        if len(focused) == 1:
            focus = {"status": "available", "node_index": focused[0]}
        elif len(focused) > 1:
            focus = {"status": "unavailable", "reason": "Ambiguous scoped focus"}
        else:
            focus = {"status": "unavailable", "reason": "No scoped focused control proven"}
        times["focus"] = {"start": focus_start, "end": self._clock()}
        selection_start = self._clock()
        selection = {"status": "unavailable", "reason": "No scoped focused control proven"}
        selected_index = focused[0] if len(focused) == 1 else None
        if request.get("token") is not None:
            selected_control = self._token_control(request["token"], window, root)
            selected_id = self._runtime_id(selected_control)
            selected_index = next(
                (
                    index
                    for index, (node, _, _) in enumerate(nodes)
                    if selected_id and self._runtime_id(node) == selected_id
                ),
                None,
            )
        elif request.get("selector") is not None:
            if truncated:
                raise Refusal("SEARCH_TRUNCATED", "Cannot select in a truncated state traversal")
            conditions = self._selector(request["selector"])
            matches = [
                index for index, (node, _, _) in enumerate(nodes) if self._matches(node, conditions)
            ]
            if "index" in conditions:
                matches = matches[conditions["index"] : conditions["index"] + 1]
            if len(matches) != 1:
                raise Refusal(
                    "ELEMENT_NOT_FOUND" if not matches else "AMBIGUOUS_SELECTOR",
                    "State selection requires exactly one scoped element",
                )
            selected_index = matches[0]
        if selected_index is not None:
            index = selected_index
            control = nodes[index][0]
            password = self._password(control)
            if password is not False or index in unsafe_text:
                selection = {"status": "redacted", "reason": "Password status true or unknown"}
            else:
                if not self._state_live_candidate(
                    control, original_ids[index], root, root_id, expected
                ):
                    raise Refusal("STALE_TARGET", "UIA selection candidate left the bound root")
                try:
                    pattern = self._pattern(control, "Text")
                    if pattern is None:
                        selection = {"status": "unavailable", "reason": "TextPattern unsupported"}
                    else:
                        if not self._state_live_candidate(
                            control, original_ids[index], root, root_id, expected
                        ):
                            raise Refusal(
                                "STALE_TARGET", "UIA selection candidate changed before read"
                            )
                        ranges = pattern.GetSelection()
                        if not self._state_live_candidate(
                            control, original_ids[index], root, root_id, expected
                        ):
                            raise Refusal(
                                "STALE_TARGET", "UIA selection candidate changed during read"
                            )
                        values, remaining = [], request.get("text_limit", 500)
                        for text_range in ranges[:16]:
                            if len(values) >= 16 or remaining <= 0:
                                break
                            if not self._state_live_candidate(
                                control, original_ids[index], root, root_id, expected
                            ):
                                raise Refusal(
                                    "STALE_TARGET", "UIA selection range left the bound root"
                                )
                            text = str(text_range.GetText(remaining))
                            if not self._state_live_candidate(
                                control, original_ids[index], root, root_id, expected
                            ):
                                raise Refusal(
                                    "STALE_TARGET",
                                    "UIA selection candidate changed during text read",
                                )
                            values.append(text[:remaining])
                            remaining -= len(values[-1])
                        selection = {
                            "status": "available",
                            "node_index": index,
                            "ranges": values,
                            "truncated": len(ranges) > len(values) or remaining == 0,
                        }
                except Refusal:
                    raise
                except Exception as exc:
                    selection = {"status": "unavailable", **self._unavailable(exc)}
        times["selection"] = {"start": selection_start, "end": self._clock()}
        rich = request.get("rich", True)
        values = []
        for index, (control, level, parent) in enumerate(nodes):
            if not self._state_live_candidate(
                control, original_ids[index], root, root_id, expected
            ):
                raise Refusal("STALE_TARGET", "UIA metadata candidate left the bound root")
            if rich:
                entry = self._rich_metadata(
                    control,
                    request.get("text_limit", 500),
                    allow_value=index not in unsafe_text,
                )
            else:
                entry = self._metadata(control)
            entry.update(depth=level, parent_index=parent)
            if index in unsafe_text:
                entry["text_redacted"] = True
            values.append(entry)
        if not all(
            self._state_live_candidate(node, original_ids[index], root, root_id, expected)
            for index, (node, _, _) in enumerate(nodes)
        ):
            raise Refusal("STALE_TARGET", "UIA element changed before state publication")
        self._state_binding(request, window)
        if int(root.ProcessId) != expected["pid"]:
            raise Refusal("STALE_TARGET", "UIA root process changed during state collection")
        self._target_binding = {**expected}
        data = {
            "nodes": values,
            "truncated": truncated,
            "focus": focus,
            "selection": selection,
            "window": {key: window.get(key) for key in ("hwnd", "pid", "title")},
            "componenttimes": times,
            "target_binding": {**expected},
            "root_binding": {**expected},
            "consistency": "non_atomic",
        }
        return result("ok", "state", method="uia", data=data)

    @staticmethod
    def _runtime_id(control):
        # uiautomation's Element property implicitly Refind()s a missing pointer.
        # Controls obtained from handles/walking must retain their original live pointer.
        if hasattr(control, "_element") and not control._element:
            raise ValueError("UIA live reference is gone; automatic Refind is forbidden")
        getter = getattr(control, "GetRuntimeId", None)
        if getter is None:
            return None
        value = getter()
        if value is None:
            return None
        value = tuple(value)
        if not value or len(value) > 128 or any(type(part) is not int for part in value):
            return None
        return value

    def _window_identity(self, window):
        reader = getattr(self.win32, "window_identity", None)
        if reader is not None:
            return reader(window["hwnd"], window["pid"])
        # Injectable adapters may lack a process timestamp. Never invent one.
        return {"hwnd": window["hwnd"], "pid": window["pid"], "process_creation_time": None}

    def _bind(self, window, root, control):
        identity = self._window_identity(window)
        root_id, element_id = self._runtime_id(root), self._runtime_id(control)
        self._target_binding = {
            **identity,
            "root_runtime_id": list(root_id) if root_id else None,
            "element_runtime_id": list(element_id) if element_id else None,
            "identity_limitations": "Provider RuntimeIds and optional process time are not "
            "an absolute proof against all provider/window lifetime reuse",
        }
        return identity, root_id, element_id

    def _issue_token(self, entry, window, root, control):
        identity, root_id, element_id = self._bind(window, root, control)
        root_metadata, metadata = self._metadata(root), self._metadata(control)
        if (
            not root_id
            or not element_id
            or any(item.get("metadata_truncated") for item in (root_metadata, metadata))
        ):
            entry["token_unavailable"] = "Reliable bounded RuntimeId/metadata unavailable"
            return
        if self._issued_this_call >= 256:
            entry["token_unavailable"] = "Per-response token limit reached"
            return
        now = self._clock()
        for token, cached in list(self._tokens.items()):
            if now >= cached["expires"]:
                del self._tokens[token]
        while len(self._tokens) >= 256:
            self._tokens.popitem(last=False)
        token = str(uuid.uuid4())
        self._tokens[token] = {
            "expires": now + 60,
            "root": root,
            "control": control,
            "root_id": root_id,
            "element_id": element_id,
            "root_metadata": root_metadata,
            "metadata": metadata,
            "identity": identity,
        }
        entry["token"] = token
        entry["token_ttl_seconds"] = 60
        self._issued_this_call += 1

    def _token_control(self, token, window, root):
        if not isinstance(token, str) or token not in self._tokens:
            raise Refusal("INVALID_TOKEN", "Token is unknown to this UIA worker")
        cached = self._tokens[token]
        if self._clock() >= cached["expires"]:
            del self._tokens[token]
            raise Refusal("EXPIRED_TOKEN", "Token TTL has expired; explicitly discover again")
        try:
            identity = self._window_identity(window)
            if identity != cached["identity"]:
                raise ValueError("Window/process binding changed")
            control = cached["control"]
            if (
                self._runtime_id(root) != cached["root_id"]
                or self._runtime_id(cached["root"]) != cached["root_id"]
                or self._runtime_id(control) != cached["element_id"]
                or self._metadata(root) != cached["root_metadata"]
                or self._metadata(cached["root"]) != cached["root_metadata"]
                or self._metadata(control) != cached["metadata"]
            ):
                raise ValueError("Live root/element identity changed")
            # A still-readable but detached/reparented cached element is not a bound target.
            ancestor = control
            for _ in range(64):
                if self._runtime_id(ancestor) == cached["root_id"]:
                    break
                ancestor = ancestor.GetParentControl()
                if ancestor is None:
                    raise ValueError("Element is detached from its original root")
            else:
                raise ValueError("Cannot establish bounded element ancestry")
            self._bind(window, root, control)
            return control
        except Exception as exc:
            del self._tokens[token]
            raise Refusal("STALE_TOKEN", "Token binding no longer valid; discover again") from exc

    def _walk(self, root, depth, max_nodes):
        """Bound sibling walking too: GetChildren would materialize an unbounded list."""
        nodes = []
        truncated = False

        def visit(control, level, parent):
            nonlocal truncated
            if len(nodes) >= max_nodes:
                truncated = True
                return
            index = len(nodes)
            nodes.append((control, level, parent))
            child = control.GetFirstChildControl()
            if child is None:
                return
            if level >= depth:
                truncated = True
                return
            while child is not None:
                if len(nodes) >= max_nodes:
                    truncated = True
                    return
                visit(child, level + 1, index)
                child = child.GetNextSiblingControl()

        visit(root, 0, None)
        return nodes, truncated

    def _selector(self, selector):
        if isinstance(selector, str):
            selector = {"name": selector}
        if not isinstance(selector, dict) or not selector:
            raise Refusal("INVALID_SELECTOR", "Selector must be an exact name or nonempty object")
        if set(selector) - {"name", "automation_id", "control_type", "index"}:
            raise Refusal("INVALID_SELECTOR", "Unknown selector condition")
        selector = dict(selector)
        for key in ("name", "automation_id", "control_type"):
            if key in selector and not isinstance(selector[key], str):
                raise Refusal("INVALID_SELECTOR", f"{key} must be a string")
        if "index" in selector and (type(selector["index"]) is not int or selector["index"] < 0):
            raise Refusal("INVALID_SELECTOR", "index must be a nonnegative integer")
        if "control_type" in selector:
            name = selector["control_type"]
            if not name.endswith("Control"):
                name += "Control"
            enum = getattr(self.auto.ControlType, name, None)
            if enum is None:
                raise Refusal("INVALID_SELECTOR", "Unknown control type")
            selector["control_type"] = enum
        return selector

    @staticmethod
    def _matches(control, selector):
        return all(
            getattr(control, prop) == selector[key]
            for key, prop in (
                ("name", "Name"),
                ("automation_id", "AutomationId"),
                ("control_type", "ControlType"),
            )
            if key in selector
        )

    def _select(self, root, selector, depth, maximum, *, allow_missing=False):
        conditions = self._selector(selector)
        nodes, truncated = self._walk(root, depth, maximum)
        if truncated:
            raise Refusal("SEARCH_TRUNCATED", "Cannot establish uniqueness in a truncated tree")
        matches = [node for node, _, _ in nodes if self._matches(node, conditions)]
        index = conditions.get("index")
        if index is not None:
            matches = matches[index : index + 1]
        if not matches:
            if allow_missing:
                return None
            raise Refusal("ELEMENT_NOT_FOUND", "No scoped element matches the exact selector")
        if len(matches) != 1:
            raise Refusal("AMBIGUOUS_SELECTOR", f"{len(matches)} scoped elements match")
        return matches[0]

    def _find(self, request, window, root, depth, maximum):
        query, selector = request.get("query"), request.get("selector")
        if query is None and selector is None:
            raise Refusal("INVALID_QUERY", "find requires query or selector")
        if query is not None and (not isinstance(query, str) or not query):
            raise Refusal("INVALID_QUERY", "query must be a nonempty string")
        match, limit = request.get("match", "contains"), request.get("limit", 20)
        if match not in {"contains", "exact"}:
            raise Refusal("INVALID_QUERY", "match must be contains or exact")
        if type(limit) is not int or not 1 <= limit <= 256:
            raise Refusal("INVALID_LIMIT", "limit must be between 1 and 256")
        conditions = self._selector(selector) if selector is not None else {}
        nodes, truncated = self._walk(root, depth, maximum)
        unsafe_values = (
            self._unsafe_text_nodes(nodes, truncated) if request.get("rich", False) else set()
        )
        matches = []
        signatures = {}
        duplicate_indexes = {}
        for index, (node, _, _) in enumerate(nodes):
            signature = (node.Name, node.AutomationId, node.ControlType)
            same = signatures.setdefault(signature, [])
            duplicate_indexes[index] = len(same)
            same.append(index)
            if not self._matches(node, conditions):
                continue
            if query is not None:
                name = str(node.Name).casefold()
                needle = query.casefold()
                if not (name == needle if match == "exact" else needle in name):
                    continue
            matches.append(index)
        known_count = len(matches)
        selected_index = conditions.get("index")
        if selected_index is not None:
            matches = matches[selected_index : selected_index + 1]
        entries = []
        for index in matches[:limit]:
            control, level, parent = nodes[index]
            entry = self._discovery_metadata(
                control, request, allow_value=index not in unsafe_values
            )
            path = [index]
            while parent is not None:
                path.append(parent)
                parent = nodes[parent][2]
            path.reverse()
            entry.update(path=path, depth=level)
            if not request.get("compact", False):
                entry["ancestry"] = [self._metadata(nodes[i][0]) for i in path[:-1]]
            if not entry.get("metadata_truncated"):
                suggestion = {
                    "name": entry["name"],
                    "automation_id": entry["automation_id"],
                    "control_type": entry["control_type"],
                }
                suggestion["index"] = duplicate_indexes[index]
                entry["selector_suggestion"] = suggestion
                entry["selector_requires_complete_search"] = True
            self._issue_token(entry, window, root, control)
            entries.append(entry)
        self._bind(window, root, root)
        return result(
            "ok",
            "find",
            method="uia",
            data={
                "matches": entries,
                "matched_total": known_count,
                "matched_total_known": not truncated,
                "truncated": truncated,
                "return_limited": len(matches) > limit,
            },
        )

    @staticmethod
    def _condition(request):
        condition = request.get("condition")
        if not isinstance(condition, dict) or len(condition) != 1:
            raise Refusal("INVALID_CONDITION", "condition requires exactly one read-only predicate")
        key, expected = next(iter(condition.items()))
        valid = (
            key in {"exists", "selected"}
            and type(expected) is bool
            or key == "value"
            and isinstance(expected, str)
            or key == "toggle_state"
            and type(expected) is int
            and expected in (0, 1, 2)
            or key == "expand_state"
            and type(expected) is int
            and expected in (0, 1, 2, 3)
        )
        if not valid:
            raise Refusal("INVALID_CONDITION", "Invalid predicate or expected value")
        return key, expected

    def _wait_for(self, request, window, root, depth, maximum):
        key, expected = self._condition(request)
        selector, token = request.get("selector"), request.get("token")
        if selector is None and token is None:
            raise Refusal("INVALID_SELECTOR", "wait_for requires selector or token")
        timeout, interval = request.get("timeout", 5), request.get("poll_interval", 0.05)
        for value, name, low, high in (
            (timeout, "timeout", 0, 120),
            (interval, "poll_interval", 0.001, 5),
        ):
            if (
                type(value) not in (int, float)
                or not math.isfinite(value)
                or not low <= value <= high
            ):
                raise Refusal("INVALID_TIMEOUT", f"{name} is outside its finite allowed range")
        deadline = request.get("_deadline_monotonic", self._clock() + timeout)
        if type(deadline) not in (int, float) or not math.isfinite(deadline):
            raise Refusal("INVALID_TIMEOUT", "Invalid absolute deadline")
        deadline = min(deadline, self._clock() + timeout)
        initial_identity = self._window_identity(window)
        initial_root_id = self._runtime_id(root)
        last = None
        while self._clock() < deadline:
            # Never retry a mutation or reinterpret stale-token/provider errors as absence.
            current_window = self.win32.resolve_window(request.get("target"))
            if self._window_identity(current_window) != initial_identity:
                raise Refusal("STALE_TARGET", "Window/process identity changed during wait")
            current_root = self.auto.ControlFromHandle(current_window["hwnd"])
            if current_root is None or int(current_root.ProcessId) != window["pid"]:
                raise Refusal("STALE_TARGET", "UIA root disappeared during wait")
            if self._runtime_id(current_root) != initial_root_id:
                raise Refusal("STALE_TARGET", "UIA root identity changed during wait")
            control = (
                self._token_control(token, current_window, current_root)
                if token is not None
                else self._select(current_root, selector, depth, maximum, allow_missing=True)
            )
            observed = None
            private_value = (
                key == "value"
                and control is not None
                and not self._aggregate_value_safe(control, depth, maximum)
            )
            if key == "exists":
                observed = control is not None
            elif control is not None:
                pattern_name, prop = {
                    "value": ("Value", "Value"),
                    "toggle_state": ("Toggle", "ToggleState"),
                    "selected": ("SelectionItem", "IsSelected"),
                    "expand_state": ("ExpandCollapse", "ExpandCollapseState"),
                }[key]
                pattern = self._pattern(control, pattern_name)
                if pattern is None:
                    raise Refusal("UNSUPPORTED_PATTERN", f"{pattern_name}Pattern is not supported")
                observed = getattr(pattern, prop)
            matched = control is not None or key == "exists"
            matched = matched and observed == expected
            last = {"exists": control is not None, key: observed}
            if (
                key == "value"
                and control is not None
                and (private_value or not self._aggregate_value_safe(control, depth, maximum))
            ):
                last[key] = None
                last["value_redacted"] = True
            elif isinstance(observed, str) and len(observed) > 4096:
                last[key] = observed[:4096]
                last["value_truncated"] = True
            if matched and self._clock() < deadline:
                if control is not None:
                    self._bind(current_window, current_root, control)
                return result(
                    "verified",
                    "wait_for",
                    method="uia",
                    data={"last_observation": last},
                    last_observation=last,
                )
            remaining = deadline - self._clock()
            if remaining > 0:
                self._sleep(min(interval, remaining))
        return result(
            "error",
            "wait_for",
            method="uia",
            code="CONDITION_TIMEOUT",
            message="Read-only condition did not match before the absolute deadline",
            last_observation=last,
            data={"last_observation": last},
        )

    def _execute(self, request, action):
        if action not in UIA_ACTIONS:
            raise Refusal("UNSUPPORTED_ACTION", f"Unsupported UIA action: {action}")
        if request.get("token") is not None and request.get("selector") is not None:
            raise Refusal("INVALID_SELECTOR", "token and selector are mutually exclusive")
        if action == "list":
            if request.get("token") is not None:
                raise Refusal("INVALID_TOKEN", "list does not accept an element token")
            windows = []
            for window in self.win32.list_windows():
                entry = dict(window)
                title = entry.get("title", "")
                if len(title) > 4096:
                    entry["title"] = title[:4096]
                    entry["title_truncated"] = True
                windows.append(entry)
            return result("ok", action, method="win32", data={"windows": windows})
        for name in ("compact", "include_tokens", "rich"):
            if type(request.get(name, name == "rich" and action == "state")) is not bool:
                raise Refusal("INVALID_VALUE", f"{name} must be boolean")
        limit = request.get("text_limit", 500 if action == "state" else 4096)
        if type(limit) is not int or not 1 <= limit <= 4096:
            raise Refusal("INVALID_LIMIT", "text_limit must be an integer between 1 and 4096")
        depth, maximum = request.get("depth", 8), request.get("max_nodes", 500)
        if type(depth) is not int or not 0 <= depth <= 32:
            raise Refusal("INVALID_LIMIT", "depth must be an integer between 0 and 32")
        if type(maximum) is not int or not 1 <= maximum <= 5000:
            raise Refusal("INVALID_LIMIT", "max_nodes must be an integer between 1 and 5000")
        if action == "state" and not request.get("target"):
            raise Refusal("TARGET_REQUIRED", "state requires an explicit target")
        window = self.win32.resolve_window(request.get("target"))
        if action == "state":
            self._state_binding(request, window)
        root = self.auto.ControlFromHandle(window["hwnd"])
        # ProcessId is a read property, NOT a supported search keyword in uiautomation.
        if root is None or int(root.ProcessId) != window["pid"]:
            raise Refusal("STALE_TARGET", "UIA root does not belong to the resolved process")
        self._bind(window, root, root)
        if action == "find":
            if request.get("token") is not None:
                raise Refusal("INVALID_QUERY", "find discovers using query/selector, not token")
            return self._find(request, window, root, depth, maximum)
        if action == "wait_for":
            return self._wait_for(request, window, root, depth, maximum)
        if action == "state":
            return self._state(request, window, root, depth, maximum)
        control = root
        selector = request.get("selector")
        if request.get("token") is not None:
            control = self._token_control(request["token"], window, root)
        elif selector is not None:
            control = self._select(root, selector, depth, maximum)
        self._bind(window, root, control)
        if action in {"tree", "text"}:
            nodes, truncated = self._walk(control, depth, maximum)
            unsafe_text = (
                self._unsafe_text_nodes(nodes, truncated)
                if action == "text" or request.get("rich", False)
                else set()
            )
            values = []
            for index, (node, level, parent) in enumerate(nodes):
                entry = self._discovery_metadata(
                    node,
                    request,
                    allow_value=index not in unsafe_text,
                )
                entry.update(depth=level, parent_index=parent)
                if request.get("compact", False):
                    entry.pop("pid", None)
                    if entry["automation_id"] == "":
                        entry.pop("automation_id")
                if request.get("include_tokens", False):
                    self._issue_token(entry, window, root, node)
                if action == "text":
                    self._safe_text(node, entry, limit, allow_document=index not in unsafe_text)
                values.append(entry)
            self._bind(window, root, control)
            return result(
                "ok", action, method="uia", data={"nodes": values, "truncated": truncated}
            )
        if action == "inspect":
            value_safe = self._aggregate_value_safe(control, depth, maximum)
            data = self._discovery_metadata(control, request, allow_value=value_safe)
            self._issue_token(data, window, root, control)
            data["patterns"] = {}
            for name, properties in {
                "Invoke": (),
                "Value": ("Value", "IsReadOnly"),
                "Toggle": ("ToggleState",),
                "SelectionItem": ("IsSelected",),
                "ExpandCollapse": ("ExpandCollapseState",),
                "ScrollItem": (),
                "Scroll": ("HorizontalScrollPercent", "VerticalScrollPercent"),
                "Text": (),
            }.items():
                try:
                    pattern = self._pattern(control, name)
                    if pattern is None:
                        data["patterns"][name] = None
                        continue
                    values = {}
                    for prop in properties:
                        if prop == "Value" and (
                            self._password(control) is not False or not value_safe
                        ):
                            values["value_redacted"] = True
                            continue
                        try:
                            value = getattr(pattern, prop)
                            if isinstance(value, str) and len(value) > limit:
                                data["properties_truncated"] = True
                            values[prop] = value[:limit] if isinstance(value, str) else value
                        except Exception as exc:
                            values[prop] = None
                            values.setdefault("unavailable", {})[prop] = self._unavailable(exc)
                    data["patterns"][name] = values
                except Exception as exc:
                    data["patterns"][name] = self._unavailable(exc)
            if request.get("compact", False):
                data["patterns"] = {
                    key: value for key, value in data["patterns"].items() if value is not None
                }
            return result("ok", action, method="uia", data=data)
        private_value = action == "set_value" and not self._aggregate_value_safe(
            control, depth, maximum
        )
        return self._mutate(control, action, request, private_value=private_value)

    @staticmethod
    def _bounded_property(value):
        return value[:4096] if isinstance(value, str) else value

    def _mutate(self, control, action, request, *, private_value=False):
        pattern_name = {
            "invoke": "Invoke",
            "set_value": "Value",
            "toggle": "Toggle",
            "select": "SelectionItem",
            "expand": "ExpandCollapse",
            "collapse": "ExpandCollapse",
            "scroll_into_view": "ScrollItem",
            "scroll": "Scroll",
        }[action]
        pattern = self._pattern(control, pattern_name)
        if pattern is None:
            raise Refusal("UNSUPPORTED_PATTERN", f"{pattern_name}Pattern is not supported")
        expected = None
        before = None
        readback = None
        private = False
        args = []
        method = {
            "invoke": "Invoke",
            "set_value": "SetValue",
            "toggle": "Toggle",
            "select": "Select",
            "expand": "Expand",
            "collapse": "Collapse",
            "scroll_into_view": "ScrollIntoView",
            "scroll": "Scroll",
        }[action]
        if action == "set_value":
            if not isinstance(request.get("value"), str):
                raise Refusal("INVALID_VALUE", "set_value requires a string")
            if pattern.IsReadOnly:
                raise Refusal("READ_ONLY", "Value pattern is read-only")
            private = self._password(control) is not False or private_value
            before, expected = pattern.Value, request["value"]

            def readback():
                return pattern.Value

            args = [expected]
        elif action == "toggle":
            before = int(pattern.ToggleState)
            if before not in (0, 1, 2):
                raise Refusal("INVALID_STATE", "Invalid toggle state")

            def readback():
                return int(pattern.ToggleState)
        elif action == "select":
            before, expected = bool(pattern.IsSelected), True

            def readback():
                return bool(pattern.IsSelected)
        elif action in {"expand", "collapse"}:
            before = int(pattern.ExpandCollapseState)
            expected = 1 if action == "expand" else 0

            def readback():
                return int(pattern.ExpandCollapseState)
        elif action == "scroll":
            direction = request.get("direction", "down")
            if direction not in {"up", "down", "left", "right"}:
                raise Refusal("INVALID_DIRECTION", "Scroll direction must be up/down/left/right")
            amount = request.get("amount", "small")
            if amount not in {"small", "large"}:
                raise Refusal("INVALID_AMOUNT", "Scroll amount must be small or large")
            axis = "Horizontal" if direction in {"left", "right"} else "Vertical"
            if not getattr(pattern, axis + "lyScrollable"):
                raise Refusal("NOT_SCROLLABLE", f"Element cannot scroll on the {axis.lower()} axis")
            before = float(getattr(pattern, axis + "ScrollPercent"))

            def readback():
                return float(getattr(pattern, axis + "ScrollPercent"))

            increment = direction in {"down", "right"}
            enum = getattr(
                self.auto.ScrollAmount,
                ("Small" if amount == "small" else "Large")
                + ("Increment" if increment else "Decrement"),
            )
            no_amount = self.auto.ScrollAmount.NoAmount
            args = [enum, no_amount] if axis == "Horizontal" else [no_amount, enum]
        self._may_have_acted = True
        returned = getattr(pattern, method)(*args, waitTime=0)
        private = action == "set_value" and (private or self._password(control) is not False)
        # Pattern methods are not obliged to return a boolean; never publish an
        # arbitrary provider object/string (which can contain entered text).
        data = {
            "before": None if private else self._bounded_property(before),
            "pattern_returned": returned if type(returned) in (bool, type(None)) else None,
        }
        if private:
            data["value_redacted"] = True
        if readback is None:
            if returned is False:
                return result(
                    "unknown",
                    action,
                    method="uia",
                    data=data,
                    may_have_acted=True,
                    code="PATTERN_REPORTED_FAILURE",
                    message="Provider did not confirm invocation",
                )
            return result("invoked", action, method="uia", data=data)
        after = readback()
        data["after"] = None if private else self._bounded_property(after)
        if action == "toggle":
            verified = after in (0, 1, 2) and after != before
        elif action == "scroll":
            verified = after > before if increment else after < before
        else:
            verified = after == expected
        if verified:
            return result("verified", action, method="uia", data=data)
        return result(
            "unknown",
            action,
            method="uia",
            data=data,
            may_have_acted=True,
            code="READBACK_MISMATCH",
            message="Observed state did not confirm the requested effect",
        )
