"""0.4 orchestration checks: synthetic capture and injected effect backends only."""

import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import anyio
import pytest
from mcp.shared.memory import create_connected_server_and_client_session
from mcp.types import ImageContent
from PIL import Image

from minano_computer_use import win32
from minano_computer_use.capture import CaptureService
from minano_computer_use.coordinates import CoordinateManager
from minano_computer_use.message_backend import BackgroundInput
from minano_computer_use.native import NativeInput
from minano_computer_use.runtime import Runtime
from minano_computer_use.server import create_server

BINDING = {"hwnd": 123, "pid": 456, "process_creation_time": 789}


@pytest.mark.parametrize(
    "nodes,ocr",
    [
        ([{"text_redacted": True}], False),
        ([{"value_redacted": True, "text": ""}], False),
        ([{"text": "", "is_password": None}], False),
        ([{"text": "", "is_password": True}], False),
        ([{"text": "", "privacy_unknown": True}], False),
        ([{"text": "", "password_unknown": True}], False),
        ([{"text": "", "unavailable": {"text": {"status": "unavailable"}}}], False),
        ([{"text": "", "unavailable": {"is_password": {"status": "unavailable"}}}], False),
        ([{"text": "", "text_truncated": True}], False),
        ([{}], False),
        ([{"text": ""}, {"text_redacted": True}], False),
        ([{"text": " ", "is_password": False}], True),
        ([{"text": " "}], True),
        ([], True),
    ],
)
def test_get_text_does_not_ocr_redacted_or_unproven_empty_nodes(nodes, ocr):
    original = {"status": "ok", "data": {"nodes": nodes}}
    calls = []

    class Broker:
        def call(self, request, timeout, cancel_event):
            return original

    class Services:
        def ocr(self, *args, **kwargs):
            calls.append((args, kwargs))
            return {"status": "ok", "method": "fake_ocr"}

    response = Runtime(broker=Broker(), services=Services()).window("get_text", "fixture")
    assert bool(calls) is ocr
    if not ocr:
        assert response["data"] == original["data"]


@pytest.mark.parametrize("password", [True, None])
def test_actual_uia_redaction_never_triggers_runtime_ocr(password):
    from test_uia_backend import Control, backend

    control = Control("private password")
    control.IsPassword = password
    service = backend(control)

    class Broker:
        def call(self, request, timeout, cancel_event):
            return service.execute(request)

    class Services:
        def ocr(self, *args, **kwargs):
            pytest.fail("Redacted UIA text must never trigger OCR")

    try:
        response = Runtime(broker=Broker(), services=Services()).window("get_text", "App")
        assert response["status"] == "ok"
        assert response["data"]["nodes"][0]["text_redacted"]
    finally:
        service.close()


@pytest.fixture
def identity(monkeypatch):
    resolved = []

    def resolve(target):
        resolved.append(target)
        return {**BINDING, "title": "fixture"}

    monkeypatch.setattr(win32, "resolve_window", resolve)
    monkeypatch.setattr(win32, "window_identity", lambda hwnd, pid: dict(BINDING))
    monkeypatch.setattr(win32, "observations", lambda: {"foreground_hwnd": 999, "cursor": [1, 2]})
    return resolved


class SnapshotBroker:
    def __init__(self, fail=False):
        self.calls = []
        self.fail = fail

    def call(self, request, timeout, cancel_event):
        self.calls.append((request, timeout, cancel_event))
        if self.fail:
            return {"status": "error", "error": {"code": "PROVIDER_FAILURE"}}
        return {
            "status": "ok",
            "target_binding": dict(BINDING),
            "data": {
                "nodes": [{"is_password": True, "value": None}],
                "focus": {"status": "unavailable"},
                "selection": {"status": "redacted"},
                "root_binding": dict(BINDING),
                "window": {"title": "fixture"},
                "consistency": "non_atomic",
            },
        }

    def close(self):
        pass


class Message:
    def __init__(self):
        self.calls = []
        self.released = 0
        self.event = None
        self.closed = False

    def set_cancel_event(self, event):
        self.event = event

    def execute(self, action, target, timeout=5):
        self.calls.append((action, target, timeout))
        return {
            "status": "invoked",
            "target_binding": dict(BINDING),
            "consumption_verified": False,
            "delivery": "message",
        }

    def release_all(self):
        self.released += 1
        return {"status": "ok"}

    def close(self):
        self.closed = True


class SyntheticCapture(CaptureService):
    def __init__(self, fail=False):
        self.calls = []
        self.fail = fail
        manager = CoordinateManager(resolver=lambda target: self.snapshot())
        super().__init__(grabber=self, coordinate_manager=manager)

    @staticmethod
    def snapshot():
        return {**BINDING, "rect": [0, 0, 40, 20], "window_rect": [0, 0, 40, 20], "dpi": 96}

    def _target_rect(self, target, adapter):
        assert target == "hwnd:123"
        return (0, 0, 40, 20, {**self.snapshot(), "target": target, "origin": [0, 0]})

    def _revalidate(self, window):
        pass

    def grab(self, rect):
        if self.fail:
            raise RuntimeError("synthetic capture failed")
        return Image.new("RGB", (40, 20), (20, 100, 60))

    def screenshot(self, **kwargs):
        self.calls.append(kwargs)
        return super().screenshot(**kwargs)

    def close(self):
        self._closed = True


@pytest.mark.parametrize(
    "options",
    [
        {"native": True, "input_mode": "message"},
        {"native": True, "input_mode": "uia"},
        {"input_mode": "message", "key_method": "message"},
        {"input_mode": "invalid"},
    ],
)
def test_conflicting_modes_reject_before_backend(options):
    backend = Message()
    response = Runtime(message=backend).act(
        [{"action": "key", "key": "A"}], "fixture", screenshot=False, **options
    )
    assert response["status"] == "refused"
    assert not backend.calls and not backend.released


@pytest.mark.parametrize(
    "invalid",
    [
        {"action": "look", "dx": 1, "dy": 2},
        {"action": "paste", "text": "secret"},
        {"action": "force_focus"},
        {"action": "key", "key": "A", "input_mode": "native"},
        {"action": "set_value", "value": 3},
        {"action": "text", "text": "bad\0text"},
    ],
)
def test_message_whole_plan_preflight(invalid):
    backend = Message()
    response = Runtime(message=backend).act(
        [{"action": "key", "key": "A"}, invalid], "fixture", input_mode="message", screenshot=False
    )
    assert response["status"] == "refused"
    assert not backend.calls and not backend.released


def test_message_mode_single_opt_in_and_set_value_cleanup():
    backend = Message()
    runtime = Runtime(message=backend)
    response = runtime.act(
        [{"action": "set_value", "value": "private"}],
        "fixture",
        input_mode="message",
        screenshot=False,
        timeout=0.3,
    )
    assert response["status"] == "ok" and response["delivery"] == "message"
    assert backend.calls[0][0]["value"] == "private"
    assert 0 < backend.calls[0][2] <= 0.3
    assert backend.released == 1 and backend.event is None
    assert runtime._native is None and runtime._broker is None
    runtime.close()
    assert backend.closed


def test_production_message_backend_fake_adapter_and_native_foreground_denial():
    class Adapter:
        def __init__(self):
            self.posts = []

        def resolve_window(self, target):
            return dict(BINDING)

        def identity(self, hwnd, pid):
            return dict(BINDING)

        def observations(self):
            return {"foreground_hwnd": 999, "cursor": [7, 8]}

        def foreground(self):
            return 999

        def is_window(self, hwnd):
            return hwnd == BINDING["hwnd"]

        def window_pid(self, hwnd):
            return BINDING["pid"]

        def thread_focus(self, hwnd):
            return hwnd

        def map_key(self, key):
            return 30

        def dispatch_message(self, *args):
            self.posts.append(args)
            return True

    adapter = Adapter()
    runtime = Runtime(
        message=BackgroundInput(adapter), native=NativeInput(enabled=True, adapter=adapter)
    )
    request = [{"action": "key_down", "key": "A"}]
    message = runtime.act(request, "fixture", input_mode="message", screenshot=False)
    assert message["status"] == "ok" and adapter.posts
    observed = message["data"]["results"][0]["result"]
    assert observed["target_binding"] == BINDING and not observed["consumption_verified"]
    assert not observed["foreground_changed"] and not observed["cursor_changed"]
    posts = list(adapter.posts)
    for key_method in ("scancode", "message"):
        denied = runtime.act(
            request, "fixture", input_mode="native", key_method=key_method, screenshot=False
        )
        assert denied["status"] == "refused"
        assert adapter.posts == posts


def test_explicit_native_alone_and_message_compatible_play_script(identity, tmp_path):
    class Native(Message):
        def execute(self, action, target, key_method="scancode"):
            self.calls.append((action, target, key_method))
            return {"status": "invoked"}

    class Camera(SyntheticCapture):
        def record(self, **kwargs):
            return self.screenshot(target="hwnd:123", max_dim=800, format="jpeg")

    backend, native, camera = Message(), Native(), Camera()
    runtime = Runtime(message=backend, native=native, capture=camera, output_dir=tmp_path)
    response = runtime.act(
        [{"action": "key", "key": "A"}], "fixture", input_mode="native", screenshot=False
    )
    assert response["status"] == "ok" and response["delivery"] == "native"
    assert len(native.calls) == 1 and not backend.calls
    played = runtime.play(
        script='tap A\ntype "hello"', target="fixture", input_mode="message", seconds=0.1
    )
    assert played["status"] == "ok" and played["delivery"] == "message"
    assert len(backend.calls) == 2 and backend.released == 1
    camera.calls.clear()
    backend.calls.clear()
    for script in ('tap A\npaste "secret"', "tap A\nlook 1 2", "tap A\nfocus"):
        refused = runtime.play(script=script, target="fixture", input_mode="message")
        assert refused["status"] == "refused"
    assert not backend.calls and not camera.calls


def test_message_after_failure_preserves_primary_outcome_and_trace(identity, tmp_path):
    runtime = Runtime(
        message=Message(),
        broker=SnapshotBroker(fail=True),
        capture=SyntheticCapture(fail=True),
        trace_enabled=True,
        output_dir=tmp_path,
    )
    response = runtime.act(
        [{"action": "set_value", "value": "private password"}],
        "fixture",
        input_mode="message",
        settle_ms=0,
    )
    assert response["status"] == "ok" and response["may_have_acted"]
    assert response["data"]["after"]["status"] == "error"
    assert response["data"]["results"][0]["result"]["status"] == "invoked"
    traces = list(tmp_path.rglob("*.jsonl"))
    assert traces and all(
        "private password" not in path.read_text(encoding="utf-8") for path in traces
    )


def test_state_same_root_image_and_opaque_password_safe_data(identity, tmp_path):
    broker, camera = SnapshotBroker(), SyntheticCapture()
    runtime = Runtime(broker=broker, capture=camera, output_dir=tmp_path)
    response = runtime.window("state", "fixture", scope="caller")
    assert response["status"] == "ok" and response["delivery"] == "read_only"
    data = response["data"]
    assert data["uia"]["selection"] == {"status": "redacted"}
    assert data["consistency"] == "non_atomic" and not data["component_errors"]
    request = broker.calls[0][0]
    assert request["target"] == camera.calls[0]["target"] == "hwnd:123"
    assert request["target_binding"] == BINDING and request["rich"]
    assert camera.calls[0]["scope"] == "caller"
    assert response["frame"].image_bytes.startswith(b"\xff\xd8")
    assert response["frame"].metadata["capture_id"]
    assert identity == ["fixture"]


@pytest.mark.parametrize("action", ["tree", "text", "find", "inspect"])
def test_rich_discovery_and_token_fields_forward_unchanged(action):
    broker = SnapshotBroker()
    runtime = Runtime(broker=broker)
    options = {"query": "fixture"} if action == "find" else {"token": "opaque"}
    response = runtime.window(action, "fixture", rich=True, text_limit=100, **options)
    assert response["status"] == "ok"
    request = broker.calls[0][0]
    assert request["rich"] and request["text_limit"] == 100
    assert request.get("token") == options.get("token")
    assert response["target_binding"] == BINDING


def test_state_selector_and_token_forwarding(identity, tmp_path):
    broker = SnapshotBroker()
    runtime = Runtime(broker=broker, output_dir=tmp_path)
    response = runtime.window(
        "state", "fixture", selector={"automation_id": "edit"}, include_screenshot=False
    )
    assert response["status"] == "ok"
    assert broker.calls[-1][0]["selector"] == {"automation_id": "edit"}
    response = runtime.window("state", "fixture", token="opaque", include_screenshot=False)
    assert response["status"] == "ok" and broker.calls[-1][0]["token"] == "opaque"


@pytest.mark.parametrize("failed", ["uia", "screenshot"])
def test_state_retains_other_component(identity, tmp_path, failed):
    runtime = Runtime(
        broker=SnapshotBroker(fail=failed == "uia"),
        capture=SyntheticCapture(fail=failed == "screenshot"),
        output_dir=tmp_path,
    )
    response = runtime.window("state", "fixture")
    assert response["status"] == "partial"
    assert failed in response["data"]["component_errors"]
    other = "uia" if failed == "screenshot" else "screenshot"
    assert other in response["data"]
    assert ("frame" in response) == (failed == "uia")


def test_state_disabled_components_and_invalid_options_are_effect_free(identity, tmp_path):
    camera = SyntheticCapture()
    runtime = Runtime(capture=camera, output_dir=tmp_path)
    assert runtime.window("state", "fixture", include_ax=False)["status"] == "ok"
    assert runtime._broker is None
    camera.calls.clear()
    broker = SnapshotBroker()
    runtime = Runtime(broker=broker, capture=camera, output_dir=tmp_path)
    assert runtime.window("state", "fixture", include_screenshot=False)["status"] == "ok"
    assert not camera.calls
    broker.calls.clear()
    for options in (
        {"include_ax": False, "include_screenshot": False},
        {"include_ax": 0},
        {"rich": 1},
    ):
        assert runtime.window("state", "fixture", **options)["status"] == "refused"
    assert not broker.calls and not camera.calls


def test_state_missing_binding_and_unknown_fields_refuse_before_components(
    identity, monkeypatch, tmp_path
):
    broker, camera = SnapshotBroker(), SyntheticCapture()
    runtime = Runtime(broker=broker, capture=camera, output_dir=tmp_path)
    response = runtime.window("state", "fixture", nonsense=True)
    assert "Unsupported state arguments" in response["error"]["message"]
    monkeypatch.setattr(
        win32, "window_identity", lambda hwnd, pid: {**BINDING, "process_creation_time": None}
    )
    response = runtime.window("state", "fixture")
    assert response["status"] == "refused" and response["error"]["code"] == "BINDING_UNAVAILABLE"
    assert not broker.calls and not camera.calls


def test_state_observations_report_provider_changes_without_intention_claim(
    identity, monkeypatch, tmp_path
):
    observations = iter(
        [{"foreground_hwnd": 999, "cursor": [1, 2]}, {"foreground_hwnd": 123, "cursor": [3, 4]}]
    )
    monkeypatch.setattr(win32, "observations", lambda: next(observations))
    response = Runtime(broker=SnapshotBroker(), output_dir=tmp_path).window(
        "state", "fixture", include_screenshot=False
    )
    assert response["observations"]["foreground_changed"]
    assert response["observations"]["cursor_changed"]
    assert "focus_intentional" not in response


def test_state_stale_identity_drops_untrusted_components(identity, monkeypatch, tmp_path):
    broker = SnapshotBroker()
    original = broker.call

    def stale(*args, **kwargs):
        response = original(*args, **kwargs)
        monkeypatch.setattr(
            win32, "window_identity", lambda hwnd, pid: {**BINDING, "process_creation_time": 999}
        )
        return response

    broker.call = stale
    camera = SyntheticCapture()
    response = Runtime(broker=broker, capture=camera, output_dir=tmp_path).window(
        "state", "fixture"
    )
    assert response["status"] == "error" and "frame" not in response
    assert "uia" not in response["data"] and "screenshot" not in response["data"]
    assert not camera.calls


def test_state_deadline_is_shared_between_components(identity, tmp_path):
    broker, camera = SnapshotBroker(), SyntheticCapture()
    original = broker.call

    def slow(*args, **kwargs):
        time.sleep(0.025)
        return original(*args, **kwargs)

    broker.call = slow
    response = Runtime(broker=broker, capture=camera, output_dir=tmp_path).window(
        "state", "fixture", timeout=0.01
    )
    assert response["status"] == "error" and not camera.calls
    assert set(response["data"]["component_errors"]) == {"uia", "screenshot"}


def test_message_cancel_unknown_and_owner_cleanup():
    entered = threading.Event()

    class Blocking(Message):
        def execute(self, action, target, timeout=5):
            entered.set()
            assert self.event.wait(2)
            return {"status": "unknown", "cancelled": True, "may_have_acted": True}

    backend = Blocking()
    runtime = Runtime(message=backend)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(
            runtime.act,
            [{"action": "key_down", "key": "A"}],
            "fixture",
            input_mode="message",
            screenshot=False,
        )
        assert entered.wait(2)
        began = time.monotonic()
        assert runtime.system("cancel")["data"]["requested"]
        assert time.monotonic() - began < 0.2
        response = future.result(2)
    assert response["status"] == "unknown" and response["cancelled"]
    assert response["delivery"] == "message" and response["may_have_acted"]
    assert backend.released == 1 and backend.event is None


def test_message_auto_after_uses_original_binding_and_fresh_capture(identity, tmp_path):
    camera = SyntheticCapture()
    runtime = Runtime(
        message=Message(), broker=SnapshotBroker(), capture=camera, output_dir=tmp_path
    )
    responses = [
        runtime.act([{"action": "key", "key": "A"}], "fixture", input_mode="message", settle_ms=0)
        for _ in range(2)
    ]
    assert all(
        item["status"] == "ok" and item["data"]["after"]["status"] == "ok" for item in responses
    )
    assert (
        responses[0]["frame"].metadata["capture_id"] != responses[1]["frame"].metadata["capture_id"]
    )
    assert not identity  # no title rediscovery after message binding


@pytest.mark.parametrize("feedback", ["auto", True, "text"])
def test_all_message_feedback_uses_original_hwnd_despite_replaced_caption(
    identity, monkeypatch, tmp_path, feedback
):
    replacement = {"hwnd": 999, "pid": 990, "process_creation_time": 991}

    def wrong_caption(target):
        pytest.fail(f"Message feedback rediscovered caption: {target}")

    monkeypatch.setattr(win32, "resolve_window", wrong_caption)
    broker, camera = SnapshotBroker(), SyntheticCapture()
    runtime = Runtime(message=Message(), broker=broker, capture=camera, output_dir=tmp_path)
    response = runtime.act(
        [{"action": "key", "key": "A"}],
        "replaced caption",
        input_mode="message",
        screenshot=feedback,
        settle_ms=0,
    )
    assert response["status"] == "ok" and response["may_have_acted"]
    assert response["data"]["after"]["status"] == "ok"
    assert all(call[0]["target"] == "hwnd:123" for call in broker.calls)
    assert all(call["target"] == "hwnd:123" for call in camera.calls)
    assert replacement != response["data"]["results"][0]["result"]["target_binding"]
    assert ("frame" in response) is (feedback != "text")


@pytest.mark.parametrize("feedback", ["auto", True, "text"])
@pytest.mark.parametrize("failure", ["metadata", "identity_before", "identity_after", "missing"])
def test_message_feedback_binding_failure_keeps_mutation_and_rejects_frame(
    identity, monkeypatch, tmp_path, feedback, failure
):
    replacement = {"hwnd": 999, "pid": 990, "process_creation_time": 991}
    backend, broker, camera = Message(), SnapshotBroker(), SyntheticCapture()
    original_execute, original_call, original_capture = (
        backend.execute,
        broker.call,
        camera.screenshot,
    )

    def stale_identity():
        monkeypatch.setattr(win32, "window_identity", lambda hwnd, pid: dict(replacement))

    def execute(*args, **kwargs):
        response = original_execute(*args, **kwargs)
        if failure == "identity_before":
            stale_identity()
        elif failure == "missing":
            response.pop("target_binding")
        return response

    def read(*args, **kwargs):
        response = original_call(*args, **kwargs)
        if failure == "metadata":
            response["target_binding"] = dict(replacement)
            response["data"]["root_binding"] = dict(replacement)
        elif failure == "identity_after":
            stale_identity()
        return response

    def capture(**kwargs):
        frame = original_capture(**kwargs)
        if failure == "metadata":
            frame.metadata.update(replacement)
        elif failure == "identity_after":
            stale_identity()
        return frame

    backend.execute, broker.call, camera.screenshot = execute, read, capture
    runtime = Runtime(message=backend, broker=broker, capture=camera, output_dir=tmp_path)
    response = runtime.act(
        [{"action": "key", "key": "A"}],
        "caption",
        input_mode="message",
        screenshot=feedback,
        settle_ms=0,
    )
    assert response["status"] == "ok" and response["may_have_acted"]
    assert response["data"]["results"][0]["result"]["status"] == "invoked"
    assert response["data"]["after"]["status"] in {"refused", "error"}
    assert "frame" not in response and len(backend.calls) == 1
    assert not identity
    if failure in {"missing", "identity_before"}:
        assert not broker.calls and not camera.calls


@pytest.mark.parametrize("feedback", ["auto", True, "text"])
def test_message_feedback_requires_last_attempt_binding_and_skips_cancel(
    identity, tmp_path, feedback
):
    backend, broker, camera = Message(), SnapshotBroker(), SyntheticCapture()
    runtime = Runtime(message=backend, broker=broker, capture=camera, output_dir=tmp_path)
    response = runtime.act(
        [{"action": "key", "key": "A"}, {"action": "wait", "seconds": 0}],
        "caption",
        input_mode="message",
        screenshot=feedback,
        settle_ms=0,
    )
    assert response["status"] == "ok" and response["data"]["after"]["status"] == "refused"
    assert not broker.calls and not camera.calls
    original = backend.execute

    def cancel(*args, **kwargs):
        observed = original(*args, **kwargs)
        backend.event.set()
        return {**observed, "status": "unknown", "cancelled": True, "may_have_acted": True}

    backend.execute = cancel
    response = runtime.act(
        [{"action": "key", "key": "A"}],
        "caption",
        input_mode="message",
        screenshot=feedback,
        settle_ms=0,
    )
    assert response["cancelled"] and "after" not in response["data"]
    assert not broker.calls and not camera.calls


@pytest.mark.parametrize("root_field", ["root_binding", "target_binding"])
def test_message_text_feedback_rejects_nested_root_mismatch_without_ocr(
    identity, tmp_path, root_field
):
    broker = SnapshotBroker()
    original = broker.call

    def read(*args, **kwargs):
        observed = original(*args, **kwargs)
        observed["data"][root_field] = {**BINDING, "hwnd": 999}
        return observed

    class Services:
        def ocr(self, *args, **kwargs):
            pytest.fail("Bound message text feedback must not start OCR")

    broker.call = read
    backend = Message()
    response = Runtime(
        message=backend, broker=broker, services=Services(), output_dir=tmp_path
    ).act(
        [{"action": "key", "key": "A"}],
        "caption",
        input_mode="message",
        screenshot="text",
        settle_ms=0,
    )
    assert response["status"] == "ok" and response["may_have_acted"]
    assert response["data"]["after"]["error"]["code"] == "STALE_TARGET"
    assert len(backend.calls) == 1 and not identity


def test_protocol_schemas_state_image_and_direct_context(identity, tmp_path):
    async def exercise():
        runtime = Runtime(
            message=Message(),
            broker=SnapshotBroker(),
            capture=SyntheticCapture(),
            output_dir=tmp_path,
        )
        server = create_server(runtime)
        direct = await server.call_tool("window", {"action": "state", "target": "fixture"})
        assert direct is not None  # no transport Context exists on this path
        async with create_connected_server_and_client_session(server) as client:
            tools = (await client.list_tools()).tools
            assert len(tools) == 7
            schemas = {tool.name: tool.inputSchema["properties"] for tool in tools}
            assert all(
                "ctx" not in schema and "target_binding" not in schema
                for schema in schemas.values()
            )
            assert "input_mode" in schemas["act"] and "input_mode" in schemas["play"]
            assert {"rich", "include_ax", "include_screenshot", "text_limit"} <= set(
                schemas["window"]
            )
            state = await client.call_tool("window", {"action": "state", "target": "fixture"})
            assert (
                not state.isError
                and len([item for item in state.content if isinstance(item, ImageContent)]) == 1
            )
            assert "image_bytes" not in state.structuredContent
            status = await client.call_tool("system", {})
            assert status.structuredContent["data"]["available_input_modes"] == [
                "uia",
                "native",
                "message",
            ]
            acted = await client.call_tool(
                "act",
                {
                    "actions": [{"action": "set_value", "value": "private"}],
                    "target": "fixture",
                    "input_mode": "message",
                    "screenshot": False,
                },
            )
            assert not acted.isError and acted.structuredContent["delivery"] == "message"
            assert "private" not in json.dumps(acted.structuredContent)

    anyio.run(exercise)
