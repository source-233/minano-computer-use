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
from .uia_membership import cross_process_pin


class Refusal(Exception):
    def __init__(self, code, message, *, details=None):
        super().__init__(message)
        self.code = code
        self.details = details


class _VerificationExpired(Exception):
    """Internal checkpoint, never provider text or a retry signal."""


def _error_type(error):
    # Provider-defined exception names can also contain private data. Expose only
    # fixed, useful categories, never exception text or arbitrary class names.
    name = type(error).__name__
    return (
        name
        if name
        in {
            "COMError",
            "OSError",
            "RuntimeError",
            "ValueError",
            "TypeError",
            "AttributeError",
            "TimeoutError",
            "PermissionError",
            "ReferenceError",
        }
        else "ProviderError"
    )


class UIABackend:
    def __init__(self, auto=None, win32=None, *, clock=None, sleep=None):
        self._owner = (os.getpid(), threading.get_ident())
        self._closed = False
        self._initialized = False
        self._clock = clock or time.monotonic
        self._sleep = sleep or time.sleep
        self._tokens = OrderedDict()
        self._memberships = {}
        self._candidate_pids = {}
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
        self._memberships.clear()
        self._candidate_pids.clear()
        self._closed = True

    def execute(self, request: dict) -> dict:
        self._check_owner()
        self._execute_started = self._clock()
        action = request.get("action", "inspect")
        before = None
        self._may_have_acted = False
        self._target_binding = None
        self._issued_this_call = 0
        self._memberships = {}
        self._candidate_pids = {}
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
            if isinstance(exc, Refusal) and exc.details is not None:
                output["error"]["details"] = exc.details
        except Exception as exc:
            output = result(
                "unknown" if self._may_have_acted else "error",
                action,
                method="uia",
                code="UIA_MUTATION_UNCERTAIN" if self._may_have_acted else "UIA_ERROR",
                # Provider exceptions can contain entered/password values. Do not echo them.
                message=f"{_error_type(exc)}: UIA provider operation failed",
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
                "error": f"{_error_type(exc)}: Observation unavailable",
            }
        output["delivery"] = "read_only" if action in READ_ACTIONS else "accessibility"
        output["fallbacks"] = []
        if self._target_binding is not None:
            output["target_binding"] = self._target_binding
        return bound_result(output, max_output_chars=budgets[0], max_output_bytes=budgets[1])

    def _pattern(self, control, name, *, checkpoint=None):
        # GetPattern swallows COMError in uiautomation. Distinguish unsupported from
        # a provider that advertises support but fails to supply the promised pattern.
        property_ids = getattr(self.auto, "PropertyId", None)
        available_id = getattr(property_ids, "Is" + name + "PatternAvailableProperty", None)
        advertised = None
        if available_id is not None:
            if checkpoint is not None:
                checkpoint()
            advertised = control.GetPropertyValue(available_id)
            if type(advertised) is not bool:
                raise RuntimeError("Provider pattern availability is unavailable")
            if not advertised:
                return None
        if checkpoint is not None:
            checkpoint()
        pattern = control.GetPattern(getattr(self.auto.PatternId, name + "Pattern"))
        if pattern is None and advertised:
            raise RuntimeError(f"Provider advertised {name}Pattern but retrieval failed")
        return pattern

    @staticmethod
    def _metadata(control, *, checkpoint=None):
        strings = {}
        for key, prop in (
            ("name", "Name"),
            ("automation_id", "AutomationId"),
            ("control_type", "ControlTypeName"),
        ):
            if checkpoint is not None:
                checkpoint()
            strings[key] = str(getattr(control, prop))
        if checkpoint is not None:
            checkpoint()
        data = {
            **{key: value if len(value) <= 4096 else None for key, value in strings.items()},
            "pid": int(control.ProcessId),
        }
        if checkpoint is not None:
            checkpoint()
        if any(len(value) > 4096 for value in strings.values()):
            data["metadata_truncated"] = True
            data["fields_omitted"] = [key for key, value in strings.items() if len(value) > 4096]
        return data

    @staticmethod
    def _unavailable(error=None, reason="Provider property unavailable"):
        evidence = {"status": "unavailable", "reason": reason}
        if error is not None:
            evidence["error_type"] = _error_type(error)
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

    def _unsafe_text_nodes(self, nodes, truncated, *, checkpoint=None):
        # A TextPattern on an ancestor can include descendant password text. If the
        # traversal cannot establish the entire scope, redact rather than infer.
        if truncated:
            return set(range(len(nodes)))
        unsafe = set()
        for index, (node, _, _) in enumerate(nodes):
            if checkpoint is not None:
                checkpoint()
            if self._password(node) is False:
                continue
            while index is not None:
                unsafe.add(index)
                index = nodes[index][2]
        return unsafe

    def _aggregate_value_safe(self, control, depth, maximum, *, checkpoint=None):
        nodes, truncated = self._walk(control, depth, maximum, checkpoint=checkpoint)
        return not truncated and not self._unsafe_text_nodes(
            nodes, truncated, checkpoint=checkpoint
        )

    @staticmethod
    def _membership_failure(predicate, error=None):
        failure = {"predicate": predicate}
        if error is not None:
            failure["error_type"] = _error_type(error)
        return failure

    @staticmethod
    def _refuse_membership(stage, node_index, failure):
        raise Refusal(
            "STALE_TARGET",
            "UIA membership could not be established",
            details={"stage": stage, "node_index": node_index, **failure},
        )

    def _state_binding(self, request, window, *, stage="initial_binding"):
        expected = request.get("target_binding")
        if (
            not isinstance(expected, dict)
            or set(expected) != {"hwnd", "pid", "process_creation_time"}
            or any(type(value) is not int or value <= 0 for value in expected.values())
        ):
            raise Refusal(
                "INVALID_TARGET_BINDING", "state requires a complete parent target binding"
            )
        try:
            actual = self._window_identity(window)
        except Exception as exc:
            self._refuse_membership(
                stage, 0, self._membership_failure("binding_identity_unavailable", exc)
            )
        if actual.get("process_creation_time") is None:
            self._refuse_membership(
                stage, 0, self._membership_failure("binding_identity_unavailable")
            )
        for key in expected:
            if actual.get(key) != expected.get(key):
                self._refuse_membership(
                    stage, 0, self._membership_failure("binding_" + key + "_mismatch")
                )
        return dict(expected)

    def _state_live_candidate(
        self, control, original_id, root, root_id, expected, *, checkpoint=None
    ):
        """Return only the first original check's failure; None means established.

        Keep provider reads/short circuiting in their original order. In particular,
        the live root HWND comparison remains stricter than the initial None check.
        """
        check = checkpoint or (lambda: None)
        predicate = "provider_property_unavailable"
        try:
            check()
            current_id = self._runtime_id(root)
            if current_id != root_id:
                return self._membership_failure(
                    "root_runtime_id_unavailable"
                    if current_id is None
                    else "root_runtime_id_changed"
                )
            check()
            if int(root.ProcessId) != expected["pid"]:
                return self._membership_failure("root_pid_mismatch")
            check()
            root_hwnd = getattr(root, "NativeWindowHandle", expected["hwnd"])
            if root_hwnd != expected["hwnd"]:
                # uiautomation also maps COMError/None to zero; its cause is unknown.
                return self._membership_failure(
                    "root_hwnd_unavailable"
                    if root_hwnd is None
                    else "root_hwnd_zero_or_unavailable"
                    if root_hwnd == 0
                    else "root_hwnd_mismatch"
                )
            check()
            current_id = self._runtime_id(control)
            if current_id != original_id:
                return self._membership_failure(
                    "candidate_runtime_id_unavailable"
                    if current_id is None
                    else "candidate_runtime_id_changed"
                )
            check()
            candidate_pid = int(control.ProcessId)
            key = (
                expected["hwnd"],
                expected["pid"],
                expected["process_creation_time"],
                root_id,
                original_id,
            )
            memberships = getattr(self, "_memberships", {})
            previous = memberships.get(key)
            candidate_pids = getattr(self, "_candidate_pids", {})
            if key in candidate_pids and candidate_pid != candidate_pids[key]:
                return self._membership_failure("element_process_changed")
            candidate_pids[key] = candidate_pid
            self._candidate_pids = candidate_pids
            if candidate_pid != expected["pid"]:
                if not hasattr(self.win32, "child_window_identity"):
                    return self._membership_failure("candidate_pid_mismatch")
                predicate = "cross_process_membership_unavailable"
                pin, failure = cross_process_pin(
                    self,
                    control,
                    root,
                    expected,
                    original_id,
                    root_id,
                    previous=previous,
                    checkpoint=checkpoint,
                )
                if failure:
                    return self._membership_failure(failure)
                memberships[key] = pin
                self._memberships = memberships
                return None
            if previous is not None:
                return self._membership_failure("element_process_changed")
            predicate = "ancestry_unavailable"
            ancestor = control
            for _ in range(64):
                check()
                ancestor_id = self._runtime_id(ancestor)
                if ancestor_id == root_id:
                    break
                check()
                ancestor = ancestor.GetParentControl()
                if ancestor is None:
                    return self._membership_failure("ancestry_unavailable")
            else:
                return self._membership_failure("ancestry_limit")
            predicate = "provider_property_unavailable"
            check()
            hwnd = getattr(control, "NativeWindowHandle", 0)
            if hwnd in (None, 0, expected["hwnd"]):
                return None
            predicate = "native_membership_unavailable"
            checker = getattr(self.win32, "is_child_window", None)
            if checker is not None:
                check()
                return (
                    None
                    if bool(checker(expected["hwnd"], int(hwnd), expected["pid"]))
                    else self._membership_failure("candidate_hwnd_membership_mismatch")
                )
            user32_factory = getattr(self.win32, "_user32", None)
            if user32_factory is None:
                return self._membership_failure("native_membership_unavailable")
            check()
            user32 = user32_factory()
            child_pid = wintypes.DWORD()
            check()
            child_thread = user32.GetWindowThreadProcessId(int(hwnd), ctypes.byref(child_pid))
            if child_thread == 0:
                return self._membership_failure("native_membership_unavailable")
            user32.IsChild.argtypes = [wintypes.HWND, wintypes.HWND]
            user32.IsChild.restype = wintypes.BOOL
            check()
            member = child_pid.value == expected["pid"] and bool(
                user32.IsChild(expected["hwnd"], int(hwnd))
            )
            return (
                None if member else self._membership_failure("candidate_hwnd_membership_mismatch")
            )
        except _VerificationExpired:
            raise
        except Refusal as exc:
            if exc.code == "DEADLINE_EXCEEDED":
                raise
            return self._membership_failure(predicate, exc)
        except Exception as exc:
            return self._membership_failure(predicate, exc)

    def _require_state_candidate(self, control, original_id, root, root_id, expected, stage, index):
        failure = self._state_live_candidate(control, original_id, root, root_id, expected)
        if failure is not None:
            self._refuse_membership(stage, index, failure)

    def _state(self, request, window, root, depth, maximum):
        expected = self._state_binding(request, window)
        self._require_root_pid(root, expected["pid"], "state_initial_root")
        try:
            root_hwnd = root.NativeWindowHandle
        except Exception:
            root_hwnd = None
        if root_hwnd not in (None, expected["hwnd"]):
            self._refuse_membership(
                "state_initial_root",
                0,
                # The wrapper's zero can mean a swallowed provider read failure.
                self._membership_failure(
                    "root_hwnd_zero_or_unavailable" if root_hwnd == 0 else "root_hwnd_mismatch"
                ),
            )
        times = {}
        start = self._clock()
        root_id = self._state_runtime_id(root, "state_initial_root", 0)
        nodes, truncated = self._walk(root, depth, maximum)
        times["traversal"] = {"start": start, "end": self._clock()}
        if root_id is None:
            self._refuse_membership(
                "state_initial_root", 0, self._membership_failure("root_runtime_id_unavailable")
            )
        original_ids = [
            self._state_runtime_id(node, "traversal_identity", index)
            for index, (node, _, _) in enumerate(nodes)
        ]
        if any(node_id is None for node_id in original_ids):
            self._refuse_membership(
                "traversal_identity",
                original_ids.index(None),
                self._membership_failure("candidate_runtime_id_unavailable"),
            )
        unsafe_text = self._unsafe_text_nodes(nodes, truncated)
        focus_start = self._clock()
        focused = []
        for index, (control, _, _) in enumerate(nodes):
            self._require_state_candidate(
                control, original_ids[index], root, root_id, expected, "focus_before_read", index
            )
            try:
                if control.HasKeyboardFocus is True:
                    focused.append(index)
            except Exception:
                continue
            self._require_state_candidate(
                control, original_ids[index], root, root_id, expected, "focus_after_read", index
            )
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
                self._require_state_candidate(
                    control,
                    original_ids[index],
                    root,
                    root_id,
                    expected,
                    "selection_before_pattern",
                    index,
                )
                try:
                    pattern = self._pattern(control, "Text")
                    if pattern is None:
                        selection = {"status": "unavailable", "reason": "TextPattern unsupported"}
                    else:
                        self._require_state_candidate(
                            control,
                            original_ids[index],
                            root,
                            root_id,
                            expected,
                            "selection_before_read",
                            index,
                        )
                        ranges = pattern.GetSelection()
                        self._require_state_candidate(
                            control,
                            original_ids[index],
                            root,
                            root_id,
                            expected,
                            "selection_after_read",
                            index,
                        )
                        values, remaining = [], request.get("text_limit", 500)
                        for text_range in ranges[:16]:
                            if len(values) >= 16 or remaining <= 0:
                                break
                            self._require_state_candidate(
                                control,
                                original_ids[index],
                                root,
                                root_id,
                                expected,
                                "text_before_read",
                                index,
                            )
                            text = str(text_range.GetText(remaining))
                            self._require_state_candidate(
                                control,
                                original_ids[index],
                                root,
                                root_id,
                                expected,
                                "text_after_read",
                                index,
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
            self._require_state_candidate(
                control, original_ids[index], root, root_id, expected, "metadata_before_read", index
            )
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
        for index, (node, _, _) in enumerate(nodes):
            self._require_state_candidate(
                node, original_ids[index], root, root_id, expected, "before_publication", index
            )
        self._state_binding(request, window, stage="binding_before_publication")
        self._require_root_pid(root, expected["pid"], "root_before_publication")
        if request.get("include_tokens", False):
            # Issuance reads provider metadata too. Commit the cache only after all
            # entries still belong to this traversal; a failed state publishes none.
            previous_tokens = self._tokens.copy()
            previous_binding = self._target_binding.copy() if self._target_binding else None
            try:
                for index, (control, _, _) in enumerate(nodes):
                    self._require_state_candidate(
                        control,
                        original_ids[index],
                        root,
                        root_id,
                        expected,
                        "token_before_issue",
                        index,
                    )
                    entry = values[index]
                    self._issue_token(entry, window, root, control)
                    if "token" in entry:
                        cached = self._tokens[entry["token"]]
                        if (
                            cached["identity"] != expected
                            or cached["root_id"] != root_id
                            or cached["element_id"] != original_ids[index]
                            or any(
                                entry.get(key) != value for key, value in cached["metadata"].items()
                            )
                            or any(
                                values[0].get(key) != value
                                for key, value in cached["root_metadata"].items()
                            )
                        ):
                            self._refuse_membership(
                                "token_after_issue",
                                index,
                                self._membership_failure("token_binding_changed"),
                            )
                for index, (control, _, _) in enumerate(nodes):
                    self._require_state_candidate(
                        control,
                        original_ids[index],
                        root,
                        root_id,
                        expected,
                        "token_before_publication",
                        index,
                    )
                    token = values[index].get("token")
                    if token is not None:
                        cached = self._tokens[token]
                        if (
                            self._metadata(control) != cached["metadata"]
                            or self._metadata(root) != cached["root_metadata"]
                        ):
                            self._refuse_membership(
                                "token_before_publication",
                                index,
                                self._membership_failure("token_metadata_changed"),
                            )
                        self._require_state_candidate(
                            control,
                            original_ids[index],
                            root,
                            root_id,
                            expected,
                            "token_after_metadata",
                            index,
                        )
                self._state_binding(request, window, stage="token_binding_before_publication")
            except Exception:
                self._tokens = previous_tokens
                self._target_binding = previous_binding
                raise
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

    def _require_root_pid(self, root, pid, stage):
        if root is None:
            self._refuse_membership(stage, 0, self._membership_failure("root_unavailable"))
        try:
            actual_pid = int(root.ProcessId)
        except Exception as exc:
            self._refuse_membership(
                stage, 0, self._membership_failure("provider_property_unavailable", exc)
            )
        if actual_pid != pid:
            self._refuse_membership(stage, 0, self._membership_failure("root_pid_mismatch"))

    def _state_runtime_id(self, control, stage, index):
        try:
            return self._runtime_id(control)
        except Exception as exc:
            self._refuse_membership(
                stage, index, self._membership_failure("provider_property_unavailable", exc)
            )

    @staticmethod
    def _runtime_id(control, *, checkpoint=None):
        # uiautomation's Element property implicitly Refind()s a missing pointer.
        # Controls obtained from handles/walking must retain their original live pointer.
        if checkpoint is not None:
            checkpoint()
        if hasattr(control, "_element") and not control._element:
            raise ValueError("UIA live reference is gone; automatic Refind is forbidden")
        getter = getattr(control, "GetRuntimeId", None)
        if getter is None:
            return None
        if checkpoint is not None:
            checkpoint()
        value = getter()
        if checkpoint is not None:
            checkpoint()
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

    def _bind(self, window, root, control, *, checkpoint=None):
        if checkpoint is not None:
            checkpoint()
        identity = self._window_identity(window)
        root_id = self._runtime_id(root, checkpoint=checkpoint)
        element_id = self._runtime_id(control, checkpoint=checkpoint)
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
        membership = self._cross_membership(window, root, control, identity, root_id, element_id)
        if membership is not None and (
            metadata["pid"] != membership["pid"]
            or self._metadata(control) != metadata
            or self._metadata(root) != root_metadata
        ):
            self._refuse_membership(
                "cross_process_binding", 0, self._membership_failure("token_metadata_changed")
            )
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
            "membership": membership,
        }
        entry["token"] = token
        entry["token_ttl_seconds"] = 60
        self._issued_this_call += 1

    def _token_control(self, token, window, root, *, checkpoint=None):
        if checkpoint is not None:
            checkpoint()
        if not isinstance(token, str) or token not in self._tokens:
            raise Refusal("INVALID_TOKEN", "Token is unknown to this UIA worker")
        cached = self._tokens[token]
        if self._clock() >= cached["expires"]:
            del self._tokens[token]
            raise Refusal("EXPIRED_TOKEN", "Token TTL has expired; explicitly discover again")
        try:
            if checkpoint is not None:
                checkpoint()
            identity = self._window_identity(window)
            if checkpoint is not None:
                checkpoint()
            if identity != cached["identity"]:
                raise ValueError("Window/process binding changed")
            control = cached["control"]
            if (
                self._runtime_id(root, checkpoint=checkpoint) != cached["root_id"]
                or self._runtime_id(cached["root"], checkpoint=checkpoint) != cached["root_id"]
                or self._runtime_id(control, checkpoint=checkpoint) != cached["element_id"]
                or self._metadata(root, checkpoint=checkpoint) != cached["root_metadata"]
                or self._metadata(cached["root"], checkpoint=checkpoint) != cached["root_metadata"]
                or self._metadata(control, checkpoint=checkpoint) != cached["metadata"]
            ):
                raise ValueError("Live root/element identity changed")
            pin = cached.get("membership")
            key = (
                identity["hwnd"],
                identity["pid"],
                identity["process_creation_time"],
                cached["root_id"],
                cached["element_id"],
            )
            self._candidate_pids[key] = cached["metadata"]["pid"]
            if pin is not None:
                self._memberships[key] = pin
            self._cross_membership(
                window,
                root,
                control,
                identity,
                cached["root_id"],
                cached["element_id"],
                checkpoint=checkpoint,
            )
            # A still-readable but detached/reparented cached element is not a bound target.
            ancestor = control
            for _ in range(64):
                if self._runtime_id(ancestor, checkpoint=checkpoint) == cached["root_id"]:
                    break
                if checkpoint is not None:
                    checkpoint()
                ancestor = ancestor.GetParentControl()
                if checkpoint is not None:
                    checkpoint()
                if ancestor is None:
                    raise ValueError("Element is detached from its original root")
            else:
                raise ValueError("Cannot establish bounded element ancestry")
            self._bind(window, root, control, checkpoint=checkpoint)
            return control
        except Exception as exc:
            if isinstance(exc, Refusal) and exc.code == "DEADLINE_EXCEEDED":
                raise
            del self._tokens[token]
            raise Refusal("STALE_TOKEN", "Token binding no longer valid; discover again") from exc

    def _cross_membership(
        self, window, root, control, identity, root_id, element_id, *, checkpoint=None
    ):
        """Apply one cross-process proof to tokens and direct semantic mutations."""
        check = checkpoint or (lambda: None)
        check()
        self._runtime_id(control, checkpoint=checkpoint)
        check()
        pid = int(control.ProcessId)
        check()
        key = (
            identity["hwnd"],
            identity["pid"],
            identity["process_creation_time"],
            root_id,
            element_id,
        )
        previous = self._memberships.get(key)
        if key in self._candidate_pids and pid != self._candidate_pids[key]:
            self._refuse_membership(
                "cross_process_binding", 0, self._membership_failure("element_process_changed")
            )
        self._candidate_pids[key] = pid
        if pid != identity["pid"] or previous is not None:
            failure = self._state_live_candidate(
                control, element_id, root, root_id, identity, checkpoint=checkpoint
            )
            if failure is not None:
                self._refuse_membership("cross_process_binding", 0, failure)
        return self._memberships.get(key)

    def _walk(self, root, depth, max_nodes, *, checkpoint=None):
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
            if checkpoint is not None:
                checkpoint()
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
                if checkpoint is not None:
                    checkpoint()
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
    def _matches(control, selector, *, checkpoint=None):
        for key, prop in (
            ("name", "Name"),
            ("automation_id", "AutomationId"),
            ("control_type", "ControlType"),
        ):
            if key not in selector:
                continue
            if checkpoint is not None:
                checkpoint()
            actual = getattr(control, prop)
            if checkpoint is not None:
                checkpoint()
            if actual != selector[key]:
                return False
        return True

    def _select(self, root, selector, depth, maximum, *, allow_missing=False, checkpoint=None):
        conditions = self._selector(selector)
        nodes, truncated = self._walk(root, depth, maximum, checkpoint=checkpoint)
        if checkpoint is not None:
            checkpoint()
        if truncated:
            raise Refusal("SEARCH_TRUNCATED", "Cannot establish uniqueness in a truncated tree")
        matches = [
            node for node, _, _ in nodes if self._matches(node, conditions, checkpoint=checkpoint)
        ]
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
        checkpoint = None
        if action == "set_value":
            timeout = request.get("timeout", 5)
            deadline = request.get("_deadline_monotonic", self._execute_started + 120)
            if (
                type(timeout) not in (int, float)
                or not math.isfinite(timeout)
                or not 0 <= timeout <= 120
                or type(deadline) not in (int, float)
                or not math.isfinite(deadline)
            ):
                raise Refusal("INVALID_TIMEOUT", "Invalid set_value deadline or timeout")
            self._value_deadline = min(deadline, self._execute_started + timeout)

            def checkpoint():
                if self._clock() >= self._value_deadline:
                    raise Refusal("DEADLINE_EXCEEDED", "set_value deadline expired before mutation")

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
        if checkpoint is not None:
            checkpoint()
        window = self.win32.resolve_window(request.get("target"))
        if checkpoint is not None:
            checkpoint()
        if action == "state":
            self._state_binding(request, window)
        try:
            root = self.auto.ControlFromHandle(window["hwnd"])
        except Exception as exc:
            self._refuse_membership(
                "initial_root", 0, self._membership_failure("root_unavailable", exc)
            )
        # ProcessId is a read property, NOT a supported search keyword in uiautomation.
        if checkpoint is not None:
            checkpoint()
        self._require_root_pid(root, window["pid"], "initial_root")
        if checkpoint is not None:
            checkpoint()
        root_pointer = getattr(root, "_element", None) if action == "set_value" else None
        try:
            self._bind(window, root, root, checkpoint=checkpoint)
        except Exception as exc:
            if isinstance(exc, Refusal) and exc.code == "DEADLINE_EXCEEDED":
                raise
            self._refuse_membership(
                "initial_root_binding",
                0,
                self._membership_failure("provider_property_unavailable", exc),
            )
        initial_binding = self._target_binding.copy()
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
            control = self._token_control(request["token"], window, root, checkpoint=checkpoint)
        elif selector is not None:
            control = self._select(root, selector, depth, maximum, checkpoint=checkpoint)
        if action == "set_value":
            return self._set_value(
                request, window, root, control, initial_binding, root_pointer, depth, maximum
            )
        self._bind(window, root, control)
        mutation_guard = None
        if action not in READ_ACTIONS:
            identity = {
                key: initial_binding[key] for key in ("hwnd", "pid", "process_creation_time")
            }
            rid = tuple(initial_binding["root_runtime_id"] or ())
            cid = self._runtime_id(control)
            pin = self._cross_membership(window, root, control, identity, rid, cid)
            if pin is not None:

                def mutation_guard():
                    failure = self._state_live_candidate(control, cid, root, rid, identity)
                    if failure is not None:
                        self._refuse_membership("cross_process_binding", 0, failure)

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
        try:
            return self._mutate(control, action, request, guard=mutation_guard)
        except Refusal as exc:
            if not self._may_have_acted:
                raise
            response = result(
                "unknown",
                action,
                method="uia",
                code=exc.code,
                message="UIA mutation lost its bound membership",
                may_have_acted=True,
            )
            if exc.details is not None:
                response["error"]["details"] = exc.details
            return response

    def _set_value(self, request, window, root, control, binding, root_pointer, depth, maximum):
        """One write, then bounded reads of the pinned live reference (never Refind)."""
        expected_identity = {key: binding[key] for key in ("hwnd", "pid", "process_creation_time")}
        root_id = tuple(binding["root_runtime_id"] or ())
        deadline = self._value_deadline

        def checkpoint():
            if self._clock() >= deadline:
                raise _VerificationExpired

        def guard():
            checkpoint()
            for node, pointer in ((root, root_pointer), (control, control_pointer)):
                if hasattr(node, "_element") and (
                    not node._element or node._element is not pointer
                ):
                    raise Refusal("STALE_TARGET", "UIA live reference changed")
            if self._window_identity(window) != expected_identity:
                raise Refusal("STALE_TARGET", "Window/process identity changed")
            failure = self._state_live_candidate(
                control, control_id, root, root_id, expected_identity, checkpoint=checkpoint
            )
            if failure is not None:
                self._refuse_membership("set_value_verification", 0, failure)
            checkpoint()
            current_root_id = self._runtime_id(root)
            checkpoint()
            current_control_id = self._runtime_id(control)
            checkpoint()
            current_identity = self._window_identity(window)
            checkpoint()
            if (
                current_root_id != root_id
                or current_control_id != control_id
                or current_identity != expected_identity
                or any(
                    hasattr(node, "_element") and node._element is not pointer
                    for node, pointer in ((root, root_pointer), (control, control_pointer))
                )
            ):
                raise Refusal("STALE_TARGET", "UIA live identity changed")

        private = False

        def privacy():
            nonlocal private
            guard()
            private = (
                not self._aggregate_value_safe(control, depth, maximum, checkpoint=checkpoint)
                or private
            )
            guard()

        returned = None
        try:
            checkpoint()
            control_id = self._state_runtime_id(control, "set_value_initial_control", 0)
            checkpoint()
            control_pointer = getattr(control, "_element", None)
            if (
                not root_id
                or not control_id
                or any(type(value) is not int or value <= 0 for value in expected_identity.values())
            ):
                raise Refusal("STALE_TARGET", "Reliable set_value identity is unavailable")
            guard()
            self._target_binding = {**binding, "element_runtime_id": list(control_id)}
            if not isinstance(request.get("value"), str):
                raise Refusal("INVALID_VALUE", "set_value requires a string")
            pattern = self._pattern(control, "Value", checkpoint=checkpoint)
            guard()
            if pattern is None:
                raise Refusal("UNSUPPORTED_PATTERN", "ValuePattern is not supported")
            readonly = pattern.IsReadOnly
            guard()
            if readonly:
                raise Refusal("READ_ONLY", "Value pattern is read-only")
            privacy()
            before = pattern.Value
            privacy()
            setter = pattern.SetValue
            guard()
            checkpoint()
            self._may_have_acted = True
            returned = setter(request["value"], waitTime=0)
            deadline = min(self._value_deadline, self._clock() + 0.6)
            while True:
                privacy()
                checkpoint()
                after = pattern.Value
                # privacy() already guards before AND after its provider reads;
                # an extra guard here adds the same cross-process RPC traversal.
                privacy()
                if after == request["value"]:
                    break
                remaining = deadline - self._clock()
                if remaining <= 0:
                    raise _VerificationExpired
                self._sleep(min(0.05, remaining))
            data = {
                "before": None if private else self._bounded_property(before),
                "after": None if private else self._bounded_property(after),
                "pattern_returned": returned if type(returned) in (bool, type(None)) else None,
            }
            if private:
                data["value_redacted"] = True
            checkpoint()
            return result("verified", "set_value", method="uia", data=data)
        except _VerificationExpired:
            if not self._may_have_acted:
                raise Refusal(
                    "DEADLINE_EXCEEDED", "set_value deadline expired before mutation"
                ) from None
            return self._readback_timeout(returned)
        except (Refusal, default_win32.TargetError) as exc:
            if not self._may_have_acted:
                raise
            response = result(
                "unknown",
                "set_value",
                method="uia",
                may_have_acted=True,
                code=exc.code,
                message="set_value verification lost its live target binding",
            )
            if isinstance(exc, Refusal) and exc.details is not None:
                response["error"]["details"] = exc.details
            return response

    @staticmethod
    def _readback_timeout(returned):
        data = {
            "before": None,
            "after": None,
            "value_redacted": True,
            "pattern_returned": returned if type(returned) in (bool, type(None)) else None,
        }
        return result(
            "unknown",
            "set_value",
            method="uia",
            data=data,
            may_have_acted=True,
            code="READBACK_TIMEOUT",
            message="Value was not confirmed before the verification deadline",
        )

    @staticmethod
    def _bounded_property(value):
        return value[:4096] if isinstance(value, str) else value

    def _mutate(self, control, action, request, *, guard=None):
        check = guard or (lambda: None)
        check()
        pattern_name = {
            "invoke": "Invoke",
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
            "toggle": "Toggle",
            "select": "Select",
            "expand": "Expand",
            "collapse": "Collapse",
            "scroll_into_view": "ScrollIntoView",
            "scroll": "Scroll",
        }[action]
        if action == "toggle":
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
        setter = getattr(pattern, method)
        check()
        self._may_have_acted = True
        returned = setter(*args, waitTime=0)
        check()
        # Pattern methods are not obliged to return a boolean; never publish an
        # arbitrary provider object/string (which can contain entered text).
        data = {
            "before": self._bounded_property(before),
            "pattern_returned": returned if type(returned) in (bool, type(None)) else None,
        }
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
        check()
        data["after"] = self._bounded_property(after)
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
