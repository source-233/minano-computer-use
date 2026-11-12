"""Fake Win32 evidence only: no user desktop input or application-consumption claim."""

import ast
import ctypes
import multiprocessing
import threading
import time
from contextlib import contextmanager
from pathlib import Path

import pytest

from minano_computer_use.message_backend import BackgroundInput, normalize_message_action


class Adapter:
    def __init__(self):
        self.binding = {"hwnd": 10, "pid": 20, "process_creation_time": 30}
        self.calls = []
        self.resolves = 0
        self.focus = 11
        self.pids = {10: 20, 11: 20, 12: 20, 99: 90}
        self.parents = {11: 10, 12: 10}
        self.value = ""
        self.readback = None
        self.visible = True
        self.iconic = False
        self.fail_dispatch = False
        self.fail_sync = False
        self.on_dispatch = None
        self.dpi_depth = 0
        self.rect = (0, 0, 200, 200)
        self.observed = {"foreground_hwnd": 99, "cursor": [800, 600]}

    def resolve_window(self, target):
        self.resolves += 1
        return dict(self.binding)

    def identity(self, hwnd, pid):
        return dict(self.binding)

    def observations(self):
        return dict(self.observed)

    def is_window(self, hwnd):
        return hwnd in self.pids

    def window_pid(self, hwnd):
        return self.pids.get(hwnd)

    def is_child(self, root, child):
        return self.parents.get(child) == root

    def thread_focus(self, root):
        assert root == 10
        return self.focus

    def is_visible(self, hwnd):
        return self.visible

    def is_iconic(self, hwnd):
        return self.iconic

    def dispatch_message(self, hwnd, message, wparam, lparam, milliseconds):
        assert 0 < milliseconds <= 500
        args = (hwnd, message, wparam, lparam)
        self.calls.append(args)
        if self.on_dispatch:
            self.on_dispatch()
        return not self.fail_dispatch

    def map_key(self, key):
        return 0x1E

    def screen_to_client(self, hwnd, x, y):
        assert self.dpi_depth
        return x - 100, y - 200

    def client_rect(self, hwnd):
        assert self.dpi_depth
        return self.rect

    def child_from_point(self, hwnd, x, y):
        return 11 if hwnd == 10 else hwnd

    @contextmanager
    def physical_coordinates(self):
        self.dpi_depth += 1
        try:
            yield
        finally:
            self.dpi_depth -= 1

    def class_name(self, hwnd):
        return "Edit"

    def send_timeout(self, hwnd, message, wparam, payload, milliseconds):
        assert 0 < milliseconds <= 500
        self.calls.append((hwnd, message, wparam, "redacted", milliseconds))
        if self.fail_sync:
            return False, 0
        if message == 0xC:
            self.value = payload
            return True, 1
        value = self.value if self.readback is None else self.readback
        if message == 0xE:
            return True, len(value.encode("utf-16-le")) // 2
        payload.value = value
        return True, len(value.encode("utf-16-le")) // 2


@pytest.fixture
def backend():
    return BackgroundInput(Adapter())


@pytest.mark.parametrize(
    "action",
    [
        {"action": "look", "dx": 1, "dy": 1},
        {"action": "paste", "text": "secret"},
        {"action": "focus"},
        {"action": "force_focus"},
        {"action": "text", "text": "a\0b"},
        {"action": "text", "text": "\ud800"},
        {"action": "set_value", "value": "\0"},
        {"action": "set_value", "value": 3},
        {"action": "set_value", "value": "x", "selector": "Edit"},
        {"action": "click"},
        {"action": "wheel", "amount": 120},
        {"action": "key", "key": "A", "recipient": True},
        {"action": "key", "key": "A", "recipient": "title"},
        {"action": "key", "key": "A", "input_mode": "native"},
        {"action": "key", "key": "A", "timeout": 0},
        {"action": "key", "key": "A", "at": float("nan")},
    ],
)
def test_pure_validation_refuses_before_effects(action, backend):
    with pytest.raises(ValueError):
        normalize_message_action(action)
    response = backend.execute(action, "target")
    assert response["status"] == "refused"
    assert backend._adapter.calls == []
    assert backend._adapter.resolves == 0


def test_normalizer_aliases_envelopes_and_no_mutation():
    action = {
        "type": "double_click",
        "coordinate": [110, 220],
        "recipient": "hwnd:0xb",
        "at": 0.5,
        "timeout": 1,
        "target": "test",
    }
    copy = dict(action)
    normalized = normalize_message_action(action)
    assert action == copy
    assert normalized["action"] == "click"
    assert normalized["clicks"] == 2
    assert normalized["recipient"] == 11
    assert normalize_message_action(normalized) == normalized
    wheel = normalize_message_action(
        {"action": "scroll", "amount": 2, "direction": "left", "coordinate": [110, 220]}
    )
    assert wheel["amount"] == -240 and wheel["horizontal"]


def test_utf16_text_thread_focus_and_observations(backend):
    response = backend.execute({"action": "text", "text": "A😀\n"}, "target")
    assert response["status"] == "invoked"
    assert response["method"] == response["delivery"] == "message"
    assert response["consumption_verified"] is False
    assert [call[2] for call in backend._adapter.calls] == [65, 0xD83D, 0xDE00, 10]
    assert all(call[0:2] == (11, 0x102) for call in backend._adapter.calls)
    assert not response["foreground_changed"] and not response["cursor_changed"]


def test_outside_thread_focus_uses_root(backend):
    backend._adapter.focus = 99
    response = backend.execute({"action": "text", "text": "x"}, "target")
    assert response["recipient"] == 10
    assert backend._adapter.calls[0][0] == 10


@pytest.mark.parametrize("recipient", [99, 12])
def test_recipient_requires_same_pid_and_descendant(backend, recipient):
    backend._adapter.parents.pop(12)
    response = backend.execute({"action": "text", "text": "x", "recipient": recipient}, "target")
    assert response["status"] == "refused"
    assert not backend._adapter.calls


def test_missing_creation_refuses(backend):
    backend._adapter.binding["process_creation_time"] = None
    response = backend.execute({"action": "text", "text": "x"}, "target")
    assert response["error"]["code"] == "IDENTITY_UNAVAILABLE"
    assert not backend._adapter.calls


def test_binding_does_not_retarget_changed_caption_or_identity(backend):
    assert backend.execute({"action": "text", "text": "x"}, "caption")["status"] == "invoked"
    backend._adapter.binding["process_creation_time"] = 31
    response = backend.execute({"action": "text", "text": "y"}, "caption")
    assert response["error"]["code"] == "STALE_TARGET"
    assert backend._adapter.resolves == 1
    assert len(backend._adapter.calls) == 1


def test_identity_checked_before_every_character(backend):
    backend._adapter.on_dispatch = lambda: backend._adapter.binding.update(process_creation_time=31)
    response = backend.execute({"action": "text", "text": "ab"}, "target")
    assert response["status"] == "unknown" and response["may_have_acted"]
    assert len(backend._adapter.calls) == 1


def test_set_value_bounded_exact_readback_redacted(backend):
    response = backend.execute({"action": "set_value", "value": "private😀"}, "target")
    assert response["status"] == "verified" and response["consumption_verified"]
    assert [call[1] for call in backend._adapter.calls] == [0xC, 0xE, 0xD]
    assert "private" not in repr(response)
    backend._adapter.readback = "different"
    response = backend.execute({"action": "set_value", "value": "private"}, "target")
    assert response["status"] == "unknown"
    assert "private" not in repr(response) and "different" not in repr(response)


class _QueuedEditAdapter(Adapter):
    """An Edit model separating posted queue work from synchronous sends."""

    def __init__(self):
        super().__init__()
        self.queued = []

    def post_message(self, hwnd, message, wparam, lparam):
        self.queued.append((hwnd, message, wparam, lparam))
        return True

    def dispatch_message(self, hwnd, message, wparam, lparam, milliseconds):
        accepted = super().dispatch_message(hwnd, message, wparam, lparam, milliseconds)
        if accepted and message == 0x102:
            self.value += chr(wparam)
        return accepted

    def drain_posted(self):
        for _, message, wparam, _ in self.queued:
            if message == 0x102:
                self.value += chr(wparam)
        self.queued.clear()


def test_text_then_set_value_cannot_leave_older_posted_char_after_verified_value():
    adapter = _QueuedEditAdapter()
    # Reproduce the old failure, without treating WM_NULL or a sleep as a barrier.
    adapter.post_message(11, 0x102, ord("A"), 1)
    adapter.send_timeout(11, 0xC, 0, "B", 100)
    adapter.drain_posted()
    assert adapter.value == "BA"
    adapter.value = ""
    adapter.calls.clear()
    backend = BackgroundInput(adapter)
    first = backend.execute({"action": "text", "text": "A"}, "target")
    second = backend.execute({"action": "set_value", "value": "B"}, "target")
    assert first["status"] == "invoked" and not first["consumption_verified"]
    assert first["dispatch"] == "SendMessageTimeoutW"
    assert second["status"] == "verified"
    assert not adapter.queued
    adapter.drain_posted()
    assert adapter.value == "B"
    assert first["target_binding"] == second["target_binding"]


def test_production_dispatch_is_bounded_sync_zero_lresult_is_success():
    from minano_computer_use.message_worker import _Win32MessageAdapter

    class User32:
        def __init__(self):
            self.calls = []
            self.success = 1

        def SendMessageTimeoutW(self, *args):
            self.calls.append(args)
            # Leave lpdwResult zero, like many legitimate window procedures.
            return self.success

    adapter = _Win32MessageAdapter.__new__(_Win32MessageAdapter)
    adapter.user32 = User32()
    assert adapter.dispatch_message(11, 0x102, 65, 1, 123)
    assert adapter.user32.calls[-1][0:6] == (11, 0x102, 65, 1, 0x22, 123)
    adapter.user32.success = 0
    assert not adapter.dispatch_message(11, 0x200, 0, 0, 45)
    with pytest.raises(ValueError):
        adapter.dispatch_message(11, 0xC, 0, 123, 100)
    with pytest.raises(ValueError):
        adapter.dispatch_message(11, 0x400, 0, 0, 100)


def test_sync_character_dispatch_failure_retains_uncertainty(backend):
    backend._adapter.fail_dispatch = True
    response = backend.execute({"action": "text", "text": "secret"}, "target")
    assert response["status"] == "unknown" and response["may_have_acted"]
    assert response["error"]["code"] == "MESSAGE_DISPATCH_FAILED"
    assert not response["consumption_verified"]
    assert "secret" not in repr(response)
    assert response["target_binding"]["hwnd"] == 10


def test_set_value_timeout_and_non_edit(backend):
    backend._adapter.fail_sync = True
    response = backend.execute({"action": "set_value", "value": "secret"}, "target")
    assert response["status"] == "unknown" and response["may_have_acted"]
    assert "secret" not in repr(response)
    backend._adapter.class_name = lambda _: "Button"
    backend._adapter.calls.clear()
    response = backend.execute({"action": "set_value", "value": "secret"}, "target")
    assert response["status"] == "refused" and not backend._adapter.calls


def test_keys_release_original_recipient_after_focus_change(backend):
    backend.execute({"action": "key_down", "key": "CTRL"}, "target")
    backend._adapter.focus = 12
    response = backend.execute({"action": "key_up", "key": "CTRL"}, "target")
    assert response["status"] == "invoked" and response["recipient"] == 11
    assert [c[1] for c in backend._adapter.calls] == [0x100, 0x101]
    assert backend._adapter.calls[-1][3] & (3 << 30) == 3 << 30
    assert backend.release_all()["released"] == 0


def test_cleanup_only_own_keys_same_identity(backend):
    backend.execute({"action": "key_down", "key": "A"}, "target")
    backend._adapter.binding["process_creation_time"] = 31
    response = backend.release_all()
    assert response["status"] == "unknown" and not response["cleanup_confirmed"]
    assert len(backend._adapter.calls) == 1
    assert (
        backend.execute({"action": "text", "text": "x"}, "target")["error"]["code"]
        == "CLEANUP_REQUIRED"
    )


def test_cancel_hold_attempts_owned_release(backend):
    event = threading.Event()
    backend.set_cancel_event(event)
    backend._adapter.on_dispatch = event.set
    response = backend.execute({"action": "hold", "key": "A", "seconds": 0.5}, "target")
    assert response["status"] == "unknown" and response["cancelled"]
    assert response["cleanup"]["cleanup_confirmed"]
    assert [c[1] for c in backend._adapter.calls] == [0x100, 0x101]


def test_pre_cancel_no_effect(backend):
    event = threading.Event()
    event.set()
    backend.set_cancel_event(event)
    response = backend.execute({"action": "text", "text": "x"}, "target")
    assert response["status"] == "refused" and response["cancelled"]
    assert not backend._adapter.calls


def test_dispatch_failure_never_verified_and_failed_up_stays_owned(backend):
    backend._adapter.fail_dispatch = True
    response = backend.execute({"action": "key_down", "key": "A"}, "target")
    assert response["status"] == "unknown"
    assert not response["cleanup"]["cleanup_confirmed"]
    backend._adapter.fail_dispatch = False
    assert backend.release_all()["cleanup_confirmed"]
    assert not backend._keys


def test_mouse_move_logical_child_physical_contract_no_cursor_change(backend):
    response = backend.execute({"action": "move", "x": 110, "y": 220}, "target")
    assert response["status"] == "invoked" and response["recipient"] == 11
    assert backend._adapter.calls == [(11, 0x200, 0, 10 | 20 << 16)]
    assert not response["cursor_changed"] and backend._adapter.dpi_depth == 0


def test_mouse_explicit_containment_hidden_minimized_overflow(backend):
    action = {"action": "click", "recipient": 11, "x": 99, "y": 220}
    assert backend.execute(action, "target")["error"]["code"] == "POINT_OUTSIDE_RECIPIENT"
    action["x"] = 110
    backend._adapter.iconic = True
    assert backend.execute(action, "target")["error"]["code"] == "MOUSE_TARGET_NOT_VISIBLE"
    backend._adapter.iconic = False
    backend._adapter.visible = False
    assert backend.execute(action, "target")["status"] == "refused"
    backend._adapter.visible = True
    backend._adapter.rect = (0, 0, 100000, 100000)
    action["x"] = 40000
    assert backend.execute(action, "target")["error"]["code"] == "COORDINATE_OVERFLOW"
    assert not backend._adapter.calls


def test_double_xbutton_sequence_flags_and_timing(backend):
    started = time.monotonic()
    response = backend.execute(
        {"action": "double_click", "button": "x2", "x": 110, "y": 220, "modifiers": "CTRL"},
        "target",
    )
    assert response["status"] == "invoked"
    assert time.monotonic() - started >= 0.09
    calls = backend._adapter.calls
    assert [c[1] for c in calls] == [0x100, 0x20B, 0x20C, 0x20D, 0x20C, 0x101]
    assert calls[1][2] == (2 << 16) | 64 | 8
    assert calls[2][2] == (2 << 16) | 8


def test_wheel_screen_coordinates_signed_delta(backend):
    response = backend.execute(
        {"action": "scroll", "amount": 1, "direction": "down", "x": 110, "y": 220}, "target"
    )
    assert response["status"] == "invoked"
    assert backend._adapter.calls == [(11, 0x20A, (0xFF88 << 16), 110 | 220 << 16)]


def test_drag_total_duration_pinned_recipient_and_cleanup(backend):
    started = time.monotonic()
    response = backend.execute(
        {"action": "drag", "x": 110, "y": 220, "to": [140, 250], "seconds": 0.05}, "target"
    )
    assert response["status"] == "invoked"
    assert time.monotonic() - started >= 0.045
    assert backend._adapter.calls[0][1] == 0x201
    assert backend._adapter.calls[-1][1] == 0x202
    assert not backend._buttons


def test_mouse_up_uses_original_recipient_and_position(backend):
    backend.execute({"action": "mouse_down", "button": "right", "x": 110, "y": 220}, "target")
    response = backend.execute({"action": "mouse_up", "button": "right"}, "target")
    assert response["status"] == "invoked" and response["recipient"] == 11
    assert backend._adapter.calls[-1] == (11, 0x205, 0, 10 | 20 << 16)


def test_timeout_uncertainty_and_owned_cleanup(backend):
    response = backend.execute(
        {"action": "hold", "key": "A", "seconds": 0.1}, "target", timeout=0.02
    )
    assert response["status"] == "unknown" and response["error"]["code"] == "TIMEOUT"
    assert response["cleanup"]["cleanup_confirmed"]


def test_observes_application_side_effects_without_undo(backend):
    backend._adapter.on_dispatch = lambda: backend._adapter.observed.update(
        foreground_hwnd=10, cursor=[1, 2]
    )
    response = backend.execute({"action": "text", "text": "x"}, "target")
    assert response["foreground_changed"] and response["cursor_changed"]


class DeadBroker:
    _process = None

    def __init__(self):
        self.calls = 0

    def call(self, *args, **kwargs):
        if args[0]["action"] == "inspect":
            return {
                "status": "ok",
                "operation": "inspect",
                "_message_worker": True,
                "target_binding": {"hwnd": 10, "pid": 20, "process_creation_time": 30},
            }
        self.calls += 1
        return {
            "status": "unknown",
            "operation": "key_down",
            "may_have_acted": True,
            "error": {"code": "TIMEOUT"},
        }

    def close(self):
        pass


def test_hard_worker_loss_no_fake_new_worker_release():
    backend = BackgroundInput()
    backend._broker = DeadBroker()
    response = backend.execute({"action": "key_down", "key": "A"}, "target")
    assert response["cleanup_unconfirmed"]
    assert response["target_binding"] == {"hwnd": 10, "pid": 20, "process_creation_time": 30}
    assert backend.release_all()["error"]["code"] == "CLEANUP_UNCONFIRMED"
    assert backend.execute({"action": "text", "text": "x"}, "target")["status"] == "unknown"
    backend.close()
    assert backend._broker.calls == 1


class _HangingAdapter(Adapter):
    def dispatch_message(self, *args):
        if (args[1] == 0x100 and args[2] == 66) or (args[1] == 0x102 and args[2] == 90):
            time.sleep(60)
        return super().dispatch_message(*args)


def synthetic_message_worker(connection):
    # Exercise the production JSON loop without loading user32 or touching desktop.
    from minano_computer_use import message_worker

    message_worker._Win32MessageAdapter = _HangingAdapter
    message_worker.worker_main(connection)


def test_spawned_worker_json_persistent_holds_hard_kill_and_reaping():
    from minano_computer_use.broker import UIABroker

    backend = BackgroundInput()
    backend._broker = UIABroker(worker_target=synthetic_message_worker)
    try:
        down = backend.execute({"action": "key_down", "key": "A"}, "caption", timeout=5)
        assert down["status"] == "invoked"
        assert backend._possible_holds
        process = backend._broker._process
        pid = process.pid
        up = backend.execute({"action": "key_up", "key": "A"}, "caption")
        assert up["status"] == "invoked" and not backend._possible_holds
        assert backend._broker._process.pid == pid
        response = backend.execute({"action": "key_down", "key": "B"}, "caption", timeout=0.1)
        assert response["status"] == "unknown" and response["may_have_acted"]
        assert response["target_binding"] == down["target_binding"]
        assert response["cleanup_unconfirmed"]
        assert backend._broker._process is None and process._closed
        assert all(child.pid != pid for child in multiprocessing.active_children())
        assert backend.release_all()["error"]["code"] == "CLEANUP_UNCONFIRMED"
        assert backend._broker._process is None
    finally:
        backend.close()


def test_spawned_worker_cancel_latches_unconfirmed_cleanup():
    from minano_computer_use.broker import UIABroker

    backend = BackgroundInput()
    backend._broker = UIABroker(worker_target=synthetic_message_worker)
    event = threading.Event()
    try:
        assert (
            backend.execute({"action": "text", "text": "warmup"}, "target")["status"] == "invoked"
        )
        backend.set_cancel_event(event)
        response = {}
        thread = threading.Thread(
            target=lambda: response.update(
                backend.execute({"action": "hold", "key": "B", "seconds": 1}, "target")
            )
        )
        thread.start()
        time.sleep(0.1)
        event.set()
        thread.join(3)
        assert not thread.is_alive()
        assert response["cancelled"] and response["may_have_acted"]
        assert response["cleanup_unconfirmed"]
        assert backend._broker._process is None
        assert not backend.release_all()["cleanup_confirmed"]
    finally:
        event.set()
        backend.close()


def test_spawned_message_text_timeout_is_write_not_uia_text_read():
    from minano_computer_use.broker import UIABroker

    backend = BackgroundInput()
    backend._broker = UIABroker(worker_target=synthetic_message_worker)
    try:
        assert (
            backend.execute({"action": "text", "text": "warmup"}, "target")["status"] == "invoked"
        )
        response = backend.execute({"action": "text", "text": "Z"}, "target", timeout=0.1)
        assert response["operation"] == "text"
        assert response["status"] == "unknown" and response["may_have_acted"]
        assert response["target_binding"]["hwnd"] == 10
        assert not backend._lost_holds
        assert backend.release_all()["released"] == 0
        assert backend._broker._process is None
    finally:
        backend.close()


def test_import_safe_and_no_physical_api_calls_in_route():
    from minano_computer_use import message_backend, message_worker

    forbidden = {
        "SendInput",
        "SetCursorPos",
        "SetFocus",
        "SetForegroundWindow",
        "AttachThreadInput",
        "WindowFromPoint",
        "PostMessageW",
    }
    for module in (message_backend, message_worker):
        tree = ast.parse(Path(module.__file__).read_text(encoding="utf-8"))
        assert (
            not {node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)}
            & forbidden
        )
    assert ctypes.sizeof(message_worker._GUIThreadInfo) >= 48
