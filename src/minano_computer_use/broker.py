"""Serialized, disposable UIA subprocess with no automatic request replay."""

import json
import math
import multiprocessing
import threading
import time

from .contracts import READ_ACTIONS, result
from .isolated_spawn import process as isolated_process
from .worker import worker_main

MAX_MESSAGE_BYTES = 16 * 1024 * 1024
STATUSES = {"ok", "invoked", "verified", "refused", "error", "unknown", "partial"}


class UIABroker:
    def __init__(self, timeout=5.0, worker_target=None):
        self.timeout = timeout
        self._target = worker_target or worker_main
        self._context = multiprocessing.get_context("spawn")
        self._lock = threading.Lock()
        self._process = None
        self._connection = None
        self._closed = False

    def _start(self):
        context = self._context
        parent, child = context.Pipe(duplex=True)
        process = isolated_process(context, self._target, (child,), daemon=True)
        try:
            process.start()
        except BaseException:
            parent.close()
            child.close()
            raise
        child.close()
        self._process, self._connection = process, parent

    def _discard(self, graceful=False):
        process, connection = self._process, self._connection
        self._process = self._connection = None
        if connection is not None:
            if graceful:
                try:
                    connection.send_bytes(b"null")
                except (OSError, EOFError):
                    pass
            connection.close()
        if process is not None:
            if graceful:
                process.join(0.3)
            if process.is_alive():
                process.terminate()
            process.join(1.0)
            if process.is_alive():
                process.kill()
                process.join()
            process.close()

    def call(self, request: dict, timeout: float | None = None, cancel_event=None):
        operation = request.get("action", "uia") if isinstance(request, dict) else "uia"
        duration = self.timeout if timeout is None else timeout
        try:
            if not isinstance(request, dict):
                raise ValueError("request must be an object")
            if (
                isinstance(duration, bool)
                or not isinstance(duration, (int, float))
                or not math.isfinite(duration)
                or not 0 < duration <= 120
            ):
                raise ValueError("timeout must be finite and in (0, 120]")
            deadline = time.monotonic() + duration
            request = dict(request)
            # Leave response handling time inside the *same* hard call deadline.
            request["_deadline_monotonic"] = deadline - 0.05
            payload = json.dumps(request, allow_nan=False).encode("utf-8")
            if len(payload) > MAX_MESSAGE_BYTES:
                raise ValueError("request too large")
        except (ValueError, TypeError) as exc:
            return result("refused", operation, code="INVALID_REQUEST", message=str(exc))

        def interrupted():
            return cancel_event is not None and cancel_event.is_set()

        while not self._lock.acquire(timeout=min(0.02, max(0, deadline - time.monotonic()))):
            if interrupted() or time.monotonic() >= deadline:
                return result(
                    "refused" if interrupted() else "error",
                    operation,
                    code="CANCELLED" if interrupted() else "TIMEOUT",
                    cancelled=interrupted(),
                    may_have_acted=False,
                    replayed=False,
                )
        try:
            if self._closed:
                return result("error", operation, code="CLOSED", message="Broker is closed")
            sent = False
            io_thread = None
            try:
                if interrupted():
                    return result(
                        "refused",
                        operation,
                        code="CANCELLED",
                        cancelled=True,
                        may_have_acted=False,
                        replayed=False,
                    )
                if self._process is None:
                    self._start()
                if interrupted():
                    raise InterruptedError("UIA operation cancelled during startup")
                if time.monotonic() >= deadline:
                    raise TimeoutError("UIA startup deadline exceeded")
                connection = self._connection
                finished = threading.Event()
                exchange = {}

                def communicate():
                    nonlocal sent
                    try:
                        if interrupted() or time.monotonic() >= deadline:
                            return
                        # A partial send may already dispatch a mutation.
                        sent = True
                        connection.send_bytes(payload)
                        exchange["response"] = json.loads(
                            connection.recv_bytes(MAX_MESSAGE_BYTES).decode("utf-8")
                        )
                    except Exception as exc:
                        exchange["error"] = exc
                    finally:
                        finished.set()

                io_thread = threading.Thread(target=communicate, name="minano-uia-pipe")
                io_thread.start()
                while not finished.is_set():
                    if interrupted():
                        raise InterruptedError("UIA operation cancelled; not replayed")
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise TimeoutError("UIA worker deadline exceeded; not replayed")
                    finished.wait(min(0.02, remaining))
                io_thread.join()
                if interrupted():
                    raise InterruptedError("UIA operation cancelled; not replayed")
                if time.monotonic() >= deadline:
                    raise TimeoutError("UIA worker deadline exceeded; not replayed")
                if "error" in exchange:
                    raise exchange["error"]
                response = exchange["response"]
                if (
                    not isinstance(response, dict)
                    or response.get("status") not in STATUSES
                    or response.get("operation") != operation
                ):
                    raise ValueError("Invalid worker result")
                return response
            except Exception as exc:
                self._discard()
                if io_thread is not None:
                    io_thread.join()
                may_have_acted = sent and operation not in READ_ACTIONS
                cancelled = isinstance(exc, InterruptedError)
                return result(
                    "unknown" if may_have_acted else ("refused" if cancelled else "error"),
                    operation,
                    code=(
                        "CANCELLED"
                        if cancelled
                        else "TIMEOUT"
                        if isinstance(exc, TimeoutError)
                        else "WORKER_FAILED"
                    ),
                    message=str(exc),
                    may_have_acted=may_have_acted,
                    replayed=False,
                    cancelled=cancelled,
                )
        finally:
            self._lock.release()

    def close(self):
        with self._lock:
            self._closed = True
            self._discard(graceful=True)
