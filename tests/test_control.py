from __future__ import annotations

import json
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor

import pytest

from minano_computer_use.control import Operation, OperationCancelled, OperationController


def test_operation_lifecycle_and_fresh_per_call_events() -> None:
    controller = OperationController()
    assert controller.snapshot() == {"state": "idle", "call_id": None, "cancel_requested": False}
    assert controller.cancel() == {"requested": False, "call_id": None}
    with controller.operation() as first:
        assert str(uuid.UUID(first.call_id)) == first.call_id
        assert not first.event.is_set()
        assert controller.cancel() == {"requested": True, "call_id": first.call_id}
        assert json.loads(json.dumps(controller.snapshot()))["cancel_requested"] is True
        with pytest.raises(OperationCancelled) as exc:
            first.checkpoint()
        assert exc.value.code == "CANCELLED"
        with pytest.raises(RuntimeError, match="already active"):
            with controller.operation():
                pytest.fail("overlapping call admitted")
        assert controller.snapshot()["call_id"] == first.call_id
        assert first.event.is_set()
    with controller.operation() as second:
        assert second.call_id != first.call_id
        assert second.event is not first.event
        assert not second.event.is_set()
    assert controller.snapshot()["state"] == "idle"


def test_queued_caller_cannot_register_or_clear_active_event() -> None:
    controller = OperationController()
    serialization = threading.Lock()
    queued = threading.Event()
    second_entered = threading.Event()

    def next_call():
        queued.set()
        with serialization, controller.operation() as operation:
            second_entered.set()
            return operation

    with ThreadPoolExecutor(max_workers=1) as pool:
        with serialization, controller.operation() as active:
            future = pool.submit(next_call)
            assert queued.wait(2)
            assert not second_entered.is_set()
            assert controller.cancel() == {"requested": True, "call_id": active.call_id}
            assert controller.snapshot()["call_id"] == active.call_id
            assert active.event.is_set()
        second = future.result(timeout=2)
    assert second.call_id != active.call_id
    assert not second.event.is_set()


def test_close_signals_but_keeps_owner_registered_until_exit() -> None:
    controller = OperationController()
    with controller.operation() as operation:
        controller.close()
        assert operation.event.is_set()
        assert controller.snapshot() == {
            "state": "closed",
            "call_id": operation.call_id,
            "cancel_requested": True,
        }
        assert controller.cancel()["call_id"] == operation.call_id
        with pytest.raises(RuntimeError, match="closed"):
            with controller.operation():
                pytest.fail("closed controller admitted a call")
    assert controller.snapshot()["call_id"] is None
    controller.close()
    with pytest.raises(RuntimeError, match="closed"):
        with controller.operation():
            pytest.fail("closed controller admitted a call")


def test_owner_exception_unregisters_operation() -> None:
    controller = OperationController()
    with pytest.raises(ValueError, match="owner failed"):
        with controller.operation():
            raise ValueError("owner failed")
    assert controller.snapshot()["state"] == "idle"


class ObservedEvent(threading.Event):
    def __init__(self):
        super().__init__()
        self.waiting = threading.Event()
        self.timeouts = []

    def wait(self, timeout=None):
        self.timeouts.append(timeout)
        self.waiting.set()
        return super().wait(timeout)


def test_wait_blocks_once_and_cancel_wakes_it_without_polling() -> None:
    event = ObservedEvent()
    operation = Operation(str(uuid.uuid4()), event)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(operation.wait, 30)
        assert event.waiting.wait(2)
        event.set()
        with pytest.raises(OperationCancelled):
            future.result(timeout=2)
    assert event.timeouts == [30]


def test_wait_deadline_and_cancelled_zero_wait() -> None:
    event = ObservedEvent()
    operation = Operation(str(uuid.uuid4()), event)
    assert operation.wait(0.001) is False
    assert event.timeouts == [0.001]
    event.set()
    with pytest.raises(OperationCancelled):
        operation.wait(0)
    assert event.timeouts == [0.001]


@pytest.mark.parametrize("seconds", [-1, float("nan"), float("inf"), True, "1", None])
def test_wait_rejects_invalid_duration(seconds) -> None:
    with OperationController().operation() as operation:
        with pytest.raises(ValueError, match="finite nonnegative"):
            operation.wait(seconds)
