import ctypes
from types import SimpleNamespace as NS

import pytest

from minano_computer_use import win32


def test_resolves_exact_title_case_insensitively(monkeypatch):
    monkeypatch.setattr(
        win32,
        "list_windows",
        lambda: [
            {"hwnd": 1, "pid": 2, "title": "My App"},
            {"hwnd": 2, "pid": 3, "title": "My App - Other"},
        ],
    )
    assert win32.resolve_window("my app")["hwnd"] == 1


def test_rejects_ambiguous_substring(monkeypatch):
    monkeypatch.setattr(
        win32,
        "list_windows",
        lambda: [
            {"hwnd": 1, "pid": 2, "title": "App One"},
            {"hwnd": 2, "pid": 3, "title": "App Two"},
        ],
    )
    with pytest.raises(win32.TargetError) as error:
        win32.resolve_window("App")
    assert error.value.code == "AMBIGUOUS_TARGET"


def test_hwnd_and_pid_must_match_live_enumerated_window(monkeypatch):
    monkeypatch.setattr(win32, "list_windows", lambda: [{"hwnd": 0x100, "pid": 2, "title": "A"}])
    assert win32.resolve_window("hwnd:0x100")["pid"] == 2
    assert win32.resolve_window("pid:2")["hwnd"] == 0x100
    with pytest.raises(win32.TargetError):
        win32.resolve_window("hwnd:0x101")


def test_empty_target_refused(monkeypatch):
    monkeypatch.setattr(win32, "list_windows", lambda: [])
    with pytest.raises(win32.TargetError) as error:
        win32.resolve_window(" ")
    assert error.value.code == "INVALID_TARGET"


@pytest.mark.parametrize(
    "target",
    [
        "hwnd:-1",
        "hwnd:0",
        "hwnd:0x10000000000000000",
        "pid:0",
        "pid:-1",
        "pid:4294967296",
        "pid:no",
    ],
)
def test_bad_hwnd_pid_ranges_refused(monkeypatch, target):
    monkeypatch.setattr(win32, "list_windows", lambda: [])
    with pytest.raises(win32.TargetError) as error:
        win32.resolve_window(target)
    assert error.value.code == "INVALID_TARGET"


def test_foreground_is_explicit_target(monkeypatch):
    monkeypatch.setattr(
        win32,
        "list_windows",
        lambda: [
            {"hwnd": 1, "pid": 2, "title": "One"},
            {"hwnd": 3, "pid": 2, "title": "Two"},
        ],
    )
    monkeypatch.setattr(win32, "observations", lambda: {"foreground_hwnd": 3, "cursor": [0, 0]})
    assert win32.resolve_window("foreground")["hwnd"] == 3
    with pytest.raises(win32.TargetError) as error:
        win32.resolve_window("pid:2")
    assert error.value.code == "AMBIGUOUS_TARGET"


class Api:
    def __init__(self, function):
        self.function = function

    def __call__(self, *args):
        return self.function(*args)


@pytest.mark.parametrize("opened,readable", [(True, True), (True, False), (False, False)])
def test_process_creation_time_optional_and_handle_always_closed(monkeypatch, opened, readable):
    closed = []

    def times(handle, creation, *other):
        creation._obj.dwHighDateTime = 5
        creation._obj.dwLowDateTime = 7
        return readable

    api = NS(
        OpenProcess=Api(lambda *args: 123 if opened else 0),
        GetProcessTimes=Api(times),
        CloseHandle=Api(lambda handle: closed.append(handle)),
    )
    monkeypatch.setattr(ctypes, "windll", NS(kernel32=api), raising=False)
    assert win32.process_creation_time(20) == ((5 << 32) | 7 if readable else None)
    assert closed == ([123] if opened else [])


def test_window_identity_rechecks_pid_after_process_time(monkeypatch):
    pids = iter([20, 99])

    def get_pid(hwnd, pid):
        pid._obj.value = next(pids)
        return 1

    monkeypatch.setattr(
        win32, "_user32", lambda: NS(IsWindow=lambda hwnd: True, GetWindowThreadProcessId=get_pid)
    )
    monkeypatch.setattr(win32, "process_creation_time", lambda pid: 1234)
    with pytest.raises(win32.TargetError) as error:
        win32.window_identity(10, 20)
    assert error.value.code == "STALE_TARGET"


def test_window_identity_exposes_unavailable_process_time_honestly(monkeypatch):
    def get_pid(hwnd, pid):
        pid._obj.value = 20
        return 1

    monkeypatch.setattr(
        win32, "_user32", lambda: NS(IsWindow=lambda hwnd: True, GetWindowThreadProcessId=get_pid)
    )
    monkeypatch.setattr(win32, "process_creation_time", lambda pid: None)
    assert win32.window_identity(10, 20) == {"hwnd": 10, "pid": 20, "process_creation_time": None}
