"""Explicit Windows-only background-input smoke; touches only our child fixtures.

Run: uv run python scripts/verify_background.py
Evidence: .artifacts/background-smoke.json. Not a default pytest test.
"""

from __future__ import annotations

import ctypes
import hashlib
import io
import json
import subprocess
import sys
import tempfile
import time
import traceback
from contextlib import contextmanager
from ctypes import wintypes as w
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
COUNTERS = (
    "clicks",
    "move",
    "down",
    "up",
    "drag",
    "wheel",
    "hwheel",
    "key_down",
    "key_up",
    "char",
    "last_point",
    "last_wheel",
)


class OwnedObserver:
    """Bounded reads/private fixture commands, never arbitrary user-window writes."""

    def __init__(self):
        self.user = ctypes.WinDLL("user32", use_last_error=True)
        self.owned = set()

        def api(name, arguments, returns):
            function = getattr(self.user, name)
            function.argtypes, function.restype = arguments, returns
            return function

        self.send = api(
            "SendMessageTimeoutW",
            [
                w.HWND,
                w.UINT,
                w.WPARAM,
                w.LPARAM,
                w.UINT,
                w.UINT,
                ctypes.POINTER(ctypes.c_size_t),
            ],
            ctypes.c_ssize_t,
        )
        self.post = api("PostMessageW", [w.HWND, w.UINT, w.WPARAM, w.LPARAM], w.BOOL)
        self.foreground = api("GetForegroundWindow", [], w.HWND)
        self.cursor = api("GetCursorPos", [ctypes.POINTER(w.POINT)], w.BOOL)
        self.rect = api("GetClientRect", [w.HWND, ctypes.POINTER(w.RECT)], w.BOOL)
        self.map_point = api("ClientToScreen", [w.HWND, ctypes.POINTER(w.POINT)], w.BOOL)
        self.pid = api("GetWindowThreadProcessId", [w.HWND, ctypes.POINTER(w.DWORD)], w.DWORD)
        self.is_child = api("IsChild", [w.HWND, w.HWND], w.BOOL)
        self.is_window = api("IsWindow", [w.HWND], w.BOOL)
        self.set_dpi = api("SetThreadDpiAwarenessContext", [ctypes.c_void_p], ctypes.c_void_p)
        self.get_dpi = api("GetThreadDpiAwarenessContext", [], ctypes.c_void_p)
        self.awareness = api("GetAwarenessFromDpiAwarenessContext", [ctypes.c_void_p], ctypes.c_int)

    @contextmanager
    def physical(self):
        previous = self.set_dpi(ctypes.c_void_p(-4))
        if not previous:
            raise RuntimeError("Cannot establish physical DPI context")
        try:
            if self.awareness(self.get_dpi()) != 2:
                raise RuntimeError("Physical DPI context not proved")
            yield
        finally:
            if not self.set_dpi(previous):
                raise RuntimeError("Cannot restore DPI context")

    def observe(self):
        with self.physical():
            point = w.POINT()
            if not self.cursor(ctypes.byref(point)):
                raise ctypes.WinError(ctypes.get_last_error())
            return {"foreground_hwnd": int(self.foreground() or 0), "cursor": [point.x, point.y]}

    def command(self, hwnd, message, wp=0, lp=0):
        if hwnd not in self.owned:
            raise RuntimeError("Read/write command refused for non-owned HWND")
        answer = ctypes.c_size_t()
        if not self.send(hwnd, message, wp, lp, 0x0002 | 0x0020, 500, ctypes.byref(answer)):
            raise ctypes.WinError(ctypes.get_last_error())
        return answer.value

    def text(self, hwnd):
        buffer = ctypes.create_unicode_buffer(1025)
        copied = self.command(hwnd, 0x000D, len(buffer), ctypes.addressof(buffer))
        if copied >= len(buffer) - 1:
            raise RuntimeError("WM_GETTEXT readback possibly truncated")
        return buffer.value

    def counters(self, root):
        values = {name: self.command(root, 0x8001, index) for index, name in enumerate(COUNTERS)}
        values["last_wheel"] = ctypes.c_ssize_t(values["last_wheel"]).value
        return values

    def point(self, hwnd, dx=0, dy=0):
        with self.physical():
            rect = w.RECT()
            if not self.rect(hwnd, ctypes.byref(rect)):
                raise ctypes.WinError(ctypes.get_last_error())
            point = w.POINT((rect.right - rect.left) // 2 + dx, (rect.bottom - rect.top) // 2 + dy)
            if not 0 <= point.x < rect.right or not 0 <= point.y < rect.bottom:
                raise RuntimeError("Fixture point outside client rect")
            if not self.map_point(hwnd, ctypes.byref(point)):
                raise ctypes.WinError(ctypes.get_last_error())
            return {"x": point.x, "y": point.y}


def serializable(value):
    """Keep image evidence hashes/metadata, not screen pixels in JSON logs."""
    if hasattr(value, "image_bytes"):
        from PIL import Image

        with Image.open(io.BytesIO(value.image_bytes)) as image:
            dimensions = list(image.size)
        return {
            "metadata": value.metadata,
            "mime_type": value.mime_type,
            "image_bytes": len(value.image_bytes),
            "dimensions": dimensions,
            "sha256": hashlib.sha256(value.image_bytes).hexdigest(),
            "pixel_claim": "screen composite may include occlusion; no unoccluded-pixel claim",
        }
    if isinstance(value, dict):
        return {key: serializable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [serializable(item) for item in value]
    return value


def main() -> int:
    report = {
        "passed": False,
        "checks": {},
        "evidence": {},
        "unavailable": {},
        "native_input_used": False,
        "clipboard_used": False,
        "focus_command_used": False,
        "scope": "owned fixtures only",
        "cancellation_timeout": "not exercised here; separate fake-adapter/worker tests",
    }
    child = runtime = observer = None
    fixture_handles = []
    baseline = None
    failed = False
    temporary = tempfile.TemporaryDirectory(prefix="minano-background-")

    def check(name, operation):
        nonlocal failed
        before = observer.observe()
        entry = {"before": before}
        report["evidence"][name] = entry
        try:
            if before != baseline or before["foreground_hwnd"] in fixture_handles:
                raise RuntimeError("Desktop changed/user interference; no force-focus or replay")
            detail = operation()
            entry["result"] = serializable(detail)
            entry["after"] = observer.observe()
            if entry["after"] != before:
                report["desktop_uncertainty"] = "Foreground/cursor changed; cause not attributed"
                raise RuntimeError("Foreground/cursor changed; no restoration or replay")
            report["checks"][name] = True
            return detail
        except Exception as exc:
            entry["after"] = observer.observe()
            entry["error"] = str(exc)
            report["checks"][name] = False
            failed = True
            return None

    try:
        if sys.platform != "win32":
            raise RuntimeError("Windows required")
        from minano_computer_use.message_backend import BackgroundInput
        from minano_computer_use.runtime import Runtime

        observer = OwnedObserver()
        report["before_launch"] = baseline = observer.observe()
        if not baseline["foreground_hwnd"]:
            raise RuntimeError("Known non-fixture foreground required; will not activate anything")
        ready = Path(temporary.name) / "ready.json"
        stderr_path = Path(temporary.name) / "fixture-stderr.txt"
        with stderr_path.open("w", encoding="utf-8") as stderr:
            child = subprocess.Popen(
                # Windows venv python.exe may be a redirector with a different child PID.
                # Fixture is stdlib-only: launch the real interpreter for exact ownership.
                [sys._base_executable, str(ROOT / "tests/background_fixture.py"), str(ready)],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=stderr,
            )
        deadline = time.monotonic() + 8
        while not ready.exists() and child.poll() is None and time.monotonic() < deadline:
            time.sleep(0.02)
        if not ready.exists():
            raise RuntimeError("Owned fixture did not become ready within 8 seconds")
        info = json.loads(ready.read_text(encoding="utf-8"))
        report["fixture"] = info
        report["launcher_pid"] = child.pid
        target, unrelated = info["windows"]
        fixture_handles = [item["hwnd"] for item in info["windows"]]
        for item in info["windows"]:
            for key in ("hwnd", "edit", "button", "surface"):
                hwnd = item[key]
                actual_pid = w.DWORD()
                observer.pid(hwnd, ctypes.byref(actual_pid))
                if actual_pid.value != child.pid or info["pid"] != child.pid:
                    raise RuntimeError(
                        f"Owned HWND/PID mismatch: HWND={hwnd} actual={actual_pid.value} "
                        f"metadata={info['pid']} launcher={child.pid}"
                    )
                if key != "hwnd" and not observer.is_child(item["hwnd"], hwnd):
                    raise RuntimeError("Owned child ancestry mismatch")
                observer.owned.add(hwnd)
        report["checks"]["owned_root_child_same_pid"] = True
        report["after_launch"] = observer.observe()
        if report["after_launch"] != baseline:
            raise RuntimeError("Desktop changed during fixture launch; no restoration attempted")
        message = BackgroundInput()
        runtime = Runtime(message=message)
        root_target = f"hwnd:{target['hwnd']:#x}"

        def act(action, accepted=True):
            response = runtime.act(
                [action],
                target=root_target,
                input_mode="message",
                native=False,
                screenshot=False,
                timeout=5,
            )
            report.setdefault("action_log", []).append({"action": action, "response": response})
            if accepted:
                assert response["status"] in {"ok", "verified", "invoked"}, response
                assert response.get("delivery") == "message", response
                results = response["data"]["results"]
                assert len(results) == 1, response
                assert results[0]["result"]["status"] in {"ok", "verified", "invoked"}, response
            else:
                assert response["status"] in {"refused", "error", "unknown", "partial"}, response
                assert response["data"]["results"][0]["result"]["status"] == "refused", response
            return response

        def wait_for(predicate):
            deadline = time.monotonic() + 2
            while time.monotonic() < deadline:
                if predicate():
                    return
                time.sleep(0.02)
            raise AssertionError("Independent fixture response not observed within 2 seconds")

        def set_value():
            value = "Minano background verified 你好"
            response = act({"action": "set_value", "recipient": target["edit"], "value": value})
            actual = observer.text(target["edit"])
            assert actual == value, actual
            item = response["data"]["results"][0]["result"]
            assert item["status"] == "verified", item
            binding = item.get("target_binding", {})
            assert binding.get("hwnd") == target["hwnd"] and binding.get("pid") == child.pid, item
            assert binding.get("process_creation_time"), item
            return {"response": response, "WM_GETTEXT": actual}

        check("background_set_value_exact_readback", set_value)

        def mixed_text_set_value_ordered():
            actions = [
                {"action": "set_value", "recipient": target["edit"], "value": ""},
                {"action": "text", "recipient": target["edit"], "text": "A"},
                {"action": "set_value", "recipient": target["edit"], "value": "B"},
            ]
            response = runtime.act(
                actions,
                target=root_target,
                input_mode="message",
                native=False,
                screenshot=False,
                timeout=5,
            )
            # Read immediately: no sleep, polling, or artificial ordering barrier.
            actual = observer.text(target["edit"])
            evidence = {"actions": actions, "response": response, "WM_GETTEXT": actual}
            report["evidence"]["mixed_text_set_value_ordered"]["result"] = evidence
            report.setdefault("action_log", []).append({"actions": actions, "response": response})
            assert response["status"] in {"ok", "verified", "invoked"}, response
            assert response.get("delivery") == "message", response
            results = response["data"]["results"]
            assert len(results) == 3, response
            assert [item["result"]["status"] for item in results] == [
                "verified",
                "invoked",
                "verified",
            ], response
            assert actual == "B", actual
            return evidence

        check("mixed_text_set_value_ordered", mixed_text_set_value_ordered)

        def type_text():
            act({"action": "set_value", "recipient": target["edit"], "value": ""})
            response = act({"action": "text", "recipient": target["edit"], "text": "typed 你好"})
            wait_for(lambda: observer.text(target["edit"]) == "typed 你好")
            return {"response": response, "WM_GETTEXT": observer.text(target["edit"])}

        check("background_text_consumed", type_text)

        def keys():
            before = observer.counters(target["hwnd"])
            response = act({"action": "key", "recipient": target["surface"], "key": "F6"})
            wait_for(lambda: observer.counters(target["hwnd"])["key_up"] > before["key_up"])
            after = observer.counters(target["hwnd"])
            assert after["key_down"] == before["key_down"] + 1, after
            assert after["key_up"] == before["key_up"] + 1, after
            return {"response": response, "counters_before": before, "counters_after": after}

        check("background_key_consumed", keys)

        def click():
            before = observer.counters(target["hwnd"])
            # No recipient: exercise recursive child hit testing on a real standard button.
            response = act({"action": "click", **observer.point(target["button"])})
            wait_for(lambda: observer.counters(target["hwnd"])["clicks"] == before["clicks"] + 1)
            return {
                "response": response,
                "counters_before": before,
                "counters_after": observer.counters(target["hwnd"]),
            }

        check("background_button_click_consumed", click)

        def mouse():
            before = observer.counters(target["hwnd"])
            start = observer.point(target["surface"], -30, -15)
            end = observer.point(target["surface"], 30, 15)
            responses = [act({"action": "move", "recipient": target["surface"], **start})]
            responses.append(
                act(
                    {
                        "action": "drag",
                        "recipient": target["surface"],
                        **start,
                        "path": [observer.point(target["surface"]), end],
                        "seconds": 0.2,
                    }
                )
            )
            for horizontal, amount in ((False, 120), (True, -120)):
                responses.append(
                    act(
                        {
                            "action": "wheel",
                            "recipient": target["surface"],
                            **end,
                            "amount": amount,
                            "horizontal": horizontal,
                        }
                    )
                )
            wait_for(lambda: observer.counters(target["hwnd"])["hwheel"] == before["hwheel"] + 1)
            after = observer.counters(target["hwnd"])
            report["evidence"]["background_move_drag_wheel_consumed"].update(
                {
                    "responses": responses,
                    "counters_before": before,
                    "counters_after": after,
                }
            )
            assert after["move"] > before["move"] and after["drag"] > before["drag"], after
            assert after["down"] == before["down"] + 1 and after["up"] == before["up"] + 1, after
            assert after["wheel"] == before["wheel"] + 1 and after["last_wheel"] == -120, after
            with observer.physical():
                rect = w.RECT()
                assert observer.rect(target["surface"], ctypes.byref(rect))
            expected_x = (rect.right // 2 + 30) & 0xFFFF
            expected_y = (rect.bottom // 2 + 15) & 0xFFFF
            assert after["last_point"] == expected_x | (expected_y << 16), after
            report["checks"]["physical_to_recipient_client_coordinates"] = True
            report["checks"]["background_drag_path_consumed"] = True
            report["checks"]["background_vertical_horizontal_wheel_consumed"] = True
            return {
                "responses": responses,
                "counters_before": before,
                "counters_after": after,
                "physical_start": start,
                "physical_end": end,
            }

        check("background_move_drag_wheel_consumed", mouse)

        def reject_unrelated():
            before = observer.counters(unrelated["hwnd"])
            response = act(
                {"action": "key", "recipient": unrelated["surface"], "key": "F7"}, accepted=False
            )
            assert observer.counters(unrelated["hwnd"]) == before
            return {"response": response, "unrelated_counters": before}

        check("same_pid_unrelated_root_recipient_refused", reject_unrelated)

        def state():
            response = runtime.window(
                "state",
                target=root_target,
                include_ax=True,
                include_screenshot=True,
                rich=True,
                max_dim=800,
                format="jpeg",
                quality=75,
                depth=4,
                max_nodes=60,
                timeout=12,
            )
            report["evidence"]["combined_state"]["response"] = serializable(response)
            assert response["status"] in {"ok", "partial"}, response
            data = response["data"]
            assert data["consistency"] == "non_atomic", data
            binding = response["target_binding"]
            assert binding["hwnd"] == target["hwnd"] and binding["pid"] == child.pid, binding
            assert binding["process_creation_time"], binding
            assert data["target_binding"] == binding, data
            if "uia" not in data:
                raise AssertionError(f"UIA state unavailable: {data.get('component_errors')}")
            uia = data["uia"]
            assert uia.get("nodes"), uia
            assert uia["root_binding"] == binding and uia["target_binding"] == binding, uia
            assert all(node.get("pid") == child.pid for node in uia["nodes"]), uia
            assert uia["window"]["hwnd"] == target["hwnd"], uia
            interval = data["interval"]
            assert interval["started_at"] <= interval["finished_at"], interval
            for component in ("uia", "screenshot"):
                times = data["component_times"][component]
                assert interval["started_at"] <= times["started_at"] <= times["finished_at"], times
                assert times["finished_at"] <= interval["finished_at"], times
            report["checks"]["state_component_timestamps_non_atomic"] = True
            assert "focus" in uia and "selection" in uia, uia
            focus = uia["focus"]
            if focus.get("status") != "unavailable":
                index = focus.get("node_index")
                assert isinstance(index, int) and 0 <= index < len(uia["nodes"]), focus
            else:
                assert not any(key in focus for key in ("element", "name", "value", "path")), focus
                assert not uia["selection"].get("ranges"), uia["selection"]
            # This does not call SetFocus: SetFocus can activate even a NOACTIVATE root.
            report["unavailable"]["target_focused_selected_text"] = (
                "Not established: SetFocus may activate the parent; no focus command was issued. "
                f"Actual provider focus={focus!r}, selection={uia['selection']!r}"
            )
            report["checks"]["uia_scoped_focus_no_external_details"] = True
            rich = [node for node in uia["nodes"] if "class_name" in node]
            assert rich, uia
            report["checks"]["uia_rich_metadata_observed"] = True
            if "frame" not in response:
                raise AssertionError(f"Screenshot unavailable: {data.get('component_errors')}")
            frame = serializable(response["frame"])
            assert frame["mime_type"] == "image/jpeg" and max(frame["dimensions"]) <= 800, frame
            metadata = frame["metadata"]
            assert metadata.get("hwnd") == target["hwnd"], metadata
            assert all(metadata.get(field) == value for field, value in binding.items()), metadata
            assert metadata.get("quality") == 75, metadata
            report["checks"]["state_single_root_binding_uia_image"] = True
            return response

        check("combined_state", state)

        def selection_readback():
            packed = observer.command(target["hwnd"], 0x8003, 0, 5)
            actual = observer.command(target["edit"], 0x00B0)
            assert (packed & 0xFFFF, packed >> 16) == (0, 5), packed
            assert actual == packed, actual
            return {
                "EM_GETSEL": {"start": actual & 0xFFFF, "end": actual >> 16},
                "focus_hwnd": observer.command(target["hwnd"], 0x8001, 100),
                "claim": "Native selection only, not proof of UIA selected-text capability",
            }

        check("owned_selection_scalar_readback", selection_readback)
        report["after_operations"] = observer.observe()
        report["checks"]["foreground_cursor_unchanged"] = report["after_operations"] == baseline
        if not report["checks"]["foreground_cursor_unchanged"]:
            failed = True
    except Exception as exc:
        failed = True
        report["error"] = str(exc)
        report["traceback"] = traceback.format_exc()
    finally:
        if runtime is not None:
            try:
                runtime.close()
            except Exception as exc:
                failed = True
                report["runtime_cleanup_error"] = str(exc)
        if child is not None:
            if observer is not None and fixture_handles:
                observer.post(fixture_handles[0], 0x0010, 0, 0)
            try:
                child.wait(timeout=3)
            except subprocess.TimeoutExpired:
                report["fixture_forced_kill"] = True
                child.kill()  # Only the exact child we created, never title/PID rediscovery.
                child.wait(timeout=3)
            report["fixture_exit_code"] = child.returncode
            report["checks"]["fixture_reaped_zero_owned_windows"] = child.poll() is not None and (
                observer is None or not any(observer.is_window(hwnd) for hwnd in fixture_handles)
            )
            if not report["checks"]["fixture_reaped_zero_owned_windows"]:
                failed = True
            stderr_path = Path(temporary.name) / "fixture-stderr.txt"
            if stderr_path.exists():
                report["fixture_stderr"] = stderr_path.read_text(encoding="utf-8", errors="replace")
        if observer is not None:
            report["after_cleanup"] = observer.observe()
            if baseline is not None and report["after_cleanup"] != baseline:
                failed = True
                report["desktop_uncertainty"] = "Desktop changed; no restoration attempted"
        temporary.cleanup()
        report["passed"] = not failed and bool(report["checks"]) and all(report["checks"].values())
        report["selected_text_passed"] = False
        report["exit_code"] = 0 if report["passed"] else 1
        output = ROOT / ".artifacts/background-smoke.json"
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(
            json.dumps(
                {
                    "report": str(output),
                    "passed": report["passed"],
                    "checks": report["checks"],
                    "unavailable": report["unavailable"],
                },
                ensure_ascii=False,
            )
        )
    return report["exit_code"]


if __name__ == "__main__":
    raise SystemExit(main())
