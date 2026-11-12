import json
import multiprocessing
import os
import sys
import threading
import time

import pytest

from minano_computer_use.broker import UIABroker


def synthetic_worker(connection):
    try:
        while True:
            request = json.loads(connection.recv_bytes())
            if request is None:
                return
            if request.get("hang"):
                time.sleep(60)
            if request.get("until_deadline"):
                time.sleep(max(0, request["_deadline_monotonic"] - time.monotonic()))
                connection.send_bytes(
                    json.dumps(
                        {
                            "status": "error",
                            "operation": request["action"],
                            "error": {"code": "CONDITION_TIMEOUT", "message": "not ready"},
                            "data": {"last_observation": {"value": "pending"}},
                        }
                    ).encode()
                )
                continue
            if request.get("bad"):
                connection.send_bytes(b'{"status":"made_up"}')
            elif request.get("eof"):
                return
            else:
                connection.send_bytes(
                    json.dumps(
                        {
                            "status": "ok",
                            "operation": request["action"],
                            "pid": multiprocessing.current_process().pid,
                        }
                    ).encode()
                )
    finally:
        connection.close()


@pytest.mark.parametrize("action,status", [("invoke", "unknown"), ("tree", "error")])
def test_timeout_reaps_without_replay_and_recovers(action, status):
    broker = UIABroker(timeout=3, worker_target=synthetic_worker)
    try:
        first = broker.call({"action": "list"})
        assert first["status"] == "ok"
        child = broker._process
        pid = first["pid"]
        response = broker.call({"action": action, "hang": True}, timeout=0.1)
        assert response["status"] == status
        assert response["may_have_acted"] is (action == "invoke")
        assert response["replayed"] is False
        assert broker._process is None
        assert child._closed
        assert all(process.pid != pid for process in multiprocessing.active_children())
        recovered = broker.call({"action": "list"})
        assert recovered["status"] == "ok"
        assert recovered["pid"] != pid
    finally:
        broker.close()
    assert broker._process is None


@pytest.mark.parametrize("fault", ["bad", "eof"])
def test_bad_protocol_invalidates_child(fault):
    broker = UIABroker(worker_target=synthetic_worker)
    try:
        assert broker.call({"action": "invoke", fault: True})["status"] == "unknown"
        assert broker._process is None
        assert broker.call({"action": "list"})["status"] == "ok"
    finally:
        broker.close()


def test_validation_and_closed_do_not_start_child():
    broker = UIABroker(worker_target=synthetic_worker)
    assert broker.call({"action": "list", "bad": object()})["status"] == "refused"
    assert broker.call({"action": "list"}, timeout=float("nan"))["status"] == "refused"
    assert broker._process is None
    broker.close()
    assert broker.call({"action": "list"})["error"]["code"] == "CLOSED"


@pytest.mark.parametrize(
    "action,status,may_have_acted",
    [
        ("invoke", "unknown", True),
        ("wait_for", "refused", False),
    ],
)
def test_cancel_kills_reaps_and_recovers_without_replay(action, status, may_have_acted):
    broker = UIABroker(worker_target=synthetic_worker)
    cancelled = threading.Event()
    response = {}
    try:
        pid = broker.call({"action": "list"})["pid"]
        child = broker._process
        thread = threading.Thread(
            target=lambda: response.update(
                broker.call(
                    {"action": action, "hang": True},
                    timeout=5,
                    cancel_event=cancelled,
                )
            )
        )
        thread.start()
        time.sleep(0.08)
        started = time.monotonic()
        cancelled.set()
        thread.join(2)
        assert not thread.is_alive()
        assert time.monotonic() - started < 2
        assert response["status"] == status
        assert response["cancelled"]
        assert response["error"]["code"] == "CANCELLED"
        assert response["may_have_acted"] is may_have_acted
        assert not response["replayed"]
        assert child._closed
        assert broker._process is None
        assert all(process.pid != pid for process in multiprocessing.active_children())
        assert not any(t.name == "minano-uia-pipe" for t in threading.enumerate())
        assert broker.call({"action": "list"})["pid"] != pid
    finally:
        cancelled.set()
        broker.close()


def test_precancelled_request_never_starts_or_dispatches():
    broker = UIABroker(worker_target=synthetic_worker)
    cancelled = threading.Event()
    cancelled.set()
    response = broker.call({"action": "invoke"}, cancel_event=cancelled)
    assert response["status"] == "refused"
    assert response["cancelled"]
    assert not response["may_have_acted"]
    assert broker._process is None
    broker.close()


def test_startup_counts_towards_hard_deadline_and_prevents_late_dispatch(monkeypatch):
    broker = UIABroker(worker_target=synthetic_worker)
    original = broker._start

    def slow_start():
        original()
        time.sleep(0.06)

    monkeypatch.setattr(broker, "_start", slow_start)
    try:
        response = broker.call({"action": "invoke"}, timeout=0.01)
        assert response["status"] == "error"
        assert response["error"]["code"] == "TIMEOUT"
        assert not response["may_have_acted"]
        assert broker._process is None
    finally:
        broker.close()


def test_wait_for_reserves_response_time_and_preserves_last_observation():
    broker = UIABroker(worker_target=synthetic_worker)
    try:
        assert broker.call({"action": "list"})["status"] == "ok"
        began = time.monotonic()
        response = broker.call({"action": "wait_for", "until_deadline": True}, timeout=0.3)
        assert time.monotonic() - began < 0.4
        assert response["error"]["code"] == "CONDITION_TIMEOUT"
        assert response["data"]["last_observation"] == {"value": "pending"}
        assert broker._process is not None
    finally:
        broker.close()


@pytest.mark.skipif(sys.platform != "win32", reason="Windows handle isolation adapter")
def test_isolated_spawn_whitelists_only_nul_stdio_and_preserves_global_factory(monkeypatch):
    import _winapi
    from subprocess import STARTF_USESTDHANDLES

    from minano_computer_use import isolated_spawn

    original = isolated_spawn._create_process
    global_original = _winapi.CreateProcess
    observed = []

    def checked_create(*args):
        startup = args[-1]
        handles = [startup.hStdInput, startup.hStdOutput, startup.hStdError]
        assert startup.dwFlags & STARTF_USESTDHANDLES
        assert args[4] is True
        assert startup.lpAttributeList == {"handle_list": handles}
        assert len(set(handles)) == 3
        assert all(os.get_handle_inheritable(handle) for handle in handles)
        assert all(_winapi.GetFileType(handle) == _winapi.FILE_TYPE_CHAR for handle in handles)
        observed.extend(handles)
        return original(*args)

    monkeypatch.setattr(isolated_spawn, "_create_process", checked_create)
    broker = UIABroker(worker_target=synthetic_worker)
    try:
        assert broker.call({"action": "list"})["status"] == "ok"
        assert observed
        assert _winapi.CreateProcess is global_original
        for handle in observed:
            with pytest.raises(OSError):
                os.get_handle_inheritable(handle)
    finally:
        broker.close()
    assert broker._process is None


@pytest.mark.skipif(sys.platform != "win32", reason="Windows handle isolation adapter")
def test_isolated_spawn_failed_create_closes_all_local_stdio_handles(monkeypatch):
    from minano_computer_use import isolated_spawn

    observed = []

    def failed_create(*args):
        observed.extend(args[-1].lpAttributeList["handle_list"])
        raise OSError("injected isolated spawn failure")

    monkeypatch.setattr(isolated_spawn, "_create_process", failed_create)
    broker = UIABroker(worker_target=synthetic_worker)
    try:
        response = broker.call({"action": "invoke"})
        assert response["status"] == "error"
        assert response["error"]["code"] == "WORKER_FAILED"
        assert not response["may_have_acted"]
        assert broker._process is None
        assert broker._connection is None
        assert len(observed) == 3
        for handle in observed:
            with pytest.raises(OSError):
                os.get_handle_inheritable(handle)
    finally:
        broker.close()
