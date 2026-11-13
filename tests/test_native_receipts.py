from __future__ import annotations

import threading
from contextlib import contextmanager

import pytest

import minano_computer_use.native as native_module
from minano_computer_use.native import KEYEVENTF_SCANCODE, NativeInput


class ReceiptAdapter:
    def __init__(self):
        self.hwnd = 10
        self.events = []
        self.result = None
        self.raise_send = False

    def resolve_window(self, _target):
        return {"hwnd": 10, "pid": 20}

    def foreground(self):
        return self.hwnd

    def window_pid(self, _hwnd):
        return 20

    def send(self, events):
        self.events.extend(events)
        if self.raise_send:
            raise OSError("send failed")
        return len(events) if self.result is None else self.result

    def key_event(self, vk, up=False):
        from minano_computer_use.native import _key

        event = _key(0, up)
        event.ki.wScan = vk
        event.ki.dwFlags |= KEYEVENTF_SCANCODE
        return event


def test_receipt_contains_actual_scancode_and_counts():
    adapter = ReceiptAdapter()
    response = NativeInput(enabled=True, adapter=adapter).execute(
        {"action": "key", "key": "CTRL+A"}, "target"
    )
    receipt = response["receipt"]
    assert response["target_binding"] == {
        "hwnd": 10,
        "pid": 20,
        "process_creation_time": None,
    }
    assert receipt["requested_events"] == 4
    assert receipt["accepted_events"] == 4
    assert receipt["unknown_events"] == 0
    assert receipt["phases"]["down"] == {"requested": 2, "accepted": 2, "unknown": 0}
    assert receipt["phases"]["up"] == {"requested": 2, "accepted": 2, "unknown": 0}
    assert receipt["events"][0]["requested_vk"] == 17
    assert receipt["events"][0]["w_vk"] == 0
    assert receipt["events"][0]["accepted"] is True
    assert all("recipient_hwnd" not in event for event in receipt["events"])
    assert all("recipient_pid" not in event for event in receipt["events"])
    assert receipt["consumption_verified"] is False


def test_partial_send_is_unknown_without_replay():
    adapter = ReceiptAdapter()
    adapter.result = 0
    response = NativeInput(enabled=True, adapter=adapter).execute(
        {"action": "key_down", "key": "A"}, "target"
    )
    receipt = response["receipt"]
    assert response["status"] == "unknown"
    assert receipt["accepted_events"] is None
    assert receipt["unknown_events"] == 1
    assert receipt["events"][0]["accepted"] is None


def test_send_exception_records_unknown_and_no_event_echo_for_text():
    adapter = ReceiptAdapter()
    adapter.raise_send = True
    response = NativeInput(enabled=True, adapter=adapter).execute(
        {"action": "text", "text": "private-value"}, "target"
    )
    assert response["status"] == "unknown"
    assert response["receipt"]["accepted_events"] is None
    assert response["receipt"]["events"] == []
    assert response["receipt"]["events_omitted"] >= 1
    assert "private-value" not in str(response)
    assert response["cleanup"]["receipt"]["events"] == []


def test_cleanup_receipt_has_own_ledger_and_no_action_binding():
    adapter = ReceiptAdapter()
    native = NativeInput(enabled=True, adapter=adapter)
    native.execute({"action": "key_down", "key": "A"}, "target")
    cleanup = native.release_all()
    assert cleanup["receipt"]["requested_events"] == 1
    assert cleanup["receipt"]["phases"]["up"]["requested"] == 1
    assert cleanup["method"] == "SendInput"
    assert "target_binding" not in cleanup


def test_creation_time_failure_is_auxiliary_and_target_is_resolved_once():
    adapter = ReceiptAdapter()
    calls = []

    def resolve(target):
        calls.append(target)
        return {"hwnd": 10, "pid": 20}

    def creation(pid):
        assert pid == 20
        raise OSError("unavailable")

    adapter.resolve_window = resolve
    adapter.process_creation_time = creation
    native = NativeInput(enabled=True, adapter=adapter)
    response = native.execute({"action": "key", "key": "A"}, "target")
    assert response["status"] == "invoked"
    assert response["target_binding"]["process_creation_time"] is None
    assert calls == ["target"]


def test_resolved_refusal_retains_binding_and_empty_receipt():
    adapter = ReceiptAdapter()
    adapter.hwnd = 99
    response = NativeInput(enabled=True, adapter=adapter).execute(
        {"action": "key", "key": "A"}, "target"
    )
    assert response["status"] == "refused"
    assert response["target_binding"]["hwnd"] == 10
    assert response["receipt"]["requested_events"] == 0
    assert not adapter.events


def test_actual_event_is_captured_before_adapter_mutates_it():
    adapter = ReceiptAdapter()

    def send(events):
        events[0].ki.wScan = 999
        return len(events)

    adapter.send = send
    response = NativeInput(enabled=True, adapter=adapter).execute(
        {"action": "key", "key": "A"}, "target"
    )
    assert [event["w_scan"] for event in response["receipt"]["events"]] == [65, 65]


def test_duration_is_requested_separately_from_measured_dispatch(monkeypatch):
    class Clock:
        now = 0.0

        def monotonic(self):
            return self.now

        def sleep(self, seconds):
            self.now += seconds

    clock = Clock()
    monkeypatch.setattr(native_module, "time", clock)
    response = NativeInput(enabled=True, adapter=ReceiptAdapter()).execute(
        {"action": "key", "key": "A", "seconds": 0.05}, "target"
    )
    assert response["receipt"]["requested_seconds"] == 0.05
    assert response["receipt"]["dispatch_interval_seconds"] == pytest.approx(0.05)


def test_short_multi_send_keeps_confirmed_prefix_and_unknown_suffix():
    adapter = ReceiptAdapter()
    adapter.result = 1
    native = NativeInput(enabled=True, adapter=adapter)
    native._begin_receipt()
    events = [adapter.key_event(65), adapter.key_event(66)]
    with pytest.raises(RuntimeError):
        native._send(events, requested_vks=[65, 66])
    receipt = native._finish_receipt()
    assert receipt["requested_events"] == 2
    assert receipt["accepted_events"] is None
    assert receipt["unknown_events"] == 1
    assert receipt["phases"]["down"]["accepted"] == 1
    assert [event["accepted"] for event in receipt["events"]] == [True, None]
    assert len(adapter.events) == 2


def test_event_details_are_bounded():
    adapter = ReceiptAdapter()
    native = NativeInput(enabled=True, adapter=adapter)
    native._begin_receipt()
    native._send([adapter.key_event(65) for _ in range(100)])
    receipt = native._finish_receipt()
    assert receipt["requested_events"] == 100
    assert len(receipt["events"]) == 64
    assert receipt["events_omitted"] == 36


def test_cancelled_chord_finally_receipt_is_separate_and_no_replay():
    adapter = ReceiptAdapter()
    cancel = threading.Event()
    original = adapter.send

    def send(events):
        sent = original(events)
        cancel.set()
        return sent

    adapter.send = send
    native = NativeInput(enabled=True, adapter=adapter)
    native.set_cancel_event(cancel)
    response = native.execute({"action": "key", "key": "CTRL+A"}, "target", feedback="detailed")
    assert response["cancelled"] is True
    assert response["receipt"]["requested_events"] == 1
    assert response["receipt"]["phases"]["up"]["requested"] == 0
    assert response["cleanup"]["receipt"]["phases"]["up"]["requested"] == 1
    assert response["cleanup"]["method"] == "SendInput"
    assert response["input_context"]["reason"] == "cancelled"
    assert len(adapter.events) == 2


def test_paste_and_controls_emit_counts_only_and_no_stale_privacy():
    class Services:
        @contextmanager
        def clipboard_session(self, _text, **_kwargs):
            yield None

    native = NativeInput(enabled=True, adapter=ReceiptAdapter(), services=Services())
    for action in (
        {"action": "text", "text": "a\n\t🔒"},
        {"action": "paste", "text": "sensitive"},
    ):
        response = native.execute(action, "target")
        assert response["receipt"]["events"] == []
        assert response["receipt"]["events_omitted"] == response["receipt"]["requested_events"]
        assert "sensitive" not in str(response)
    response = native.execute({"action": "key", "key": "A"}, "target")
    assert response["receipt"]["events"]


def test_failed_cleanup_attempt_remains_visible_after_successful_retry():
    adapter = ReceiptAdapter()
    calls = 0

    def send(events):
        nonlocal calls
        calls += 1
        adapter.events.extend(events)
        return 0 if calls == 2 else len(events)

    adapter.send = send
    response = NativeInput(enabled=True, adapter=adapter).execute(
        {"action": "key", "key": "A"}, "target"
    )
    assert response["status"] == "unknown"
    assert response["receipt"]["unknown_events"] == 1
    assert response["cleanup"]["receipt"]["requested_events"] == 1
    assert len(adapter.events) == 3


def test_message_receipt_uses_actual_mapping_and_send_result():
    adapter = ReceiptAdapter()
    messages = []
    adapter.is_window = lambda _hwnd: True
    adapter.map_key = lambda _key: 0x4D
    adapter.resolve_window = lambda _target: {"hwnd": 10, "pid": 20, "process": "notepad.exe"}
    adapter.post_message = lambda *args: messages.append(args) or True
    response = NativeInput(enabled=True, adapter=adapter).execute(
        {"action": "key", "key": "RIGHT"}, "target", key_method="message"
    )
    assert response["method"] == "PostMessage"
    assert response["receipt"]["accepted_events"] == 2
    assert [event["w_scan"] for event in response["receipt"]["events"]] == [0x4D, 0x4D]
    assert all(event["recipient_hwnd"] == 10 for event in response["receipt"]["events"])
    assert all(event["recipient_pid"] == 20 for event in response["receipt"]["events"])
    assert len(messages) == 2 and not adapter.events


@pytest.mark.parametrize("release_method", ["scancode", "message"])
def test_held_message_key_up_reports_actual_recipient_not_current_target(release_method):
    adapter = ReceiptAdapter()
    messages = []
    adapter.window_pid = lambda hwnd: {10: 20, 11: 20, 30: 40, 31: 40}[hwnd]
    adapter.is_window = lambda _hwnd: True
    adapter.map_key = lambda key: key
    adapter.render_children = lambda hwnd: [hwnd + 1]
    adapter.resolve_window = lambda target: {
        "hwnd": 10 if target == "A" else 30,
        "pid": 20 if target == "A" else 40,
        "process": "chrome.exe",
    }
    adapter.post_message = lambda *args: messages.append(args) or True
    native = NativeInput(enabled=True, adapter=adapter)
    held = native.execute({"action": "key_down", "key": "A"}, "A", key_method="message")
    assert held["status"] == "invoked"
    adapter.hwnd = 30
    released = native.execute({"action": "key_up", "key": "A"}, "B", key_method=release_method)
    assert released["status"] == "invoked"
    assert released["method"] == "PostMessage"
    assert released["target_binding"]["hwnd"] == 30
    assert released["target_binding"]["pid"] == 40
    assert released["receipt"]["requested_events"] == 1
    event = released["receipt"]["events"][0]
    assert event["phase"] == "up"
    assert event["recipient_hwnd"] == 11
    assert event["recipient_pid"] == 20
    assert [message[0] for message in messages] == [11, 11]
    assert not adapter.events


def test_message_receipt_recipient_evidence_respects_bounds_and_text_privacy():
    adapter = ReceiptAdapter()
    adapter.is_window = lambda _hwnd: True
    adapter.map_key = lambda key: key
    adapter.resolve_window = lambda _target: {"hwnd": 10, "pid": 20, "process": "notepad.exe"}
    adapter.post_message = lambda *_args: True
    native = NativeInput(enabled=True, adapter=adapter)
    native._begin_receipt()
    for _ in range(100):
        native._post_key(10, 10, 20, 65)
    receipt = native._finish_receipt()
    assert len(receipt["events"]) == 64
    assert receipt["events_omitted"] == 36
    assert all(event["recipient_hwnd"] == 10 for event in receipt["events"])
    response = native.execute(
        {"action": "text", "text": "private-value"}, "target", key_method="message"
    )
    assert response["status"] == "invoked"
    assert response["receipt"]["events"] == []
    assert response["receipt"]["events_omitted"] == len("private-value")
    assert "private-value" not in str(response)


@pytest.mark.parametrize("prior_native_hold", [False, True])
def test_cancelled_message_cleanup_reports_only_actual_accumulated_methods(prior_native_hold):
    adapter = ReceiptAdapter()
    cancel = threading.Event()
    messages = []
    adapter.is_window = lambda _hwnd: True
    adapter.map_key = lambda key: key
    adapter.resolve_window = lambda _target: {"hwnd": 10, "pid": 20, "process": "notepad.exe"}

    def post(*args):
        messages.append(args)
        cancel.set()
        return True

    adapter.post_message = post
    native = NativeInput(enabled=True, adapter=adapter)
    if prior_native_hold:
        native.execute({"action": "key_down", "key": "B"}, "target")
    native.set_cancel_event(cancel)
    response = native.execute({"action": "key", "key": "CTRL+A"}, "target", key_method="message")
    cleanup = response["cleanup"]
    assert response["cancelled"] is True
    assert cleanup["status"] == "ok"  # Final empty cleanup's existing outcome.
    assert cleanup["method"] == ("PostMessage+SendInput" if prior_native_hold else "PostMessage")
    assert cleanup["receipt"]["phases"]["up"] == {
        "requested": 2 if prior_native_hold else 1,
        "accepted": 2 if prior_native_hold else 1,
        "unknown": 0,
    }
    assert len(messages) == 2
    assert len(adapter.events) == (2 if prior_native_hold else 0)
    assert cleanup["receipt"]["events"] == []


def test_empty_cleanup_does_not_claim_an_input_api_call():
    adapter = ReceiptAdapter()
    cleanup = NativeInput(enabled=True, adapter=adapter).release_all()
    assert cleanup["status"] == "ok"
    assert cleanup["receipt"]["requested_events"] == 0
    assert "method" not in cleanup
    assert not adapter.events


def test_chrome_hold_focus_notepad_release_separates_requested_root_and_actual_recipient():
    adapter = ReceiptAdapter()
    messages = []
    targets = {
        "chrome": {"hwnd": 10, "pid": 20, "process": "chrome.exe"},
        "notepad": {"hwnd": 30, "pid": 40, "process": "notepad.exe"},
    }
    adapter.resolve_window = lambda target: dict(targets[target])
    adapter.window_pid = lambda hwnd: {10: 20, 11: 20, 30: 40}[hwnd]
    adapter.is_window = lambda hwnd: hwnd in {10, 11, 30}
    adapter.render_children = lambda hwnd: [11] if hwnd == 10 else []
    adapter.map_key = lambda key: key
    adapter.post_message = lambda *args: messages.append(args) or True

    def activate(hwnd):
        adapter.hwnd = hwnd
        return True

    adapter.activate = activate
    native = NativeInput(enabled=True, adapter=adapter)
    assert (
        native.execute({"action": "key_down", "key": "A"}, "chrome", key_method="message")["status"]
        == "invoked"
    )
    assert native.execute({"action": "focus"}, "notepad")["status"] == "invoked"
    released = native.execute({"action": "key_up", "key": "A"}, "notepad", key_method="message")
    assert released["target_binding"] == {"hwnd": 30, "pid": 40, "process_creation_time": None}
    assert released["method"] == "PostMessage"
    event = released["receipt"]["events"][0]
    assert event["recipient_hwnd"] == 11
    assert event["recipient_pid"] == 20
    assert event["phase"] == "up" and event["accepted"] is True
    assert [(message[0], message[1]) for message in messages] == [(11, 0x100), (11, 0x101)]
    assert not adapter.events
