"""Small, cooperative operation cancellation primitive.

The controller deliberately does not own a runtime or backend lock.  A caller
must acquire its runtime serialization lock before entering ``operation()``;
the controller only records the currently admitted owner and signals its
event when cancellation is requested.
"""

from __future__ import annotations

import math
import threading
import uuid
from contextlib import AbstractContextManager
from typing import Any


class OperationCancelled(Exception):
    """Raised at a cooperative cancellation checkpoint."""

    code = "CANCELLED"

    def __init__(self, message: str = "Operation was cancelled") -> None:
        super().__init__(message)


class Operation:
    """The per-call cancellation state exposed to an operation owner."""

    def __init__(self, call_id: str, event: threading.Event) -> None:
        self.call_id = call_id
        self.event = event

    def checkpoint(self) -> None:
        """Raise if cancellation has been requested."""
        if self.event.is_set():
            raise OperationCancelled()

    def wait(self, seconds: float) -> bool:
        """Wait interruptibly, without polling, and raise when cancelled.

        The return value matches ``Event.wait`` when the wait completes without
        cancellation.  It is intentionally not a sleep helper: cancellation
        is checked both before and after the blocking wait.
        """
        if isinstance(seconds, bool) or not isinstance(seconds, (int, float)):
            raise ValueError("seconds must be a finite nonnegative number")
        if not math.isfinite(seconds) or seconds < 0:
            raise ValueError("seconds must be a finite nonnegative number")
        self.checkpoint()
        signalled = self.event.wait(seconds)
        self.checkpoint()
        return signalled


class _OperationContext(AbstractContextManager[Operation]):
    def __init__(self, controller: OperationController) -> None:
        self._controller = controller
        self._operation: Operation | None = None

    def __enter__(self) -> Operation:
        self._operation = self._controller._admit()
        return self._operation

    def __exit__(self, exc_type: Any, exc_value: Any, traceback: Any) -> bool:
        operation = self._operation
        if operation is not None:
            self._controller._finish(operation)
        return False


class OperationController:
    """Admit at most one operation and signal it cooperatively on cancel."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._active: Operation | None = None
        self._closed = False

    def operation(self) -> AbstractContextManager[Operation]:
        """Return a context manager that admits one caller-owned operation."""
        return _OperationContext(self)

    def _admit(self) -> Operation:
        with self._lock:
            if self._closed:
                raise RuntimeError("Operation controller is closed")
            if self._active is not None:
                raise RuntimeError("An operation is already active")
            operation = Operation(str(uuid.uuid4()), threading.Event())
            self._active = operation
            return operation

    def _finish(self, operation: Operation) -> None:
        with self._lock:
            if self._active is operation:
                self._active = None

    def cancel(self) -> dict[str, Any]:
        """Signal the admitted operation, without waiting for its owner."""
        with self._lock:
            operation = self._active
            if operation is None:
                return {"requested": False, "call_id": None}
            operation.event.set()
            return {"requested": True, "call_id": operation.call_id}

    def snapshot(self) -> dict[str, Any]:
        """Return a JSON-friendly view of the controller and active call."""
        with self._lock:
            operation = self._active
            return {
                "state": "closed"
                if self._closed
                else "active"
                if operation is not None
                else "idle",
                "call_id": operation.call_id if operation is not None else None,
                "cancel_requested": operation.event.is_set() if operation is not None else False,
            }

    def close(self) -> None:
        """Stop future admissions and signal the current owner, if any."""
        with self._lock:
            self._closed = True
            if self._active is not None:
                self._active.event.set()
