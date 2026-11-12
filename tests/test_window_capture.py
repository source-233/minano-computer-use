from __future__ import annotations

import multiprocessing as mp
import threading
from pathlib import Path
from typing import Any

import pytest

from minano_computer_use.window_capture import capture_window


class FakeProcess:
    def __init__(self, target, args, daemon):
        self.target, self.args = target, args
        self.alive = False
        self.terminated = False
        self.killed = False
        self.joined = False
        self.closed = False

    def start(self):
        self.alive = True
        Path(self.args[-1]).write_bytes(b"pixels")

    def is_alive(self):
        return self.alive

    def terminate(self):
        self.terminated = True
        self.alive = False

    def join(self, timeout=None):
        self.joined = True

    def kill(self):
        self.killed = True
        self.alive = False

    def close(self):
        self.closed = True


class FakePipe:
    def __init__(self):
        self.value: Any = None
        self.closed = False

    def poll(self, timeout=0):
        return self.value is not None

    def recv(self):
        return self.value

    def send(self, value):
        self.value = value

    def close(self):
        self.closed = True


class FakeContext:
    def __init__(self):
        self.parent = FakePipe()
        self.child = FakePipe()
        self.process = None

    def Pipe(self, duplex=False):
        return self.parent, self.child

    def Process(self, **kwargs):
        self.process = FakeProcess(**kwargs)
        return self.process


def install_context(monkeypatch, context):
    import minano_computer_use.window_capture as module

    monkeypatch.setattr(mp, "get_context", lambda _: context)
    monkeypatch.setattr(
        module,
        "isolated_process",
        lambda ctx, target, args, daemon=True: ctx.Process(target=target, args=args, daemon=daemon),
    )


def test_child_result_and_reaping(monkeypatch):
    context = FakeContext()
    context.parent.value = {"data": b"pixels", "size": [2, 1], "rect": [0, 0, 2, 1]}
    install_context(monkeypatch, context)
    result = capture_window(10, 20, "printwindow", 1)
    assert result["data"] == b"pixels"
    assert context.process.joined
    assert context.process.closed
    assert context.child.closed
    assert context.parent.closed


def test_timeout_terminates_and_reaps_worker(monkeypatch):
    context = FakeContext()
    install_context(monkeypatch, context)
    with pytest.raises(TimeoutError):
        capture_window(10, 20, "printwindow", 0.01)
    assert context.process.terminated
    assert context.process.joined
    assert context.process.closed


def test_cancellation_terminates_and_reaps_worker(monkeypatch):
    context = FakeContext()
    install_context(monkeypatch, context)
    cancel = threading.Event()
    cancel.set()
    with pytest.raises(InterruptedError):
        capture_window(10, 20, "wgc", 1, cancel)
    assert context.process.terminated
    assert context.process.joined
    assert context.process.closed


def test_deadline_includes_spawn_and_escalates_stubborn_child(monkeypatch):
    import minano_computer_use.window_capture as module

    context = FakeContext()
    install_context(monkeypatch, context)
    clock = iter([0.0, 2.0])
    monkeypatch.setattr(module.time, "monotonic", lambda: next(clock))
    monkeypatch.setattr(FakeProcess, "terminate", lambda self: setattr(self, "terminated", True))
    with pytest.raises(TimeoutError):
        capture_window(10, 20, "printwindow", 1)
    assert context.process.killed
    assert context.process.joined
    assert context.process.closed


def test_child_does_not_interpret_black_frame_as_failure(monkeypatch, tmp_path):
    import minano_computer_use.window_capture as module

    sent = []

    class Connection:
        def send(self, value):
            sent.append(value)

        def close(self):
            pass

    monkeypatch.setattr(
        module, "_validate", lambda hwnd, pid: {"size": [1, 1], "rect": [0, 0, 1, 1]}
    )
    monkeypatch.setattr(module, "_printwindow", lambda hwnd, width, height: b"\0\0\0\0")
    path = tmp_path / "frame.bgra"
    module._child(Connection(), 10, 20, "printwindow", str(path))
    assert "error" not in sent[0]
    assert path.read_bytes() == b"\0\0\0\0"


def test_optional_wgc_import_is_only_attempted_for_wgc(monkeypatch):
    from minano_computer_use.capture import CaptureService

    monkeypatch.setattr(
        "minano_computer_use.win32.resolve_window",
        lambda _: {
            "hwnd": 10,
            "pid": 20,
            "rect": [0, 0, 2, 1],
            "visible": True,
            "minimized": False,
        },
    )

    def no_wgc(*args, **kwargs):
        raise RuntimeError("ModuleNotFoundError: No module named windows_capture")

    monkeypatch.setattr("minano_computer_use.window_capture.capture_window", no_wgc)
    with pytest.raises(RuntimeError, match="window capture unavailable"):
        CaptureService().screenshot("hwnd:10", capture_mode="wgc")


def test_wgc_surface_transform_unknown_and_window_attempt_order(monkeypatch):
    from minano_computer_use.capture import CaptureService

    monkeypatch.setattr(
        "minano_computer_use.win32.resolve_window",
        lambda _: {
            "hwnd": 10,
            "pid": 20,
            "rect": [100, 100, 102, 102],
            "visible": True,
            "minimized": False,
        },
    )
    attempts = []

    def worker(hwnd, pid, backend, timeout, cancel_event):
        attempts.append(backend)
        if backend == "printwindow":
            raise RuntimeError("provider returned failure")
        return {"data": b"\0" * 16, "size": [2, 2], "rect": [100, 100, 102, 102]}

    monkeypatch.setattr("minano_computer_use.window_capture.capture_window", worker)
    frame = CaptureService().screenshot("hwnd:10", capture_mode="window", region=[1, 0, 2, 2])
    assert attempts == ["printwindow", "wgc"]
    assert frame.metadata["physical_transform"] is None
    assert frame.metadata["origin"] is None
    assert frame.metadata["occluded"] is None
    assert frame.metadata["capture_backend"] == "wgc"
    assert len(frame.metadata["fallbacks"]) == 2


def test_capture_revalidates_pid_after_worker(monkeypatch):
    from minano_computer_use.capture import CaptureService

    counter = 0

    def resolve(target):
        nonlocal counter
        counter += 1
        return {
            "hwnd": 10,
            "pid": 20 if counter == 1 else 30,
            "rect": [0, 0, 2, 2],
            "visible": True,
            "minimized": False,
        }

    monkeypatch.setattr("minano_computer_use.win32.resolve_window", resolve)
    monkeypatch.setattr(
        "minano_computer_use.window_capture.capture_window",
        lambda *args: {"data": b"\0" * 16, "size": [2, 2], "rect": [0, 0, 2, 2]},
    )
    with pytest.raises(RuntimeError, match="PID changed"):
        CaptureService().screenshot("hwnd:10", capture_mode="printwindow")


def test_screen_fallback_is_explicit_and_reported(monkeypatch):
    from PIL import Image

    from minano_computer_use.capture import CaptureService

    monkeypatch.setattr(
        "minano_computer_use.win32.resolve_window",
        lambda _: {
            "hwnd": 10,
            "pid": 20,
            "rect": [0, 0, 2, 2],
            "visible": True,
            "minimized": False,
        },
    )

    def failed_worker(*args):
        raise RuntimeError("provider unavailable")

    class Grabber:
        monitors = [{"left": 0, "top": 0, "width": 4, "height": 4}]
        calls = 0

        def grab(self, rect):
            self.calls += 1
            return Image.new("RGB", (2, 2), "black")

    monkeypatch.setattr("minano_computer_use.window_capture.capture_window", failed_worker)
    grabber = Grabber()
    service = CaptureService(grabber=grabber)
    with pytest.raises(RuntimeError):
        service.screenshot("hwnd:10", capture_mode="window")
    assert grabber.calls == 0
    frame = service.screenshot("hwnd:10", capture_mode="window", allow_fallback=True)
    assert grabber.calls == 1
    assert frame.metadata["capture_surface"] == "screen_composite"
    assert len(frame.metadata["fallbacks"]) == 3


def test_wgc_item_conversion_failure_preserves_precise_code(monkeypatch):
    import sys
    from types import SimpleNamespace

    from minano_computer_use.window_capture import WindowCaptureError, _wgc

    class UnavailableCapture:
        def __init__(self, **kwargs):
            assert kwargs == {
                "window_hwnd": 123,
                "cursor_capture": False,
                "draw_border": None,
                "secondary_window": False,
            }

        def event(self, handler):
            return handler

        def start_free_threaded(self):
            raise Exception(
                "Capture session threw an exception: "
                "Failed to convert item to `GraphicsCaptureItem`"
            )

    monkeypatch.setitem(
        sys.modules, "windows_capture", SimpleNamespace(WindowsCapture=UnavailableCapture)
    )
    with pytest.raises(WindowCaptureError, match="Native error: Capture session") as caught:
        _wgc(123)
    assert caught.value.code == "WGC_ITEM_CONVERSION_FAILED"
