"""Production dispatch with fake windows only; never touches the desktop."""

import copy
import json
import threading
import time
from contextlib import nullcontext
from types import SimpleNamespace

import pytest

from minano_computer_use.coordinates import CoordinateManager
from minano_computer_use.message_backend import BackgroundInput
from minano_computer_use.message_worker import worker_main
from minano_computer_use.native import (
    INPUT_KEYBOARD,
    KEYEVENTF_KEYUP,
    MOUSEEVENTF_LEFTDOWN,
    NativeInput,
)
from minano_computer_use.runtime import Runtime


class World:
    def __init__(self):
        self.windows = {
            10: dict(
                hwnd=10,
                pid=20,
                process_creation_time=100,
                rect=[100, 100, 300, 300],
                dpi=96,
                visible=True,
                minimized=False,
            ),
            30: dict(
                hwnd=30,
                pid=40,
                process_creation_time=200,
                rect=[100, 100, 300, 300],
                dpi=96,
                visible=True,
                minimized=False,
            ),
        }
        self.title_hwnd = self.foreground_hwnd = 10
        self.events = []
        self.moves = []
        self.resolved = []
        self.on_send = None
        self.on_cursor = None
        self.on_resolve = None

    def resolve_window(self, target):
        self.resolved.append(target)
        if self.on_resolve:
            self.on_resolve()
        hwnd = int(target[5:]) if target.startswith("hwnd:") else self.title_hwnd
        return copy.deepcopy(self.windows[hwnd])

    def foreground(self):
        return self.foreground_hwnd

    def window_pid(self, hwnd):
        return self.windows[hwnd]["pid"]

    def send(self, events):
        self.events.extend(events)
        if self.on_send:
            self.on_send()
        return len(events)

    def set_cursor(self, x, y):
        self.moves.append((x, y))
        if self.on_cursor:
            self.on_cursor()
        return True

    def point_hits(self, x, y, hwnd):
        return self.foreground_hwnd == hwnd

    def replace_title(self):
        self.title_hwnd = self.foreground_hwnd = 30

    def snapshot(self):
        return {
            **copy.deepcopy(self.windows[10]),
            "target": "fixture",
            "window_rect": [100, 100, 300, 300],
            "output_size": [100, 100],
            "physical_transform": {"origin": [100, 100], "output_to_physical_scale": [2, 2]},
        }


def setup(world, tmp_path, surface=None):
    manager = CoordinateManager(world.resolve_window)
    metadata = world.snapshot()
    if surface:
        for field in ("hwnd", "pid", "process_creation_time", "window_rect", "rect", "dpi"):
            metadata.pop(field)
        metadata["target"] = surface
    capture_id = manager.remember(metadata)
    native = NativeInput(enabled=True, adapter=world)
    runtime = Runtime(native=native, coordinate_manager=manager, output_dir=tmp_path)
    return runtime, manager, capture_id


def act(runtime, capture_id, actions=None, **kwargs):
    return runtime.act(
        actions or [{"action": "click", "x": 25, "y": 25}],
        target=kwargs.pop("target", "fixture"),
        native=True,
        coordinate_space="image",
        capture_id=capture_id,
        screenshot=False,
        **kwargs,
    )


def mouse_downs(world):
    return [
        event
        for event in world.events
        if event.type != INPUT_KEYBOARD and event.mi.dwFlags & MOUSEEVENTF_LEFTDOWN
    ]


def test_title_replacement_after_first_key_cannot_retarget_click(tmp_path):
    world = World()
    runtime, _, capture_id = setup(world, tmp_path)
    world.on_send = world.replace_title
    response = act(
        runtime,
        capture_id,
        [
            {"action": "key", "key": "A"},
            {"action": "click", "x": 25, "y": 25},
        ],
    )
    assert response["status"] == "partial" and response["may_have_acted"]
    assert response["data"]["completed"] == 1
    assert response["data"]["results"][1]["result"]["status"] == "refused"
    assert not world.moves and not mouse_downs(world)
    assert all(target == "hwnd:10" for target in world.resolved[4:])


@pytest.mark.parametrize(
    "change",
    [
        {"pid": 21},
        {"process_creation_time": 101},
        {"rect": [101, 100, 301, 300]},
        {"dpi": 144},
    ],
)
def test_same_hwnd_drift_between_actions_refuses_before_pointer(tmp_path, change):
    world = World()
    runtime, _, capture_id = setup(world, tmp_path)
    world.on_send = lambda: world.windows[10].update(change)
    response = act(
        runtime,
        capture_id,
        [
            {"action": "key", "key": "A"},
            {"action": "click", "x": 25, "y": 25},
        ],
    )
    stale = response["data"]["results"][1]["result"]
    assert stale["status"] == "refused" and stale["error"]["code"] == "STALE_CAPTURE"
    assert not stale["may_have_acted"] and response["may_have_acted"]
    assert not world.moves and not mouse_downs(world)


@pytest.mark.parametrize(
    "change",
    [
        {"pid": 21},
        {"process_creation_time": 101},
        {"rect": [101, 100, 301, 300]},
        {"dpi": 144},
    ],
)
def test_native_checkpoint_catches_change_after_predispatch_check(tmp_path, change):
    world = World()
    runtime, _, capture_id = setup(world, tmp_path)
    original = runtime._native._target_info

    def resolving(target):
        info = original(target)
        world.windows[10].update(change)
        return info

    runtime._native._target_info = resolving
    response = act(runtime, capture_id)
    stale = response["data"]["results"][0]["result"]
    assert stale["status"] == "refused" and stale["error"]["code"] == "STALE_CAPTURE"
    assert not response["may_have_acted"] and not world.moves and not world.events


def test_native_geometry_change_after_cursor_blocks_down(tmp_path):
    world = World()
    runtime, _, capture_id = setup(world, tmp_path)
    world.on_cursor = lambda: world.windows[10].update(dpi=144)
    response = act(runtime, capture_id)
    stale = response["data"]["results"][0]["result"]
    assert stale["status"] == "unknown" and stale["error"]["code"] == "STALE_CAPTURE"
    assert world.moves == [(150, 150)] and not mouse_downs(world)
    assert response["may_have_acted"]


def test_native_chord_drift_blocks_next_down_but_releases_owned_key(tmp_path):
    world = World()
    runtime, _, capture_id = setup(world, tmp_path)
    world.on_send = lambda: world.windows[10].update(dpi=144)
    response = act(runtime, capture_id, [{"action": "key", "key": "CTRL+A"}])
    assert response["status"] == "unknown" and response["may_have_acted"]
    assert len(world.events) == 2
    assert world.events[1].ki.dwFlags & KEYEVENTF_KEYUP
    assert not runtime._native._held_keys


@pytest.mark.parametrize("surface", [None, "desktop", "display:0"])
def test_success_pins_window_and_screen_recipient_and_preserves_transform(tmp_path, surface):
    world = World()
    runtime, _, capture_id = setup(world, tmp_path, surface)
    response = act(runtime, capture_id)
    assert response["status"] == "ok"
    assert world.moves == [(150, 150)] and len(mouse_downs(world)) == 1
    assert response["data"]["results"][0]["result"]["target_binding"] == {
        "hwnd": 10,
        "pid": 20,
        "process_creation_time": 100,
    }


def test_wrong_action_target_and_forged_pin_rejected_before_entire_batch(tmp_path):
    world = World()
    runtime, _, capture_id = setup(world, tmp_path)
    for extra in (
        {"target": "hwnd:30"},
        {"_coordinate_binding": world.snapshot()},
        {"coordinate_binding": world.snapshot()},
        {"target_binding": world.snapshot()},
    ):
        response = act(
            runtime,
            capture_id,
            [
                {"action": "key", "key": "A"},
                {"action": "click", "x": 25, "y": 25, **extra},
            ],
        )
        assert response["status"] == "refused"
        assert not world.moves and not world.events


def test_viewport_pin_keeps_original_capture_geometry(tmp_path):
    world = World()
    runtime, manager, capture_id = setup(world, tmp_path)
    manager.define_viewport("roi", world.snapshot())
    world.on_send = lambda: world.windows[10].update(dpi=144)
    response = act(
        runtime,
        capture_id,
        [
            {"action": "key", "key": "A"},
            {"action": "click", "x": 25, "y": 25},
        ],
        target="viewport:roi",
    )
    assert response["status"] == "partial" and not world.moves


def test_desktop_viewport_keeps_explicit_recipient_pin(tmp_path):
    world = World()
    runtime, manager, capture_id = setup(world, tmp_path, "desktop")
    metadata = world.snapshot()
    for field in ("hwnd", "pid", "process_creation_time", "window_rect", "rect", "dpi"):
        metadata.pop(field)
    metadata["target"] = "desktop"
    manager.define_viewport("screen", metadata)
    response = act(
        runtime,
        capture_id,
        [
            {"action": "click", "x": 25, "y": 25, "target": "viewport:screen"},
        ],
    )
    assert response["status"] == "ok" and world.moves == [(150, 150)]
    assert len(mouse_downs(world)) == 1


def test_screen_recipient_title_not_rebound_while_mapping_batch(tmp_path):
    world = World()
    runtime, manager, capture_id = setup(world, tmp_path, "desktop")
    original = manager.map_actions
    first = True

    def mapping(*args, **kwargs):
        nonlocal first
        converted = original(*args, **kwargs)
        if first:
            first = False
            world.replace_title()
        return converted

    manager.map_actions = mapping
    response = act(
        runtime,
        capture_id,
        [
            {"action": "key", "key": "A"},
            {"action": "click", "x": 25, "y": 25},
        ],
    )
    assert response["status"] == "refused" and not world.events and not world.moves


def test_wait_and_timed_play_recheck_original_not_new_recorded_frame(tmp_path):
    world = World()
    runtime, manager, capture_id = setup(world, tmp_path)
    changed = threading.Event()

    def record(**kwargs):
        world.windows[10]["process_creation_time"] = 101
        manager.remember(world.snapshot())
        changed.set()
        return SimpleNamespace(metadata={"timestamps": [0]})

    runtime._capture = SimpleNamespace(record=record)
    response = runtime.play(
        actions=[
            {"action": "wait", "seconds": 0.01},
            {"action": "click", "x": 25, "y": 25, "at": 0.03},
        ],
        target="fixture",
        native=True,
        coordinate_space="image",
        capture_id=capture_id,
        seconds=0.1,
    )
    assert changed.is_set() and response["status"] == "partial"
    assert not response["may_have_acted"] and not world.moves and not world.events
    stale = response["data"]["actions"]["data"]["results"][1]["result"]
    assert stale["error"]["code"] == "STALE_CAPTURE"


@pytest.mark.parametrize("mode", ["native", "message"])
def test_unknown_backend_cannot_ignore_private_pin(tmp_path, mode):
    world = World()
    runtime, _, capture_id = setup(world, tmp_path)
    calls = []
    legacy = SimpleNamespace(
        execute=lambda *args, **kwargs: calls.append(args),
        set_cancel_event=lambda event: None,
        release_all=lambda: {"status": "ok"},
    )
    runtime._native = runtime._message = legacy
    response = runtime.act(
        [{"action": "click", "x": 25, "y": 25}],
        target="fixture",
        input_mode=mode,
        coordinate_space="image",
        capture_id=capture_id,
        screenshot=False,
    )
    assert response["status"] == "refused" and not response["may_have_acted"]
    assert not calls


class MessageWorld(World):
    def identity(self, hwnd, pid):
        return {key: self.windows[hwnd][key] for key in ("hwnd", "pid", "process_creation_time")}

    def window_snapshot(self, hwnd):
        return self.resolve_window(f"hwnd:{hwnd}")

    def observations(self):
        return {"foreground_hwnd": 99, "cursor": [0, 0]}

    def thread_focus(self, root):
        return root

    def is_window(self, hwnd):
        return hwnd in self.windows

    def map_key(self, key):
        return key

    def dispatch_message(self, hwnd, message, wparam, lparam, milliseconds):
        self.events.append((hwnd, message))
        if self.on_send:
            self.on_send()
        return True

    def physical_coordinates(self):
        return nullcontext()


@pytest.mark.parametrize("change", [{"process_creation_time": 101}, {"dpi": 144}])
def test_message_per_event_pin_blocks_second_down_and_keeps_cleanup_identity(tmp_path, change):
    world = MessageWorld()
    runtime, _, capture_id = setup(world, tmp_path)
    runtime._message = BackgroundInput(world)
    world.on_send = lambda: world.windows[10].update(change)
    response = runtime.act(
        [{"action": "key", "key": "CTRL+A"}],
        target="fixture",
        input_mode="message",
        coordinate_space="image",
        capture_id=capture_id,
        screenshot=False,
    )
    stale = response["data"]["results"][0]["result"]
    assert stale["status"] == "unknown" and stale["error"]["code"] == "STALE_CAPTURE"
    assert sum(message == 0x100 for _, message in world.events) == 1
    if "dpi" in change:
        assert (10, 0x101) in world.events and not runtime._message._keys
    else:
        assert (10, 0x101) not in world.events  # Never send cleanup into a reused identity.


def test_message_pin_requires_reliable_snapshot():
    world = MessageWorld()
    world.window_snapshot = None
    response = BackgroundInput(world).execute(
        {"action": "key", "key": "A"},
        "hwnd:10",
        coordinate_binding=world.snapshot(),
    )
    assert response["error"]["code"] == "CAPTURE_BINDING_UNAVAILABLE"
    assert not response["may_have_acted"] and not world.events


def test_remote_pin_skips_title_handshake_and_worker_refuses_reused_hwnd(monkeypatch):
    world = MessageWorld()
    pin = world.snapshot()
    world.windows[10]["process_creation_time"] = 101
    requests = []

    class Connection:
        def __init__(self, request):
            self.request = request
            self.response = None

        def recv_bytes(self, limit):
            request, self.request = self.request, None
            return json.dumps(request).encode()

        def send_bytes(self, payload):
            self.response = json.loads(payload)

        def close(self):
            pass

    class Broker:
        _process = object()

        def call(self, request, **kwargs):
            requests.append(copy.deepcopy(request))
            assert request["action"] == "message_execute"  # No replacement-title inspect.
            request["_deadline_monotonic"] = time.monotonic() + 1
            connection = Connection(request)
            worker_main(connection)
            return connection.response

    monkeypatch.setattr("minano_computer_use.message_worker._Win32MessageAdapter", lambda: world)
    backend = BackgroundInput()
    backend._broker = Broker()
    response = backend.execute({"action": "key", "key": "A"}, "hwnd:10", coordinate_binding=pin)
    assert response["error"]["code"] == "STALE_CAPTURE" and not response["may_have_acted"]
    assert requests[0]["coordinate_binding"] == pin
    assert requests[0]["binding"]["process_creation_time"] == 100
    assert not world.events
    backend._bindings["hwnd:10"]["process_creation_time"] = 99
    response = backend.execute({"action": "key", "key": "A"}, "hwnd:10", coordinate_binding=pin)
    assert response["error"]["code"] == "STALE_CAPTURE" and len(requests) == 1
