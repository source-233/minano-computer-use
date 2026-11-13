import json
from concurrent.futures import ThreadPoolExecutor

import anyio
import pytest
from mcp.shared.memory import create_connected_server_and_client_session
from test_uia_backend import Control, FakeAuto, FakeWin32
from test_uia_queries import Clock

from minano_computer_use import win32
from minano_computer_use.runtime import Runtime
from minano_computer_use.server import create_server
from minano_computer_use.uia_backend import UIABackend

TIME = 134387468188212291


class ExactWin32(FakeWin32):
    def resolve_window(self, target):
        assert target in {"App", "hwnd:10"}
        return dict(self.windows[0])


class RecordingClock(Clock):
    def __init__(self):
        super().__init__()
        self.sleeps = []

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        super().sleep(seconds)


class Value:
    IsReadOnly = False

    def __init__(self, clock, delay):
        self.clock, self.delay = clock, delay
        self.writes = []
        self.reads = []

    @property
    def Value(self):
        current = "old"
        if self.writes and self.delay is not None:
            written_at, requested = self.writes[0]
            if self.clock() - written_at + 1e-9 >= self.delay:
                current = requested
        self.reads.append((self.clock(), current))
        return current

    def SetValue(self, value, waitTime=0):
        assert waitTime == 0
        self.writes.append((self.clock(), value))
        return True


class Invoke:
    def __init__(self, clock):
        self.clock = clock
        self.calls = []

    def Invoke(self, waitTime=0):
        assert waitTime == 0
        self.calls.append(self.clock())
        return True


class SourceBroker:
    """Keep the actual UIABackend on one owner thread, including shutdown."""

    def __init__(self, root, adapter, clock):
        self.clock = clock
        self.calls, self.responses = [], []
        self.worker = ThreadPoolExecutor(max_workers=1)
        self.backend = self.worker.submit(
            UIABackend, auto=FakeAuto(root), win32=adapter, clock=clock, sleep=clock.sleep
        ).result(timeout=5)
        self.closed = False

    def call(self, request, timeout, cancel_event):
        assert not cancel_event.is_set()
        payload = {**request, "_deadline_monotonic": self.clock() + timeout}
        self.calls.append(payload)
        response = self.worker.submit(self.backend.execute, payload).result(timeout=5)
        self.responses.append(response)
        return response

    def close(self):
        if not self.closed:
            self.closed = True
            try:
                self.worker.submit(self.backend.close).result(timeout=5)
            finally:
                self.worker.shutdown(wait=True)


@pytest.fixture
def harness(monkeypatch, tmp_path):
    resources = []

    def build(delay):
        clock, adapter = RecordingClock(), ExactWin32()
        adapter.creation = TIME
        value, invoke = Value(clock, delay), Invoke(clock)
        edit = Control("Edit", patterns={2: value})
        edit.AutomationId, edit.ControlTypeName = "1001", "EditControl"
        apply = Control("Apply", patterns={1: invoke})
        apply.AutomationId = "1002"
        root = Control("App", children=[edit, apply])
        broker = SourceBroker(root, adapter, clock)
        runtime = Runtime(broker=broker, allow_native=False, output_dir=tmp_path)
        resources.append(runtime)
        monkeypatch.setattr(win32, "resolve_window", adapter.resolve_window)
        monkeypatch.setattr(win32, "window_identity", adapter.window_identity)
        monkeypatch.setattr(win32, "observations", adapter.observations)

        def no_external_backend(*args, **kwargs):
            pytest.fail("semantic-only protocol must not use input or capture backends")

        for name in ("_input", "_background", "_camera"):
            monkeypatch.setattr(runtime, name, no_external_backend)
        return runtime, broker, clock, root, edit, value, invoke

    yield build
    for runtime in reversed(resources):
        runtime.close()


def public(response, *, error=False):
    assert response.isError is error, response
    assert len(response.content) == 1
    assert json.loads(response.content[0].text) == response.structuredContent
    return response.structuredContent


async def state_tokens(client, broker):
    state = public(
        await client.call_tool(
            "window",
            {
                "action": "state",
                "target": "App",
                "include_ax": True,
                "include_screenshot": False,
                "include_tokens": True,
                "timeout": 2,
            },
        )
    )
    assert state["status"] == "ok", state
    binding = {"hwnd": 10, "pid": 20, "process_creation_time": str(TIME)}
    assert state["target_binding"] == binding
    assert state["data"]["target_binding"] == binding
    assert state["data"]["uia"]["root_binding"] == binding
    assert not state["data"]["component_errors"]
    assert "screenshot" not in state["data"]
    assert state["data"]["uia"]["truncated"] is False
    nodes = state["data"]["uia"]["nodes"]
    tokens = {}
    for identifier, name in (("1001", "Edit"), ("1002", "Apply")):
        matches = [node for node in nodes if node.get("automation_id") == identifier]
        assert len(matches) == 1
        node = matches[0]
        assert node["name"] == name
        assert isinstance(node["token"], str) and node["token"]
        assert 0 < node["token_ttl_seconds"] <= 60
        raw = next(
            node
            for node in broker.responses[0]["data"]["nodes"]
            if node.get("automation_id") == identifier
        )
        assert node["token"] == raw["token"]
        tokens[identifier] = node["token"]
    assert len(set(tokens.values())) == 2
    assert type(broker.responses[0]["data"]["root_binding"]["process_creation_time"]) is int
    assert broker.calls[0]["target"] == "hwnd:10"
    return tokens


def forbid_discovery(monkeypatch, broker, root, edit):
    def forbidden(*args, **kwargs):
        pytest.fail("state tokens must not rediscover by selector/name or walk the root")

    walk = broker.backend._walk

    def privacy_walk(control, *args, **kwargs):
        assert control is edit, "only selected Edit privacy traversal is permitted"
        return walk(control, *args, **kwargs)

    monkeypatch.setattr(broker.backend, "_select", forbidden)
    monkeypatch.setattr(broker.backend, "_selector", forbidden)
    monkeypatch.setattr(broker.backend, "_find", forbidden)
    monkeypatch.setattr(broker.backend, "_walk", privacy_walk)
    monkeypatch.setattr(root, "GetFirstChildControl", forbidden)


async def token_batch(client, tokens, *, error=False):
    return public(
        await client.call_tool(
            "act",
            {
                "target": "App",
                "actions": [
                    {
                        "action": "set_value",
                        "token": tokens["1001"],
                        "value": "token value 你好",
                    },
                    {"action": "invoke", "token": tokens["1002"]},
                ],
                "screenshot": False,
                "timeout": 2,
            },
        ),
        error=error,
    )


def assert_token_only_calls(broker, expected):
    assert [request["action"] for request in broker.calls] == expected
    for request in broker.calls[1:]:
        assert request["token"]
        assert request.get("selector") is None


def test_mcp_state_tokens_drive_source_backend_actions_without_discovery(harness, monkeypatch):
    runtime, broker, clock, root, edit, value, invoke = harness(0)

    async def exercise():
        async with create_connected_server_and_client_session(create_server(runtime)) as client:
            tokens = await state_tokens(client, broker)
            forbid_discovery(monkeypatch, broker, root, edit)
            response = await token_batch(client, tokens)
            assert response["status"] == "ok", response
            results = response["data"]["results"]
            assert [entry["result"]["status"] for entry in results] == [
                "verified",
                "invoked",
            ]
            assert results[0]["result"]["data"]["after"] == "token value 你好"
            for entry in results:
                result = entry["result"]
                assert result["target_binding"]["process_creation_time"] == str(TIME)
                assert result["delivery"] == "accessibility"
                assert result["fallbacks"] == []
            assert response["data"]["completed"] == 2
            assert response["data"]["stopped_early"] is False
            assert len(value.writes) == len(invoke.calls) == 1
            assert value.Value == "token value 你好"
            assert clock.sleeps == []
            assert_token_only_calls(broker, ["state", "set_value", "invoke"])

    anyio.run(exercise)


def test_mcp_delayed_set_value_verifies_once_then_continues_default_batch(harness, monkeypatch):
    runtime, broker, clock, root, edit, value, invoke = harness(0.2)

    async def exercise():
        async with create_connected_server_and_client_session(create_server(runtime)) as client:
            tokens = await state_tokens(client, broker)
            forbid_discovery(monkeypatch, broker, root, edit)
            response = await token_batch(client, tokens)
            assert response["status"] == "ok", response
            results = response["data"]["results"]
            assert [entry["result"]["status"] for entry in results] == [
                "verified",
                "invoked",
            ]
            assert results[0]["result"]["data"]["before"] == "old"
            assert results[0]["result"]["data"]["after"] == "token value 你好"
            assert response["data"]["attempted"] == response["data"]["completed"] == 2
            assert response["data"]["stopped_early"] is False
            assert len(value.writes) == len(invoke.calls) == 1
            assert invoke.calls[0] - value.writes[0][0] == pytest.approx(0.2)
            assert clock.sleeps == pytest.approx([0.05] * 4)
            assert any(observed == "old" for _, observed in value.reads)
            assert value.reads[-1][1] == "token value 你好"
            assert_token_only_calls(broker, ["state", "set_value", "invoke"])

    anyio.run(exercise)


def test_mcp_set_value_readback_expiry_stops_default_batch_without_replay(harness, monkeypatch):
    runtime, broker, clock, root, edit, value, invoke = harness(None)

    async def exercise():
        async with create_connected_server_and_client_session(create_server(runtime)) as client:
            tokens = await state_tokens(client, broker)
            forbid_discovery(monkeypatch, broker, root, edit)
            response = await token_batch(client, tokens, error=True)
            assert response["status"] == "unknown", response
            assert response["may_have_acted"] is True
            data = response["data"]
            assert data["requested"] == 2 and data["attempted"] == 1
            assert data["completed"] == 0 and data["stopped_early"] is True
            result = data["results"][0]["result"]
            assert result["status"] == "unknown" and result["may_have_acted"] is True
            assert result["data"]["before"] is None
            assert result["data"]["after"] is None
            assert result["data"]["value_redacted"] is True
            assert result["error"]["code"] == "READBACK_TIMEOUT"
            assert result["delivery"] == "accessibility" and result["fallbacks"] == []
            assert len(value.writes) == 1 and invoke.calls == []
            assert clock() - value.writes[0][0] == pytest.approx(0.6)
            assert clock.sleeps and all(0 < delay <= 0.05 for delay in clock.sleeps)
            assert sum(clock.sleeps) == pytest.approx(0.6)
            assert_token_only_calls(broker, ["state", "set_value"])

    anyio.run(exercise)
