"""Real MCP stdio concurrency with synthetic effects, never desktop input."""

import asyncio
import json
import multiprocessing
import queue
import subprocess
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import anyio
import pytest

from minano_computer_use.broker import UIABroker
from minano_computer_use.runtime import Runtime
from minano_computer_use.server import create_server


def safe_worker(connection):
    try:
        while True:
            request = json.loads(connection.recv_bytes())
            if request is None:
                break
            if request.get("selector") == "blocked":
                time.sleep(30)
            connection.send_bytes(
                json.dumps(
                    {
                        "status": "ok",
                        "operation": request["action"],
                        "data": {"worker_pid": multiprocessing.current_process().pid},
                    }
                ).encode()
            )
    finally:
        connection.close()


class SafeCamera:
    def __init__(self):
        self.active = False
        self.closed = False
        self.started = threading.Event()

    def record(self, *, cancel_event, **kwargs):
        self.active = True
        self.started.set()
        try:
            if cancel_event.wait(kwargs.get("seconds", 30)):
                from minano_computer_use.control import OperationCancelled

                raise OperationCancelled()
            return SimpleNamespace(image_bytes=b"image", mime_type="image/png", metadata={})
        finally:
            self.active = False

    def close(self):
        assert not self.active
        self.closed = True


def stdio_fixture(report_path):
    camera = SafeCamera()

    class SafeRuntime(Runtime):
        def close(self):
            super().close()
            Path(report_path).write_text(
                json.dumps(
                    {
                        "camera_active": camera.active,
                        "camera_closed": camera.closed,
                        "mutations": self._broker.mutations,
                        "worker_alive": self._broker._process is not None,
                        "threads": [
                            t.name for t in threading.enumerate() if t.name.startswith("minano-")
                        ],
                        "children": [p.pid for p in multiprocessing.active_children()],
                    }
                ),
                encoding="utf-8",
            )

    class SafeBroker(UIABroker):
        mutations = 0

        def call(self, request, timeout=None, cancel_event=None):
            if request["action"] == "invoke":
                self.mutations += 1
            return super().call(request, timeout=timeout, cancel_event=cancel_event)

    broker = SafeBroker(worker_target=safe_worker)
    assert broker.call({"action": "inspect"}, timeout=5)["status"] == "ok"
    runtime = SafeRuntime(broker=broker, capture=camera)
    create_server(runtime).run(transport="stdio")


class StdioClient:
    def __init__(self, report):
        self.process = subprocess.Popen(
            [sys.executable, str(Path(__file__).resolve()), "--stdio-fixture", str(report)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            bufsize=1,
        )
        self.responses = queue.Queue()
        self.pending = {}
        self.errors = []
        self.counter = 0
        self.reader = threading.Thread(target=self._read, name="test-stdio-reader")
        self.stderr = threading.Thread(target=self._errors, name="test-stdio-stderr")
        self.reader.start()
        self.stderr.start()

    def _read(self):
        for line in self.process.stdout:
            try:
                self.responses.put(json.loads(line))
            except json.JSONDecodeError as exc:
                self.responses.put({"stdio_error": str(exc), "line": line})

    def _errors(self):
        for line in self.process.stderr:
            self.errors.append(line)

    def send(self, method, params=None, notification=False):
        message = {"jsonrpc": "2.0", "method": method}
        if not notification:
            self.counter += 1
            message["id"] = self.counter
        if params is not None:
            message["params"] = params
        self.process.stdin.write(json.dumps(message) + "\n")
        self.process.stdin.flush()
        return message.get("id")

    def tool(self, name, arguments=None):
        return self.send("tools/call", {"name": name, "arguments": arguments or {}})

    def receive(self, request_id, timeout=5):
        deadline = time.monotonic() + timeout
        while request_id not in self.pending:
            remaining = deadline - time.monotonic()
            assert remaining > 0, self.errors
            try:
                response = self.responses.get(timeout=remaining)
            except queue.Empty:
                pytest.fail(f"stdio response timed out: {self.errors}")
            assert "stdio_error" not in response, response
            self.pending[response.get("id")] = response
        return self.pending.pop(request_id)

    def result(self, request_id, timeout=5):
        response = self.receive(request_id, timeout)
        assert "error" not in response, response
        return json.loads(response["result"]["content"][0]["text"])

    def initialize(self):
        initialized = self.send(
            "initialize",
            {
                "protocolVersion": "2025-11-25",
                "capabilities": {},
                "clientInfo": {"name": "cancel-regression", "version": "1"},
            },
        )
        assert "result" in self.receive(initialized, 10)
        self.send("notifications/initialized", notification=True)

    def wait_active(self):
        deadline = time.monotonic() + 3
        while True:
            status = self.result(self.tool("system", {"action": "status"}))
            if status["data"]["operation"]["state"] == "active":
                return status["data"]["operation"]["call_id"]
            assert time.monotonic() < deadline
            time.sleep(0.005)

    def close(self):
        if self.process.poll() is None:
            self.process.stdin.close()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=5)
                pytest.fail(f"stdio disconnect did not finish cleanup: {self.errors}")
        self.reader.join(timeout=2)
        self.stderr.join(timeout=2)
        assert not self.reader.is_alive()
        assert not self.stderr.is_alive()
        assert self.process.returncode == 0, self.errors


def test_real_stdio_cancel_bypasses_saturated_executor_and_stops_batch(tmp_path):
    report = tmp_path / "cleanup.json"
    client = StdioClient(report)
    try:
        client.initialize()
        batch = client.tool(
            "act",
            {
                "target": "fixture",
                "stop_on_error": False,
                "actions": [{"action": "wait", "seconds": 30}, {"action": "invoke"}],
            },
        )
        owner_id = client.wait_active()
        # More queued threads than the default executor's maximum of 32.
        queued = [
            client.tool(
                "act",
                {
                    "actions": [{"action": "wait", "seconds": 0}],
                },
            )
            for _ in range(40)
        ]
        started = time.monotonic()
        accepted = client.result(client.tool("system", {"action": "cancel"}), timeout=1.5)
        assert time.monotonic() - started < 1.5
        assert accepted["data"] == {"requested": True, "call_id": owner_id}
        cancelled = client.result(batch, timeout=3)
        assert cancelled["cancelled"]
        assert cancelled["error"]["code"] == "CANCELLED"
        assert cancelled["data"]["attempted"] == 1
        assert cancelled["data"]["stopped_early"]
        for request_id in queued:
            assert client.result(request_id)["status"] == "ok"
    finally:
        client.close()
    cleanup = json.loads(report.read_text(encoding="utf-8"))
    assert cleanup == {
        "camera_active": False,
        "camera_closed": True,
        "mutations": 0,
        "worker_alive": False,
        "threads": [],
        "children": [],
    }


def test_real_stdio_disconnect_cancels_play_and_joins_capture_and_uia(tmp_path):
    report = tmp_path / "cleanup.json"
    client = StdioClient(report)
    try:
        client.initialize()
        client.tool(
            "play",
            {
                "target": "fixture",
                "seconds": 30,
                "stop_on_error": False,
                "actions": [{"action": "wait", "seconds": 30}, {"action": "invoke"}],
            },
        )
        client.wait_active()
    finally:
        client.close()
    cleanup = json.loads(report.read_text(encoding="utf-8"))
    assert not cleanup["camera_active"]
    assert cleanup["camera_closed"]
    assert not cleanup["worker_alive"]
    assert not cleanup["children"]
    assert not cleanup["threads"]
    assert cleanup["mutations"] == 0


def test_real_stdio_cancelled_notification_does_not_cancel_unrelated_owner(tmp_path):
    report = tmp_path / "cleanup.json"
    client = StdioClient(report)
    try:
        client.initialize()
        owner = client.tool("act", {"actions": [{"action": "wait", "seconds": 0.4}]})
        owner_id = client.wait_active()
        queued = client.tool(
            "act",
            {
                "target": "fixture",
                "actions": [{"action": "invoke"}],
            },
        )
        client.send("notifications/cancelled", {"requestId": queued}, notification=True)
        time.sleep(0.04)
        status = client.result(client.tool("system", {"action": "status"}))
        assert status["data"]["operation"]["call_id"] == owner_id
        assert not status["data"]["operation"]["cancel_requested"]
        assert client.result(owner)["status"] == "ok"
        # A completed later request proves the cancelled queued owner has drained.
        assert (
            client.result(
                client.tool(
                    "act",
                    {
                        "actions": [{"action": "wait", "seconds": 0}],
                    },
                )
            )["status"]
            == "ok"
        )
    finally:
        client.close()
    cleanup = json.loads(report.read_text(encoding="utf-8"))
    assert cleanup["mutations"] == 0
    assert not cleanup["threads"]
    assert not cleanup["children"]


def test_real_stdio_cancel_stalled_uia_reaps_and_recovers(tmp_path):
    report = tmp_path / "cleanup.json"
    client = StdioClient(report)
    try:
        client.initialize()
        before = client.result(
            client.tool(
                "window",
                {
                    "action": "inspect",
                    "target": "fixture",
                    "selector": "ready",
                },
            )
        )
        assert before["status"] == "ok"
        first_pid = before["data"]["worker_pid"]
        blocked = client.tool(
            "window",
            {
                "action": "inspect",
                "target": "fixture",
                "selector": "blocked",
                "timeout": 30,
            },
        )
        client.wait_active()
        time.sleep(0.06)
        accepted = client.result(client.tool("system", {"action": "cancel"}), timeout=1.5)
        assert accepted["data"]["requested"]
        response = client.result(blocked, timeout=3)
        assert response["cancelled"]
        assert not response["may_have_acted"]
        recovered = client.result(
            client.tool(
                "window",
                {
                    "action": "inspect",
                    "target": "fixture",
                    "selector": "ready",
                },
            ),
            timeout=8,
        )
        if recovered["status"] != "ok":
            pytest.fail(str(recovered) + "\n" + "".join(client.errors))
        assert recovered["data"]["worker_pid"] != first_pid
    finally:
        client.close()
    cleanup = json.loads(report.read_text(encoding="utf-8"))
    assert not cleanup["worker_alive"]
    assert not cleanup["threads"]
    assert not cleanup["children"]
    assert cleanup["mutations"] == 0


def test_safe_worker_cancel_recovery_direct():
    broker = UIABroker(worker_target=safe_worker)
    event = threading.Event()
    response = {}
    try:
        assert broker.call({"action": "inspect"}, timeout=5)["status"] == "ok"
        thread = threading.Thread(
            target=lambda: response.update(
                broker.call(
                    {"action": "inspect", "selector": "blocked"},
                    timeout=30,
                    cancel_event=event,
                )
            )
        )
        thread.start()
        time.sleep(0.06)
        event.set()
        thread.join(3)
        assert not thread.is_alive()
        assert response["cancelled"]
        assert broker.call({"action": "inspect", "selector": "ready"}, timeout=5)["status"] == "ok"
    finally:
        broker.close()


def test_async_queued_cancel_joins_own_thread_without_signalling_owner():
    async def exercise():
        runtime = Runtime()
        server = create_server(runtime)
        owner = asyncio.create_task(
            server.call_tool(
                "act",
                {
                    "actions": [{"action": "wait", "seconds": 0.3}],
                    "screenshot": False,
                },
            )
        )
        deadline = time.monotonic() + 2
        while runtime.controller.snapshot()["state"] != "active":
            if owner.done():
                pytest.fail(f"Owner exited before admission: {owner.result()!r}")
            assert time.monotonic() < deadline, "Owner was never admitted"
            await asyncio.sleep(0.001)
        owner_id = runtime.controller.snapshot()["call_id"]
        queued = asyncio.create_task(
            server.call_tool(
                "act",
                {
                    "actions": [{"action": "wait", "seconds": 30}],
                    "screenshot": False,
                },
            )
        )
        await asyncio.sleep(0.02)
        queued.cancel()
        await asyncio.sleep(0.02)
        assert runtime.controller.snapshot()["call_id"] == owner_id
        assert not runtime.controller.snapshot()["cancel_requested"]
        await asyncio.wait_for(owner, timeout=2)
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(queued, timeout=2)
        assert runtime.controller.snapshot()["state"] == "idle"
        await asyncio.to_thread(runtime.close)

    anyio.run(exercise)


def test_cancelled_active_awaiter_joins_play_capture_before_return():
    async def exercise():
        camera = SafeCamera()
        runtime = Runtime(capture=camera)
        server = create_server(runtime)
        active = asyncio.create_task(
            server.call_tool(
                "play",
                {
                    "target": "fixture",
                    "seconds": 30,
                    "actions": [{"action": "wait", "seconds": 30}],
                },
            )
        )
        deadline = time.monotonic() + 2
        while not camera.started.is_set():
            if active.done():
                pytest.fail(f"Play exited before capture started: {active.result()!r}")
            assert time.monotonic() < deadline, "Capture never started"
            await asyncio.sleep(0.001)
        active.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(active, timeout=2)
        assert not camera.active
        assert runtime.controller.snapshot()["state"] == "idle"
        assert not any(t.name == "minano-play-capture" for t in threading.enumerate())
        await asyncio.to_thread(runtime.close)

    anyio.run(exercise)


if __name__ == "__main__" and len(sys.argv) == 3 and sys.argv[1] == "--stdio-fixture":
    stdio_fixture(sys.argv[2])
