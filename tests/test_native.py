from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

import minano_computer_use.native as native_module
from minano_computer_use.control import OperationController
from minano_computer_use.native import (
    INPUT_KEYBOARD,
    KEYEVENTF_KEYUP,
    KEYEVENTF_SCANCODE,
    MOUSEEVENTF_LEFTDOWN,
    MOUSEEVENTF_LEFTUP,
    SW_RESTORE,
    SW_SHOW,
    NativeInput,
    _key,
)


class FakeAdapter:
    def __init__(self) -> None:
        self.hwnd = 23
        self.events = []
        self.short = False
        self.activate_calls = 0

    def resolve_window(self, _target: str) -> dict:
        return {"hwnd": 23, "rect": [0, 0, 200, 100]}

    def foreground(self) -> int:
        return self.hwnd

    def send(self, events) -> int:
        self.events.extend(events)
        if self.short:
            self.short = False
            return 0
        return len(events)

    def set_cursor(self, _x: int, _y: int) -> bool:
        return True

    def cursor_position(self):
        return 20, 20

    def activate(self, _hwnd: int) -> bool:
        self.activate_calls += 1
        return True

    def point_hits(self, _x: int, _y: int, _hwnd: int) -> bool:
        return True

    def virtual_bounds(self) -> tuple[int, int, int, int]:
        return 0, 0, 200, 100


def test_disabled_and_focus_is_explicit() -> None:
    adapter = FakeAdapter()
    native = NativeInput(adapter=adapter)
    assert native.execute({"action": "key", "key": "A"}, "target")["status"] == "refused"
    assert adapter.events == []
    native.enabled = True
    assert native.execute({"action": "focus"}, "target")["status"] == "invoked"
    assert adapter.activate_calls == 1


class ForceFocusAdapter(FakeAdapter):
    """Focus API state machine; never touches the real desktop."""

    def __init__(self):
        super().__init__()
        self.hwnd = 99
        self.pid = 123
        self.live = True
        self.minimized = False
        self.calls = []
        self.attach_ok = True
        self.activate_ok = True
        self.activation_effect = True
        self.activation_error = False
        self.threads = {23: 200, 99: 300}

    def resolve_window(self, target):
        return {**super().resolve_window(target), "pid": 123}

    def is_window(self, hwnd):
        assert hwnd == 23
        return self.live

    def window_pid(self, hwnd):
        assert hwnd == 23
        return self.pid if self.live else None

    def is_iconic(self, hwnd):
        assert hwnd == 23
        return self.minimized

    def current_thread_id(self):
        return 100

    def window_thread_id(self, hwnd):
        self.calls.append(("thread", hwnd))
        return self.threads[hwnd]

    def attach_thread_input(self, source, target, attach):
        self.calls.append(("attach", source, target, attach))
        return self.attach_ok

    def show_window(self, hwnd, command):
        self.calls.append(("show", hwnd, command))
        self.minimized = False
        return False  # ShowWindow reports previous visibility, not success.

    def bring_to_top(self, hwnd):
        self.calls.append(("top", hwnd))
        return True

    def activate(self, hwnd):
        self.calls.append(("activate", hwnd))
        self.activate_calls += 1
        if self.activation_error:
            raise OSError("activation raised")
        if self.activation_effect:
            self.hwnd = hwnd
        return self.activate_ok


def test_force_focus_disabled_and_already_foreground_no_reactivation():
    adapter = ForceFocusAdapter()
    native = NativeInput(adapter=adapter)
    assert native.execute({"action": "force_focus"}, "target")["error"]["code"] == "NATIVE_DISABLED"
    assert not adapter.calls
    native.enabled = True
    adapter.hwnd = 23
    response = native.execute({"action": "force_focus"}, "target")
    assert response["status"] == "verified"
    assert response["method"] == "AttachThreadInput+SetForegroundWindow"
    assert response["delivery"] == "native"
    assert response["target_binding"] == {"hwnd": 23, "pid": 123, "process_creation_time": None}
    assert response["no_op"] and response["target_foreground"]
    assert not response["focus_changed"] and not response["may_have_acted"]
    assert not adapter.calls and not adapter.events


def test_force_focus_restores_minimized_then_attaches_and_detaches_in_reverse_order():
    adapter = ForceFocusAdapter()
    adapter.minimized = True
    response = NativeInput(enabled=True, adapter=adapter).execute(
        {"action": "force_focus"}, "target"
    )
    assert response["status"] == "verified"
    assert response["focus_changed"] and response["target_foreground"]
    assert response["may_have_acted"]
    assert adapter.calls == [
        ("show", 23, SW_RESTORE),
        ("thread", 99),
        ("thread", 23),
        ("attach", 100, 300, True),
        ("attach", 100, 200, True),
        ("top", 23),
        ("show", 23, SW_SHOW),
        ("activate", 23),
        ("attach", 100, 200, False),
        ("attach", 100, 300, False),
    ]
    assert not adapter.events


def test_force_focus_activation_exception_still_detaches_and_fails_verification():
    adapter = ForceFocusAdapter()
    adapter.activation_error = True
    response = NativeInput(enabled=True, adapter=adapter).execute(
        {"action": "force_focus"}, "target"
    )
    assert response["status"] == "error"
    assert response["error"]["code"] == "FOCUS_NOT_VERIFIED"
    assert not response["target_foreground"]
    assert not response["focus_changed"]
    assert adapter.calls[-2:] == [("attach", 100, 200, False), ("attach", 100, 300, False)]


@pytest.mark.parametrize("attach_ok", [True, False])
@pytest.mark.parametrize("activation_effect", [True, False])
def test_force_focus_requires_exact_foreground_even_when_attach_or_activate_returns_success(
    attach_ok, activation_effect
):
    adapter = ForceFocusAdapter()
    adapter.attach_ok = attach_ok
    adapter.activation_effect = activation_effect
    response = NativeInput(enabled=True, adapter=adapter).execute(
        {"action": "force_focus"}, "target"
    )
    assert adapter.activate_calls == 1
    assert response["status"] == ("verified" if activation_effect else "error")
    assert response["target_foreground"] is activation_effect
    if not activation_effect:
        assert response["error"]["code"] == "FOCUS_NOT_VERIFIED"
    if not attach_ok:
        assert not any(call[0] == "attach" and call[-1] is False for call in adapter.calls)


@pytest.mark.parametrize("failure", ["gone", "pid_changed"])
def test_force_focus_refuses_stale_target_before_any_activation(failure):
    adapter = ForceFocusAdapter()
    if failure == "gone":
        adapter.live = False
    else:
        adapter.pid = 456
    response = NativeInput(enabled=True, adapter=adapter).execute(
        {"action": "force_focus"}, "target"
    )
    assert response["status"] == "refused"
    assert response["error"]["code"] == "STALE_TARGET"
    assert not response["target_foreground"]
    assert not response["may_have_acted"]
    assert not adapter.calls


def test_force_focus_rechecks_pid_before_final_success():
    class ReplacedAdapter(ForceFocusAdapter):
        def activate(self, hwnd):
            response = super().activate(hwnd)
            self.pid = 456
            return response

    adapter = ReplacedAdapter()
    response = NativeInput(enabled=True, adapter=adapter).execute(
        {"action": "force_focus"}, "target"
    )
    assert response["status"] == "refused"
    assert response["error"]["code"] == "STALE_TARGET"
    assert not response["target_foreground"]
    assert response["may_have_acted"]
    assert adapter.calls[-2:] == [("attach", 100, 200, False), ("attach", 100, 300, False)]


def test_force_focus_foreground_but_minimized_is_restored_not_no_op():
    adapter = ForceFocusAdapter()
    adapter.hwnd = 23
    adapter.minimized = True
    response = NativeInput(enabled=True, adapter=adapter).execute(
        {"action": "force_focus"}, "target"
    )
    assert response["status"] == "verified"
    assert not response.get("no_op")
    assert not response["focus_changed"]
    assert adapter.calls[0] == ("show", 23, SW_RESTORE)
    assert adapter.activate_calls == 1


def test_force_focus_target_disappears_after_attach_detaches_without_activation():
    class GoneAdapter(ForceFocusAdapter):
        def attach_thread_input(self, source, target, attach):
            response = super().attach_thread_input(source, target, attach)
            if attach:
                self.live = False
            return response

    adapter = GoneAdapter()
    response = NativeInput(enabled=True, adapter=adapter).execute(
        {"action": "force_focus"}, "target"
    )
    assert response["status"] == "refused"
    assert response["error"]["code"] == "STALE_TARGET"
    assert not adapter.activate_calls
    assert adapter.calls[-2:] == [("attach", 100, 200, False), ("attach", 100, 300, False)]


def test_force_focus_attach_exceptions_are_best_effort_and_exact_foreground_is_required():
    class AttachErrorAdapter(ForceFocusAdapter):
        def attach_thread_input(self, source, target, attach):
            super().attach_thread_input(source, target, attach)
            raise OSError("attach failed")

    adapter = AttachErrorAdapter()
    response = NativeInput(enabled=True, adapter=adapter).execute(
        {"action": "force_focus"}, "target"
    )
    assert response["status"] == "verified"
    assert adapter.activate_calls == 1
    adapter.hwnd = 99
    adapter.activation_effect = False
    response = NativeInput(enabled=True, adapter=adapter).execute(
        {"action": "force_focus"}, "target"
    )
    assert response["status"] == "error"
    assert response["error"]["code"] == "FOCUS_NOT_VERIFIED"


def test_force_focus_cancellation_during_attachment_always_detaches():
    event = threading.Event()

    class CancelAdapter(ForceFocusAdapter):
        def attach_thread_input(self, source, target, attach):
            response = super().attach_thread_input(source, target, attach)
            if attach:
                event.set()
            return response

    adapter = CancelAdapter()
    native = NativeInput(enabled=True, adapter=adapter)
    native.set_cancel_event(event)
    response = native.execute({"action": "force_focus"}, "target")
    assert response["cancelled"] and response["error"]["code"] == "CANCELLED"
    assert adapter.calls[-1] == ("attach", 100, 300, False)
    assert not adapter.activate_calls


@pytest.mark.parametrize(
    "foreground,target_thread,foreground_thread,expected",
    [(0, 200, 300, [200]), (99, 100, 300, [300]), (99, 200, 200, [200])],
)
def test_force_focus_skips_null_foreground_self_and_duplicate_threads(
    foreground, target_thread, foreground_thread, expected
):
    adapter = ForceFocusAdapter()
    adapter.hwnd = foreground
    adapter.threads = {23: target_thread, 99: foreground_thread}
    adapter.attach_ok = False  # Failed attaches must also be deduplicated.
    response = NativeInput(enabled=True, adapter=adapter).execute(
        {"action": "force_focus"}, "target"
    )
    assert response["status"] == "verified"
    assert [call[2] for call in adapter.calls if call[0] == "attach"] == expected
    assert ("thread", 0) not in adapter.calls


def test_key_chord_sends_keyups_and_short_write_is_unknown() -> None:
    adapter = FakeAdapter()
    native = NativeInput(enabled=True, adapter=adapter)
    result = native.execute({"action": "key", "key": "CTRL+A"}, "target")
    assert result["status"] == "invoked"
    assert len(adapter.events) == 4
    assert all(event.type == INPUT_KEYBOARD for event in adapter.events)
    assert all(event.ki.dwFlags & KEYEVENTF_KEYUP for event in adapter.events[2:])
    adapter.short = True
    result = native.execute({"action": "key_down", "key": "B"}, "target")
    assert result["status"] == "unknown"
    assert any(event.ki.dwFlags & KEYEVENTF_KEYUP for event in adapter.events)


def test_lost_foreground_prevents_input_and_click_bounds() -> None:
    adapter = FakeAdapter()
    native = NativeInput(enabled=True, adapter=adapter)
    adapter.hwnd = 99
    assert native.execute({"action": "key", "key": "A"}, "target")["status"] == "refused"
    adapter.hwnd = 23
    assert native.execute({"action": "click", "x": 250, "y": 30}, "target")["status"] == "refused"
    assert adapter.events == []


class ClickClock:
    """Advance native waits deterministically without touching the desktop."""

    def __init__(self):
        self.now = 0.0
        self.waits = []
        self.after_sleep = lambda: None

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.waits.append((self.now, seconds))
        self.now += seconds
        self.after_sleep()


class ClickEvent(threading.Event):
    def __init__(self, clock):
        super().__init__()
        self.clock = clock
        self.wait_calls = 0

    def wait(self, timeout=None):
        self.wait_calls += 1
        if not self.is_set():
            self.clock.sleep(timeout)
        return self.is_set()


class TimedClickAdapter(FakeAdapter):
    def __init__(self, clock):
        super().__init__()
        self.clock = clock
        self.timeline = []
        self.covered = False
        self.after_send = lambda: None

    def foreground(self):
        self.timeline.append(("foreground", self.clock.now))
        return super().foreground()

    def point_hits(self, x, y, hwnd):
        self.timeline.append(("point", self.clock.now))
        return not self.covered

    def set_cursor(self, x, y):
        self.timeline.append(("cursor", self.clock.now))
        return super().set_cursor(x, y)

    def send(self, events):
        flags = events[0].mi.dwFlags
        name = "down" if flags & MOUSEEVENTF_LEFTDOWN else "up"
        self.timeline.append((name, self.clock.now))
        sent = super().send(events)
        self.after_send()
        return sent


@pytest.fixture
def timed_click(monkeypatch):
    clock = ClickClock()
    monkeypatch.setattr(native_module, "time", clock)
    adapter = TimedClickAdapter(clock)
    native = NativeInput(enabled=True, adapter=adapter)
    return native, adapter, clock


@pytest.mark.parametrize("with_cancel_event", [False, True])
def test_click_settles_then_dwells_with_target_and_foreground_checks(
    timed_click, with_cancel_event
):
    native, adapter, clock = timed_click
    event = ClickEvent(clock)
    if with_cancel_event:
        native.set_cancel_event(event)
    response = native.execute({"action": "click", "x": 20, "y": 20}, "target")
    assert response["status"] == "invoked"
    effects = [(name, at) for name, at in adapter.timeline if name in {"cursor", "down", "up"}]
    assert [name for name, _ in effects] == ["cursor", "down", "up"]
    assert effects[1][1] - effects[0][1] == pytest.approx(0.020)
    assert effects[2][1] - effects[1][1] == pytest.approx(0.030)
    assert [at for name, at in adapter.timeline if name == "point"] == [0, 0.020]
    checks = [at for name, at in adapter.timeline if name == "foreground"]
    assert all(
        any(at == pytest.approx(end) for at in checks) for end in (0, 0.01, 0.02, 0.03, 0.04, 0.05)
    )
    assert clock.waits and all(0 < seconds <= 0.010 for _, seconds in clock.waits)
    assert bool(event.wait_calls) is with_cancel_event
    assert not native._held_mouse and not native._cleanup_required


def test_move_does_not_add_click_waits(timed_click):
    native, adapter, clock = timed_click
    response = native.execute({"action": "move", "x": 20, "y": 20}, "target")
    assert response["status"] == "invoked"
    assert not clock.waits and not adapter.events


@pytest.mark.parametrize("failure", ["cancel", "foreground", "covered"])
def test_click_settle_revalidation_never_sends_down_after_change(timed_click, failure):
    native, adapter, clock = timed_click
    event = ClickEvent(clock)
    native.set_cancel_event(event)

    def interrupt():
        if failure == "cancel":
            event.set()
        elif failure == "foreground":
            adapter.hwnd = 99
        else:
            adapter.covered = True

    clock.after_sleep = interrupt
    response = native.execute({"action": "click", "x": 20, "y": 20}, "target")
    assert response["status"] == "unknown"
    assert response["may_have_acted"] is True  # The cursor already moved.
    assert (
        response["error"]["code"]
        == {
            "cancel": "CANCELLED",
            "foreground": "FOREGROUND_LOST",
            "covered": "INVALID_ACTION",
        }[failure]
    )
    assert not adapter.events and not native._held_mouse
    assert clock.now == pytest.approx(0.020 if failure == "covered" else 0.010)


@pytest.mark.parametrize("failure", ["cancel", "foreground", "exception"])
@pytest.mark.parametrize("at", [0.030, 0.050])
def test_click_dwell_interruption_releases_on_owner_thread(timed_click, failure, at):
    native, adapter, clock = timed_click
    event = ClickEvent(clock)
    native.set_cancel_event(event)
    release_calls = []
    release_all = native.release_all

    def release():
        release_calls.append(threading.get_ident())
        return release_all()

    native.release_all = release

    def interrupt():
        if clock.now < at:
            return
        adapter.hwnd = 99  # Cleanup must bypass foreground in every case.
        if failure == "cancel":
            event.set()
        elif failure == "exception":
            raise OSError("wait failed")

    clock.after_sleep = interrupt
    response = native.execute({"action": "click", "x": 20, "y": 20}, "target")
    assert response["status"] == "unknown" and response["may_have_acted"] is True
    assert (
        response["error"]["code"]
        == {
            "cancel": "CANCELLED",
            "foreground": "FOREGROUND_LOST",
            "exception": "INPUT_FAILED",
        }[failure]
    )
    assert release_calls and set(release_calls) == {threading.get_ident()}
    assert [event.mi.dwFlags for event in adapter.events] == [
        MOUSEEVENTF_LEFTDOWN,
        MOUSEEVENTF_LEFTUP,
    ]
    assert clock.now == pytest.approx(at)
    assert not native._held_mouse and not native._cleanup_required


@pytest.mark.parametrize("fail_up", [False, True])
@pytest.mark.parametrize("failure", ["short", "exception"])
def test_click_uncertain_send_tracks_down_and_releases_without_foreground(
    timed_click, fail_up, failure
):
    native, adapter, clock = timed_click
    send = adapter.send
    failed = False

    def fail_once(events):
        nonlocal failed
        assert native._held_mouse  # Recorded before an uncertain down or up.
        is_up = bool(events[0].mi.dwFlags & MOUSEEVENTF_LEFTUP)
        if is_up == fail_up and not failed:
            failed = True
            adapter.hwnd = 99
            adapter.short = failure == "short"
            sent = send(events)
            if failure == "exception":
                raise OSError("injection failed")
            return sent
        return send(events)

    adapter.send = fail_once
    response = native.execute({"action": "click", "x": 20, "y": 20}, "target")
    assert response["status"] == "unknown"
    assert response["error"]["code"] == "INPUT_FAILED"
    assert response["may_have_acted"] is True
    assert [event.mi.dwFlags for event in adapter.events] == [
        MOUSEEVENTF_LEFTDOWN,
        *([MOUSEEVENTF_LEFTUP] if fail_up else []),
        MOUSEEVENTF_LEFTUP,
    ]
    assert clock.now == pytest.approx(0.050 if fail_up else 0.020)
    assert not native._held_mouse and not native._cleanup_required


def test_invalid_keys_and_non_integer_coordinates_refused() -> None:
    native = NativeInput(enabled=True, adapter=FakeAdapter())
    assert native.execute({"action": "key", "key": "界"}, "target")["status"] == "refused"
    assert native.execute({"action": "move", "x": 1.5, "y": 2}, "target")["status"] == "refused"
    assert (
        native.execute({"action": "hold", "key": "A", "seconds": float("nan")}, "target")["status"]
        == "refused"
    )


class BarrierAdapter(FakeAdapter):
    """Pause the first down event; never call Win32."""

    def __init__(self):
        super().__init__()
        self.down_entered = threading.Event()
        self.resume = threading.Event()
        self.threads = []

    def send(self, events):
        self.threads.append(threading.get_ident())
        sent = super().send(events)
        if len(self.events) == 1:
            self.down_entered.set()
            assert self.resume.wait(2), "test failed to release adapter barrier"
        return sent


@pytest.mark.parametrize(
    "action",
    [
        {"action": "hold", "key": "A", "seconds": 10},
        {"action": "key", "key": "CTRL+A"},
        {"action": "text", "text": "AB"},
        {"action": "click", "x": 20, "y": 20},
    ],
)
def test_cancellation_only_releases_owned_holds_on_owner_thread(action) -> None:
    adapter = BarrierAdapter()
    controller = OperationController()
    native = NativeInput(enabled=True, adapter=adapter)

    def owner():
        with controller.operation() as operation:
            native.set_cancel_event(operation.event)
            response = native.execute(action, "target")
            native.set_cancel_event(None)
            return response, threading.get_ident()

    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(owner)
        assert adapter.down_entered.wait(2)
        requested = controller.cancel()
        assert requested["requested"] is True
        # Request acceptance is not proof that keys were released.
        assert len(adapter.events) == 1
        assert controller.snapshot()["call_id"] == requested["call_id"]
        adapter.hwnd = 99  # Cleanup bypasses foreground as well as cancellation.
        adapter.resume.set()
        response, owner_id = future.result(timeout=2)
    assert response["status"] == "unknown"
    assert response["error"]["code"] == "CANCELLED"
    assert response["cancelled"] is True
    assert response["may_have_acted"] is True
    assert len(adapter.events) == 2
    up = adapter.events[-1]
    assert (
        up.ki.dwFlags & KEYEVENTF_KEYUP
        if up.type == INPUT_KEYBOARD
        else up.mi.dwFlags & MOUSEEVENTF_LEFTUP
    )
    assert set(adapter.threads) == {owner_id}


def test_pre_cancelled_input_is_refused_without_any_adapter_events() -> None:
    adapter = FakeAdapter()
    native = NativeInput(enabled=True, adapter=adapter)
    event = threading.Event()
    event.set()
    native.set_cancel_event(event)
    for action in ({"action": "key", "key": "A"}, {"action": "focus"}):
        response = native.execute(action, "target")
        assert response["status"] == "refused"
        assert response["error"]["code"] == "CANCELLED"
        assert response["cancelled"] is True
        assert response["may_have_acted"] is False
    assert adapter.events == []
    assert adapter.activate_calls == 0
    native.set_cancel_event(None)
    assert native.execute({"action": "key", "key": "A"}, "target")["status"] == "invoked"


class FailedReleaseAdapter(FakeAdapter):
    def __init__(self):
        super().__init__()
        self.fail_releases = True

    def send(self, events):
        super().send(events)
        if self.fail_releases and events[0].ki.dwFlags & KEYEVENTF_KEYUP:
            return 0
        return len(events)


def test_failed_cleanup_fault_gates_new_actions_until_release_all_succeeds() -> None:
    adapter = FailedReleaseAdapter()
    native = NativeInput(enabled=True, adapter=adapter)
    response = native.execute({"action": "key", "key": "A"}, "target")
    assert response["status"] == "unknown"
    count = len(adapter.events)
    for action in ({"action": "key_down", "key": "B"}, {"action": "focus"}):
        assert native.execute(action, "target")["error"]["code"] == "CLEANUP_REQUIRED"
    assert len(adapter.events) == count
    assert native.release_all()["error"]["code"] == "CLEANUP_FAILED"
    adapter.fail_releases = False
    assert native.release_all()["status"] == "invoked"
    assert native.execute({"action": "key", "key": "B"}, "target")["status"] == "invoked"


def test_cancelled_cleanup_failure_is_reported_and_still_fault_gated() -> None:
    adapter = FailedReleaseAdapter()
    native = NativeInput(enabled=True, adapter=adapter)
    event = threading.Event()
    native.set_cancel_event(event)
    assert native.execute({"action": "key_down", "key": "A"}, "target")["status"] == "invoked"
    event.set()
    response = native.execute({"action": "key", "key": "B"}, "target")
    assert response["error"]["code"] == "CANCELLED"
    assert response["cancelled"] is True
    assert response["may_have_acted"] is True
    assert response["cleanup"]["error"]["code"] == "CLEANUP_FAILED"
    native.set_cancel_event(None)
    assert native.execute({"action": "key", "key": "C"}, "target")["error"]["code"] == (
        "CLEANUP_REQUIRED"
    )
    adapter.fail_releases = False
    native.release_all()


def test_release_all_does_not_inject_from_non_owner_thread() -> None:
    adapter = FakeAdapter()
    native = NativeInput(enabled=True, adapter=adapter)
    native.execute({"action": "key_down", "key": "A"}, "target")
    with ThreadPoolExecutor(max_workers=1) as pool:
        response = pool.submit(native.release_all).result(timeout=2)
    assert response["error"]["code"] == "CLEANUP_FAILED"
    assert len(adapter.events) == 1
    assert native.release_all()["status"] == "invoked"
    assert len(adapter.events) == 2


def test_foreground_rechecked_between_chord_events_releases_only_attempted_keys() -> None:
    class ForegroundAdapter(FakeAdapter):
        def send(self, events):
            sent = super().send(events)
            self.hwnd = 99
            return sent

    adapter = ForegroundAdapter()
    response = NativeInput(enabled=True, adapter=adapter).execute(
        {"action": "key", "key": "CTRL+A"}, "target"
    )
    assert response["error"]["code"] == "FOREGROUND_LOST"
    assert len(adapter.events) == 2
    assert [event.ki.wVk for event in adapter.events] == [0x11, 0x11]
    assert adapter.events[1].ki.dwFlags & KEYEVENTF_KEYUP


def test_key_whitespace_normalized_and_duplicate_chords_refused() -> None:
    adapter = FakeAdapter()
    native = NativeInput(enabled=True, adapter=adapter)
    assert native.execute({"action": "key", "key": " ctrl + a "}, "target")["status"] == ("invoked")
    count = len(adapter.events)
    response = native.execute({"action": "key", "key": ["CTRL", "CONTROL"]}, "target")
    assert response["error"]["code"] == "INVALID_ACTION"
    assert len(adapter.events) == count


def test_scancode_key_events_release_original_virtual_key_not_zero() -> None:
    class ScancodeAdapter(FakeAdapter):
        def key_event(self, vk, up=False):
            event = _key(0, up)
            event.ki.wScan = vk
            event.ki.dwFlags |= KEYEVENTF_SCANCODE
            return event

    adapter = ScancodeAdapter()
    native = NativeInput(enabled=True, adapter=adapter)
    assert native.execute({"action": "key_down", "key": "A"}, "target")["status"] == "invoked"
    assert native.release_all()["status"] == "invoked"
    assert len(adapter.events) == 2
    assert adapter.events[-1].ki.wScan == ord("A")
    assert adapter.events[-1].ki.dwFlags & KEYEVENTF_KEYUP


def test_cancel_wakes_hold_wait_without_waiting_for_hold_deadline() -> None:
    class WaitingEvent(threading.Event):
        def __init__(self):
            super().__init__()
            self.waiting = threading.Event()
            self.wait_calls = 0

        def wait(self, timeout=None):
            self.wait_calls += 1
            self.waiting.set()
            return super().wait(timeout)

    event = WaitingEvent()
    adapter = FakeAdapter()
    native = NativeInput(enabled=True, adapter=adapter)
    native.set_cancel_event(event)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(
            native.execute, {"action": "hold", "key": "A", "seconds": 10}, "target"
        )
        assert event.waiting.wait(2)
        event.set()
        response = future.result(timeout=2)
    assert response["error"]["code"] == "CANCELLED"
    assert event.wait_calls >= 1
    assert len(adapter.events) == 2
    assert adapter.events[-1].ki.dwFlags & KEYEVENTF_KEYUP


def test_release_all_and_close_ignore_cancel_foreground_and_disabled_input() -> None:
    adapter = FakeAdapter()
    native = NativeInput(enabled=True, adapter=adapter)
    native.execute({"action": "key_down", "key": "A"}, "target")
    event = threading.Event()
    event.set()
    native.set_cancel_event(event)
    native.enabled = False
    adapter.hwnd = 99
    native.close()
    assert len(adapter.events) == 2
    assert adapter.events[-1].ki.wVk == ord("A")
    assert adapter.events[-1].ki.dwFlags & KEYEVENTF_KEYUP
    native.enabled = True
    assert native.execute({"action": "key", "key": "B"}, "target")["error"]["code"] == ("CLOSED")


@pytest.mark.parametrize(
    "button,down,up,data",
    [
        ("left", 2, 4, 0),
        ("right", 8, 16, 0),
        ("middle", 32, 64, 0),
        ("x1", 128, 256, 1),
        ("x2", 128, 256, 2),
    ],
)
def test_every_button_repeats_and_modifiers(timed_click, button, down, up, data):
    native, adapter, _ = timed_click
    response = native.execute(
        {
            "action": "click",
            "button": button,
            "clicks": 3,
            "x": 20,
            "y": 20,
            "modifiers": "ctrl+shift",
        },
        "target",
    )
    assert response["status"] == "invoked"
    mouse = [event for event in adapter.events if event.type == 0]
    assert [event.mi.dwFlags for event in mouse] == [down, up] * 3
    assert [event.mi.mouseData for event in mouse] == [data] * 6
    assert not native._held_mouse and not native._held_keys


def test_multibutton_cleanup_releases_all_owned_buttons():
    adapter = FakeAdapter()
    native = NativeInput(enabled=True, adapter=adapter)
    for button in ["left", "right", "middle", "x1", "x2"]:
        assert (
            native.execute({"action": "mouse_down", "button": button}, "target")["status"]
            == "invoked"
        )
    assert len(native._held_buttons) == 5
    adapter.hwnd = 99
    assert native.release_all()["data"]["released"] == 5
    assert not native._held_buttons


def test_current_pointer_click_and_mousedown_validate_exact_target(timed_click):
    native, adapter, _ = timed_click
    assert native.execute({"action": "click"}, "target")["status"] == "invoked"
    adapter.covered = True
    count = len(adapter.events)
    assert native.execute({"action": "click"}, "target")["status"] == "refused"
    assert native.execute({"action": "mouse_down"}, "target")["status"] == "refused"
    assert len(adapter.events) == count


def test_drag_total_duration_and_button_cleanup(timed_click):
    native, adapter, clock = timed_click
    response = native.execute(
        {
            "action": "drag",
            "x": 10,
            "y": 20,
            "path": [[20, 30], [40, 50]],
            "seconds": 0.2,
            "button": "right",
            "press_dwell_ms": 40,
        },
        "target",
    )
    assert response["status"] == "invoked"
    assert clock.now == pytest.approx(0.2)
    assert [event.mi.dwFlags for event in adapter.events] == [8, 16]
    assert not native._held_buttons


def test_drag_cancel_releases_all_buttons(timed_click):
    native, adapter, clock = timed_click
    native.execute({"action": "mouse_down", "button": "left"}, "target")
    event = ClickEvent(clock)
    native.set_cancel_event(event)
    clock.after_sleep = event.set
    response = native.execute(
        {"action": "drag", "x": 10, "y": 20, "to": [40, 50], "button": "x2", "seconds": 0.2},
        "target",
    )
    assert response["cancelled"]
    assert not native._held_buttons
    assert [event.mi.dwFlags for event in adapter.events] == [2, 128, 256, 4]


def test_horizontal_scroll_alias_raw_and_legacy_units():
    adapter = FakeAdapter()
    native = NativeInput(enabled=True, adapter=adapter)
    native.execute({"action": "scroll", "scroll_amount": 3, "scroll_direction": "left"}, "target")
    assert adapter.events[-1].mi.dwFlags == 0x1000
    assert adapter.events[-1].mi.mouseData == (-360 & 0xFFFFFFFF)
    native.execute({"action": "wheel", "delta": 7}, "target")
    assert adapter.events[-1].mi.mouseData == 7


def test_chord_down_up_and_not_held():
    adapter = FakeAdapter()
    native = NativeInput(enabled=True, adapter=adapter)
    assert native.execute({"action": "key_down", "keys": "ctrl+w"}, "target")["status"] == "invoked"
    assert native.execute({"action": "key_up", "keys": "ctrl+w"}, "target")["status"] == "invoked"
    assert [event.ki.wVk for event in adapter.events] == [17, 87, 87, 17]
    assert native.execute({"action": "key_up", "key": "A"}, "target")["error"]["code"] == "NOT_HELD"


def test_text_controls_and_literal_unicode_are_explicit():
    adapter = FakeAdapter()
    native = NativeInput(enabled=True, adapter=adapter)
    assert native.execute({"action": "text", "text": "\r\n\t"}, "target")["status"] == "invoked"
    assert [event.ki.wVk for event in adapter.events] == [13, 13, 9, 9]
    adapter.events.clear()
    native.execute({"action": "text", "text": "\n\t", "literal": True}, "target")
    assert [event.ki.wScan for event in adapter.events] == [10, 10, 9, 9]
    assert all(event.ki.dwFlags & native_module.KEYEVENTF_UNICODE for event in adapter.events)


class MessageAdapter(FakeAdapter):
    def __init__(self, process="chrome.exe"):
        super().__init__()
        self.process = process
        self.children = [24]
        self.messages = []
        self.pids = {23: 123, 24: 456}
        self.post_ok = True
        self.after_post = lambda: None

    def resolve_window(self, target):
        return {**super().resolve_window(target), "pid": 123, "process": self.process}

    def render_children(self, hwnd):
        assert hwnd == 23
        return self.children

    def window_pid(self, hwnd):
        return self.pids.get(hwnd)

    def is_window(self, hwnd):
        return hwnd in self.pids

    def map_key(self, vk):
        return 0x4D if vk == 0x27 else vk

    def post_message(self, hwnd, msg, wparam, lparam):
        self.messages.append((hwnd, msg, wparam, lparam))
        self.after_post()
        return self.post_ok


def test_message_target_unique_browser_and_lparam_bits():
    adapter = MessageAdapter()
    native = NativeInput(enabled=True, adapter=adapter)
    response = native.execute({"action": "key", "key": "right"}, "target", key_method="auto")
    assert response["status"] == "invoked" and response["method"] == "PostMessage"
    assert response["consumption_verified"] is False
    assert adapter.messages == [
        (24, 0x100, 39, 1 | 0x4D << 16 | 1 << 24),
        (24, 0x101, 39, 1 | 0x4D << 16 | 1 << 24 | 3 << 30),
    ]
    assert not adapter.events


@pytest.mark.parametrize("children", [[], [24, 25], [24, 24]])
def test_message_refuses_ambiguous_or_absent_browser_child_without_fallback(children):
    adapter = MessageAdapter()
    adapter.children = children
    native = NativeInput(enabled=True, adapter=adapter)
    response = native.execute({"action": "key", "key": "A"}, "target", key_method="message")
    assert response["status"] == "refused"
    assert not adapter.events and not adapter.messages


def test_auto_nonbrowser_scancode_and_explicit_message_exact_hwnd():
    adapter = MessageAdapter(process="notepad.exe")
    native = NativeInput(enabled=True, adapter=adapter)
    native.execute({"action": "key", "key": "A"}, "target", key_method="auto")
    assert len(adapter.events) == 2 and not adapter.messages
    native.execute({"action": "key", "key": "A"}, "target", key_method="message")
    assert [message[0] for message in adapter.messages] == [23, 23]


def test_message_false_return_faultgates_and_never_falls_back():
    adapter = MessageAdapter()
    adapter.post_ok = False
    native = NativeInput(enabled=True, adapter=adapter)
    response = native.execute({"action": "key", "key": "A"}, "target", key_method="message")
    assert response["status"] == "unknown" and not adapter.events
    assert native.execute({"action": "focus"}, "target")["error"]["code"] == "CLEANUP_REQUIRED"
    adapter.post_ok = True
    assert native.release_all()["status"] == "invoked"


@pytest.mark.parametrize("change", ["foreground", "root_pid", "child_pid"])
def test_message_each_event_rechecks_foreground_and_identity(change):
    adapter = MessageAdapter()
    native = NativeInput(enabled=True, adapter=adapter)

    def after_post():
        if change == "foreground":
            adapter.hwnd = 99
        elif change == "root_pid":
            adapter.pids[23] = 999
        else:
            adapter.pids[24] = 999

    adapter.after_post = after_post
    response = native.execute({"action": "key", "key": "ctrl+a"}, "target", key_method="message")
    assert response["status"] == "unknown"
    assert all(message[2] == 17 for message in adapter.messages)
    assert not adapter.events


def test_injection_rechecks_pid_before_each_event():
    adapter = MessageAdapter()
    native = NativeInput(enabled=True, adapter=adapter)
    send = adapter.send

    def replace(events):
        sent = send(events)
        adapter.pids[23] = 999
        return sent

    adapter.send = replace
    response = native.execute({"action": "key", "key": "ctrl+a"}, "target")
    assert response["error"]["code"] == "FOREGROUND_LOST"
    assert [event.ki.wVk for event in adapter.events] == [17, 17]


def test_clipboard_session_is_injected_restored_and_never_recursive():
    from contextlib import contextmanager

    adapter = FakeAdapter()
    calls = []

    class Services:
        @contextmanager
        def clipboard_session(self, text, cancel_event=None, deadline=None):
            calls.append(("enter", text, cancel_event))
            try:
                yield {"sequence": 10}
            finally:
                calls.append(("exit",))

    native = NativeInput(enabled=True, adapter=adapter, services=Services())
    assert native.execute({"action": "paste", "text": "private"}, "target")["status"] == "invoked"
    assert calls == [("enter", "private", None), ("exit",)]
    assert [event.ki.wVk for event in adapter.events] == [17, 86, 86, 17]


def test_paste_cancel_after_clipboard_write_restores_and_sends_no_keys():
    from contextlib import contextmanager

    event = threading.Event()
    calls = []

    class Services:
        @contextmanager
        def clipboard_session(self, text, cancel_event=None, deadline=None):
            assert cancel_event is event
            event.set()
            try:
                yield object()
            finally:
                calls.append("restored")

    adapter = FakeAdapter()
    native = NativeInput(enabled=True, adapter=adapter, services=Services())
    native.set_cancel_event(event)
    response = native.execute({"action": "paste", "text": "private"}, "target")
    assert response["cancelled"] and calls == ["restored"] and not adapter.events


def test_paste_service_error_does_not_disclose_clipboard():
    class Services:
        def clipboard_session(self, text, **kwargs):
            raise RuntimeError("secret clipboard contents")

    native = NativeInput(enabled=True, adapter=FakeAdapter(), services=Services())
    response = native.execute({"action": "paste", "text": "private"}, "target")
    assert "secret" not in str(response) and "private" not in str(response)


@pytest.mark.parametrize(
    "action",
    [
        {"action": "right_click"},
        {"action": "mouse_down"},
        {"action": "paste", "text": "hello"},
        {"action": "key_down", "key": "CTRL+A"},
        {"action": "drag", "x": 1, "y": 2, "to": [3, 4]},
    ],
)
def test_aliases_and_new_actions_cannot_bypass_disabled_guard(action):
    adapter = MessageAdapter()
    response = NativeInput(adapter=adapter).execute(action, "target", key_method="message")
    assert response["error"]["code"] == "NATIVE_DISABLED"
    assert not adapter.messages and not adapter.events


def test_drag_interpolates_at_bounded_intervals_without_multiplying_total(timed_click):
    native, adapter, clock = timed_click
    positions = []
    adapter.set_cursor = lambda x, y: positions.append((x, y)) or True
    response = native.execute(
        {"action": "drag", "x": 10, "y": 10, "to": [110, 10], "seconds": 0.13}, "target"
    )
    assert response["status"] == "invoked"
    assert positions[0] == (10, 10) and positions[-1] == (110, 10)
    assert len(positions) >= 10
    assert all(x < next_x for (x, _), (next_x, _) in zip(positions, positions[1:], strict=False))
    assert clock.now == pytest.approx(0.13)


def test_message_text_rechecks_child_pid_between_characters():
    adapter = MessageAdapter()
    native = NativeInput(enabled=True, adapter=adapter)
    adapter.after_post = lambda: adapter.pids.update({24: 999})
    response = native.execute({"action": "text", "text": "ab"}, "target", key_method="auto")
    assert response["error"]["code"] == "FOREGROUND_LOST"
    assert len(adapter.messages) == 1
    assert adapter.messages[0][1:3] == (0x102, 97)


def test_browser_identity_lookup_uses_adapter_not_title_guessing():
    adapter = MessageAdapter()
    adapter.process = ""
    adapter.process_name = lambda pid: "chrome.exe"
    native = NativeInput(enabled=True, adapter=adapter)
    assert (
        native.execute({"action": "key", "key": "A"}, "target", key_method="auto")["method"]
        == "PostMessage"
    )
    assert not adapter.events


def test_invalid_action_or_target_error_releases_existing_holds():
    adapter = FakeAdapter()
    native = NativeInput(enabled=True, adapter=adapter)
    native.execute({"action": "mouse_down", "button": "right"}, "target")
    native.execute({"action": "key_down", "key": "shift"}, "target")
    assert native.execute({"action": "key", "key": "not-a-key"}, "target")["status"] == "refused"
    assert not native._held_buttons and not native._held_keys
    native.execute({"action": "mouse_down", "button": "x1"}, "target")
    adapter.hwnd = 99
    native.execute({"action": "key", "key": "A"}, "target")
    assert not native._held_buttons


def test_exact_old_payloads_execute_on_fake_adapter(timed_click):
    native, adapter, _ = timed_click
    assert native.execute({"action": "key", "text": "ctrl+s"}, "target")["status"] == "invoked"
    assert (
        native.execute({"action": "hold_key", "text": "ctrl+s", "duration": 0.01}, "target")[
            "status"
        ]
        == "invoked"
    )
    assert (
        native.execute({"action": "left_click", "coordinate": [10, 20]}, "target")["status"]
        == "invoked"
    )
    assert (
        native.execute(
            {
                "action": "drag",
                "coordinate": [10, 20],
                "to": {"coordinate": [30, 40]},
                "duration": 0.1,
            },
            "target",
        )["status"]
        == "invoked"
    )
    assert (
        native.execute({"action": "scroll", "amount": 2, "direction": "right"}, "target")["status"]
        == "invoked"
    )
    assert adapter.events[-1].mi.dwFlags == 0x1000
    assert adapter.events[-1].mi.mouseData == 240


def test_printscreen_message_has_extended_bit():
    adapter = MessageAdapter()
    native = NativeInput(enabled=True, adapter=adapter)
    assert (
        native.execute({"action": "key", "text": "printscreen"}, "target", key_method="message")[
            "status"
        ]
        == "invoked"
    )
    assert all(message[3] & (1 << 24) for message in adapter.messages)


@pytest.mark.parametrize("state", ["restored", "restore_skipped", "restore_error", "none"])
def test_paste_reports_clipboard_token_cleanup_truthfully(timed_click, state):
    from contextlib import contextmanager
    from types import SimpleNamespace

    native, adapter, clock = timed_click
    token = SimpleNamespace(restored=False, restore_skipped=False, restore_error=None)

    class Services:
        @contextmanager
        def clipboard_session(self, text, **kwargs):
            yield None if state == "none" else token
            if state == "restore_error":
                token.restore_error = "private clipboard text must not leak"
            elif state != "none":
                setattr(token, state, True)

    native._services = Services()
    response = native.execute({"action": "paste", "text": "secret text"}, "target")
    metadata = response["clipboard_cleanup"]
    assert metadata["restored"] is (None if state == "none" else state == "restored")
    assert metadata["restore_skipped"] is (None if state == "none" else state == "restore_skipped")
    assert response["consumption_verified"] is False
    if state == "restore_error":
        assert response["status"] == "unknown"
        assert response["error"]["code"] == "CLIPBOARD_RESTORE_FAILED"
        assert response["may_have_acted"] is True
        assert metadata["restore_error"] == "CLIPBOARD_RESTORE_FAILED"
    else:
        assert response["status"] == "invoked"
        assert metadata["restore_error"] is None
    assert "secret text" not in str(response) and "private clipboard" not in str(response)
    assert [event.ki.wVk for event in adapter.events] == [17, 86, 86, 17]
    assert clock.now == pytest.approx(0.020)


def test_cancelled_paste_keeps_session_restoration_failure_metadata(timed_click):
    from contextlib import contextmanager
    from types import SimpleNamespace

    native, adapter, clock = timed_click
    event = ClickEvent(clock)
    native.set_cancel_event(event)
    token = SimpleNamespace(restored=False, restore_skipped=False, restore_error=None)

    class Services:
        @contextmanager
        def clipboard_session(self, text, **kwargs):
            try:
                yield token
            finally:
                token.restore_error = "CLIPBOARD_RESTORE_FAILED"

    native._services = Services()
    clock.after_sleep = event.set
    response = native.execute({"action": "paste", "text": "secret"}, "target")
    assert response["cancelled"]
    assert response["status"] == "unknown"
    assert response["error"]["code"] == "CLIPBOARD_RESTORE_FAILED"
    assert response["clipboard_cleanup"]["restore_error"] == "CLIPBOARD_RESTORE_FAILED"
    assert not native._held_keys
    assert len(adapter.events) == 4
    # Subsequent actions must not inherit stale paste metadata.
    native.set_cancel_event(None)
    assert "clipboard_cleanup" not in native.execute({"action": "key", "key": "A"}, "target")


@pytest.mark.parametrize("outcome", ["success", "error", "cancel"])
def test_physical_coordinate_context_wraps_resolution_input_and_cleanup(outcome):
    from contextlib import contextmanager

    event = threading.Event()
    calls = []

    class Adapter(FakeAdapter):
        in_context = False

        @contextmanager
        def physical_coordinates(self):
            assert not self.in_context
            calls.append(("enter", threading.get_ident()))
            self.in_context = True
            try:
                yield
            finally:
                calls.append(("restore", threading.get_ident()))
                self.in_context = False

        def resolve_window(self, target):
            assert self.in_context
            calls.append(("resolve", threading.get_ident()))
            return super().resolve_window(target)

        def send(self, events):
            assert self.in_context
            calls.append(("send", threading.get_ident()))
            sent = super().send(events)
            if len(self.events) == 1:
                if outcome == "cancel":
                    event.set()
                elif outcome == "error":
                    raise OSError("input failed")
            return sent

    adapter = Adapter()
    native = NativeInput(enabled=True, adapter=adapter)
    native.set_cancel_event(event)
    response = native.execute({"action": "key", "key": "A"}, "target")
    assert response["status"] == ("invoked" if outcome == "success" else "unknown")
    assert [name for name, _ in calls] == ["enter", "resolve", "send", "send", "restore"]
    assert all(thread == threading.get_ident() for _, thread in calls)
    assert not adapter.in_context and not native._held_keys


def test_explicit_release_has_its_own_physical_context():
    from contextlib import contextmanager

    calls = []

    class Adapter(FakeAdapter):
        @contextmanager
        def physical_coordinates(self):
            calls.append("enter")
            try:
                yield
            finally:
                calls.append("restore")

    native = NativeInput(enabled=True, adapter=Adapter())
    native.execute({"action": "key_down", "key": "A"}, "target")
    assert native.release_all()["status"] == "invoked"
    assert calls == ["enter", "restore", "enter", "restore"]


def test_unavailable_physical_context_refuses_before_resolution_or_effects():
    from contextlib import contextmanager

    class Adapter(FakeAdapter):
        @contextmanager
        def physical_coordinates(self):
            raise native_module._PhysicalCoordinatesError("DPI APIs unavailable")
            yield

        def resolve_window(self, target):
            pytest.fail("resolution must not occur without verified physical coordinates")

    adapter = Adapter()
    native = NativeInput(enabled=True, adapter=adapter)
    response = native.execute({"action": "click", "coordinate": [10, 20]}, "target")
    assert response["status"] == "refused"
    assert response["error"]["code"] == "PHYSICAL_COORDINATES_UNAVAILABLE"
    assert not response["may_have_acted"] and not adapter.events


@pytest.mark.parametrize("awareness", [0, 2])
def test_win32_physical_coordinates_verifies_and_restores_exact_context(awareness):
    calls = []

    class Function:
        def __init__(self, callback):
            self.callback = callback

        def __call__(self, *args):
            return self.callback(*args)

    class User32:
        SetThreadDpiAwarenessContext = Function(
            lambda context: calls.append(("set", getattr(context, "value", context))) or 123
        )
        GetThreadDpiAwarenessContext = Function(lambda: 456)
        GetAwarenessFromDpiAwarenessContext = Function(lambda context: awareness)

    adapter = native_module._Win32Adapter.__new__(native_module._Win32Adapter)
    adapter.user32 = User32()
    if awareness == 2:
        with adapter.physical_coordinates():
            calls.append(("body",))
        assert calls[1] == ("body",)
    else:
        with pytest.raises(native_module._PhysicalCoordinatesError):
            with adapter.physical_coordinates():
                pytest.fail("unverified awareness must not enter body")
    assert calls[0][0] == "set" and calls[-1] == ("set", 123)
