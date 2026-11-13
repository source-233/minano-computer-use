import json

import anyio
import pytest
from mcp.shared.memory import create_connected_server_and_client_session

from minano_computer_use import __version__, win32
from minano_computer_use.capture import CaptureFrame
from minano_computer_use.runtime import VERSION, Runtime
from minano_computer_use.server import create_server

TIME = 134387468188212291
BINDING = {"hwnd": 123, "pid": 456, "process_creation_time": TIME}


class ModernNative:
    def __init__(self, error=False):
        self.calls = []
        self.releases = 0
        self.error = error

    def set_cancel_event(self, event):
        pass

    def execute(self, action, target, *, key_method="scancode", feedback="compact"):
        self.calls.append((action, target, key_method, feedback))
        if self.error:
            raise TypeError("post-dispatch failure")
        return {
            "status": "invoked",
            "target_binding": dict(BINDING),
            "consumption_verified": False,
            "receipt": {"requested_events": 2, "accepted_events": 2, "unknown_events": 0},
            **({"input_context": {"status": "unknown"}} if feedback == "detailed" else {}),
        }

    def release_all(self):
        self.releases += 1
        return {"status": "ok"}

    def close(self):
        pass


class Capture:
    def __init__(self):
        self.calls = []

    def screenshot(self, **kwargs):
        self.calls.append(kwargs)
        return {"status": "error", "error": {"code": "CAPTURE_FAILED"}}

    def record(self, **kwargs):
        self.calls.append(kwargs)
        return CaptureFrame(b"synthetic", "image/png", {"process_creation_time": TIME})

    def close(self):
        pass


class Broker:
    def __init__(self):
        self.calls = []

    def call(self, request, timeout, cancel_event):
        self.calls.append(request)
        return {
            "status": "ok",
            "target_binding": dict(BINDING),
            "data": {"root_binding": dict(BINDING), "nodes": []},
        }

    def close(self):
        pass


def test_public_tools_schema_and_real_mcp_time_feedback():
    async def exercise():
        native = ModernNative()
        runtime = Runtime(native=native)
        async with create_connected_server_and_client_session(create_server(runtime)) as client:
            tools = {tool.name: tool.inputSchema for tool in (await client.list_tools()).tools}
            assert set(tools) == {
                "window",
                "act",
                "play",
                "screenshot",
                "record",
                "process",
                "system",
            }
            for schema in tools.values():
                assert not {"ctx", "scope", "_request_cancel"} & set(schema["properties"])
            for name in ("act", "play"):
                assert tools[name]["properties"]["feedback"]["default"] == "compact"
            assert tools["window"]["properties"]["capture_mode"]["default"] == "screen"
            assert tools["window"]["properties"]["allow_fallback"]["default"] is False
            for feedback in ("compact", "detailed"):
                response = await client.call_tool(
                    "act",
                    {
                        "actions": [{"action": "key", "key": "A"}],
                        "target": "fixture",
                        "native": True,
                        "screenshot": False,
                        "feedback": feedback,
                    },
                )
                assert json.loads(response.content[0].text) == response.structuredContent
                action = response.structuredContent["data"]["results"][0]["result"]
                assert action["target_binding"]["process_creation_time"] == str(TIME)
                assert ("input_context" in action) is (feedback == "detailed")
                assert native.calls[-1][-1] == feedback
            assert type(BINDING["process_creation_time"]) is int

    anyio.run(exercise)


@pytest.mark.parametrize("mode", ["uia", "native", "message"])
@pytest.mark.parametrize("method", ["act", "play"])
def test_invalid_feedback_rejected_before_any_backend_effect(mode, method, tmp_path):
    native, broker, capture = ModernNative(), Broker(), Capture()
    runtime = Runtime(
        native=native, message=native, broker=broker, capture=capture, output_dir=tmp_path
    )
    options = {"screenshot": False} if method == "act" else {"seconds": 0.1}
    action = (
        {"action": "invoke", "selector": "button"}
        if mode == "uia"
        else {"action": "key", "key": "A"}
    )
    response = getattr(runtime, method)(
        [action], "fixture", input_mode=mode, feedback="invalid", **options
    )
    assert response["status"] == "refused"
    assert response["error"]["code"] == "INVALID_ARGUMENT"
    assert not native.calls and not capture.calls and not broker.calls
    runtime.close()


def test_native_typeerror_never_replayed_and_cleanup_kept():
    native = ModernNative(error=True)
    response = Runtime(native=native).act(
        [{"action": "key", "key": "A"}],
        "fixture",
        native=True,
        feedback="detailed",
        screenshot=False,
    )
    action = response["data"]["results"][0]["result"]
    assert action["status"] == "unknown" and action["may_have_acted"]
    assert len(native.calls) == 1 and native.releases == 1


def test_legacy_native_without_feedback_keyword_remains_supported():
    class Legacy(ModernNative):
        def execute(self, action, target):
            self.calls.append((action, target))
            return {"status": "invoked"}

    native = Legacy()
    response = Runtime(native=native).act(
        [{"action": "key", "key": "A"}],
        "fixture",
        native=True,
        feedback="detailed",
        screenshot=False,
    )
    assert response["status"] == "ok"
    assert len(native.calls) == 1 and native.releases == 1


@pytest.mark.parametrize("mode", ["uia", "native", "message"])
def test_feedback_override_in_later_action_rejects_complete_plan(mode):
    native, broker = ModernNative(), Broker()
    action = (
        {"action": "invoke", "selector": "button"}
        if mode == "uia"
        else {"action": "key", "key": "A"}
    )
    response = Runtime(native=native, message=native, broker=broker).act(
        [action, {**action, "feedback": "detailed"}],
        "fixture",
        input_mode=mode,
        screenshot=False,
    )
    assert response["status"] == "refused"
    assert not native.calls and not broker.calls


def test_failed_after_observation_does_not_replace_action_receipt():
    native, capture = ModernNative(), Capture()
    response = Runtime(native=native, capture=capture).act(
        [{"action": "key", "key": "A"}],
        "fixture",
        native=True,
        feedback="detailed",
        screenshot=True,
        settle_ms=0,
    )
    assert response["status"] == "ok" and response["may_have_acted"]
    assert response["data"]["results"][0]["result"]["receipt"]["accepted_events"] == 2
    assert response["data"]["after"]["error"]["code"] == "CAPTURE_FAILED"
    assert len(native.calls) == 1 and native.releases == 1


def test_play_routes_feedback_without_changing_recording(tmp_path):
    native, capture = ModernNative(), Capture()
    runtime = Runtime(native=native, capture=capture, output_dir=tmp_path)
    response = runtime.play(
        [{"action": "key", "key": "A"}], "fixture", native=True, feedback="detailed", seconds=0.1
    )
    assert "data" in response, response
    assert response["data"]["actions"]["data"]["results"][0]["result"]["input_context"]
    assert native.calls[0][-1] == "detailed" and native.releases == 1
    assert "feedback" not in capture.calls[0]
    runtime.close()


def test_status_instance_facts_are_stable_and_build_unknown(monkeypatch):
    runtime = Runtime()
    original = runtime.system()["data"]
    monkeypatch.setenv("GIT_COMMIT", "untrusted")
    monkeypatch.setattr("minano_computer_use.__version__", "untrusted")
    assert runtime.system()["data"] == original
    assert original["package_version"] == VERSION == __version__
    assert original["pid"] > 0 and original["started_at"] > 0
    assert original["revision"] is None and original["build"] is None
    assert Runtime().system()["data"]["instance_id"] != original["instance_id"]


@pytest.mark.parametrize("capture_mode", ["screen", "auto", "window", "printwindow", "wgc"])
def test_state_capture_backend_partial_and_exact_binding(monkeypatch, capture_mode):
    monkeypatch.setattr(win32, "resolve_window", lambda target: dict(BINDING))
    identities = []

    def identity(hwnd, pid):
        identities.append((hwnd, pid))
        return dict(BINDING)

    monkeypatch.setattr(win32, "window_identity", identity)
    monkeypatch.setattr(win32, "observations", lambda: {})
    broker, capture = Broker(), Capture()
    response = Runtime(broker=broker, capture=capture).window(
        "state", "fixture", capture_mode=capture_mode, allow_fallback=True
    )
    assert response["status"] == "partial"
    assert response["data"]["consistency"] == "non_atomic"
    assert response["data"]["uia"]["root_binding"] == BINDING
    assert capture.calls[0]["capture_mode"] == capture_mode
    assert capture.calls[0]["allow_fallback"] is True
    assert capture.calls[0]["target"] == "hwnd:123"
    assert identities and set(identities) == {(123, 456)}
    assert type(broker.calls[0]["target_binding"]["process_creation_time"]) is int


@pytest.mark.parametrize(
    "options", [{"capture_mode": "invalid"}, {"capture_mode": []}, {"allow_fallback": "yes"}]
)
def test_state_validates_capture_arguments_even_without_image(monkeypatch, options):
    def unexpected(*args):
        pytest.fail("invalid state must not resolve target")

    monkeypatch.setattr(win32, "resolve_window", unexpected)
    response = Runtime().window("state", "fixture", include_screenshot=False, **options)
    assert response["status"] == "refused" and response["error"]["code"] == "INVALID_ARGUMENT"
