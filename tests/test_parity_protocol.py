"""Parity orchestration tests: all mutation backends are injected, never desktop input."""

import io
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import anyio
import pytest
from mcp.shared.memory import create_connected_server_and_client_session
from PIL import Image

from minano_computer_use.capture import CaptureFrame, CaptureService
from minano_computer_use.coordinates import CoordinateManager
from minano_computer_use.native import MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_LEFTUP, NativeInput
from minano_computer_use.runtime import Runtime
from minano_computer_use.server import create_server


class Native:
    def __init__(self):
        self.calls = []
        self.released = 0

    def set_cancel_event(self, event):
        self.event = event

    def execute(self, action, target, key_method="scancode"):
        self.calls.append((action, target, key_method))
        return {"status": "invoked", "operation": action["action"]}

    def release_all(self):
        self.released += 1
        return {"status": "ok"}

    def close(self):
        pass


def explicit_test_window(target):
    if target != "hwnd:42":
        raise ValueError("Test requires explicit HWND recipient")
    return {
        "hwnd": 42,
        "pid": 7,
        "process_creation_time": 99,
        "visible": True,
        "rect": [-100, 0, 1600, 1050],
        "dpi": 96,
    }


class Broker:
    def __init__(self, response=None):
        self.calls = []
        self.response = response or {"status": "ok", "data": {"nodes": []}}

    def call(self, request, timeout=None, cancel_event=None):
        self.calls.append(request)
        return dict(self.response)

    def close(self):
        pass


class Services:
    def __init__(self):
        self.calls = []

    def process(self, action, argv=None, **kwargs):
        self.calls.append((action, argv, kwargs))
        return {"status": "ok", "data": {"stdout": '{"done": true}'}}

    def system(self, action, **kwargs):
        self.calls.append((action, kwargs))
        return {"status": "ok", "data": {"injected": True}}

    def window(self, action, target, selector, **kwargs):
        self.calls.append((action, target, selector, kwargs))
        return {"status": "invoked"}

    def ocr(self, target, **kwargs):
        self.calls.append(("ocr", target, kwargs))
        return {"status": "ok", "method": "injected_ocr", "data": {"text": "OCR"}}

    def close(self):
        pass


class Camera:
    def __init__(self, coordinates=None):
        self.coordinates = coordinates
        self.calls = []
        self.stopped = threading.Event()

    def screenshot(self, **kwargs):
        self.calls.append(("screenshot", kwargs))
        stream = io.BytesIO()
        Image.new("RGB", (10, 10)).save(stream, format="PNG")
        metadata = {
            "output_size": [10, 10],
            "physical_transform": {"origin": [100, 200], "output_to_physical_scale": [2, 2]},
            "target": "desktop",
            "capture_surface": "screen_composite",
        }
        if self.coordinates and kwargs.get("remember", True):
            metadata["capture_id"] = self.coordinates.remember(metadata, kwargs["scope"])
        return CaptureFrame(stream.getvalue(), "image/png", metadata)

    def record(self, **kwargs):
        self.calls.append(("record", kwargs))
        if "stop_event" in kwargs:
            assert kwargs["stop_event"].wait(2)
        self.stopped.set()
        return self.screenshot(scope=kwargs.get("scope", "default"), remember=False)

    def close(self):
        pass


@pytest.mark.parametrize("compat", [False, True])
def test_protocol_per_call_native_and_aliases_without_startup_gate(compat, tmp_path):
    async def exercise():
        native = Native()
        runtime = Runtime(allow_native=compat, native=native, output_dir=tmp_path)
        async with create_connected_server_and_client_session(create_server(runtime)) as client:
            status = (await client.call_tool("system", {})).structuredContent["data"]
            assert status["input_policy"] == "explicit_per_call"
            assert status["default_input"] == "strict_uia"
            request = {
                "actions": [{"type": "mouse_move", "coordinates": [1, 2]}],
                "target": "fixture",
            }
            assert (await client.call_tool("act", {**(request), "screenshot": False})).isError
            assert not native.calls
            positive = await client.call_tool(
                "act",
                {**({**request, "native": True, "key_method": "message"}), "screenshot": False},
            )
            assert not positive.isError
            assert native.calls == [({"action": "move", "x": 1, "y": 2}, "fixture", "message")]
            assert native.released == 1

    anyio.run(exercise)


def test_seven_schemas_expose_parity_without_context_or_scope():
    async def exercise():
        runtime = Runtime(services=Services())
        async with create_connected_server_and_client_session(create_server(runtime)) as client:
            tools = {tool.name: tool.inputSchema for tool in (await client.list_tools()).tools}
            assert len(tools) == 7
            assert tools["act"]["properties"]["screenshot"]["default"] == "auto"
            for name in ("screenshot", "record", "play"):
                properties = tools[name]["properties"]
                assert properties["max_dim"]["default"] == 800
                assert properties["format"]["default"] == "jpeg"
                assert properties["quality"]["default"] == 75
            required = {
                "act": {"coordinate_space", "capture_id", "screenshot", "focus", "key_method"},
                "play": {"script", "probe", "allow_shell", "montage_frames"},
                "screenshot": {"region_space", "define_viewport", "save", "foreground"},
                "record": {"region", "keep_frames", "capture_mode"},
                "window": {"window", "role", "nth", "native", "allow_process_control"},
                "process": {"command", "shell_kind", "allow_shell", "allow_process_control", "pid"},
                "system": {"text", "name"},
            }
            for name, fields in required.items():
                assert fields <= tools[name]["properties"].keys()
                assert not {"ctx", "scope"} & tools[name]["properties"].keys()

    anyio.run(exercise)


@pytest.mark.parametrize("startup,call", [(False, False), (False, True), (True, False)])
def test_shell_and_control_dual_gates_before_service(startup, call):
    services = Services()
    runtime = Runtime(services=services, allow_shell=startup, allow_process_control=startup)
    assert runtime.process("shell", command="secret", allow_shell=call)["status"] == "refused"
    assert runtime.process("kill", pid=42, allow_process_control=call)["status"] == "refused"
    assert runtime.window("close", "fixture", allow_process_control=call)["status"] == "refused"
    assert not services.calls


@pytest.mark.parametrize(
    "response,ocr",
    [
        ({"status": "ok", "data": {"nodes": []}}, True),
        ({"status": "ok", "data": {"nodes": [{"text": " "}]}}, True),
        ({"status": "ok", "data": {"nodes": [{"text": "UIA"}]}}, False),
        ({"status": "ok", "data": {"nodes": [], "truncated": True}}, False),
        ({"status": "unknown", "error": {"code": "TIMEOUT"}}, False),
        ({"status": "refused", "error": {"code": "STALE_TOKEN"}}, False),
    ],
)
def test_get_text_ocr_only_successfully_empty(response, ocr):
    services, broker = Services(), Broker(response)
    runtime = Runtime(services=services, broker=broker)
    observed = runtime.window("get_text", window="fixture")
    assert bool(services.calls) is ocr
    assert broker.calls[0]["action"] == "text"
    if ocr:
        assert observed["method"] == "injected_ocr"


def test_window_aliases_use_strict_invoke_and_explicit_selection():
    broker = Broker()
    runtime = Runtime(broker=broker)
    assert (
        runtime.window("click_element", "fixture", "Button", role="Button", nth=0)["status"] == "ok"
    )
    assert broker.calls[-1]["action"] == "invoke"
    assert broker.calls[-1]["selector"] == {"name": "Button", "control_type": "Button", "index": 0}
    assert runtime.window("ui_tree", "fixture")["status"] == "ok"
    assert broker.calls[-1]["action"] == "tree"
    assert runtime.window("focus", "fixture")["status"] == "refused"


@pytest.mark.parametrize(
    "kwargs",
    [
        {},
        {"allow_shell": True},
        {"allow_shell": True, "probe": {"command": "secret", "bad": 1}},
        {"allow_shell": True, "probe": {"command": "secret", "shell_kind": "bad"}},
    ],
)
def test_dsl_probe_invalid_plan_never_starts_capture_or_input(kwargs, tmp_path):
    native, services, camera = Native(), Services(), Camera()
    runtime = Runtime(
        native=native, services=services, capture=camera, allow_shell=True, output_dir=tmp_path
    )
    response = runtime.play(target="fixture", native=True, script="tap A\nprobe", **kwargs)
    assert response["status"] == "refused"
    assert "secret" not in json.dumps(response)
    assert not native.calls and not services.calls and not camera.calls


def test_dsl_probe_until_shot_stop_record_and_redact_trace(tmp_path):
    native, services, camera = Native(), Services(), Camera()
    runtime = Runtime(
        native=native,
        services=services,
        capture=camera,
        allow_shell=True,
        trace_enabled=True,
        output_dir=tmp_path,
    )
    response = runtime.play(
        target="fixture",
        native=True,
        script="probe\nshot\nuntil done\ntap A",
        probe="private command",
        allow_shell=True,
        montage_frames=2,
    )
    assert response["status"] == "ok"
    assert response["data"]["actions"]["data"]["stopped_early"]
    assert len(services.calls) == 1 and not native.calls
    assert camera.stopped.is_set()
    record = next(kwargs for name, kwargs in camera.calls if name == "record")
    assert record["montage_frames"] == 2 and record["stop_event"].is_set()
    assert len(record["extra_frames"]) == 1
    runtime.close()
    for path in (tmp_path / "minano-artifacts").glob("*.jsonl"):
        assert "private command" not in path.read_text(encoding="utf-8")


def test_after_capture_failure_preserves_action_outcome(tmp_path):
    class Broken(Camera):
        def screenshot(self, **kwargs):
            raise OSError("injected capture IO failure")

    native = Native()
    response = Runtime(native=native, capture=Broken(), output_dir=tmp_path).act(
        [{"action": "key", "key": "A"}],
        "fixture",
        True,
        screenshot=True,
        settle_ms=0,
    )
    assert response["status"] == "ok" and response["may_have_acted"]
    assert response["data"]["after"]["status"] == "refused"
    assert native.released == 1


def test_coordinate_scope_latest_shared_across_protocol_calls(tmp_path):
    async def exercise():
        coordinates, native = CoordinateManager(resolver=explicit_test_window), Native()
        camera = Camera(coordinates)
        runtime = Runtime(
            native=native, capture=camera, coordinate_manager=coordinates, output_dir=tmp_path
        )
        async with create_connected_server_and_client_session(create_server(runtime)) as client:
            screenshot = await client.call_tool("screenshot", {})
            assert not screenshot.isError
            observed = await client.call_tool(
                "act",
                {
                    **(
                        {
                            "actions": [{"action": "click", "x": 1, "y": 2}],
                            "native": True,
                            "target": "hwnd:42",
                            "coordinate_space": "image",
                        }
                    ),
                    "screenshot": False,
                },
            )
            assert not observed.isError, observed
            assert native.calls[0][0]["x"] == 102 and native.calls[0][0]["y"] == 204

    anyio.run(exercise)


def test_protocol_client_cannot_reuse_another_sessions_capture(tmp_path):
    async def exercise():
        coordinates, native = CoordinateManager(), Native()
        camera = Camera(coordinates)
        first = Runtime(
            native=native, capture=camera, coordinate_manager=coordinates, output_dir=tmp_path
        )
        second = Runtime(
            native=native, capture=camera, coordinate_manager=coordinates, output_dir=tmp_path
        )
        async with create_connected_server_and_client_session(create_server(first)) as client:
            frame = (await client.call_tool("screenshot", {})).structuredContent["data"]
            async with create_connected_server_and_client_session(create_server(second)) as other:
                response = await other.call_tool(
                    "act",
                    {
                        **(
                            {
                                "actions": [{"action": "click", "x": 1, "y": 2}],
                                "native": True,
                                "target": "desktop",
                                "coordinate_space": "image",
                                "capture_id": frame["capture_id"],
                            }
                        ),
                        "screenshot": False,
                    },
                )
                assert response.isError
                assert not native.calls

    anyio.run(exercise)


def test_explicit_focus_queue_only_native_and_whole_batch_validated():
    native = Native()
    runtime = Runtime(native=native)
    actions = [{"action": "key", "key": "A"}]
    assert runtime.act(actions, "fixture", focus=True, screenshot=False)["status"] == "refused"
    assert not native.calls
    assert (
        runtime.act(actions, "fixture", native=True, focus=True, screenshot=False)["status"] == "ok"
    )
    assert [action["action"] for action, _, _ in native.calls] == ["force_focus", "key"]
    native.calls.clear()
    assert (
        runtime.act(
            [{"action": "key", "key": "bad key"}],
            "fixture",
            native=True,
            focus=True,
            screenshot=False,
        )["status"]
        == "refused"
    )
    assert not native.calls


def test_protocol_services_receive_options_and_close_default_kwargs_are_not_leaked():
    async def exercise():
        services = Services()
        runtime = Runtime(services=services, allow_shell=True, allow_process_control=True)
        async with create_connected_server_and_client_session(create_server(runtime)) as client:
            response = await client.call_tool(
                "process",
                {
                    "action": "shell",
                    "command": "injected only",
                    "allow_shell": True,
                    "shell_kind": "cmd",
                    "timeout": 1,
                },
            )
            assert not response.isError
            assert services.calls[-1][2]["command"] == "injected only"
            assert services.calls[-1][2]["cancel_event"] is not None
            assert services.calls[-1][2]["deadline"] is not None
            response = await client.call_tool(
                "window",
                {
                    "action": "close",
                    "target": "fixture",
                    "allow_process_control": True,
                },
            )
            assert not response.isError
            assert services.calls[-1][0] == "close"
            assert set(services.calls[-1][3]) == {
                "allow_process_control",
                "cancel_event",
                "deadline",
            }
            response = await client.call_tool("system", {"action": "cursor"})
            assert not response.isError
            assert services.calls[-1][0] == "cursor"

    anyio.run(exercise)


def test_viewports_and_clear_are_runtime_scoped():
    class Coordinates:
        def list_viewports(self, scope="default"):
            return [{"name": "fixture", "scope": scope}]

        def clear_viewport(self, name, scope="default"):
            return name == "fixture" and scope == "caller"

    runtime = Runtime(coordinate_manager=Coordinates())
    assert runtime.system("viewports", scope="caller")["data"] == [
        {"name": "fixture", "scope": "caller"},
    ]
    assert runtime.system("clear_viewport", name="fixture", scope="caller")["data"]


def test_legacy_window_query_targets_are_explicit_and_click_query_is_control_name():
    broker, native, services = Broker(), Native(), Services()
    runtime = Runtime(broker=broker, native=native, services=services, allow_process_control=True)
    assert runtime.window("ui_tree", query="Exact fixture")["status"] == "ok"
    assert broker.calls[-1]["target"] == "Exact fixture"
    assert (
        runtime.window("click_element", window="Exact fixture", query="Exact button")["status"]
        == "ok"
    )
    assert broker.calls[-1]["target"] == "Exact fixture"
    assert broker.calls[-1]["selector"] == "Exact button"
    assert runtime.window("focus", query="Exact fixture", native=True)["status"] == "ok"
    assert native.calls[-1][1] == "Exact fixture"
    assert (
        runtime.window("close", query="Exact fixture", allow_process_control=True)["status"]
        == "invoked"
    )
    assert services.calls[-1][1] == "Exact fixture"


def test_one_transport_scopes_opencode_conversations_without_model_scope(tmp_path):
    async def exercise():
        coordinates, native = CoordinateManager(resolver=explicit_test_window), Native()
        runtime = Runtime(
            native=native,
            capture=Camera(coordinates),
            coordinate_manager=coordinates,
            output_dir=tmp_path,
        )
        async with create_connected_server_and_client_session(create_server(runtime)) as client:
            owner = {"ai.opencode/sessionID": "conversation-a"}
            other = {"ai.opencode/sessionID": "conversation-b"}
            frame = (await client.call_tool("screenshot", {}, meta=owner)).structuredContent["data"]
            request = {
                "actions": [{"action": "click", "x": 1, "y": 2}],
                "native": True,
                "target": "hwnd:42",
                "coordinate_space": "image",
            }
            assert (
                await client.call_tool("act", {**(request), "screenshot": False}, meta=other)
            ).isError
            assert (
                await client.call_tool(
                    "act",
                    {**({**request, "capture_id": frame["capture_id"]}), "screenshot": False},
                    meta=other,
                )
            ).isError
            assert not native.calls
            assert not (
                await client.call_tool("act", {**(request), "screenshot": False}, meta=owner)
            ).isError
            assert len(native.calls) == 1

    anyio.run(exercise)


def test_dsl_cancel_stops_capture_without_nested_operation_deadlock(tmp_path):
    native, camera = Native(), Camera()
    runtime = Runtime(native=native, capture=camera, output_dir=tmp_path)
    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(runtime.play, target="fixture", native=True, script="wait 10s\ntap A")
        deadline = time.monotonic() + 2
        while not camera.calls:
            assert time.monotonic() < deadline
            time.sleep(0.001)
        assert runtime.system("cancel")["data"]["requested"]
        response = pending.result(2)
    assert response["cancelled"] and camera.stopped.is_set()
    assert runtime.controller.snapshot()["state"] == "idle"
    assert not native.calls


def test_service_exception_never_echoes_shell_or_clipboard_secrets():
    class Broken(Services):
        def process(self, action, argv=None, **kwargs):
            raise RuntimeError(kwargs["command"])

        def system(self, action, **kwargs):
            raise RuntimeError(kwargs["text"])

    runtime = Runtime(services=Broken(), allow_shell=True)
    shell = runtime.process("shell", command="private command", allow_shell=True)
    clipboard = runtime.system("set_clipboard", text="private clipboard")
    assert "private" not in json.dumps(shell)
    assert "private" not in json.dumps(clipboard)
    assert shell["may_have_acted"] and clipboard["may_have_acted"]


def test_image_mapping_checks_overridden_action_target_before_any_input():
    coordinates, native = CoordinateManager(), Native()
    coordinates.remember(
        {
            "target": "desktop",
            "output_size": [10, 10],
            "physical_transform": {"origin": [0, 0], "output_to_physical_scale": [1, 1]},
        }
    )
    response = Runtime(native=native, coordinate_manager=coordinates).act(
        [
            {"action": "click", "x": 1, "y": 1},
            {"action": "click", "x": 1, "y": 1, "target": "different window"},
        ],
        "desktop",
        native=True,
        coordinate_space="image",
        screenshot=False,
    )
    assert response["status"] == "refused"
    assert not native.calls


@pytest.mark.parametrize("script", ["probe\ntap A", "until done\ntap A"])
def test_probe_timeout_cancel_retains_uncertainty_and_never_replays(script, tmp_path):
    class TimedOut(Services):
        def process(self, action, argv=None, **kwargs):
            self.calls.append((action, argv, kwargs))
            return {
                "status": "unknown",
                "may_have_acted": True,
                "cancelled": True,
                "error": {"code": "CANCELLED", "message": "secret command diagnostics"},
            }

    services, native = TimedOut(), Native()
    runtime = Runtime(
        services=services, native=native, capture=Camera(), allow_shell=True, output_dir=tmp_path
    )
    response = runtime.play(
        target="fixture",
        native=True,
        script=script,
        probe="secret command",
        allow_shell=True,
        stop_on_error=False,
    )
    assert response["may_have_acted"] and response["cancelled"]
    assert response["status"] == "unknown"
    assert len(services.calls) == 1 and not native.calls
    assert "secret" not in json.dumps(
        {key: value for key, value in response.items() if key != "frame"}
    )
    step = response["data"]["actions"]["data"]["results"][0]["result"]
    assert step["may_have_acted"] and step["cancelled"]


def test_successful_probe_invalid_json_still_may_have_mutated(tmp_path):
    class InvalidJSON(Services):
        def process(self, action, argv=None, **kwargs):
            self.calls.append(action)
            return {"status": "ok", "data": {"stdout": "secret non-json"}}

    services = InvalidJSON()
    runtime = Runtime(services=services, capture=Camera(), allow_shell=True, output_dir=tmp_path)
    response = runtime.play(
        target="fixture", script="probe", probe="secret command", allow_shell=True
    )
    assert response["may_have_acted"]
    assert response["data"]["actions"]["data"]["results"][0]["result"]["may_have_acted"]
    assert services.calls == ["shell"]
    assert "secret" not in json.dumps(
        {key: value for key, value in response.items() if key != "frame"}
    )


@pytest.mark.parametrize(
    "marker",
    [
        {"action": "probe", "command": "bad"},
        {"action": "shot", "at": 1},
        {"action": "until", "condition": "__import__('os')"},
        {"action": "until", "condition": "True\ntap A"},
        {"action": "until"},
    ],
)
def test_dynamic_marker_revalidation_refuses_entire_plan(marker):
    runtime = Runtime()
    with pytest.raises(ValueError):
        runtime._validate_actions(
            [{"action": "wait", "seconds": 0}, marker], "fixture", False, True, 5, dynamic=True
        )


def test_clear_viewport_text_alias_and_conflict():
    class Coordinates:
        def clear_viewport(self, name, scope="default"):
            assert name == "fixture" and scope == "caller"
            return True

    runtime = Runtime(coordinate_manager=Coordinates())
    assert runtime.system("clear_viewport", text="fixture", scope="caller")["data"]
    assert runtime.system("clear_viewport", text="fixture", name="fixture")["status"] == "refused"


def test_window_list_details_enriches_inventory_monitor_foreground(monkeypatch):
    class Details(Services):
        def process(self, action, argv=None, **kwargs):
            return {"status": "ok", "data": [{"pid": 7, "name": "fixture.exe"}]}

        def system(self, action, **kwargs):
            return {
                "status": "ok",
                "data": [
                    {
                        "index": 0,
                        "rect": [0, 0, 100, 100],
                        "primary": True,
                        "dpi": [96, 96],
                        "scale": 1,
                    }
                ],
            }

    monkeypatch.setattr(
        "minano_computer_use.win32.list_windows",
        lambda: [
            {"hwnd": 42, "pid": 7, "title": "fixture", "rect": [0, 0, 80, 80]},
        ],
    )
    monkeypatch.setattr("minano_computer_use.win32.observations", lambda: {"foreground_hwnd": 42})
    runtime = Runtime(services=Details())
    plain = runtime.window("list")["data"][0]
    assert "process" not in plain
    detailed = runtime.window("list", details=True)["data"][0]
    assert detailed["process"]["name"] == "fixture.exe"
    assert detailed["foreground"] is True
    assert detailed["monitor"]["index"] == 0


def test_protocol_legacy_query_and_clear_viewport_text_alias():
    class Coordinates:
        def clear_viewport(self, name, scope="default"):
            assert name == "fixture" and scope != "default"
            return True

    async def exercise():
        native, services, broker = Native(), Services(), Broker()
        runtime = Runtime(
            native=native,
            services=services,
            broker=broker,
            coordinate_manager=Coordinates(),
            allow_process_control=True,
        )
        async with create_connected_server_and_client_session(create_server(runtime)) as client:
            assert not (
                await client.call_tool(
                    "system",
                    {
                        "action": "clear_viewport",
                        "text": "fixture",
                    },
                )
            ).isError
            assert (
                await client.call_tool(
                    "system",
                    {
                        "action": "clear_viewport",
                        "text": "fixture",
                        "name": "fixture",
                    },
                )
            ).isError
            assert not (
                await client.call_tool(
                    "window",
                    {
                        "action": "ui_tree",
                        "query": "Exact title",
                    },
                )
            ).isError
            assert broker.calls[-1]["target"] == "Exact title"
            assert not (
                await client.call_tool(
                    "window",
                    {
                        "action": "click_element",
                        "window": "Exact title",
                        "query": "Exact control",
                    },
                )
            ).isError
            assert broker.calls[-1]["selector"] == "Exact control"
            assert not (
                await client.call_tool(
                    "window",
                    {
                        "action": "close",
                        "query": "Exact title",
                        "allow_process_control": True,
                    },
                )
            ).isError
            assert services.calls[-1][1] == "Exact title"

    anyio.run(exercise)


@pytest.mark.parametrize("tool", ["act", "play"])
@pytest.mark.parametrize("space", ["screen", "image"])
@pytest.mark.parametrize("action_target", [False, True])
def test_window_viewport_screenshot_to_native_actions(
    tool, space, action_target, tmp_path, monkeypatch
):
    window = {
        "hwnd": 42,
        "pid": 7,
        "rect": [100, 200, 200, 300],
        "process_creation_time": 99,
        "dpi": 96,
        "visible": True,
    }

    def resolve(target):
        assert target == "hwnd:42"
        return dict(window)

    class Grabber:
        monitors = [{"left": 0, "top": 0, "width": 500, "height": 500}]

        def grab(self, rect):
            return Image.new("RGB", (rect["width"], rect["height"]))

    monkeypatch.setattr("minano_computer_use.coordinates.window_snapshot", resolve)
    coordinates, native = CoordinateManager(resolver=resolve), Native()
    camera = CaptureService(tmp_path, grabber=Grabber(), coordinate_manager=coordinates)
    # This regression checks viewport/input routing, not video encoding.
    monkeypatch.setattr(camera, "_write_video", lambda *args, **kwargs: None)
    runtime = Runtime(
        native=native, capture=camera, coordinate_manager=coordinates, output_dir=tmp_path
    )
    assert runtime.screenshot("hwnd:42", define_viewport="fixture")["status"] == "ok"
    captured = runtime.screenshot("viewport:fixture")
    assert captured["status"] == "ok"
    capture_id = captured["data"]["capture_id"]
    kwargs = {
        "target": "viewport:fixture",
        "native": True,
        "coordinate_space": space,
        "capture_id": capture_id,
    }
    if tool == "play":
        kwargs["seconds"] = 0.1
    action = {"action": "click", "x": 1, "y": 2}
    if action_target:
        action["target"] = "viewport:fixture"
        kwargs["target"] = "hwnd:42"
    response = getattr(runtime, tool)([action], **kwargs)
    assert response["status"] == "ok", response
    assert native.calls[0][1] == "hwnd:42"
    assert native.calls[0][0]["x"] == (101 if space == "image" else 1)
    assert native.calls[0][0]["y"] == (202 if space == "image" else 2)
    runtime.close()


def test_screen_viewport_never_guesses_native_recipient():
    coordinates, native = CoordinateManager(), Native()
    coordinates.define_viewport(
        "screen",
        {
            "target": "desktop",
            "output_size": [10, 10],
            "physical_transform": {"origin": [0, 0], "output_to_physical_scale": [1, 1]},
        },
    )
    runtime = Runtime(native=native, coordinate_manager=coordinates)
    action = {"action": "click", "x": 1, "y": 2, "target": "viewport:screen"}
    assert (
        runtime.act([action], "viewport:screen", native=True, screenshot=False)["status"]
        == "refused"
    )
    assert not native.calls
    assert runtime.act([action], "hwnd:42", native=True, screenshot=False)["status"] == "ok"
    assert native.calls[-1][1] == "hwnd:42"


def test_exact_legacy_wait_duration_batch_and_conflict_prevalidation():
    broker = Broker()
    runtime = Runtime(broker=broker)
    response = runtime.act(
        [
            {"action": "wait", "duration": 0.001},
            {"action": "invoke"},
        ],
        target="fixture",
        screenshot=False,
    )
    assert response["status"] == "ok"
    assert response["data"]["results"][0]["result"]["data"]["seconds"] == 0.001
    broker.calls.clear()
    for invalid in (
        {"action": "wait", "duration": 0.001, "seconds": 0.001},
        {"action": "wait", "duration": float("nan")},
        {"action": "wait", "duration": 0.001, "native": True},
    ):
        assert (
            runtime.act([{"action": "invoke"}, invalid], "fixture", screenshot=False)["status"]
            == "refused"
        )
    assert not broker.calls


def test_integrated_capture_record_mapping_preserves_conversation_scope(tmp_path, monkeypatch):
    class Grabber:
        monitors = [{"left": -100, "top": 50, "width": 100, "height": 100}]

        def grab(self, rect):
            return Image.new("RGB", (rect["width"], rect["height"]))

    async def exercise():
        coordinates, native = CoordinateManager(resolver=explicit_test_window), Native()
        camera = CaptureService(
            tmp_path,
            grabber=Grabber(),
            coordinate_manager=coordinates,
        )
        # Preserve this test's deliberately unavailable video-backend path.
        monkeypatch.setattr(camera, "_write_video", lambda *args, **kwargs: None)
        runtime = Runtime(
            capture=camera, native=native, coordinate_manager=coordinates, output_dir=tmp_path
        )
        async with create_connected_server_and_client_session(create_server(runtime)) as client:
            owner = {"ai.opencode/sessionID": "owner"}
            other = {"ai.opencode/sessionID": "other"}
            frame = await client.call_tool("screenshot", {"max_dim": 50}, meta=owner)
            capture_id = frame.structuredContent["data"]["capture_id"]
            assert not (await client.call_tool("record", {"seconds": 0.05}, meta=owner)).isError
            request = {
                "target": "hwnd:42",
                "native": True,
                "coordinate_space": "image",
                "capture_id": capture_id,
                "actions": [{"action": "click", "x": 2, "y": 3}],
            }
            assert (
                await client.call_tool("act", {**(request), "screenshot": False}, meta=other)
            ).isError
            assert not native.calls
            assert not (
                await client.call_tool("act", {**(request), "screenshot": False}, meta=owner)
            ).isError
            assert native.calls[-1][0]["x"] == -96
            assert native.calls[-1][0]["y"] == 56

    anyio.run(exercise)


def test_capture_preview_knobs_default_and_explicit_forwarding(tmp_path):
    camera = Camera()
    runtime = Runtime(capture=camera, output_dir=tmp_path)
    assert runtime.screenshot()["status"] == "ok"
    shot = camera.calls[-1][1]
    assert (shot["max_dim"], shot["format"], shot["quality"]) == (800, "jpeg", 75)
    assert runtime.record(max_dim=0, format="png", quality=95)["status"] == "ok"
    recorded = next(kwargs for name, kwargs in reversed(camera.calls) if name == "record")
    assert (recorded["max_dim"], recorded["format"], recorded["quality"]) == (0, "png", 95)
    camera.calls.clear()
    assert (
        runtime.play(target="fixture", script="shot", max_dim=320, format="jpeg", quality=70)[
            "status"
        ]
        == "ok"
    )
    recorded = next(kwargs for name, kwargs in camera.calls if name == "record")
    shot = next(
        kwargs for name, kwargs in camera.calls if name == "screenshot" and "max_dim" in kwargs
    )
    for kwargs in (recorded, shot):
        assert (kwargs["max_dim"], kwargs["format"], kwargs["quality"]) == (320, "jpeg", 70)


def test_native_auto_feedback_real_capture_jpeg_and_fresh_scoped_context(tmp_path, monkeypatch):
    class Grabber:
        monitors = [{"left": 0, "top": 0, "width": 1600, "height": 1000}]

        def grab(self, rect):
            return Image.new("RGB", (rect["width"], rect["height"]))

    async def exercise():
        monkeypatch.setattr("minano_computer_use.coordinates.window_snapshot", explicit_test_window)
        coordinates, native = CoordinateManager(resolver=explicit_test_window), Native()
        camera = CaptureService(tmp_path, grabber=Grabber(), coordinate_manager=coordinates)
        runtime = Runtime(
            native=native, capture=camera, coordinate_manager=coordinates, output_dir=tmp_path
        )
        async with create_connected_server_and_client_session(create_server(runtime)) as client:
            meta = {"ai.opencode/sessionID": "feedback-owner"}
            before = await client.call_tool("screenshot", {}, meta=meta)
            response = await client.call_tool(
                "act",
                {
                    "actions": [{"action": "move", "x": 1, "y": 2}],
                    "native": True,
                    "target": "hwnd:42",
                    "settle_ms": 0,
                },
                meta=meta,
            )
            assert not response.isError
            after = response.structuredContent["data"]["after"]
            assert after["capture_id"] != before.structuredContent["data"]["capture_id"]
            assert after["output_size"] == [800, 500]
            images = [item for item in response.content if item.type == "image"]
            assert len(images) == 1 and images[0].mimeType == "image/jpeg"
            assert not (
                await client.call_tool(
                    "act",
                    {
                        "actions": [{"action": "click", "x": 2, "y": 3}],
                        "target": "hwnd:42",
                        "native": True,
                        "coordinate_space": "image",
                        "screenshot": False,
                    },
                    meta=meta,
                )
            ).isError
            assert native.calls[-1][0]["x"] == 4 and native.calls[-1][0]["y"] == 6

    anyio.run(exercise)


def test_uia_auto_feedback_bounded_compact_text_and_final_attempted_target(tmp_path):
    class TextBroker(Broker):
        def call(self, request, timeout=None, cancel_event=None):
            self.calls.append(request)
            if request["action"] == "text":
                return {"status": "ok", "data": {"nodes": [{"text": "readback" * 4000}]}}
            return {"status": "invoked"}

    broker, camera = TextBroker(), Camera()
    response = Runtime(broker=broker, capture=camera, output_dir=tmp_path).act(
        [
            {"action": "invoke"},
            {"action": "invoke", "target": "last exact target"},
        ],
        "outer exact target",
        settle_ms=0,
    )
    assert response["status"] == "ok" and response["may_have_acted"]
    assert broker.calls[-1]["target"] == "last exact target"
    assert broker.calls[-1]["compact"] and broker.calls[-1]["max_nodes"] == 40
    assert len(json.dumps(response["data"]["after"], ensure_ascii=False)) <= 4000
    assert "frame" not in response and not camera.calls


@pytest.mark.parametrize("cancelled", [False, True])
def test_auto_feedback_failure_never_overwrites_unknown_or_cancellation(cancelled, tmp_path):
    class Unknown(Native):
        def execute(self, action, target, key_method="scancode"):
            self.calls.append((action, target, key_method))
            return {"status": "unknown", "may_have_acted": True, "cancelled": cancelled}

    class Broken(Camera):
        def screenshot(self, **kwargs):
            self.calls.append(("screenshot", kwargs))
            raise OSError("capture failed")

    native, camera = Unknown(), Broken()
    response = Runtime(native=native, capture=camera, output_dir=tmp_path).act(
        [{"action": "move", "x": 1, "y": 2, "target": "last exact target"}],
        "outer target",
        native=True,
        settle_ms=0,
    )
    assert response["status"] == "unknown" and response["may_have_acted"]
    if cancelled:
        assert response["cancelled"] and not camera.calls
        assert "after" not in response["data"]
    else:
        assert response["data"]["after"]["status"] == "refused"
        assert camera.calls[-1][1]["target"] == "last exact target"


def test_feedback_false_disables_and_invalid_mode_refuses_before_input(tmp_path):
    native, camera = Native(), Camera()
    runtime = Runtime(native=native, capture=camera, output_dir=tmp_path)
    actions = [{"action": "move", "x": 1, "y": 2}]
    assert runtime.act(actions, "fixture", native=True, screenshot=False)["status"] == "ok"
    assert not camera.calls
    native.calls.clear()
    assert runtime.act(actions, "fixture", native=True, screenshot="invalid")["status"] == "refused"
    assert not native.calls


def test_protocol_uia_auto_text_is_bounded_without_image(tmp_path):
    class Readback(Broker):
        def call(self, request, timeout=None, cancel_event=None):
            self.calls.append(request)
            return {"status": "ok", "data": {"nodes": [{"text": "readback" * 5000}]}}

    async def exercise():
        broker, camera = Readback(), Camera()
        runtime = Runtime(broker=broker, capture=camera, output_dir=tmp_path)
        async with create_connected_server_and_client_session(create_server(runtime)) as client:
            response = await client.call_tool(
                "act",
                {
                    "actions": [{"action": "invoke"}],
                    "target": "fixture",
                    "settle_ms": 0,
                },
            )
            assert not response.isError
            assert not any(item.type == "image" for item in response.content)
            assert len(json.dumps(response.structuredContent["data"]["after"])) <= 4000
            assert broker.calls[-1]["compact"] and not camera.calls

    anyio.run(exercise)


def test_auto_targetless_list_reuses_results_without_capture_or_uia(monkeypatch, tmp_path):
    monkeypatch.setattr("minano_computer_use.win32.list_windows", lambda: [{"hwnd": 42}])
    broker, camera = Broker(), Camera()
    response = Runtime(broker=broker, capture=camera, output_dir=tmp_path).act(
        [{"action": "list"}],
        settle_ms=0,
    )
    assert response["status"] == "ok"
    assert response["data"]["after"]["method"] == "batch_readback"
    assert not broker.calls and not camera.calls


@pytest.mark.parametrize(
    "flags",
    [
        {"stdout_truncated": True},
        {"truncated": True},
        {"stderr_truncated": True},
        {"stdout_bytes": 9000, "stdout_retained_bytes": 9000},
    ],
)
def test_probe_json_prefix_never_used_as_complete_sample(flags, tmp_path):
    class Prefix(Services):
        def process(self, action, argv=None, **kwargs):
            self.calls.append(action)
            return {"status": "ok", "data": {"stdout": '{"done":true}', **flags}}

    services, native = Prefix(), Native()
    response = Runtime(
        services=services, native=native, capture=Camera(), allow_shell=True, output_dir=tmp_path
    ).play(
        target="fixture",
        native=True,
        script="probe\nuntil done\ntap A",
        probe="private command",
        allow_shell=True,
        stop_on_error=False,
    )
    assert response["status"] == "unknown" and response["may_have_acted"]
    steps = response["data"]["actions"]["data"]["results"]
    assert len(steps) == 1
    assert steps[0]["result"]["error"]["code"] == "PROBE_OUTPUT_TRUNCATED"
    assert services.calls == ["shell"] and not native.calls


@pytest.mark.parametrize("surface", ["desktop", "display:0", "viewport:screen"])
def test_screen_capture_to_real_native_fake_adapter(surface, tmp_path, monkeypatch):
    window = {
        "hwnd": 42,
        "pid": 7,
        "rect": [0, 0, 400, 300],
        "visible": True,
        "process_creation_time": 99,
        "dpi": 96,
    }

    def resolve(target):
        assert target == "hwnd:42"
        return dict(window)

    class Adapter:
        def __init__(self):
            self.events = []
            self.cursor = None

        def resolve_window(self, target):
            return resolve(target)

        def foreground(self):
            return 42

        def window_pid(self, hwnd):
            assert hwnd == 42
            return 7

        def send(self, events):
            self.events.extend(events)
            return len(events)

        def set_cursor(self, x, y):
            self.cursor = (x, y)
            return True

        def point_hits(self, x, y, hwnd):
            return hwnd == 42 and 0 <= x < 400 and 0 <= y < 300

        def virtual_bounds(self):
            return 0, 0, 400, 300

    class Grabber:
        monitors = [
            {"left": 0, "top": 0, "width": 400, "height": 300},
            {"left": 0, "top": 0, "width": 400, "height": 300},
        ]

        def grab(self, rect):
            return Image.new("RGB", (rect["width"], rect["height"]))

    monkeypatch.setattr("minano_computer_use.coordinates.window_snapshot", resolve)
    coordinates, adapter = CoordinateManager(resolver=resolve), Adapter()
    camera = CaptureService(tmp_path, grabber=Grabber(), coordinate_manager=coordinates)
    runtime = Runtime(
        native=NativeInput(enabled=True, adapter=adapter),
        capture=camera,
        coordinate_manager=coordinates,
        output_dir=tmp_path,
    )
    assert runtime.screenshot("desktop", define_viewport="screen")["status"] == "ok"
    assert runtime.screenshot(surface, max_dim=200)["status"] == "ok"
    action = {"action": "click", "x": 10, "y": 20}
    if surface.startswith("viewport:"):
        action["target"] = surface
    response = runtime.act([action], "hwnd:42", native=True, coordinate_space="image", settle_ms=0)
    assert response["status"] == "ok", response
    assert adapter.cursor == (20, 40)
    assert [event.mi.dwFlags for event in adapter.events] == [
        MOUSEEVENTF_LEFTDOWN,
        MOUSEEVENTF_LEFTUP,
    ]
    assert response["data"]["after"]["hwnd"] == 42
    runtime.close()


def test_paste_nul_later_batch_refused_before_first_native_effect(tmp_path):
    native = Native()
    response = Runtime(native=native, output_dir=tmp_path).act(
        [
            {"action": "key", "key": "A"},
            {"action": "paste", "text": "before\0after"},
        ],
        "hwnd:42",
        native=True,
        screenshot=False,
    )
    assert response["status"] == "refused"
    assert not native.calls
