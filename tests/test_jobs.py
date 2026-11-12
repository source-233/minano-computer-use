import ctypes
import os
import sys
import threading
import time

import pytest

from minano_computer_use.jobs import JobRunner, OwnedJob, ServiceError, validate_argv


class Kernel:
    def __init__(self, assign=True):
        self.events = []
        self.assign = assign

    def CreateJobObjectW(self, *args):
        return 901

    def SetInformationJobObject(self, *args):
        return True

    def InitializeProcThreadAttributeList(self, buffer, count, flags, size):
        ctypes.cast(size, ctypes.POINTER(ctypes.c_size_t)).contents.value = 64
        return buffer is not None

    def UpdateProcThreadAttribute(self, *args):
        return True

    def DeleteProcThreadAttributeList(self, *args):
        pass

    def CreateProcessW(self, *args):
        from minano_computer_use.jobs import _ProcessInfo

        assert args[5] & 0x4  # CREATE_SUSPENDED
        assert args[5] & 0x80000  # EXTENDED_STARTUPINFO_PRESENT
        info = ctypes.cast(args[-1], ctypes.POINTER(_ProcessInfo)).contents
        info.process, info.thread, info.pid = 902, 903, 41005
        self.events.append("create_suspended")
        return True

    def AssignProcessToJobObject(self, *args):
        self.events.append("assign")
        return self.assign

    def ResumeThread(self, *args):
        self.events.append("resume")
        return 1

    def CloseHandle(self, *args):
        return True

    def TerminateProcess(self, *args):
        self.events.append("terminate_suspended")
        return True

    def WaitForSingleObject(self, *args):
        return 0


@pytest.mark.skipif(os.name != "nt", reason="Windows inherited handles")
@pytest.mark.parametrize("assign", [True, False])
def test_containment_order_and_fail_closed(monkeypatch, assign):
    kernel = Kernel(assign)
    monkeypatch.setattr("minano_computer_use.jobs.kernel32", lambda: kernel)
    if assign:
        job = OwnedJob([sys.executable], capture=False)
        job.close()
        assert kernel.events == ["create_suspended", "assign", "resume"]
    else:
        with pytest.raises(ServiceError):
            OwnedJob([sys.executable], capture=False)
        assert kernel.events == ["create_suspended", "assign", "terminate_suspended"]


@pytest.mark.parametrize("argv", [[], "cmd text", [None], [""], ["exe", "x\0y"]])
def test_argv_validation(argv):
    with pytest.raises(ValueError):
        validate_argv(argv)


@pytest.mark.skipif(os.name != "nt", reason="Windows Job Objects")
def test_owned_child_bounded_output_and_real_exit():
    runner = JobRunner()
    try:
        data = runner.run(
            [
                sys.executable,
                "-c",
                "import sys;sys.stdout.write('x'*1500000);sys.stderr.write('err')",
            ],
            deadline=time.monotonic() + 10,
        )
        assert data["exit_code"] == 0
        assert data["stdout_bytes"] == 1500000
        assert data["stdout_retained_bytes"] == 524288
        assert len(data["stdout"]) <= 16384
        assert data["stderr"] == "err"
        assert not runner.protected_pids
    finally:
        runner.close()


@pytest.mark.skipif(os.name != "nt", reason="Windows Job Objects")
@pytest.mark.parametrize("mode", ["cancel", "timeout"])
def test_cancel_or_timeout_owned_child_tree_and_reap(tmp_path, mode):
    marker = tmp_path / "grandchild-must-not-run.txt"
    grandchild = (
        f"import time,pathlib;time.sleep(1);pathlib.Path({str(marker)!r}).write_text('bad')"
    )
    child = (
        "import subprocess,sys,time;subprocess.Popen([sys.executable,'-c',"
        f"{grandchild!r}]);time.sleep(30)"
    )
    event = threading.Event()
    timer = threading.Timer(0.35, event.set)
    runner = JobRunner()
    if mode == "cancel":
        timer.start()
    try:
        with pytest.raises(ServiceError) as info:
            runner.run(
                [sys.executable, "-c", child],
                cancel_event=event,
                deadline=time.monotonic() + (10 if mode == "cancel" else 0.35),
            )
        assert info.value.code == ("CANCELLED" if mode == "cancel" else "TIMEOUT")
        assert info.value.acted
        assert not runner.protected_pids
        time.sleep(1.1)
        assert not marker.exists()
    finally:
        timer.cancel()
        runner.close()


@pytest.mark.skipif(os.name != "nt", reason="Windows Job Objects")
def test_timeout_owned_child_has_unknown_outcome():
    runner = JobRunner()
    try:
        with pytest.raises(ServiceError) as info:
            runner.run(
                [sys.executable, "-c", "import time;time.sleep(30)"],
                deadline=time.monotonic() + 0.1,
            )
        assert info.value.code == "TIMEOUT" and info.value.acted
        assert not runner.protected_pids
    finally:
        runner.close()


@pytest.mark.skipif(os.name != "nt", reason="Windows Job Objects")
def test_large_output_uses_artifact_store_safe_writer(tmp_path):
    import json

    from minano_computer_use.trace import ArtifactStore

    store = ArtifactStore(tmp_path)
    runner = JobRunner(store)
    try:
        data = runner.run(
            [sys.executable, "-c", "print('x'*20000)"], deadline=time.monotonic() + 10
        )
        path = data["stdout_artifact"]
        with open(path, encoding="utf-8") as artifact:
            saved = json.load(artifact)
        assert len(saved["text"]) >= 20000
        assert not saved["truncated"]
    finally:
        runner.close()
        store.close()


@pytest.mark.skipif(os.name != "nt", reason="Windows process launch")
def test_regular_launch_survives_service_close_and_self_fixture_exits(tmp_path):
    from minano_computer_use.automation_services import AutomationServices

    marker = tmp_path / "ordinary-owned-fixture.txt"
    script = f"import pathlib,time;time.sleep(.3);pathlib.Path({str(marker)!r}).write_text('ok')"
    services = AutomationServices()
    outcome = services.process("launch", argv=[sys.executable, "-c", script])
    assert outcome["status"] == "invoked"
    assert not outcome["data"]["owned"] and not outcome["data"]["contained"]
    handle = services.adapter.open_process(outcome["data"]["pid"])
    try:
        services.close()
        deadline = time.monotonic() + 5
        while handle.poll() is None and time.monotonic() < deadline:
            time.sleep(0.02)
        assert handle.poll() == 0
        assert marker.read_text() == "ok"
    finally:
        # This handle is bound to our own fixture only, never an external app.
        handle.close()
