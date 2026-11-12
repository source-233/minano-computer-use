"""Policy and orchestration, with lazy platform backends and injectable dependencies."""

from __future__ import annotations

import inspect
import json
import math
import platform
import threading
import time
from pathlib import Path

from .broker import UIABroker
from .contracts import READ_ACTIONS, STOP_STATUSES, UIA_ACTIONS, result
from .control import OperationCancelled, OperationController
from .output import bound_result, validate_budget


def _number(value, name, minimum, maximum):
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or not minimum <= value <= maximum
    ):
        raise ValueError(f"{name} must be finite and in [{minimum}, {maximum}]")
    return value


def _target(value):
    if not isinstance(value, str) or not value.strip():
        raise ValueError("An explicit nonempty target is required")
    return value


def _limits(action):
    for field, low, high in (("depth", 0, 32), ("max_nodes", 1, 5000)):
        if field in action:
            value = action[field]
            if type(value) is not int or not low <= value <= high:
                raise ValueError(f"{field} must be an integer in [{low}, {high}]")


class Runtime:
    def __init__(
        self,
        allow_native=False,
        broker=None,
        native=None,
        capture=None,
        output_dir=None,
        trace_enabled=False,
        artifact_max_bytes=256 * 1024 * 1024,
        artifact_max_files=200,
        allow_shell=False,
        allow_process_control=False,
        services=None,
        coordinate_manager=None,
    ):
        # Deprecated constructor switch: permission is selected per call only.
        self.allow_native = True
        self.allow_shell = allow_shell
        self.allow_process_control = allow_process_control
        self._services = services
        self._coordinate_manager = coordinate_manager
        self._broker = broker
        self._native = native
        self._capture = capture
        self._output_dir = output_dir
        self._lock = threading.RLock()
        self._closed = False
        self.controller = OperationController()
        self._operation = None
        self._request_lock = threading.Lock()
        self._active_request = None
        self._trace_enabled = trace_enabled
        self._artifact_max_bytes = artifact_max_bytes
        self._artifact_max_files = artifact_max_files
        self._artifact_store = None
        self._trace = None

    def cancel_request(self, request_event):
        """Cancel only this awaiter, never the unrelated owner ahead of it."""
        with self._request_lock:
            request_event.set()
            if self._active_request is request_event and request_event is not None:
                return self.controller.cancel()
        return {"requested": False, "call_id": None}

    def _artifacts(self):
        if self._artifact_store is None:
            from .trace import ArtifactStore

            self._artifact_store = ArtifactStore(
                Path(self._output_dir or ".artifacts"),
                max_bytes=self._artifact_max_bytes,
                max_files=self._artifact_max_files,
            )
        return self._artifact_store

    def _log(self, operation, request, response):
        if not self._trace_enabled:
            return
        try:
            if self._trace is None:
                from .trace import TraceRecorder

                self._trace = TraceRecorder(self._artifacts(), enabled=True)
            # The recorder independently allowlists. Never hand it raw user input.
            safe_request = {"call_id": response.get("call_id")}
            safe_result = {
                key: response[key]
                for key in (
                    "call_id",
                    "status",
                    "method",
                    "cancelled",
                    "may_have_acted",
                    "started_at",
                    "finished_at",
                    "delivery",
                )
                if key in response
            }
            if isinstance(response.get("error"), dict):
                safe_result["error"] = {"code": response["error"].get("code")}
            binding = response.get("target_binding")
            if isinstance(binding, dict):
                safe_result["target_binding"] = {
                    key: binding[key] for key in ("pid", "hwnd") if type(binding.get(key)) is int
                }
            data = response.get("data", {})
            if operation == "play":
                data = data.get("actions", {}).get("data", {})
            if isinstance(data, dict):
                safe_data = {
                    key: data[key]
                    for key in ("requested", "attempted", "completed", "stopped_early")
                    if type(data.get(key)) in (int, bool)
                }
                cleanup = data.get("cleanup")
                if isinstance(cleanup, dict):
                    safe_data["cleanup"] = {"status": cleanup.get("status")}
                if isinstance(data.get("results"), list):
                    safe_data["results"] = []
                    for item in data["results"]:
                        observed = item.get("result", {})
                        facts = {
                            key: observed[key]
                            for key in ("status", "method", "cancelled")
                            if key in observed
                        }
                        if isinstance(observed.get("error"), dict):
                            facts["error"] = {"code": observed["error"].get("code")}
                        safe_data["results"].append({"index": item.get("index"), "result": facts})
                safe_result["data"] = safe_data
            self._trace.record(operation, safe_request, safe_result)
        except Exception:
            # Metadata IO cannot alter the observed action outcome.
            pass

    @staticmethod
    def _cancelled(operation, may_have_acted=False):
        return result(
            "unknown" if may_have_acted else "refused",
            operation,
            code="CANCELLED",
            cancelled=True,
            may_have_acted=may_have_acted,
            message="Operation cancelled; any dispatched mutation is not replayed",
        )

    def _execute(self, name, request, callback, delivery="read_only", request_event=None):
        # Admission occurs *after* serialization; queued requests cannot reset the owner event.
        with self._lock:
            try:
                self._check_open()
                with self.controller.operation() as operation:
                    group_token = None
                    response = None
                    try:
                        # A capture or trace call may return multiple artifacts.
                        # Pin the whole call before admission so quota pruning
                        # cannot delete one result while another is registered.
                        if (
                            self._trace_enabled
                            or name in {"screenshot", "record", "play", "process.shell"}
                            or request.get("capture_artifacts")
                        ):
                            group_token = self._artifacts().begin_group()
                        with self._request_lock:
                            self._active_request = request_event
                            self._operation = operation
                            if request_event is not None and request_event.is_set():
                                operation.event.set()
                        began = time.monotonic()
                        try:
                            operation.checkpoint()
                            response = callback(operation)
                            if not isinstance(response, dict):
                                response = result(
                                    "ok", name, data=response.metadata, frame=response
                                )
                        except OperationCancelled:
                            response = self._cancelled(name)
                        except Exception as exc:
                            response = self._failure(name, exc)
                        finally:
                            with self._request_lock:
                                self._active_request = None
                                self._operation = None
                        response.setdefault("delivery", delivery)
                        response.setdefault("fallbacks", [])
                        if operation.event.is_set() and not response.get("cancelled"):
                            acted = response.get("may_have_acted", False) or (
                                delivery != "read_only"
                                and response.get("status")
                                in {"invoked", "verified", "ok", "partial"}
                            )
                            response.update(self._cancelled(name, acted))
                        response["call_id"] = operation.call_id
                        response["started_at"] = began
                        response["finished_at"] = time.monotonic()
                        # Callbacks return only after native release and capture join.
                        self._log(name, request, response)
                        return response
                    finally:
                        if group_token is not None:
                            try:
                                self._artifact_store.end_group(group_token)
                            except Exception as exc:
                                if isinstance(response, dict):
                                    response.setdefault("warnings", []).append(
                                        {
                                            "code": "ARTIFACT_RETENTION_FAILED",
                                            "detail": type(exc).__name__,
                                        }
                                    )
            except Exception as exc:
                return self._failure(name, exc)

    def _check_open(self):
        if self._closed:
            raise ValueError("Runtime is closed")

    def _uia(self):
        if self._broker is None:
            self._broker = UIABroker()
        return self._broker

    @staticmethod
    def _broker_call(broker, request, timeout, cancel_event):
        """Keep small injected test brokers source-compatible with the old API."""
        parameters = inspect.signature(broker.call).parameters.values()
        supported = any(
            item.name == "cancel_event" or item.kind == inspect.Parameter.VAR_KEYWORD
            for item in parameters
        )
        if supported:
            return broker.call(request, timeout=timeout, cancel_event=cancel_event)
        return broker.call(request, timeout=timeout)

    def _input(self):
        if self._native is None:
            from .native import NativeInput

            self._native = NativeInput(enabled=True, services=self._automation())
        return self._native

    def _automation(self):
        if self._services is None:
            from .automation_services import AutomationServices

            self._services = AutomationServices(
                allow_shell=self.allow_shell,
                allow_process_control=self.allow_process_control,
                artifact_store=self._artifacts(),
            )
        return self._services

    def _coordinates(self):
        if self._coordinate_manager is None:
            from .coordinates import CoordinateManager

            self._coordinate_manager = CoordinateManager()
        return self._coordinate_manager

    def _camera(self):
        if self._capture is None:
            from .capture import CaptureService

            self._capture = CaptureService(
                output_dir=self._output_dir,
                artifact_store=self._artifacts(),
                coordinate_manager=self._coordinates(),
            )
        return self._capture

    def _map_actions(self, actions, coordinate_space, capture_id, target, scope, native=False):
        manager = self._coordinates()
        if native:
            resolved = {}

            def viewport(selected):
                if selected not in resolved:
                    resolved[selected] = manager.resolve(selected, scope=scope)
                return resolved[selected]

            mapped = []
            for item in actions:
                action = dict(item)
                selected = action.get("target", target)
                provenance = selected
                if isinstance(selected, str) and selected.startswith("viewport:"):
                    definition = viewport(selected)
                    concrete = definition["target"]
                    if concrete == "desktop":
                        # A screen crop supplies geometry, not a keyboard/mouse
                        # recipient. Never silently choose the foreground window.
                        if (
                            not isinstance(target, str)
                            or target in {"desktop", "foreground"}
                            or target.startswith("viewport:")
                        ):
                            raise ValueError(
                                "Screen viewport input requires an explicit concrete outer target"
                            )
                        concrete = target
                        provenance = concrete
                    else:
                        provenance = concrete
                    if "target" in action:
                        action["target"] = concrete
                mapped.extend(
                    manager.map_actions(
                        [action],
                        coordinate_space=coordinate_space,
                        capture_id=capture_id,
                        target=provenance,
                        scope=scope,
                    )
                )
            return mapped
        # Each native action may override the outer target. Validate every binding
        # before the first effect; checking only the outer target would allow a
        # different per-action window to consume another window's image points.
        if coordinate_space == "image":
            for selected in {action.get("target", target) for action in actions}:
                manager.map_actions(
                    [],
                    coordinate_space=coordinate_space,
                    capture_id=capture_id,
                    target=selected,
                    scope=scope,
                )
        return manager.map_actions(
            actions,
            coordinate_space=coordinate_space,
            capture_id=capture_id,
            target=target,
            scope=scope,
        )

    @staticmethod
    def _failure(operation, exc, status="refused"):
        return result(
            status, operation, code=getattr(exc, "code", "INVALID_ARGUMENT"), message=str(exc)
        )

    def window(self, action="list", target=None, selector=None, *, _request_cancel=None, **kwargs):
        alias = action
        window_target = kwargs.pop("window", None)
        if window_target is not None:
            if target is not None and target != window_target:
                return self._failure("window", ValueError("Conflicting window and target"))
            target = window_target
        native = kwargs.pop("native", False)
        process_permission = kwargs.pop("allow_process_control", False)
        if action in {"get_text", "ui_tree", "focus", "force_focus", "close"}:
            query = kwargs.pop("query", None)
            if query is not None:
                if target is not None:
                    return self._failure("window", ValueError("Conflicting query and target"))
                target = query
        elif action == "click_element":
            query = kwargs.pop("query", None)
            if query is not None:
                if selector is not None:
                    return self._failure("window", ValueError("Conflicting query and selector"))
                selector = query
        role, nth = kwargs.pop("role", None), kwargs.pop("nth", None)
        if role is not None or nth is not None:
            if selector is None:
                selector = {}
            elif isinstance(selector, str):
                selector = {"name": selector}
            else:
                selector = dict(selector)
            if role is not None:
                selector["control_type"] = role
            if nth is not None:
                selector["index"] = nth
        action = {"get_text": "text", "ui_tree": "tree", "click_element": "invoke"}.get(
            action, action
        )
        request = dict(action=action, target=target, selector=selector, **kwargs)

        def execute(operation):
            if action in {"focus", "force_focus"}:
                prepared = self._validate_actions(
                    [{"action": action}],
                    target,
                    native,
                    True,
                    kwargs.get("timeout", 5),
                )
                return self._run_actions(
                    prepared, native, True, kwargs.get("timeout", 5), operation=operation
                )
            if action in {"close", "ocr"}:
                if action == "close" and not (
                    self.allow_process_control and process_permission is True
                ):
                    raise ValueError(
                        "close requires startup and per-call process-control permission"
                    )
                options = {
                    key: value
                    for key, value in kwargs.items()
                    if key
                    not in {
                        "depth",
                        "max_nodes",
                        "compact",
                        "include_tokens",
                        "token",
                        "max_output_chars",
                        "max_output_bytes",
                        "timeout",
                    }
                }
                if action == "ocr":
                    options["timeout"] = kwargs.get("timeout", 5)
                return self._automation().window(
                    action,
                    target,
                    selector,
                    allow_process_control=process_permission,
                    cancel_event=operation.event,
                    deadline=time.monotonic() + kwargs.get("timeout", 5),
                    **options,
                )
            if alias == "get_text":
                return self._window_read_text(target, operation, selector=selector, **kwargs)
            discovery = {}
            if action == "list":
                discovery = {
                    key: request.pop(key) for key in ("query", "limit", "details") if key in request
                }
            self._validate_uia(request)
            if action == "list" and target is None and selector is None:
                from . import win32

                operation.checkpoint()
                windows = win32.list_windows()
                query = discovery.get("query")
                if query is not None:
                    if not isinstance(query, str):
                        raise ValueError("query must be text")
                    windows = [
                        item
                        for item in windows
                        if query.casefold()
                        in " ".join(str(value) for value in item.values()).casefold()
                    ]
                if "limit" in discovery:
                    limit = discovery["limit"]
                    if type(limit) is not int or not 1 <= limit <= 256:
                        raise ValueError("limit must be 1..256")
                    windows = windows[:limit]
                if type(discovery.get("details", False)) is not bool:
                    raise ValueError("details must be boolean")
                if discovery.get("details"):
                    return self._window_details(windows, operation)
                return result("ok", "list", method="win32", data=windows)
            return self._broker_call(
                self._uia(),
                request,
                timeout=request.get("timeout", 5.0),
                cancel_event=operation.event,
            )

        return self._execute(
            "window",
            request,
            execute,
            "read_only" if action in READ_ACTIONS else "accessibility",
            _request_cancel,
        )

    def _window_details(self, windows, operation):
        from . import win32

        deadline = time.monotonic() + 5
        inventory = self._automation().process(
            "list",
            cancel_event=operation.event,
            deadline=deadline,
        )
        if inventory.get("status") != "ok":
            return inventory
        displays = self._automation().system(
            "displays",
            cancel_event=operation.event,
            deadline=deadline,
        )
        if displays.get("status") != "ok":
            return displays
        operation.checkpoint()
        foreground = win32.observations()["foreground_hwnd"]
        processes = {entry["pid"]: entry for entry in inventory["data"]}
        detailed = []
        for window in windows:
            entry = dict(window)
            entry["process"] = processes.get(entry.get("pid"))
            entry["foreground"] = entry.get("hwnd") == foreground
            rect = entry.get("rect", [0, 0, 0, 0])
            overlaps = []
            for display in displays["data"]:
                left, top, right, bottom = display["rect"]
                area = max(0, min(rect[2], right) - max(rect[0], left)) * max(
                    0, min(rect[3], bottom) - max(rect[1], top)
                )
                if area:
                    overlaps.append((area, display))
            entry["monitor"] = max(overlaps, key=lambda item: item[0])[1] if overlaps else None
            detailed.append(entry)
        return result("ok", "list", method="win32+process_inventory+displays", data=detailed)

    def _window_read_text(self, target, operation, selector=None, **kwargs):
        request = dict(action="text", target=target, selector=selector, **kwargs)
        self._validate_uia(request)
        deadline = time.monotonic() + kwargs.get("timeout", 5)
        response = self._broker_call(
            self._uia(), request, kwargs.get("timeout", 5), operation.event
        )
        if response.get("status") != "ok":
            return response
        data = response.get("data")
        if isinstance(data, str):
            empty = not data.strip()
        elif isinstance(data, dict):
            nodes = data.get("nodes")
            empty = (
                isinstance(nodes, list)
                and not data.get("truncated")
                and not any(str(node.get("text", "")).strip() for node in nodes)
            )
        else:
            empty = False
        if empty:
            operation.checkpoint()
            return self._automation().ocr(
                target,
                cancel_event=operation.event,
                timeout=kwargs.get("timeout", 5),
                deadline=deadline,
            )
        return response

    def _validate_uia(self, request):
        name = request.get("action")
        if name not in UIA_ACTIONS:
            raise ValueError("Unsupported UIA action; physical input is never a fallback")
        common = {
            "action",
            "target",
            "selector",
            "token",
            "timeout",
            "depth",
            "max_nodes",
            "compact",
            "include_tokens",
            "max_output_chars",
            "max_output_bytes",
        }
        specific = {
            "set_value": {"value"},
            "scroll": {"direction", "amount"},
            "find": {"query", "match", "limit"},
            "wait_for": {"condition", "poll_interval"},
        }.get(name, set())
        if set(request) - common - specific:
            unknown = sorted(set(request) - common - specific)
            raise ValueError(f"Unsupported {name} arguments: {unknown}")
        token = request.get("token")
        selector = request.get("selector")
        if name != "list" or selector is not None or token is not None:
            _target(request.get("target"))
        self._validate_selector(selector)
        if token is not None:
            if not isinstance(token, str) or not token:
                raise ValueError("token must be a nonempty string")
            if selector is not None:
                raise ValueError("token and selector are mutually exclusive")
        _limits(request)
        _number(request.get("timeout", 5.0), "timeout", 0.001, 120)
        validate_budget(
            request.get("max_output_chars", 24000), request.get("max_output_bytes", 65536)
        )
        for key in ("compact", "include_tokens"):
            if key in request and type(request[key]) is not bool:
                raise ValueError(f"{key} must be a boolean")
        if name == "find":
            query = request.get("query")
            if query is not None and (not isinstance(query, str) or not query):
                raise ValueError("query must be a nonempty string")
            if query is None and selector is None:
                raise ValueError("find requires query or selector")
            if request.get("match", "contains") not in {"contains", "exact"}:
                raise ValueError("match must be contains or exact")
            if (
                type(request.get("limit", 20)) is not int
                or not 1 <= request.get("limit", 20) <= 256
            ):
                raise ValueError("limit must be an integer in [1, 256]")
        if name == "wait_for":
            if selector is None and token is None:
                raise ValueError("wait_for requires selector or token")
            condition = request.get("condition")
            if not isinstance(condition, dict) or len(condition) != 1:
                raise ValueError("condition requires exactly one supported field")
            key, value = next(iter(condition.items()))
            valid = (
                key in {"exists", "selected"}
                and type(value) is bool
                or key == "value"
                and isinstance(value, str)
                or key == "toggle_state"
                and type(value) is int
                and value in {0, 1, 2}
                or key == "expand_state"
                and type(value) is int
                and value in {0, 1, 2, 3}
            )
            if not valid:
                raise ValueError("Invalid wait_for condition")
            _number(request.get("poll_interval", 0.05), "poll_interval", 0.001, 5)
        if name == "set_value" and not isinstance(request.get("value"), str):
            raise ValueError("set_value requires a string value")
        if name == "scroll":
            if request.get("direction", "down") not in {"up", "down", "left", "right"}:
                raise ValueError("scroll direction must be up/down/left/right")
            if request.get("amount", "small") not in {"small", "large"}:
                raise ValueError("UIA scroll amount must be small or large")

    @staticmethod
    def _validate_selector(selector):
        if selector is None or isinstance(selector, str):
            return
        if not isinstance(selector, dict) or not selector:
            raise ValueError("selector must be an exact name or a nonempty selector object")
        if set(selector) - {"name", "automation_id", "control_type", "index"}:
            raise ValueError("Unknown selector field")
        for key, value in selector.items():
            if key == "index":
                if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                    raise ValueError("selector index must be a nonnegative integer")
            elif not isinstance(value, str):
                raise ValueError(f"selector {key} must be a string")

    def _validate_actions(
        self, actions, target, native, stop_on_error, timeout, timed=False, dynamic=False
    ):
        self._check_open()
        if not isinstance(native, bool) or not isinstance(stop_on_error, bool):
            raise ValueError("native and stop_on_error must be booleans")
        _number(timeout, "timeout", 0.001, 120)
        if not isinstance(actions, list) or not 1 <= len(actions) <= 100:
            raise ValueError("actions must contain 1 to 100 action objects")
        prepared = []
        previous = 0.0
        total_wait = 0.0
        for item in actions:
            if not isinstance(item, dict):
                raise ValueError("Each action must be an object")
            action = dict(item)
            name = action.get("action", action.get("type") if native else None)
            if not isinstance(name, str):
                raise ValueError("Each action requires an action name")
            if "native" in action:
                raise ValueError("Choose native once for the entire batch")
            if dynamic and name in {"probe", "until", "shot"}:
                allowed = {"action", "condition"} if name == "until" else {"action"}
                if set(action) != allowed:
                    raise ValueError(f"Invalid {name} marker fields")
                if name == "until":
                    from .playscript import parse_script

                    condition = action["condition"]
                    if not isinstance(condition, str) or parse_script("until " + condition) != [
                        action
                    ]:
                        raise ValueError("Invalid until condition")
                prepared.append(action)
                continue
            if timed:
                at = _number(action.get("at", 0), "at", 0, 60)
                if at < previous:
                    raise ValueError("Action timestamps must be monotonically nondecreasing")
                previous = at
            elif "at" in action:
                raise ValueError("Timed actions belong in play")
            if name == "wait":
                if "duration" in action:
                    if "seconds" in action:
                        raise ValueError("Conflicting wait seconds and duration")
                    action["seconds"] = action.pop("duration")
                if set(action) - {"action", "seconds", "at"}:
                    raise ValueError("wait accepts only seconds")
                duration = _number(action.get("seconds", 0), "wait seconds", 0, 30)
                total_wait += duration
                if total_wait > 60:
                    raise ValueError("Total wait duration exceeds 60 seconds")
            else:
                if not native:
                    action["target"] = action.get("target", target)
                    semantic = {key: value for key, value in action.items() if key != "at"}
                    self._validate_uia(semantic)
                    prepared.append(action)
                    continue
                from .input_plan import normalize_native_action

                selected_target = action.get("target", target)
                _target(selected_target)
                if "timeout" in action:
                    _number(action["timeout"], "action timeout", 0.001, 120)
                envelope = {key: action[key] for key in ("at", "timeout") if key in action}
                action = normalize_native_action(
                    {
                        key: value
                        for key, value in action.items()
                        if key not in {"target", "at", "timeout"}
                    }
                )
                action.update(envelope, target=selected_target)
            prepared.append(action)
        return prepared

    def _run_actions(
        self,
        actions,
        native,
        stop_on_error,
        timeout,
        start=None,
        operation=None,
        key_method="scancode",
        dynamic=None,
    ):
        results = []
        cleanup = None
        may_have_acted = False
        cancelled = False
        try:
            for index, item in enumerate(actions):
                action = dict(item)
                at = action.pop("at", 0)
                began = time.monotonic()
                dispatched = False
                mutation = action["action"] not in {"wait", "shot"} and (
                    native or action["action"] not in READ_ACTIONS
                )
                try:
                    operation.checkpoint()
                    if start is not None:
                        operation.wait(max(0, start + at - time.monotonic()))
                    if dynamic is not None and action["action"] in {"probe", "until", "shot"}:
                        dispatched = True
                        response = dynamic(action)
                    elif action["action"] == "wait":
                        operation.wait(action.get("seconds", 0))
                        response = result("ok", "wait", data={"seconds": action.get("seconds", 0)})
                    elif not native and action["action"] == "list" and action["target"] is None:
                        from . import win32

                        response = result("ok", "list", method="win32", data=win32.list_windows())
                    elif native:
                        self._input().set_cancel_event(operation.event)
                        dispatched = True
                        backend = self._input()
                        # Inspect before dispatch; never retry a TypeError after input.
                        parameters = inspect.signature(backend.execute).parameters
                        payload = {
                            key: value
                            for key, value in action.items()
                            if key not in {"target", "timeout"}
                        }
                        if "key_method" in parameters:
                            response = backend.execute(
                                payload, action["target"], key_method=key_method
                            )
                        else:
                            if key_method != "scancode":
                                raise ValueError(
                                    "Injected native backend does not support key_method"
                                )
                            response = backend.execute(action, action["target"])
                    else:
                        dispatched = True
                        response = self._broker_call(
                            self._uia(),
                            action,
                            timeout=action.get("timeout", timeout),
                            cancel_event=operation.event,
                        )
                    if not isinstance(response, dict) or "status" not in response:
                        raise ValueError("Backend returned an invalid result")
                except OperationCancelled:
                    response = self._cancelled(action["action"], mutation and dispatched)
                except Exception as exc:
                    response = self._failure(action["action"], exc, "unknown")
                    response["may_have_acted"] = mutation and dispatched
                response.setdefault(
                    "delivery",
                    "native" if native else "read_only" if not mutation else "accessibility",
                )
                response.setdefault("fallbacks", [])
                response["call_id"] = operation.call_id
                may_have_acted |= response.get(
                    "may_have_acted",
                    mutation and response["status"] in {"invoked", "verified", "ok", "partial"},
                )
                entry = {
                    "index": index,
                    "started_at": began,
                    "finished_at": time.monotonic(),
                    "result": response,
                }
                results.append(entry)
                cancelled = (
                    operation.event.is_set()
                    or response.get("cancelled", False)
                    or response.get("error", {}).get("code") == "CANCELLED"
                )
                if cancelled:
                    response["cancelled"] = True
                    break
                if response.get("stop_script"):
                    break
                if stop_on_error and response["status"] in STOP_STATUSES:
                    break
        finally:
            if native:
                try:
                    cleanup = self._input().release_all()
                except Exception as exc:
                    cleanup = self._failure("release_all", exc, "unknown")
                finally:
                    self._input().set_cancel_event(None)
        failed = [entry for entry in results if entry["result"]["status"] in STOP_STATUSES]
        if failed:
            status = "partial" if len(results) > len(failed) else failed[0]["result"]["status"]
        else:
            status = "ok"
        if cleanup and cleanup.get("status") in STOP_STATUSES:
            status = "partial" if results else cleanup["status"]
        output = result(
            status,
            "act",
            method="native" if native else "uia",
            data={
                "results": results,
                "requested": len(actions),
                "attempted": len(results),
                "completed": len(results) - len(failed),
                "stopped_early": len(results) < len(actions),
                "cleanup": cleanup,
            },
        )
        if cancelled or operation.event.is_set():
            output.update(self._cancelled("act", may_have_acted))
        output["may_have_acted"] = may_have_acted
        return output

    def act(
        self,
        actions,
        target=None,
        native=False,
        stop_on_error=True,
        timeout=5.0,
        *,
        max_output_chars=24000,
        max_output_bytes=65536,
        coordinate_space="screen",
        capture_id=None,
        screenshot="auto",
        settle_ms=80,
        focus=None,
        key_method="scancode",
        scope="default",
        _request_cancel=None,
    ):
        def execute(operation):
            try:
                validate_budget(max_output_chars, max_output_bytes)
                if key_method not in {"auto", "scancode", "message"}:
                    raise ValueError("key_method must be auto, scancode, or message")
                if type(screenshot) is not bool and (
                    not isinstance(screenshot, str) or screenshot not in {"auto", "text"}
                ):
                    raise ValueError("screenshot must be false, true, text, or auto")
                _number(settle_ms, "settle_ms", 0, 5000)
                plan = actions
                if focus is not None:
                    if not native or type(focus) not in (bool, str):
                        raise ValueError("focus requires explicit native=true")
                    if focus is not False:
                        selected = target if focus is True else focus
                        plan = [{"action": "force_focus", "target": _target(selected)}, *actions]
                prepared = self._validate_actions(plan, target, native, stop_on_error, timeout)
                prepared = self._map_actions(
                    prepared,
                    coordinate_space=coordinate_space,
                    capture_id=capture_id,
                    target=target,
                    scope=scope,
                    native=native,
                )
            except Exception as exc:
                return self._failure("act", exc)
            feedback_deadline = time.monotonic() + timeout
            response = self._run_actions(
                prepared, native, stop_on_error, timeout, operation=operation, key_method=key_method
            )
            if screenshot and not operation.event.is_set() and not response.get("cancelled"):
                try:
                    attempted = response["data"]["results"]
                    observed_target = target
                    for entry in reversed(attempted):
                        candidate = prepared[entry["index"]].get("target")
                        if candidate is not None:
                            observed_target = candidate
                            break
                    operation.wait(settle_ms / 1000)
                    remaining = feedback_deadline - time.monotonic()
                    if remaining <= 0:
                        raise TimeoutError("After-observation deadline elapsed")
                    if screenshot == "text" or screenshot == "auto" and not native:
                        if observed_target is None:
                            observed = result(
                                "ok", "after", method="batch_readback", data={"results": attempted}
                            )
                        else:
                            observed = self._window_read_text(
                                observed_target,
                                operation,
                                timeout=remaining,
                                compact=True,
                                depth=4,
                                max_nodes=40,
                                max_output_chars=4000,
                                max_output_bytes=12000,
                            )
                        observed = bound_result(observed, 4000, 12000)
                        response["data"]["after"] = observed
                    else:
                        frame = self._camera().screenshot(
                            target=observed_target or "desktop",
                            cancel_event=operation.event,
                            scope=scope,
                            timeout=remaining,
                            deadline=feedback_deadline,
                            max_dim=800,
                            format="jpeg",
                            quality=75,
                        )
                        if isinstance(frame, dict):
                            response["data"]["after"] = frame
                        else:
                            response["frame"] = frame
                            response["data"]["after"] = frame.metadata
                except Exception as exc:
                    response["data"]["after"] = self._failure("after", exc)
            return response

        return self._execute(
            "act",
            {"capture_artifacts": bool(screenshot)},
            execute,
            "native" if native else "accessibility",
            _request_cancel,
        )

    def screenshot(
        self,
        target="desktop",
        max_dim=800,
        format="jpeg",
        region=None,
        capture_mode="screen",
        allow_fallback=False,
        timeout=5.0,
        *,
        max_output_chars=24000,
        max_output_bytes=65536,
        scope="default",
        region_space="surface",
        save=False,
        define_viewport=None,
        foreground=False,
        quality=75,
        _request_cancel=None,
    ):
        def execute(operation):
            validate_budget(max_output_chars, max_output_bytes)
            return self._camera().screenshot(
                target=target,
                max_dim=max_dim,
                format=format,
                region=region,
                capture_mode=capture_mode,
                allow_fallback=allow_fallback,
                timeout=timeout,
                cancel_event=operation.event,
                scope=scope,
                region_space=region_space,
                save=save,
                define_viewport=define_viewport,
                foreground=foreground,
                quality=quality,
            )

        return self._execute(
            "screenshot", {"target": target}, execute, "read_only", _request_cancel
        )

    def record(
        self,
        target="desktop",
        seconds=3.0,
        fps=10,
        montage_frames=6,
        *,
        max_output_chars=24000,
        max_output_bytes=65536,
        region=None,
        region_space="surface",
        keep_frames=False,
        capture_mode="screen",
        scope="default",
        foreground=False,
        max_dim=800,
        format="jpeg",
        quality=75,
        _request_cancel=None,
    ):
        def execute(operation):
            validate_budget(max_output_chars, max_output_bytes)
            return self._camera().record(
                target=target,
                seconds=seconds,
                fps=fps,
                montage_frames=montage_frames,
                cancel_event=operation.event,
                region=region,
                region_space=region_space,
                keep_frames=keep_frames,
                capture_mode=capture_mode,
                scope=scope,
                foreground=foreground,
                max_dim=max_dim,
                format=format,
                quality=quality,
            )

        return self._execute("record", {"target": target}, execute, "read_only", _request_cancel)

    def play(
        self,
        actions=None,
        target=None,
        native=False,
        seconds=3.0,
        fps=10,
        stop_on_error=True,
        timeout=5.0,
        *,
        max_output_chars=24000,
        max_output_bytes=65536,
        script=None,
        montage_frames=6,
        probe=None,
        coordinate_space="screen",
        capture_id=None,
        key_method="scancode",
        scope="default",
        allow_shell=False,
        max_dim=800,
        format="jpeg",
        quality=75,
        _request_cancel=None,
    ):
        def execute(operation):
            try:
                validate_budget(max_output_chars, max_output_bytes)
                if (actions is None) == (script is None):
                    raise ValueError("Provide exactly one of actions or script")
                if key_method not in {"auto", "scancode", "message"}:
                    raise ValueError("Invalid key_method")
                if type(max_dim) is not int or not 0 <= max_dim <= 4096:
                    raise ValueError("max_dim must be an integer in [0, 4096]")
                if not isinstance(format, str) or format.lower() not in {
                    "png",
                    "jpeg",
                    "jpg",
                    "webp",
                }:
                    raise ValueError("format must be png, jpeg, or webp")
                if (
                    format.lower() != "png"
                    and quality is not None
                    and (type(quality) is not int or not 1 <= quality <= 95)
                ):
                    raise ValueError("quality must be an integer in [1, 95]")
                if type(montage_frames) is not int or not 1 <= montage_frames <= 24:
                    raise ValueError("montage_frames must be 1..24")
                plan = actions
                if script is not None:
                    from .playscript import parse_script

                    plan = parse_script(script)
                needs_probe = any(item.get("action") in {"probe", "until"} for item in plan)
                probe_kwargs = (
                    self._probe_options(probe, allow_shell)
                    if needs_probe or probe is not None
                    else None
                )
                prepared = self._validate_actions(
                    plan,
                    target,
                    native,
                    stop_on_error,
                    timeout,
                    timed=script is None,
                    dynamic=script is not None,
                )
                prepared = self._map_actions(
                    prepared,
                    coordinate_space=coordinate_space,
                    capture_id=capture_id,
                    target=target,
                    scope=scope,
                    native=native,
                )
                _target(target)
                _number(seconds, "seconds", 0.1, 30)
                _number(fps, "fps", 1, 30)
                if any(item.get("at", 0) > seconds for item in prepared):
                    raise ValueError("Action timestamps must fit within recording duration")
                camera = self._camera()
            except Exception as exc:
                return self._failure("play", exc)
            captured = {}
            ready = threading.Event()
            stop_capture = threading.Event()
            extra_frames = []
            sample = None
            probe_deadline = time.monotonic() + seconds + timeout

            def dynamic(action):
                nonlocal sample
                name = action["action"]
                probe_acted = False
                if name == "shot":
                    frame = camera.screenshot(
                        target=target,
                        cancel_event=operation.event,
                        scope=scope,
                        remember=False,
                        deadline=probe_deadline,
                        max_dim=max_dim,
                        format=format,
                        quality=quality,
                    )
                    if isinstance(frame, dict):
                        return frame
                    extra_frames.append(frame)
                    return result("ok", "shot", data=frame.metadata)
                if name == "probe" or sample is None:
                    try:
                        observed = self._automation().process(
                            "shell",
                            allow_shell=allow_shell,
                            cancel_event=operation.event,
                            deadline=probe_deadline,
                            **probe_kwargs,
                        )
                    except Exception:
                        return result(
                            "unknown",
                            "probe",
                            code="PROBE_FAILED",
                            may_have_acted=True,
                            cancelled=operation.event.is_set(),
                            message="Probe failed; command diagnostics redacted",
                        )
                    if observed.get("status") not in {"ok", "invoked", "verified"}:
                        return result(
                            observed.get("status", "error"),
                            "probe",
                            code=observed.get("error", {}).get("code", "PROBE_FAILED"),
                            may_have_acted=observed.get("may_have_acted", True),
                            cancelled=observed.get("cancelled", False),
                            message="Probe failed; command diagnostics redacted",
                        )
                    probe_acted = True
                    data = observed.get("data", {})
                    output = data.get("stdout", "")
                    retained = len(output.encode("utf-8")) if isinstance(output, str) else 0
                    truncated = observed.get("truncated") or any(
                        value
                        for key, value in data.items()
                        if key == "truncated" or key.endswith("_truncated")
                    )
                    for key in ("stdout_bytes", "output_bytes", "total_bytes"):
                        count = data.get(key)
                        if type(count) is int and count > retained:
                            truncated = True
                    if truncated:
                        sample = None
                        return result(
                            "unknown",
                            "probe",
                            code="PROBE_OUTPUT_TRUNCATED",
                            stop_script=True,
                            may_have_acted=True,
                            cancelled=observed.get("cancelled", False),
                            message="Probe output incomplete; command diagnostics redacted",
                        )
                    try:
                        if not isinstance(output, str) or len(output.encode("utf-8")) > 65536:
                            raise ValueError("Invalid probe JSON")
                        sample = json.loads(output)
                        if type(sample) is not dict:
                            raise ValueError("Probe JSON must be an object")
                        from .playscript import evaluate_condition

                        evaluate_condition("True", sample)
                    except Exception:
                        sample = None
                        return result(
                            "error",
                            "probe",
                            code="INVALID_PROBE_JSON",
                            may_have_acted=True,
                            cancelled=observed.get("cancelled", False),
                            message="Probe output invalid; diagnostics redacted",
                        )
                    if observed.get("cancelled"):
                        return self._cancelled("probe", True)
                if name == "until":
                    from .playscript import evaluate_condition

                    try:
                        matched = evaluate_condition(action["condition"], sample)
                    except Exception:
                        return result(
                            "error",
                            "until",
                            code="INVALID_CONDITION",
                            may_have_acted=probe_acted,
                            message="Condition evaluation failed; diagnostics redacted",
                        )
                    return result(
                        "ok",
                        "until",
                        data={"matched": matched},
                        stop_script=matched,
                        may_have_acted=probe_acted,
                    )
                return result("ok", "probe", data={"sampled": True}, may_have_acted=True)

            def capture():
                captured["started_at"] = time.monotonic()
                ready.set()
                try:
                    record_kwargs = dict(
                        target=target,
                        seconds=seconds,
                        fps=fps,
                        montage_frames=montage_frames,
                        cancel_event=operation.event,
                        scope=scope,
                        deadline=probe_deadline,
                        max_dim=max_dim,
                        format=format,
                        quality=quality,
                    )
                    if script is not None:
                        record_kwargs.update(stop_event=stop_capture, extra_frames=extra_frames)
                    captured["frame"] = camera.record(**record_kwargs)
                except Exception as exc:
                    captured["error"] = self._failure("record", exc, "error")
                finally:
                    captured["finished_at"] = time.monotonic()

            thread = threading.Thread(target=capture, name="minano-play-capture")
            thread.start()
            try:
                ready.wait()
                batch = self._run_actions(
                    prepared,
                    native,
                    stop_on_error,
                    timeout,
                    start=captured["started_at"] if script is None else None,
                    operation=operation,
                    key_method=key_method,
                    dynamic=dynamic if script is not None else None,
                )
            finally:
                if script is not None:
                    stop_capture.set()
                # The capture contract bounds record; never return with a dangling capture thread.
                thread.join()
            frame = captured.pop("frame", None)
            status = batch["status"]
            if "error" in captured:
                status = "partial" if batch["data"]["completed"] else "error"
            output = result(
                status,
                "play",
                data={
                    "actions": batch,
                    "capture": captured,
                    "temporal_pairing": "independent timestamps; no strict action/frame pairing",
                },
                may_have_acted=batch.get("may_have_acted", False),
            )
            if batch.get("cancelled") or operation.event.is_set():
                output.update(self._cancelled("play", batch.get("may_have_acted", False)))
            if frame is not None:
                output["frame"] = frame
                output["data"]["capture"]["metadata"] = frame.metadata
            return output

        return self._execute(
            "play",
            {"target": target},
            execute,
            "native" if native else "accessibility",
            _request_cancel,
        )

    def _probe_options(self, probe, allow_shell):
        if not (self.allow_shell and allow_shell is True):
            raise ValueError("probe requires startup and per-call shell permission")
        if isinstance(probe, str):
            probe = {"command": probe}
        if not isinstance(probe, dict) or set(probe) - {"command", "shell_kind", "timeout"}:
            raise ValueError("probe requires command and optional shell_kind/timeout")
        if not isinstance(probe.get("command"), str) or not probe["command"].strip():
            raise ValueError("probe requires a nonempty command")
        _number(probe.get("timeout", 10), "probe timeout", 0.001, 120)
        if probe.get("shell_kind", "powershell") not in {"powershell", "cmd"}:
            raise ValueError("Invalid probe shell_kind")
        return dict(probe)

    def system(self, action="status", *, scope="default", _request_cancel=None, **kwargs):
        if action == "cancel":
            return result(
                "ok",
                "system.cancel",
                data=self.controller.cancel(),
                delivery="read_only",
                fallbacks=[],
            )
        if action == "status":
            return result(
                "ok",
                "system.status",
                delivery="read_only",
                fallbacks=[],
                data={
                    "platform": platform.system(),
                    "python": platform.python_version(),
                    "allow_native": self.allow_native,
                    "input_policy": "explicit_per_call",
                    "allow_shell": self.allow_shell,
                    "allow_process_control": self.allow_process_control,
                    "default_input": "strict_uia",
                    "uia_worker_started": self._broker is not None,
                    "provider_focus_side_effects_possible": True,
                    "operation": self.controller.snapshot(),
                    "trace_enabled": self._trace_enabled,
                },
            )

        def execute(operation):
            if action == "viewports":
                if kwargs:
                    raise ValueError("viewports accepts no additional arguments")
                return result(
                    "ok", "system.viewports", data=self._coordinates().list_viewports(scope=scope)
                )
            if action == "clear_viewport":
                if set(kwargs) not in ({"name"}, {"text"}):
                    raise ValueError("clear_viewport requires name or text, not both")
                viewport_name = kwargs.get("name", kwargs.get("text"))
                if not isinstance(viewport_name, str) or not viewport_name:
                    raise ValueError("clear_viewport requires a nonempty name")
                return result(
                    "ok",
                    "system.clear_viewport",
                    data=self._coordinates().clear_viewport(viewport_name, scope=scope),
                )
            try:
                return self._automation().system(
                    action, cancel_event=operation.event, deadline=time.monotonic() + 10, **kwargs
                )
            except Exception:
                if action in {"get_clipboard", "set_clipboard"}:
                    return result(
                        "unknown" if action == "set_clipboard" else "error",
                        f"system.{action}",
                        code="CLIPBOARD_FAILED",
                        may_have_acted=action == "set_clipboard",
                        message="Clipboard operation failed; diagnostics redacted",
                    )
                raise

        return self._execute(
            f"system.{action}",
            {},
            execute,
            delivery="native" if action == "set_clipboard" else "read_only",
            request_event=_request_cancel,
        )

    def process(
        self,
        action="list",
        argv=None,
        *,
        max_output_chars=24000,
        max_output_bytes=65536,
        _request_cancel=None,
        **kwargs,
    ):
        def execute(operation):
            validate_budget(max_output_chars, max_output_bytes)
            if action == "shell" and not (self.allow_shell and kwargs.get("allow_shell") is True):
                raise ValueError("shell requires startup and per-call permission")
            if action in {"kill", "terminate"} and not (
                self.allow_process_control and kwargs.get("allow_process_control") is True
            ):
                raise ValueError("process control requires startup and per-call permission")
            try:
                response = self._automation().process(
                    action,
                    argv,
                    cancel_event=operation.event,
                    deadline=time.monotonic() + kwargs.get("timeout", 10),
                    **kwargs,
                )
                if action == "shell" and response.get("status") in STOP_STATUSES:
                    response = dict(response)
                    response["error"] = {
                        "code": response.get("error", {}).get("code", "SHELL_FAILED"),
                        "message": "Shell operation failed; command diagnostics redacted",
                    }
                return response
            except Exception:
                if action == "shell":
                    return result(
                        "unknown",
                        "process.shell",
                        code="SHELL_FAILED",
                        may_have_acted=True,
                        message="Shell operation failed; command diagnostics redacted",
                    )
                raise

        return self._execute(
            f"process.{action}",
            {},
            execute,
            "native" if action in {"launch", "shell", "kill", "terminate"} else "read_only",
            _request_cancel,
        )

    def close(self):
        # Closing admissions and signalling never waits for the owner lock.
        self.controller.close()
        with self._lock:
            if self._closed:
                return
            self._closed = True
            # Capture/worker cleanup precedes trace/store close; the runtime lock is
            # held here, so an admitted owner has already joined its child threads.
            for resource in (self._native, self._capture, self._broker, self._services):
                if resource is not None:
                    try:
                        resource.close()
                    except Exception:
                        # Cleanup of one backend must not prevent reaping the worker.
                        pass
            for resource in (self._trace, self._artifact_store):
                if resource is not None:
                    try:
                        resource.close()
                    except Exception:
                        pass
