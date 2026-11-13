"""Production native/runtime/wire integration; no real Win32 input or windows."""

from minano_computer_use import native
from minano_computer_use.runtime import Runtime
from minano_computer_use.server import _response

CREATION = 134387468188212291


class Adapter:
    def __init__(self):
        self.events = []

    def resolve_window(self, target):
        assert target == "fixture"
        return {
            "hwnd": 10,
            "pid": 20,
            "process_creation_time": CREATION,
            "rect": [0, 0, 100, 100],
        }

    def foreground(self):
        return 10

    def window_pid(self, hwnd):
        assert hwnd == 10
        return 20

    def key_event(self, key, up=False):
        event = native._Input(type=native.INPUT_KEYBOARD)
        flags = native.KEYEVENTF_SCANCODE | (native.KEYEVENTF_KEYUP if up else 0)
        event.ki = native._KeyboardInput(0, 0x2E, flags, 0, 0)
        return event

    def send(self, events):
        self.events.extend(events)
        return len(events)


def test_production_receipt_survives_runtime_and_public_wire():
    adapter = Adapter()
    runtime = Runtime(native=native.NativeInput(enabled=True, adapter=adapter))
    try:
        response = runtime.act(
            [{"action": "hold", "key": "C", "seconds": 0}],
            target="fixture",
            input_mode="native",
            screenshot=False,
        )
        assert response["status"] == "ok", response
        outcome = response["data"]["results"][0]["result"]
        assert outcome["status"] == "invoked"
        assert outcome["consumption_verified"] is False
        assert outcome["target_binding"]["hwnd"] == 10
        assert outcome["receipt"]["requested_events"] == 2
        assert outcome["receipt"]["accepted_events"] == 2
        assert outcome["receipt"]["unknown_events"] == 0
        assert len(adapter.events) == 2
        assert adapter.events[0].ki.wVk == adapter.events[1].ki.wVk == 0
        assert adapter.events[0].ki.wScan == adapter.events[1].ki.wScan == 0x2E
        public = _response(response, "act").structuredContent
        item = public["data"]["results"][0]["result"]
        assert item["receipt"]["accepted_events"] == 2
        assert item["target_binding"]["process_creation_time"] == str(CREATION)
        assert outcome["target_binding"]["process_creation_time"] == CREATION
    finally:
        runtime.close()


def test_invalid_feedback_refuses_before_production_native_dispatch():
    adapter = Adapter()
    runtime = Runtime(native=native.NativeInput(enabled=True, adapter=adapter))
    try:
        response = runtime.act(
            [{"action": "key", "key": "C"}],
            target="fixture",
            input_mode="native",
            feedback="diagnose_and_fix",
            screenshot=False,
        )
        assert response["status"] == "refused", response
        assert not adapter.events
    finally:
        runtime.close()
