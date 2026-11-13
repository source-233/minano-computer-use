from __future__ import annotations

import threading

import pytest

import minano_computer_use.input_context as context_module
from minano_computer_use.input_context import read_input_context


class ContextAdapter:
    def __init__(self):
        self.hwnd = 10
        self.thread_id = 30
        self.creation_time = 134387468188212291

    def foreground(self):
        return self.hwnd

    def window_pid(self, _hwnd):
        return 20

    def window_thread_id(self, _hwnd):
        return self.thread_id

    def window_identity(self, hwnd, pid):
        return {"hwnd": hwnd, "pid": pid, "process_creation_time": self.creation_time}


class SafeContext:
    def focus_hwnd(self, _thread_id):
        return 11

    def keyboard_layout(self, _thread_id):
        return 0x04090409

    def is_ime(self, _hkl):
        return False

    def modifier_down(self, vk):
        return vk == 0x10


def test_context_is_read_only_and_reports_fields():
    adapter = ContextAdapter()
    value = read_input_context(adapter, {"hwnd": 10, "pid": 20}, source=SafeContext())
    assert value["status"] == "partial"
    assert value["non_atomic"] is True
    assert value["fields"]["foreground"]["value"]["matches_target"] is True
    assert value["fields"]["keyboard_layout"]["value"]["language_id"] == 0x0409
    assert value["fields"]["modifiers"]["value"] == {
        "shift": True,
        "ctrl": False,
        "alt": False,
        "win": False,
    }
    assert value["fields"]["ime_open"]["status"] == "unknown"
    assert value["fields"]["ime_conversion"]["status"] == "unknown"


def test_context_cancellation_returns_unknown_without_queries_after_cancel():
    event = threading.Event()
    event.set()
    value = read_input_context(
        ContextAdapter(), {"hwnd": 10, "pid": 20}, source=SafeContext(), cancel_event=event
    )
    assert value["status"] == "unknown"
    assert value["reason"] == "cancelled"
    assert all(field["status"] == "unknown" for field in value["fields"].values())


def test_context_binding_drift_is_unknown():
    adapter = ContextAdapter()
    source = SafeContext()
    original = adapter.foreground
    calls = 0

    def drift():
        nonlocal calls
        calls += 1
        if calls > 1:
            adapter.hwnd = 99
        return original()

    adapter.foreground = drift
    value = read_input_context(adapter, {"hwnd": 10, "pid": 20}, source=source)
    assert value["reason"] == "binding_changed"
    assert all(field["status"] == "unknown" for field in value["fields"].values())


def test_context_strong_identity_is_checked_before_and_after():
    adapter = ContextAdapter()
    calls = []
    original = adapter.window_identity

    def identity(hwnd, pid):
        calls.append((hwnd, pid))
        return original(hwnd, pid)

    adapter.window_identity = identity
    binding = {"hwnd": 10, "pid": 20, "process_creation_time": adapter.creation_time}
    value = read_input_context(adapter, binding, source=SafeContext())
    assert value["status"] == "partial"
    assert calls == [(10, 20), (10, 20)]
    for snapshot in (value["binding_before"], value["binding_after"]):
        assert snapshot["target_identity"] == binding
        assert snapshot["target_thread_id"] == 30
    assert all(
        field["consistency"] == "unknown"
        for field in value["fields"].values()
        if field["status"] == "unknown"
    )


def test_context_exact_creation_mismatch_stops_observation():
    adapter = ContextAdapter()

    class UnusedSource(SafeContext):
        def focus_hwnd(self, _thread_id):
            raise AssertionError("must not query a different process identity")

    # Adjacent 64-bit identity values must not be compared as rounded floats.
    value = read_input_context(
        adapter,
        {"hwnd": 10, "pid": 20, "process_creation_time": adapter.creation_time + 1},
        source=UnusedSource(),
    )
    assert value["reason"] == "binding_changed"
    assert all(field["value"] is None for field in value["fields"].values())


@pytest.mark.parametrize("attribute", ["creation_time", "thread_id"])
def test_context_identity_and_thread_drift_discard_all_fields(attribute):
    adapter = ContextAdapter()
    binding = {"hwnd": 10, "pid": 20, "process_creation_time": adapter.creation_time}

    class DriftSource(SafeContext):
        def focus_hwnd(self, thread_id):
            assert thread_id == 30
            setattr(adapter, attribute, getattr(adapter, attribute) + 1)
            return 11

    value = read_input_context(adapter, binding, source=DriftSource())
    assert value["status"] == "unknown"
    assert value["reason"] == "binding_changed"
    assert all(field["value"] is None for field in value["fields"].values())
    assert all(
        field["consistency"] != "consistent_non_atomic" for field in value["fields"].values()
    )


def test_context_missing_strong_identity_never_certifies_observations():
    adapter = ContextAdapter()
    adapter.creation_time = None
    value = read_input_context(
        adapter,
        {"hwnd": 10, "pid": 20, "process_creation_time": 134387468188212291},
        source=SafeContext(),
    )
    assert value["status"] == "unknown"
    assert value["reason"] == "target_binding_unverified"
    assert all(field["value"] is None for field in value["fields"].values())
    assert all(field["consistency"] == "unknown" for field in value["fields"].values())


def test_context_default_win32_identity_query(monkeypatch):
    from minano_computer_use import win32

    adapter = ContextAdapter()

    class Win32ContextAdapter:
        user32 = object()
        foreground = adapter.foreground
        window_pid = adapter.window_pid
        window_thread_id = adapter.window_thread_id

    calls = []

    def identity(hwnd, pid):
        calls.append((hwnd, pid))
        return adapter.window_identity(hwnd, pid)

    monkeypatch.setattr(win32, "window_identity", identity)
    value = read_input_context(
        Win32ContextAdapter(),
        {"hwnd": 10, "pid": 20, "process_creation_time": adapter.creation_time},
        source=SafeContext(),
    )
    assert value["status"] == "partial"
    assert calls == [(10, 20), (10, 20)]


def test_context_missing_identity_api_cannot_verify_expected_creation():
    class WeakAdapter:
        foreground = ContextAdapter.foreground
        window_pid = ContextAdapter.window_pid
        window_thread_id = ContextAdapter.window_thread_id
        hwnd = 10
        thread_id = 30

    value = read_input_context(
        WeakAdapter(),
        {"hwnd": 10, "pid": 20, "process_creation_time": 134387468188212291},
        source=SafeContext(),
    )
    assert value["status"] == "unknown"
    assert value["reason"] == "target_binding_unverified"
    assert all(field["consistency"] == "unknown" for field in value["fields"].values())


def test_context_identity_api_stale_error_discards_observations():
    from minano_computer_use.win32 import TargetError

    adapter = ContextAdapter()
    calls = 0

    def identity(hwnd, pid):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise TargetError("STALE_TARGET", "changed")
        return {"hwnd": hwnd, "pid": pid, "process_creation_time": adapter.creation_time}

    adapter.window_identity = identity
    value = read_input_context(adapter, {"hwnd": 10, "pid": 20}, source=SafeContext())
    assert value["reason"] == "binding_changed"
    assert all(field["value"] is None for field in value["fields"].values())
    assert all(
        field["consistency"] != "consistent_non_atomic" for field in value["fields"].values()
    )


def test_context_unsupported_adapter_is_unknown():
    value = read_input_context(ContextAdapter(), {"hwnd": 10, "pid": 20})
    assert value["status"] == "unknown"
    assert value["reason"] == "unavailable"
    assert all(field["value"] is None for field in value["fields"].values())


def test_budget_stops_following_queries_and_discards_unverified_values(monkeypatch):
    clock = [0.0]
    monkeypatch.setattr(context_module.time, "monotonic", lambda: clock[0])
    calls = []

    class SlowSource(SafeContext):
        def focus_hwnd(self, thread_id):
            calls.append("focus")
            clock[0] = 1.0
            return super().focus_hwnd(thread_id)

        def keyboard_layout(self, _thread_id):
            raise AssertionError("query after budget exceeded")

    value = read_input_context(ContextAdapter(), {"hwnd": 10, "pid": 20}, source=SlowSource())
    assert calls == ["focus"]
    assert value["reason"] == "budget_exceeded"
    assert all(field["value"] is None for field in value["fields"].values())


def test_cancel_during_observation_stops_queries():
    event = threading.Event()

    class CancelSource(SafeContext):
        def focus_hwnd(self, _thread_id):
            event.set()
            return 11

        def keyboard_layout(self, _thread_id):
            raise AssertionError("query after cancellation")

    value = read_input_context(
        ContextAdapter(), {"hwnd": 10, "pid": 20}, source=CancelSource(), cancel_event=event
    )
    assert value["reason"] == "cancelled"
    assert all(field["value"] is None for field in value["fields"].values())


def test_context_failure_does_not_change_native_success():
    from minano_computer_use.native import NativeInput

    adapter = ContextAdapter()
    adapter.resolve_window = lambda _target: {"hwnd": 10, "pid": 20}
    adapter.send = lambda events: len(events)
    response = NativeInput(enabled=True, adapter=adapter, input_context=object()).execute(
        {"action": "key", "key": "A"}, "target", feedback="detailed"
    )
    assert response["status"] == "invoked"
    assert response["receipt"]["accepted_events"] == 2
    assert response["input_context"]["status"] == "unknown"


@pytest.mark.parametrize(
    "raw,expected", [(0, None), (1, False), (0x8000, True), (-32768, True), (-32767, True)]
)
def test_win32_modifier_zero_is_ambiguous_not_known_up(raw, expected):
    class Function:
        def __call__(self, vk):
            assert vk == 0x10
            return raw

    class User32:
        GetAsyncKeyState = Function()

    class Adapter:
        user32 = User32()

    source = context_module._Win32Context(Adapter())
    assert source.modifier_down(0x10) is expected


def test_ambiguous_modifier_samples_remain_null_without_losing_other_context():
    class Function:
        def __call__(self, _vk):
            return 0

    class User32:
        GetAsyncKeyState = Function()

    class Adapter:
        user32 = User32()

    class AmbiguousSource(SafeContext):
        modifier_down = context_module._Win32Context(Adapter()).modifier_down

    value = read_input_context(ContextAdapter(), {"hwnd": 10, "pid": 20}, source=AmbiguousSource())
    field = value["fields"]["modifiers"]
    assert field["status"] == "unknown"
    assert field["consistency"] == "unknown"
    assert field["reason"] == "ambiguous_or_unavailable"
    assert field["unknown"] == ["shift", "ctrl", "alt", "win"]
    assert field["value"] == dict.fromkeys(("shift", "ctrl", "alt", "win"))
    assert value["fields"]["keyboard_layout"]["status"] == "known"
    assert value["status"] == "partial"


@pytest.mark.parametrize(
    "win_states,expected_win",
    [((None, True), True), ((True, None), True), ((None, False), None), ((False, False), False)],
)
def test_modifier_union_preserves_known_down_and_ambiguous_win_samples(win_states, expected_win):
    class MixedSource(SafeContext):
        def modifier_down(self, vk):
            return {0x10: True, 0x11: False, 0x12: None, 0x5B: win_states[0], 0x5C: win_states[1]}[
                vk
            ]

    value = read_input_context(ContextAdapter(), {"hwnd": 10, "pid": 20}, source=MixedSource())
    assert value["fields"]["modifiers"]["status"] == "partial"
    assert value["fields"]["modifiers"]["consistency"] == "partial_unknown"
    assert value["fields"]["modifiers"]["unknown"] == (
        ["alt", "win"] if expected_win is None else ["alt"]
    )
    assert value["fields"]["modifiers"]["value"] == {
        "shift": True,
        "ctrl": False,
        "alt": None,
        "win": expected_win,
    }
