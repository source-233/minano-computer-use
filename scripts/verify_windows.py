"""Opt-in real UIA smoke against our own fixture, not Calculator or a game."""

import ctypes
import json
import subprocess
import sys
import time
from ctypes import wintypes as w
from pathlib import Path


def main() -> None:
    if sys.platform != "win32":
        raise SystemExit("Windows required")
    from minano_computer_use.runtime import Runtime

    user = ctypes.WinDLL("user32", use_last_error=True)
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
    user.SendMessageW.argtypes = [w.HWND, w.UINT, w.WPARAM, w.LPARAM]
    user.SendMessageW.restype = ctypes.c_ssize_t
    user.PostMessageW.argtypes = [w.HWND, w.UINT, w.WPARAM, w.LPARAM]
    user.PostMessageW.restype = w.BOOL
    fixture = Path(__file__).resolve().parents[1] / "tests/windows_fixture.py"
    child = subprocess.Popen(
        [sys.executable, str(fixture)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    runtime = None
    hwnd = 0
    report: dict = {"native_input_used": False, "checks": {}}

    def text(handle: int) -> str:
        buffer = ctypes.create_unicode_buffer(1024)
        copied = ctypes.c_size_t()
        if not user.SendMessageTimeoutW(
            handle,
            0x000D,
            len(buffer),
            ctypes.addressof(buffer),
            0x0002,
            2000,
            ctypes.byref(copied),
        ):
            raise ctypes.WinError(ctypes.get_last_error())
        return buffer.value

    try:
        assert child.stdout is not None
        info = json.loads(child.stdout.readline())
        hwnd = info["hwnd"]
        report["fixture"] = info
        runtime = Runtime(allow_native=False)
        target = f"hwnd:{hwnd:#x}"
        tree = runtime.window("tree", target=target, depth=4, max_nodes=100, timeout=10)
        report["tree"] = tree
        assert tree["status"] in {"ok", "verified"}, tree
        value = "Minano verified 你好"
        changed = runtime.window(
            "set_value",
            target=target,
            selector={"automation_id": "1001"},
            value=value,
            timeout=5,
        )
        report["set_value"] = changed
        assert changed["status"] == "verified", changed
        assert text(info["edit"]) == value
        report["checks"]["set_value_native_readback"] = True
        invoked = runtime.window("invoke", target=target, selector="Apply", timeout=5)
        report["invoke"] = invoked
        assert invoked["status"] == "invoked", invoked
        deadline = time.monotonic() + 2
        while text(info["status"]) != "Invocations:1" and time.monotonic() < deadline:
            time.sleep(0.02)
        assert text(info["status"]) == "Invocations:1"
        report["checks"]["invoke_native_counter"] = True
        toggled = runtime.window("toggle", target=target, selector="Enabled", timeout=5)
        report["toggle"] = toggled
        assert toggled["status"] == "verified", toggled
        assert user.SendMessageW(info["checkbox"], 0x00F0, 0, 0) == 1
        report["checks"]["toggle_native_readback"] = True
        ambiguous = runtime.window("invoke", target=target, selector="Duplicate", timeout=5)
        report["ambiguous"] = ambiguous
        assert ambiguous["status"] == "refused", ambiguous
        unsupported = runtime.window(
            "invoke",
            target=target,
            selector={"automation_id": "1001"},
            timeout=5,
        )
        report["unsupported"] = unsupported
        assert unsupported["status"] == "refused", unsupported
        report["checks"]["ambiguous_and_unsupported_refused"] = True
        assert text(info["status"]) == "Invocations:1"
        report["checks"]["no_unrequested_second_invoke"] = True
        found = runtime.window(
            "find",
            target=target,
            query="Apply",
            match="contains",
            include_tokens=True,
            timeout=5,
        )
        report["find"] = found
        assert found["status"] == "ok", found
        inspected = runtime.window(
            "inspect",
            target=target,
            selector={"automation_id": "1001"},
            include_tokens=True,
            timeout=5,
        )
        report["token_issued"] = inspected
        token = inspected["data"]["token"]
        token_use = runtime.window("inspect", target=target, token=token, timeout=5)
        report["token_use"] = token_use
        assert token_use["status"] == "ok", token_use
        rejected = runtime.window("inspect", target=target, token="invalid-token", timeout=5)
        assert rejected["status"] == "refused", rejected
        report["checks"]["token_and_invalid_token"] = True
        delayed = runtime.window("invoke", target=target, selector="Delayed apply", timeout=5)
        assert delayed["status"] == "invoked", delayed
        waited = runtime.window(
            "wait_for",
            target=target,
            selector="Delayed ready",
            condition={"exists": True},
            poll_interval=0.02,
            timeout=2,
        )
        report["wait_for"] = waited
        assert waited["status"] == "verified", waited
        report["checks"]["delayed_wait_for"] = True
        budgeted = runtime.window(
            "tree",
            target=target,
            max_output_chars=2048,
            max_output_bytes=4096,
            timeout=5,
        )
        report["bounded_tree"] = budgeted
        assert budgeted["status"] == "ok", budgeted
        assert len(json.dumps(budgeted, ensure_ascii=False).encode()) <= 4096
        report["checks"]["budgeted_tree"] = True
        # 组合观察只读现有自有窗口；窗口后端不借用真实键鼠或隐式激活。
        state = runtime.window(
            "state",
            target=target,
            include_ax=True,
            include_screenshot=True,
            capture_mode="printwindow",
            allow_fallback=False,
            depth=4,
            max_nodes=100,
            timeout=10,
        )
        frame = state.pop("frame", None)
        report["window_state"] = state
        assert state["status"] == "ok", state
        assert frame is not None, state
        data = state["data"]
        assert data["consistency"] == "non_atomic", data
        assert data["uia"]["root_binding"] == state["target_binding"], data
        assert data["screenshot"]["hwnd"] == hwnd, data
        assert data["screenshot"]["capture_backend"] == "printwindow", data
        assert not data["component_errors"], data
        report["checks"]["bound_state_explicit_printwindow"] = True
        from minano_computer_use.input_context import read_input_context
        from minano_computer_use.native import _Win32Adapter

        context = read_input_context(_Win32Adapter(), state["target_binding"])
        report["read_only_input_context"] = context
        assert context["status"] == "partial", context
        assert context["non_atomic"] is True
        fields = context["fields"]
        assert fields["keyboard_layout"]["status"] == "known", context
        assert fields["keyboard_layout"]["value"]["hkl"] > 0, context
        assert fields["ime_open"]["status"] == "unknown", context
        assert fields["ime_conversion"]["status"] == "unknown", context
        report["checks"]["read_only_input_context_unknowns_preserved"] = True
        # 从同一次只读 state 获取能力令牌；写操作不再按名称/selector 重搜。
        token_state = runtime.window(
            "state",
            target=target,
            include_ax=True,
            include_screenshot=False,
            include_tokens=True,
            depth=4,
            max_nodes=100,
            timeout=10,
        )
        report["window_state_tokens"] = token_state
        assert token_state["status"] == "ok", token_state
        assert "frame" not in token_state, token_state
        token_data = token_state["data"]
        binding = token_state["target_binding"]
        assert binding["hwnd"] == hwnd and binding["pid"] == info["pid"], binding
        assert binding["process_creation_time"] is not None, binding
        assert token_data["target_binding"] == binding, token_data
        assert token_data["uia"]["root_binding"] == binding, token_data
        assert not token_data["component_errors"], token_data
        assert "screenshot" not in token_data, token_data
        assert token_data["uia"]["truncated"] is False, token_data
        nodes = token_data["uia"]["nodes"]
        edit_nodes = [node for node in nodes if node.get("automation_id") == "1001"]
        apply_nodes = [node for node in nodes if node.get("automation_id") == "1002"]
        assert len(edit_nodes) == len(apply_nodes) == 1, nodes
        edit_node, apply_node = edit_nodes[0], apply_nodes[0]
        assert apply_node["name"] == "Apply", apply_node
        for node in (edit_node, apply_node):
            assert isinstance(node.get("token"), str) and node["token"], node
            assert 0 < node["token_ttl_seconds"] <= 60, node
        assert edit_node["token"] != apply_node["token"], nodes
        report["checks"]["state_tokens_exact_root_binding"] = True
        token_value = "Minano state token verified 你好"
        token_changed = runtime.window(
            "set_value",
            target=target,
            token=edit_node["token"],
            value=token_value,
            timeout=5,
        )
        report["state_token_set_value"] = token_changed
        assert token_changed["status"] == "verified", token_changed
        assert text(info["edit"]) == token_value
        assert text(info["status"]) == "Invocations:1"
        report["checks"]["state_token_set_value_native_readback"] = True
        token_invoked = runtime.window(
            "invoke", target=target, token=apply_node["token"], timeout=5
        )
        report["state_token_invoke"] = token_invoked
        assert token_invoked["status"] == "invoked", token_invoked
        deadline = time.monotonic() + 2
        while text(info["status"]) != "Invocations:2" and time.monotonic() < deadline:
            time.sleep(0.02)
        assert text(info["status"]) == "Invocations:2"
        report["checks"]["state_token_invoke_native_counter"] = True
        report["passed"] = True
    finally:
        if runtime is not None:
            runtime.close()
        if hwnd:
            user.PostMessageW(hwnd, 0x0010, 0, 0)
        try:
            child.wait(timeout=3)
        except subprocess.TimeoutExpired:
            child.kill()
            child.wait(timeout=3)
        if child.stderr is not None:
            report["fixture_stderr"] = child.stderr.read()
        report["fixture_exit_code"] = child.returncode
        output = Path(__file__).resolve().parents[1] / ".artifacts/windows-smoke.json"
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps({"report": str(output), **report["checks"]}, ensure_ascii=False))


if __name__ == "__main__":
    main()
