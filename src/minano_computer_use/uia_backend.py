"""Strict, process-local semantic UIA; no focus or physical-input fallbacks."""

from __future__ import annotations

import contextlib
import math
import os
import sys
import threading
import time
import uuid
from collections import OrderedDict

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
                message=f"{type(exc).__name__}: {exc}",
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
            output["observations"] = {"before": before, "after": None, "error": str(exc)}
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
            advertised = bool(control.GetPropertyValue(available_id))
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
            raise Refusal("STALE_TOKEN", f"Token binding no longer valid: {exc}") from exc

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
            entry = self._metadata(control)
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
            if isinstance(observed, str) and len(observed) > 4096:
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
        for name in ("compact", "include_tokens"):
            if type(request.get(name, False)) is not bool:
                raise Refusal("INVALID_VALUE", f"{name} must be boolean")
        depth, maximum = request.get("depth", 8), request.get("max_nodes", 500)
        if type(depth) is not int or not 0 <= depth <= 32:
            raise Refusal("INVALID_LIMIT", "depth must be an integer between 0 and 32")
        if type(maximum) is not int or not 1 <= maximum <= 5000:
            raise Refusal("INVALID_LIMIT", "max_nodes must be an integer between 1 and 5000")
        window = self.win32.resolve_window(request.get("target"))
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
        control = root
        selector = request.get("selector")
        if request.get("token") is not None:
            control = self._token_control(request["token"], window, root)
        elif selector is not None:
            control = self._select(root, selector, depth, maximum)
        self._bind(window, root, control)
        if action in {"tree", "text"}:
            nodes, truncated = self._walk(control, depth, maximum)
            values = []
            for node, level, parent in nodes:
                entry = self._metadata(node)
                entry.update(depth=level, parent_index=parent)
                if request.get("compact", False):
                    entry.pop("pid", None)
                    if entry["automation_id"] == "":
                        entry.pop("automation_id")
                if request.get("include_tokens", False):
                    self._issue_token(entry, window, root, node)
                if action == "text":
                    pattern = self._pattern(node, "Text")
                    value_pattern = self._pattern(node, "Value") if pattern is None else None
                    text = (
                        pattern.DocumentRange.GetText(4096)
                        if pattern is not None
                        else value_pattern.Value
                        if value_pattern is not None
                        else node.Name
                    )
                    entry["text"] = str(text)[:4096]
                    if len(str(text)) > 4096:
                        entry["text_truncated"] = True
                values.append(entry)
            self._bind(window, root, control)
            return result(
                "ok", action, method="uia", data={"nodes": values, "truncated": truncated}
            )
        if action == "inspect":
            data = self._metadata(control)
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
                pattern = self._pattern(control, name)
                if pattern is None:
                    data["patterns"][name] = None
                else:
                    values = {prop: getattr(pattern, prop) for prop in properties}
                    if any(
                        isinstance(value, str) and len(value) > 4096 for value in values.values()
                    ):
                        data["properties_truncated"] = True
                    data["patterns"][name] = {
                        prop: self._bounded_property(value) for prop, value in values.items()
                    }
            if request.get("compact", False):
                data["patterns"] = {
                    key: value for key, value in data["patterns"].items() if value is not None
                }
            return result("ok", action, method="uia", data=data)
        return self._mutate(control, action, request)

    @staticmethod
    def _bounded_property(value):
        return value[:4096] if isinstance(value, str) else value

    def _mutate(self, control, action, request):
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
        data = {"before": before, "pattern_returned": returned}
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
        data["after"] = after
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
