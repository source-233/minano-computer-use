"""Opt-in real cross-process UIA child proof; not a UWP compatibility claim."""

import ctypes
import json
import queue
import subprocess
import sys
import threading
from ctypes import wintypes as w
from pathlib import Path


def launch(script, *arguments):
    # Bypass the venv redirector: the Popen PID must equal native HWND ownership.
    child = subprocess.Popen(
        [sys._base_executable, str(script), *map(str, arguments)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    line = queue.Queue()
    thread = threading.Thread(target=lambda: line.put(child.stdout.readline()), daemon=True)
    thread.start()
    try:
        info = json.loads(line.get(timeout=8))
        thread.join(1)
        assert info["pid"] == child.pid
        return child, info
    except BaseException:
        child.kill()
        child.wait(timeout=3)
        thread.join(1)
        raise


def main():
    from minano_computer_use import win32
    from minano_computer_use.runtime import Runtime

    root = Path(__file__).resolve().parents[1]
    user = ctypes.WinDLL("user32", use_last_error=True)
    user.PostMessageW.argtypes = [w.HWND, w.UINT, w.WPARAM, w.LPARAM]
    user.PostMessageW.restype = w.BOOL
    user.SendMessageTimeoutW.argtypes = [
        w.HWND,
        w.UINT,
        w.WPARAM,
        w.LPARAM,
        w.UINT,
        w.UINT,
        ctypes.POINTER(ctypes.c_size_t),
    ]
    user.SendMessageTimeoutW.restype = ctypes.c_ssize_t
    processes, info, runtime = [], {}, None
    report = {"native_input_used": False, "passed": False, "checks": {}}
    before = win32.observations()
    report["desktop_before"] = before
    try:
        host, info = launch(root / "tests/windows_fixture.py")
        processes.append(host)
        child, embedded = launch(root / "tests/windows_hosted_edit.py", info["hwnd"])
        processes.append(child)
        report["host"], report["child"] = info, embedded
        assert host.pid != child.pid
        native = win32.child_window_identity(info["hwnd"], host.pid, embedded["hwnd"], child.pid)
        assert native["process_creation_time"] > 0
        report["checks"]["native_cross_process_descendant"] = True
        runtime = Runtime()
        target = f"hwnd:{info['hwnd']}"
        response = runtime.window(
            "state",
            target,
            include_screenshot=False,
            include_tokens=True,
            depth=8,
            max_nodes=100,
            timeout=10,
        )
        report["state"] = response
        assert response["status"] == "ok", response
        node = next(
            n
            for n in response["data"]["uia"]["nodes"]
            if n["pid"] == child.pid and n["automation_id"] == "2001"
        )
        assert response["target_binding"]["pid"] == host.pid
        assert node["token"]
        report["checks"]["state_keeps_host_and_element_identity"] = True
        changed = runtime.window(
            "set_value", target, token=node["token"], value="hosted verified 你好", timeout=5
        )
        report["token_set_value"] = changed
        assert changed["status"] == "verified", changed
        text = ctypes.create_unicode_buffer(512)
        copied = ctypes.c_size_t()
        assert user.SendMessageTimeoutW(
            embedded["hwnd"],
            0x000D,
            len(text),
            ctypes.addressof(text),
            2,
            2000,
            ctypes.byref(copied),
        )
        assert text.value == "hosted verified 你好"
        report["checks"]["token_write_native_readback"] = True
        after = win32.observations()
        report["desktop_after"] = after
        assert after["foreground_hwnd"] == before["foreground_hwnd"], "Foreground changed"
        assert before["cursor_available"] and after["cursor_available"]
        assert after["cursor"] == before["cursor"], (
            "External cursor movement or provider side effect"
        )
        report["checks"]["foreground_and_cursor_unchanged"] = True
        report["passed"] = True
    finally:
        if runtime is not None:
            runtime.close()
        if info:
            user.PostMessageW(info["hwnd"], 0x0010, 0, 0)
        exits = []
        for child in processes:
            try:
                child.wait(timeout=3)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait(timeout=3)
            exits.append(
                {"pid": child.pid, "exit_code": child.returncode, "stderr": child.stderr.read()}
            )
        report["fixture_exits"] = exits
        if any(entry["exit_code"] != 0 for entry in exits):
            report["passed"] = False
        output = root / ".artifacts/hosted-uia-smoke.json"
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(
            json.dumps(
                {"report": str(output), "passed": report["passed"], "checks": report["checks"]}
            )
        )
        assert all(entry["exit_code"] == 0 for entry in exits), exits


if __name__ == "__main__":
    main()
