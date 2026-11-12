import os
import threading
import time

import pytest

from minano_computer_use.automation_services import AutomationServices
from minano_computer_use.jobs import ServiceError


class Handle:
    def __init__(self, adapter, pid, creation=100):
        self.adapter, self.pid, self.creation = adapter, pid, creation
        self.closed, self.killed = False, False

    def close(self):
        self.closed = True

    def terminate(self):
        self.killed = True
        self.adapter.kills.append(self.pid)

    def poll(self):
        if self.adapter.wait_error:
            raise ServiceError("WIN32_ERROR", "WaitForSingleObject failed")
        return 0 if self.killed or self.adapter.exited else None

    def input_idle(self, milliseconds):
        if self.adapter.idle_error:
            raise ServiceError("INPUT_IDLE_UNAVAILABLE", "No message loop")
        return self.adapter.idle


class Adapter:
    def __init__(self):
        self.entries = [{"pid": 41001, "name": "fixture.exe", "process_creation_time": 100}]
        self.handles, self.kills, self.closes = [], [], []
        self.wait_error, self.exited, self.idle_error, self.idle = False, True, False, True
        self.stale = False
        self.window_entries = [{"pid": 41001, "hwnd": 501, "title": "Fixture", "visible": True}]

    def inventory(self):
        return self.entries

    def open_process(self, pid, creation=None, *, terminate=False):
        if self.stale:
            raise ServiceError("STALE_PROCESS", "Identity changed")
        handle = Handle(self, pid, creation or 100)
        self.handles.append(handle)
        return handle

    def windows(self):
        return self.window_entries

    def resolve_window(self, target):
        assert target == "Fixture"
        return self.window_entries[0]

    def validate_window(self, window, creation):
        if self.stale:
            raise ServiceError("STALE_TARGET", "Identity changed")

    def close_window(self, window, creation):
        self.validate_window(window, creation)
        self.closes.append(window["hwnd"])

    def shell_execute(self, path, args, cwd):
        return {"path": path, "args": args, "mode": "shell_execute"}

    def launch_detached(self, argv, cwd):
        return {
            "pid": 41003,
            "owned": False,
            "contained": False,
            "mode": "detached_create_process",
            "argv": argv,
            "cwd": cwd,
        }

    def cursor(self):
        return {"x": 1, "y": 2}

    def displays(self):
        return [{"index": 0, "dpi": [144, 144], "scale": 1.5, "primary": True}]


@pytest.mark.parametrize("startup,call", [(False, False), (True, False), (False, True)])
def test_dual_gates(startup, call):
    adapter = Adapter()
    services = AutomationServices(startup, startup, adapter=adapter)
    assert services.process("shell", command="never", allow_shell=call)["status"] == "refused"
    assert services.process("kill", pid=41001, allow_process_control=call)["status"] == "refused"
    assert services.window("close", "Fixture", allow_process_control=call)["status"] == "refused"
    assert adapter.kills == adapter.closes == []


def test_inventory_is_not_window_inventory():
    adapter = Adapter()
    adapter.window_entries = []
    data = AutomationServices(adapter=adapter).process()["data"]
    assert data == adapter.entries


def test_launch_argv_and_shell_execute_are_not_raw_command():
    services = AutomationServices(adapter=Adapter())
    assert services.process("launch", command="echo nope")["status"] == "refused"
    data = services.process("launch", exe="https://example.invalid/", shell=True)["data"]
    assert data["mode"] == "shell_execute"
    assert services.process("launch", argv=["one"], exe="two")["status"] == "refused"
    assert services.process("launch", exe="x", args="not-list")["status"] == "refused"


def test_ambiguous_kill_and_explicit_all():
    adapter = Adapter()
    adapter.entries.append({"pid": 41002, "name": "fixture.exe", "process_creation_time": 100})
    services = AutomationServices(allow_process_control=True, adapter=adapter)
    denied = services.process("kill", name="fixture.exe", allow_process_control=True)
    assert denied["error"]["code"] == "AMBIGUOUS_TARGET"
    assert not adapter.kills
    outcome = services.process("kill", name="fixture.exe", all=True, allow_process_control=True)
    assert outcome["status"] == "verified"
    assert adapter.kills == [41001, 41002]
    assert all(handle.closed for handle in adapter.handles)


def test_own_process_refused_and_stale_identity_refused():
    adapter = Adapter()
    services = AutomationServices(allow_process_control=True, adapter=adapter)
    adapter.entries[0]["pid"] = os.getpid()
    assert (
        services.process("kill", pid=os.getpid(), allow_process_control=True)["error"]["code"]
        == "PROTECTED_PROCESS"
    )
    adapter.entries[0]["pid"] = 41001
    adapter.stale = True
    assert (
        services.process("kill", pid=41001, allow_process_control=True)["error"]["code"]
        == "STALE_PROCESS"
    )
    assert not adapter.kills


def test_ancestor_is_protected_and_launch_cannot_alias_shell():
    adapter = Adapter()
    adapter.entries.append(
        {
            "pid": os.getpid(),
            "parent_pid": 41001,
            "name": "server.exe",
            "process_creation_time": 100,
        }
    )
    services = AutomationServices(allow_process_control=True, adapter=adapter)
    assert (
        services.process("kill", pid=41001, allow_process_control=True)["error"]["code"]
        == "PROTECTED_PROCESS"
    )
    assert not adapter.kills
    for executable in ("cmd.exe", "powershell.exe", "pwsh.exe"):
        assert (
            services.process("launch", argv=[executable, "-command", "nope"])["error"]["code"]
            == "SHELL_REQUIRED"
        )


def test_launch_application_survives_close_without_owned_job():
    services = AutomationServices(adapter=Adapter())
    outcome = services.process("launch", exe="fixture.exe", args=["space argument"])
    assert outcome["status"] == "invoked"
    assert not outcome["data"]["owned"]
    assert outcome["data"]["argv"] == ["fixture.exe", "space argument"]
    services.close()


def test_wait_failed_is_not_exit_and_wait_deadline_is_preserved():
    adapter = Adapter()
    adapter.wait_error = True
    services = AutomationServices(adapter=adapter)
    assert services.process("wait", pid=41001)["status"] == "error"
    adapter.wait_error, adapter.exited = False, False
    outcome = services.process("wait", pid=41001, deadline=time.monotonic() + 0.02)
    assert outcome["error"]["code"] == "TIMEOUT"
    assert all(handle.closed for handle in adapter.handles)


def test_wait_file_baseline_and_stable_new_file(tmp_path, monkeypatch):
    (tmp_path / "old.txt").write_text("old")
    services = AutomationServices(adapter=Adapter())
    baseline_ready = threading.Event()
    original_glob = type(tmp_path).glob

    def observed_glob(path, pattern, **kwargs):
        yield from original_glob(path, pattern, **kwargs)
        if path == tmp_path and pattern == "*.txt":
            baseline_ready.set()

    monkeypatch.setattr(type(tmp_path), "glob", observed_glob)

    def create_after_baseline():
        if baseline_ready.wait(2):
            (tmp_path / "new.txt").write_text("new")

    thread = threading.Thread(target=create_after_baseline)
    thread.start()
    try:
        outcome = services.process("wait_for_file", dir=str(tmp_path), glob="*.txt", timeout=2)
    finally:
        thread.join()
    assert outcome["status"] == "verified", outcome
    assert outcome["data"]["path"].endswith("new.txt")
    assert outcome["data"]["stable_seconds"] >= 0.2
    assert services.process("wait_for_file", dir=str(tmp_path), glob="../*")["status"] == "refused"


@pytest.mark.parametrize("ready", ["exists", "visible", "input_idle"])
def test_window_actual_readiness(ready):
    outcome = AutomationServices(adapter=Adapter()).process(
        "wait_for_window", query="Fixture", ready=ready
    )
    assert outcome["status"] == "verified"
    assert outcome["data"]["actual_mode"] == ready


def test_window_ambiguous_and_idle_unsupported():
    adapter = Adapter()
    services = AutomationServices(adapter=adapter)
    adapter.idle_error = True
    assert (
        services.process("wait_for_window", query="Fixture", ready="input_idle")["error"]["code"]
        == "INPUT_IDLE_UNAVAILABLE"
    )
    adapter.window_entries *= 2
    assert (
        services.process("wait_for_window", query="Fixture")["error"]["code"] == "AMBIGUOUS_TARGET"
    )


def test_close_is_delivery_not_verified_and_cancel_has_no_effect():
    adapter = Adapter()
    services = AutomationServices(allow_process_control=True, adapter=adapter)
    outcome = services.window("close", "Fixture", allow_process_control=True)
    assert outcome["status"] == "invoked"
    assert adapter.closes == [501]
    event = threading.Event()
    event.set()
    outcome = services.window("close", "Fixture", allow_process_control=True, cancel_event=event)
    assert outcome["cancelled"] and adapter.closes == [501]


def test_readonly_system_services():
    services = AutomationServices(adapter=Adapter())
    assert services.system("cursor")["data"] == {"x": 1, "y": 2}
    assert services.system("displays")["data"][0]["primary"]


@pytest.mark.skipif(os.name != "nt", reason="Windows process inventory")
def test_real_readonly_inventory_contains_server_pid():
    outcome = AutomationServices().process()
    assert outcome["status"] == "ok"
    own = next(item for item in outcome["data"] if item["pid"] == os.getpid())
    assert own["process_creation_time"] > 0


@pytest.mark.skipif(os.name != "nt", reason="Windows owned command runner")
@pytest.mark.parametrize(
    "kind,command",
    [
        ("cmd", "echo MINANO_SHELL_FIXTURE"),
        ("cmd", '"' + os.sys.executable + '" -c "print(\'MINANO_SHELL_FIXTURE\')"'),
        ("powershell", "[Console]::Write('MINANO_SHELL_FIXTURE')"),
    ],
)
def test_real_owned_gated_shell_output(kind, command):
    services = AutomationServices(allow_shell=True)
    try:
        outcome = services.process("shell", command=command, shell_kind=kind, allow_shell=True)
        assert outcome["status"] == "ok"
        assert "MINANO_SHELL_FIXTURE" in outcome["data"]["stdout"]
        assert outcome["data"]["contained"]
    finally:
        services.close()
