import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest

from minano_computer_use.runtime import Runtime


@pytest.mark.parametrize("outcome", ["invoked", "unknown"])
def test_retention_cleanup_failure_preserves_mutation_outcome(tmp_path, monkeypatch, outcome):
    class OutcomeBroker:
        def call(self, request, timeout=None, cancel_event=None):
            response = {"status": outcome, "operation": request["action"], "may_have_acted": True}
            if outcome == "unknown":
                response.update(cancelled=True, error={"code": "CANCELLED"})
            return response

        def close(self):
            pass

    runtime = Runtime(trace_enabled=True, output_dir=tmp_path, broker=OutcomeBroker())
    store = runtime._artifacts()

    def broken_retention(_token):
        raise OSError("ledger flush failed")

    monkeypatch.setattr(store, "end_group", broken_retention)
    try:
        response = runtime.window("invoke", "fixture")
        assert response["status"] == outcome
        assert response["may_have_acted"]
        assert response["call_id"]
        assert response["warnings"][0]["code"] == "ARTIFACT_RETENTION_FAILED"
        if outcome == "unknown":
            assert response["cancelled"] and response["error"]["code"] == "CANCELLED"
    finally:
        runtime.close()


class Broker:
    def __init__(self, fail_at=None, raises=False):
        self.calls = []
        self.fail_at = fail_at
        self.raises = raises
        self.closed = False

    def call(self, request, timeout=None, cancel_event=None):
        self.calls.append(request)
        if self.raises:
            raise RuntimeError("injected failure")
        return {
            "status": "refused" if len(self.calls) == self.fail_at else "invoked",
            "operation": request["action"],
        }

    def close(self):
        self.closed = True


class Native:
    def __init__(self, raises=False):
        self.calls = []
        self.released = 0
        self.raises = raises
        self.cancel_event = None

    def set_cancel_event(self, event):
        self.cancel_event = event

    def execute(self, action, target):
        self.calls.append((action, target))
        if self.raises:
            raise RuntimeError("injected native failure")
        return {"status": "invoked", "operation": action["action"]}

    def release_all(self):
        self.released += 1
        return {"status": "ok", "operation": "release_all"}

    def close(self):
        pass


class Camera:
    def __init__(self):
        self.calls = []
        self.finished = False

    def record(self, **kwargs):
        self.calls.append(kwargs)
        time.sleep(0.03)
        self.finished = True
        return SimpleNamespace(
            image_bytes=b"png", mime_type="image/png", metadata={"timestamps": [0, 0.03]}
        )

    def close(self):
        pass


def test_order_and_stop_preserve_partial_results():
    broker = Broker(fail_at=2)
    runtime = Runtime(broker=broker)
    actions = [{"action": "invoke", "selector": name} for name in ("one", "two", "three")]
    response = runtime.act(actions, target="fixture", screenshot=False)
    assert response["status"] == "partial"
    assert [item["selector"] for item in broker.calls] == ["one", "two"]
    assert response["data"]["attempted"] == 2
    assert response["data"]["completed"] == 1
    assert response["data"]["stopped_early"]
    broker.calls.clear()
    response = runtime.act(actions, target="fixture", stop_on_error=False, screenshot=False)
    assert len(response["data"]["results"]) == 3


@pytest.mark.parametrize(
    "actions,kwargs",
    [
        ([{"action": "invoke"}], {}),
        ([{"action": "click", "x": 1, "y": 2}], {"target": "fixture"}),
        ([{"action": "invoke", "x": 1}], {"target": "fixture"}),
        (
            [{"action": "invoke"}, {"action": "wait", "seconds": float("nan")}],
            {"target": "fixture"},
        ),
        ([{"action": "invoke"}] * 101, {"target": "fixture"}),
        ([{"action": "invoke", "target": None}], {"target": "fixture"}),
        ([{"action": "invoke"}, {"action": "set_value", "value": 42}], {"target": "fixture"}),
    ],
)
def test_validate_entire_batch_before_effects(actions, kwargs):
    broker = Broker()
    runtime = Runtime(broker=broker)
    assert runtime.act(actions, **kwargs, screenshot=False)["status"] == "refused"
    assert not broker.calls


def test_native_per_call_gate_and_finally_cleanup():
    native = Native(raises=True)
    runtime = Runtime(native=native)
    action = [{"action": "key_down", "key": 65}]
    assert runtime.act(action, "fixture", native=False, screenshot=False)["status"] == "refused"
    assert not native.calls
    runtime = Runtime(allow_native=True, native=native)
    assert runtime.act(action, "fixture", screenshot=False)["status"] == "refused"
    response = runtime.act(action, "fixture", native=True, screenshot=False)
    assert response["status"] == "unknown"
    assert native.released == 1
    assert len(native.calls) == 1


def test_force_focus_is_native_only_and_requires_explicit_call_and_target():
    native, broker = Native(), Broker()
    runtime = Runtime(native=native, broker=broker)
    actions = [{"action": "force_focus"}]
    assert runtime.act(actions, "fixture", screenshot=False)["status"] == "refused"
    assert runtime.window("force_focus", "fixture")["status"] == "refused"
    assert runtime.act(actions, native=True, screenshot=False)["status"] == "refused"
    invalid = runtime.act(
        [{"action": "force_focus", "key": "A"}], "fixture", native=True, screenshot=False
    )
    assert invalid["status"] == "refused"
    assert not native.calls and not broker.calls
    response = runtime.act(actions, "fixture", native=True, screenshot=False)
    assert response["status"] == "ok"
    observed = response["data"]["results"][0]["result"]
    assert observed["delivery"] == "native"
    assert native.calls == [({"action": "force_focus", "target": "fixture"}, "fixture")]


def test_native_keyboard_does_not_automatically_force_focus():
    native = Native()
    response = Runtime(allow_native=True, native=native).act(
        [{"action": "key", "key": "A"}], "fixture", native=True, screenshot=False
    )
    assert response["status"] == "ok"
    assert [action["action"] for action, _ in native.calls] == ["key"]


def test_malformed_later_native_action_prevents_first_input():
    native = Native()
    runtime = Runtime(allow_native=True, native=native)
    actions = [{"action": "focus"}, {"action": "key", "key": "not a keycode"}]
    assert runtime.act(actions, "fixture", native=True, screenshot=False)["status"] == "refused"
    assert not native.calls


@pytest.mark.parametrize("action", ["key", "hold"])
def test_native_chord_prevalidation_matches_backend(action):
    native = Native()
    runtime = Runtime(allow_native=True, native=native)
    chord = {"action": action, "key": "CTRL+A"}
    assert runtime.act([chord], "fixture", native=True, screenshot=False)["status"] == "ok"
    assert native.calls[0][0]["key"] == [17, 65]
    assert native.released == 1


@pytest.mark.parametrize(
    "invalid",
    [
        {"action": "key", "key": "+".join(["A"] * 33)},
        {"action": "hold", "key": ["A"] * 33},
        {"action": "key", "key": "CTRL+"},
        {"action": "key_down", "key": ["CTRL", "bad-key"]},
        {"action": "key_up", "key": [True]},
        {"action": "wheel"},
        {"action": "wheel", "amount": 1.5},
        {"action": "wheel", "delta": True},
        {"action": "text", "text": "x" * 10001},
        {"action": "text", "value": "x" * 10001},
    ],
)
def test_native_invalid_later_action_is_refused_before_any_effect(invalid):
    native = Native()
    runtime = Runtime(allow_native=True, native=native)
    response = runtime.act([{"action": "focus"}, invalid], "fixture", native=True, screenshot=False)
    assert response["status"] == "refused"
    assert not native.calls


@pytest.mark.parametrize("field", ["amount", "delta"])
def test_native_explicit_integer_wheel(field):
    native = Native()
    runtime = Runtime(allow_native=True, native=native)
    assert (
        runtime.act([{"action": "wheel", field: 120}], "fixture", native=True, screenshot=False)[
            "status"
        ]
        == "ok"
    )
    assert native.calls[0][0]["amount"] == 120


def test_native_text_at_limit_is_accepted():
    native = Native()
    runtime = Runtime(allow_native=True, native=native)
    assert (
        runtime.act(
            [{"action": "text", "text": "x" * 10000}], "fixture", native=True, screenshot=False
        )["status"]
        == "ok"
    )


def test_semantic_scroll_and_limits_prevalidated():
    broker = Broker()
    runtime = Runtime(broker=broker)
    action = {"action": "scroll", "direction": "down", "amount": "large"}
    assert runtime.act([action], "fixture", screenshot=False)["status"] == "ok"
    assert broker.calls[-1]["amount"] == "large"
    assert runtime.window("scroll", "fixture", amount="small")["status"] == "invoked"
    broker.calls.clear()
    for invalid in (
        {**action, "amount": 1},
        {**action, "direction": "diagonal"},
        {"action": "invoke", "depth": 1.5},
        {"action": "invoke", "max_nodes": 5001},
    ):
        response = runtime.act([{"action": "invoke"}, invalid], "fixture", screenshot=False)
        assert response["status"] == "refused"
    assert not broker.calls


def test_play_timing_join_and_prevalidation():
    broker, camera = Broker(), Camera()
    runtime = Runtime(broker=broker, capture=camera)
    actions = [{"action": "invoke", "at": 0}, {"action": "invoke", "at": 0.02}]
    response = runtime.play(actions, "fixture", seconds=0.1)
    assert response["status"] == "ok"
    assert camera.finished
    assert len(broker.calls) == 2
    entries = response["data"]["actions"]["data"]["results"]
    assert entries[1]["started_at"] >= entries[0]["started_at"]
    assert "no strict" in response["data"]["temporal_pairing"]
    camera.calls.clear()
    assert runtime.play(list(reversed(actions)), "fixture")["status"] == "refused"
    assert not camera.calls


def test_status_lazy_close_and_window_names(monkeypatch):
    runtime = Runtime()
    assert runtime.system()["status"] == "ok"
    assert runtime._broker is None
    import minano_computer_use.win32 as win32

    monkeypatch.setattr(win32, "list_windows", lambda: [{"hwnd": 1, "title": "fixture"}])
    assert runtime.window()["data"] == [{"hwnd": 1, "title": "fixture"}]
    assert runtime._broker is None
    broker = Broker()
    runtime = Runtime(broker=broker)
    runtime.window("inspect", "fixture", "Exact Name")
    assert broker.calls[0]["selector"] == "Exact Name"
    runtime.close()
    runtime.close()
    assert broker.closed
    assert runtime.act([{"action": "invoke"}], "fixture", screenshot=False)["status"] == "refused"


def test_process_launch_separate_argv_and_no_shell():
    calls = []

    class Services:
        def process(self, action, argv, **kwargs):
            calls.append((action, argv, kwargs))
            return {"status": "invoked", "data": {"foreground_guaranteed": False}}

    response = Runtime(services=Services()).process(
        "launch", ["program.exe", "argument with spaces"]
    )
    assert response["status"] == "invoked"
    assert response["data"]["foreground_guaranteed"] is False
    assert calls[0][0:2] == ("launch", ["program.exe", "argument with spaces"])
    assert calls[0][2]["cancel_event"] is not None
    assert Runtime().process("launch", "program.exe arg")["status"] in {"error", "refused"}


def test_find_wait_token_and_budget_schemas_end_to_end():
    broker = Broker()
    runtime = Runtime(broker=broker)
    found = runtime.window(
        "find",
        "fixture",
        query="name",
        match="exact",
        limit=2,
        compact=True,
        include_tokens=True,
        max_output_chars=2048,
        max_output_bytes=4096,
    )
    assert found["delivery"] == "read_only"
    assert broker.calls[-1]["query"] == "name"
    assert broker.calls[-1]["max_output_chars"] == 2048
    actions = [
        {"action": "find", "query": "x", "include_tokens": True},
        {"action": "wait_for", "token": "opaque", "condition": {"exists": True}},
        {"action": "invoke", "token": "opaque"},
    ]
    assert runtime.act(actions, "fixture", screenshot=False)["status"] == "ok"
    assert broker.calls[-2]["condition"] == {"exists": True}
    assert broker.calls[-1]["token"] == "opaque"
    count = len(broker.calls)
    for invalid in (
        {"action": "find"},
        {"action": "find", "query": "x", "match": "regex"},
        {"action": "invoke", "token": "opaque", "selector": "x"},
        {"action": "wait_for", "selector": "x", "condition": {"toggle_state": True}},
        {"action": "wait_for", "selector": "x", "condition": {"selected": 1}},
        {"action": "wait_for", "selector": "x", "condition": {"unknown": True}},
        {"action": "wait_for", "selector": "x", "condition": {"value": "x", "exists": True}},
        {"action": "inspect", "max_output_bytes": 100},
    ):
        assert (
            runtime.act([{"action": "invoke"}, invalid], "fixture", screenshot=False)["status"]
            == "refused"
        )
    assert len(broker.calls) == count


def test_cancel_wait_always_stops_and_status_does_not_wait_for_owner():
    broker = Broker()
    runtime = Runtime(broker=broker)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(
            runtime.act,
            [{"action": "wait", "seconds": 30}, {"action": "invoke"}],
            "fixture",
            False,
            False,
            screenshot=False,
        )
        deadline = time.monotonic() + 2
        while runtime.controller.snapshot()["state"] != "active":
            assert time.monotonic() < deadline
            time.sleep(0.001)
        began = time.monotonic()
        assert runtime.system()["data"]["operation"]["state"] == "active"
        accepted = runtime.system("cancel")
        assert time.monotonic() - began < 0.2
        assert accepted["data"]["requested"]
        response = future.result(timeout=2)
    assert response["cancelled"]
    assert response["status"] == "refused"
    assert response["may_have_acted"] is False
    assert response["data"]["attempted"] == 1
    assert response["data"]["stopped_early"]
    assert not broker.calls
    assert runtime.controller.snapshot()["state"] == "idle"
    assert runtime.act([{"action": "invoke"}], "fixture", screenshot=False)["status"] == "ok"


def test_native_cancel_releases_on_owner_and_holds_registration_until_cleanup():
    entered, cleaning, allow_cleanup = threading.Event(), threading.Event(), threading.Event()

    class BlockingNative(Native):
        def execute(self, action, target):
            self.owner = threading.get_ident()
            entered.set()
            assert self.cancel_event.wait(2)
            return {
                "status": "unknown",
                "operation": action["action"],
                "cancelled": True,
                "may_have_acted": True,
            }

        def release_all(self):
            assert threading.get_ident() == self.owner
            cleaning.set()
            assert allow_cleanup.wait(2)
            return super().release_all()

    native = BlockingNative()
    runtime = Runtime(allow_native=True, native=native)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(
            runtime.act,
            [{"action": "key_down", "key": "A"}],
            "fixture",
            True,
            False,
            screenshot=False,
        )
        assert entered.wait(2)
        call_id = runtime.system("cancel")["data"]["call_id"]
        assert cleaning.wait(2)
        assert runtime.controller.snapshot()["call_id"] == call_id
        assert not future.done()
        allow_cleanup.set()
        response = future.result(2)
    assert response["status"] == "unknown"
    assert response["cancelled"]
    assert response["call_id"] == call_id
    assert native.cancel_event is None
    assert native.released == 1


def test_queued_request_cancel_never_cancels_other_active_owner():
    runtime = Runtime()
    request = threading.Event()
    with ThreadPoolExecutor(max_workers=2) as pool:
        owner = pool.submit(runtime.act, [{"action": "wait", "seconds": 0.12}], screenshot=False)
        deadline = time.monotonic() + 2
        while runtime.controller.snapshot()["state"] != "active":
            assert time.monotonic() < deadline
            time.sleep(0.001)
        queued = pool.submit(
            runtime.act,
            [{"action": "wait", "seconds": 30}],
            _request_cancel=request,
            screenshot=False,
        )
        assert not runtime.cancel_request(request)["requested"]
        assert not runtime.controller.snapshot()["cancel_requested"]
        assert owner.result(2)["status"] == "ok"
        denied = queued.result(2)
        assert denied["cancelled"]
        assert denied["status"] == "refused"


def test_screenshot_new_options_and_default_status_does_not_create_store(tmp_path):
    class Camera:
        def screenshot(self, **kwargs):
            self.kwargs = kwargs
            return SimpleNamespace(
                image_bytes=b"png",
                mime_type="image/png",
                metadata={"fallbacks": [], "capture_backend": "injected"},
            )

    camera = Camera()
    runtime = Runtime(capture=camera, output_dir=tmp_path)
    assert runtime.system()["status"] == "ok"
    assert not list(tmp_path.iterdir())
    response = runtime.screenshot(
        "fixture",
        region=[0, 1, 2, 3],
        capture_mode="window",
        allow_fallback=True,
        timeout=1,
    )
    assert response["frame"].image_bytes == b"png"
    assert camera.kwargs["region"] == [0, 1, 2, 3]
    assert camera.kwargs["capture_mode"] == "window"
    assert camera.kwargs["allow_fallback"] is True
    assert camera.kwargs["cancel_event"] is not None
    assert response["delivery"] == "read_only"


def test_trace_redacts_secrets_and_runs_only_after_native_cleanup(tmp_path):
    native = Native()
    runtime = Runtime(allow_native=True, native=native, output_dir=tmp_path, trace_enabled=True)
    response = runtime.act(
        [{"action": "text", "text": "private secret"}], "private title", True, screenshot=False
    )
    assert native.released == 1
    assert runtime._trace.store is runtime._artifact_store
    traces = list((tmp_path / "minano-artifacts").glob("*.jsonl"))
    assert len(traces) == 1
    text = traces[0].read_text(encoding="utf-8")
    assert "private" not in text
    assert json.loads(text)["call_id"] == response["call_id"]
    runtime.close()


def test_broker_typeerror_after_dispatch_is_never_replayed():
    class Broken(Broker):
        def call(self, request, timeout=None, cancel_event=None):
            self.calls.append(request)
            raise TypeError("cancel_event injected failure after mutation")

    broker = Broken()
    response = Runtime(broker=broker).act([{"action": "invoke"}], "fixture", screenshot=False)
    assert response["status"] == "unknown"
    assert response["may_have_acted"]
    assert len(broker.calls) == 1
