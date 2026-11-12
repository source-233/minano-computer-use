"""Explicit, independently gated Windows automation services.

Adapters are injectable; production never replaces unavailable Windows operations
with simulated success. Clipboard diagnostics deliberately omit clipboard data.
"""

from __future__ import annotations

import ctypes
import multiprocessing
import os
import shutil
import subprocess
import time
from contextlib import contextmanager
from ctypes import wintypes as W
from dataclasses import dataclass
from pathlib import Path

from .contracts import result
from .jobs import (
    JobRunner,
    ServiceError,
    checkpoint,
    kernel32,
    launch_detached,
    make_deadline,
    validate_argv,
    win_error,
)


class _ProcessEntry(ctypes.Structure):
    _fields_ = [
        ("size", W.DWORD),
        ("usage", W.DWORD),
        ("pid", W.DWORD),
        ("heap", ctypes.c_size_t),
        ("module", W.DWORD),
        ("threads", W.DWORD),
        ("parent_pid", W.DWORD),
        ("priority", ctypes.c_long),
        ("flags", W.DWORD),
        ("name", W.WCHAR * 260),
    ]


class _MonitorInfo(ctypes.Structure):
    _fields_ = [
        ("size", W.DWORD),
        ("monitor", W.RECT),
        ("work", W.RECT),
        ("flags", W.DWORD),
        ("device", W.WCHAR * 32),
    ]


class ProcessHandle:
    def __init__(self, adapter, pid, creation=None, *, terminate=False):
        self.adapter, self.k, self.pid = adapter, adapter.k, pid
        self.handle = self.k.OpenProcess(0x1000 | 0x100000 | (1 if terminate else 0), False, pid)
        if not self.handle:
            raise win_error("OpenProcess")
        try:
            actual_pid = self.k.GetProcessId(self.handle)
            self.creation = adapter.creation(self.handle)
            if actual_pid != pid or (creation is not None and self.creation != creation):
                raise ServiceError("STALE_PROCESS", "Process identity changed")
        except BaseException:
            self.close()
            raise

    def close(self):
        if self.handle:
            self.k.CloseHandle(self.handle)
            self.handle = None

    def poll(self):
        state = self.k.WaitForSingleObject(self.handle, 0)
        if state == 258:
            return None
        if state != 0:
            raise win_error("WaitForSingleObject")
        code = W.DWORD()
        if not self.k.GetExitCodeProcess(self.handle, ctypes.byref(code)):
            raise win_error("GetExitCodeProcess")
        return int(code.value)

    def terminate(self):
        if self.k.GetProcessId(self.handle) != self.pid:
            raise ServiceError("STALE_PROCESS", "Process handle identity changed")
        if not self.k.TerminateProcess(self.handle, 1):
            raise win_error("TerminateProcess")

    def input_idle(self, milliseconds):
        self.adapter.u.WaitForInputIdle.argtypes = [W.HANDLE, W.DWORD]
        self.adapter.u.WaitForInputIdle.restype = W.DWORD
        state = self.adapter.u.WaitForInputIdle(self.handle, milliseconds)
        if state == 0xFFFFFFFF:
            raise ServiceError(
                "INPUT_IDLE_UNAVAILABLE", "Process has no usable input-idle readiness"
            )
        if state not in (0, 258):
            raise win_error("WaitForInputIdle")
        return state == 0


class WindowsAdapter:
    def __init__(self):
        self.k = kernel32()
        self.u = ctypes.WinDLL("user32", use_last_error=True)
        self._clipboard_owner = None

    def inventory(self):
        k = self.k
        k.CreateToolhelp32Snapshot.argtypes = [W.DWORD, W.DWORD]
        k.CreateToolhelp32Snapshot.restype = W.HANDLE
        for name in ("Process32FirstW", "Process32NextW"):
            getattr(k, name).argtypes = [W.HANDLE, ctypes.POINTER(_ProcessEntry)]
            getattr(k, name).restype = W.BOOL
        snapshot = k.CreateToolhelp32Snapshot(2, 0)
        if snapshot == W.HANDLE(-1).value:
            raise win_error("CreateToolhelp32Snapshot")
        entries = []
        try:
            entry = _ProcessEntry()
            entry.size = ctypes.sizeof(entry)
            found = k.Process32FirstW(snapshot, ctypes.byref(entry))
            while found:
                item = {
                    "pid": int(entry.pid),
                    "name": entry.name,
                    "parent_pid": int(entry.parent_pid),
                    "threads": int(entry.threads),
                }
                try:
                    handle = self.open_process(item["pid"])
                    try:
                        item["process_creation_time"] = handle.creation
                    finally:
                        handle.close()
                except ServiceError as exc:
                    item["process_creation_time"] = None
                    item["identity_error"] = exc.code
                entries.append(item)
                found = k.Process32NextW(snapshot, ctypes.byref(entry))
            if ctypes.get_last_error() != 18:  # ERROR_NO_MORE_FILES, not access denied
                raise win_error("Process32 enumeration")
        finally:
            k.CloseHandle(snapshot)
        return entries

    def creation(self, handle):
        values = [W.FILETIME() for _ in range(4)]
        if not self.k.GetProcessTimes(handle, *(ctypes.byref(value) for value in values)):
            raise win_error("GetProcessTimes")
        return values[0].dwHighDateTime << 32 | values[0].dwLowDateTime

    def open_process(self, pid, creation=None, *, terminate=False):
        return ProcessHandle(self, pid, creation, terminate=terminate)

    def windows(self):
        from . import win32

        return win32.list_windows()

    def resolve_window(self, target):
        from . import win32

        return win32.resolve_window(target)

    def validate_window(self, window, creation):
        from . import win32

        identity = win32.window_identity(window["hwnd"], window["pid"])
        if identity["process_creation_time"] != creation:
            raise ServiceError("STALE_TARGET", "Window process identity changed or is unavailable")

    def close_window(self, window, creation):
        self.validate_window(window, creation)
        self.u.PostMessageW.argtypes = [W.HWND, W.UINT, W.WPARAM, W.LPARAM]
        self.u.PostMessageW.restype = W.BOOL
        if not self.u.PostMessageW(window["hwnd"], 0x10, 0, 0):
            raise win_error("PostMessage WM_CLOSE")

    def shell_execute(self, path, args=None, cwd=None):
        class ExecuteInfo(ctypes.Structure):
            _fields_ = [
                ("size", W.DWORD),
                ("mask", W.ULONG),
                ("hwnd", W.HWND),
                ("verb", W.LPCWSTR),
                ("file", W.LPCWSTR),
                ("params", W.LPCWSTR),
                ("directory", W.LPCWSTR),
                ("show", ctypes.c_int),
                ("instance", W.HANDLE),
                ("idlist", ctypes.c_void_p),
                ("class_name", W.LPCWSTR),
                ("class_key", W.HKEY),
                ("hotkey", W.DWORD),
                ("icon", W.HANDLE),
                ("process", W.HANDLE),
            ]

        shell = ctypes.WinDLL("shell32", use_last_error=True)
        shell.ShellExecuteExW.argtypes = [ctypes.POINTER(ExecuteInfo)]
        shell.ShellExecuteExW.restype = W.BOOL
        info = ExecuteInfo()
        info.size, info.mask, info.show = ctypes.sizeof(info), 0x40 | 0x100 | 0x400, 1
        info.verb, info.file = "open", path
        info.params = subprocess.list2cmdline(args) if args else None
        info.directory = cwd
        if not shell.ShellExecuteExW(ctypes.byref(info)):
            raise win_error("ShellExecuteEx")
        try:
            return {
                "pid": int(self.k.GetProcessId(info.process)) if info.process else None,
                "owned": False,
                "contained": False,
                "mode": "shell_execute",
            }
        finally:
            if info.process:
                self.k.CloseHandle(info.process)

    def launch_detached(self, argv, cwd=None):
        return launch_detached(argv, cwd)

    def cursor(self):
        point = W.POINT()
        self.u.GetCursorPos.argtypes = [ctypes.POINTER(W.POINT)]
        self.u.GetCursorPos.restype = W.BOOL
        if not self.u.GetCursorPos(ctypes.byref(point)):
            raise win_error("GetCursorPos")
        return {"x": point.x, "y": point.y, "coordinate_space": "screen"}

    def displays(self):
        u = self.u
        u.GetMonitorInfoW.argtypes = [W.HANDLE, ctypes.POINTER(_MonitorInfo)]
        u.GetMonitorInfoW.restype = W.BOOL
        callback_type = ctypes.WINFUNCTYPE(
            W.BOOL, W.HANDLE, W.HDC, ctypes.POINTER(W.RECT), W.LPARAM
        )
        u.EnumDisplayMonitors.argtypes = [W.HDC, ctypes.POINTER(W.RECT), callback_type, W.LPARAM]
        u.EnumDisplayMonitors.restype = W.BOOL
        found, errors = [], []

        @callback_type
        def visit(monitor, _dc, _rect, _data):
            try:
                info = _MonitorInfo()
                info.size = ctypes.sizeof(info)
                if not u.GetMonitorInfoW(monitor, ctypes.byref(info)):
                    raise win_error("GetMonitorInfo")
                dpi = None
                try:
                    shcore = ctypes.WinDLL("shcore", use_last_error=True)
                    shcore.GetDpiForMonitor.argtypes = [
                        W.HANDLE,
                        ctypes.c_int,
                        ctypes.POINTER(W.UINT),
                        ctypes.POINTER(W.UINT),
                    ]
                    shcore.GetDpiForMonitor.restype = ctypes.c_long
                    x, y = W.UINT(), W.UINT()
                    if shcore.GetDpiForMonitor(monitor, 0, ctypes.byref(x), ctypes.byref(y)) == 0:
                        dpi = [x.value, y.value]
                except (OSError, AttributeError):
                    pass
                rect = info.monitor
                found.append(
                    {
                        "index": len(found),
                        "name": info.device,
                        "rect": [rect.left, rect.top, rect.right, rect.bottom],
                        "primary": bool(info.flags & 1),
                        "dpi": dpi,
                        "scale": dpi[0] / 96 if dpi else None,
                        "dpi_mode": "effective_monitor" if dpi else "unavailable",
                    }
                )
                return True
            except (ServiceError, OSError):
                errors.append(True)
                return False

        if not u.EnumDisplayMonitors(None, None, visit, 0) or errors:
            raise win_error("EnumDisplayMonitors")
        return found

    @contextmanager
    def clipboard_open(self, cancel_event=None, deadline=None):
        u = self.u
        u.OpenClipboard.argtypes, u.OpenClipboard.restype = [W.HWND], W.BOOL
        u.CloseClipboard.argtypes, u.CloseClipboard.restype = [], W.BOOL
        # EmptyClipboard + SetClipboardData requires a real owner, not NULL.
        u.CreateWindowExW.argtypes = [
            W.DWORD,
            W.LPCWSTR,
            W.LPCWSTR,
            W.DWORD,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            W.HWND,
            W.HMENU,
            W.HINSTANCE,
            ctypes.c_void_p,
        ]
        u.CreateWindowExW.restype = W.HWND
        u.DestroyWindow.argtypes, u.DestroyWindow.restype = [W.HWND], W.BOOL
        owner = u.CreateWindowExW(0, "STATIC", "", 0, 0, 0, 0, 0, W.HWND(-3), None, None, None)
        if not owner:
            raise win_error("Create clipboard owner")
        opened = False
        try:
            local_deadline = (
                min(deadline, time.monotonic() + 1) if deadline else time.monotonic() + 1
            )
            while not u.OpenClipboard(owner):
                checkpoint(cancel_event, local_deadline)
                time.sleep(0.01)
            opened = True
            yield
        finally:
            if opened:
                u.CloseClipboard()
            u.DestroyWindow(owner)

    def clipboard_sequence(self):
        self.u.GetClipboardSequenceNumber.argtypes = []
        self.u.GetClipboardSequenceNumber.restype = W.DWORD
        return int(self.u.GetClipboardSequenceNumber())

    def clipboard_read(self):
        u, k = self.u, self.k
        u.IsClipboardFormatAvailable.argtypes, u.IsClipboardFormatAvailable.restype = (
            [W.UINT],
            W.BOOL,
        )
        u.GetClipboardData.argtypes, u.GetClipboardData.restype = [W.UINT], W.HANDLE
        k.GlobalLock.argtypes, k.GlobalLock.restype = [W.HGLOBAL], ctypes.c_void_p
        k.GlobalUnlock.argtypes, k.GlobalUnlock.restype = [W.HGLOBAL], W.BOOL
        k.GlobalSize.argtypes, k.GlobalSize.restype = [W.HGLOBAL], ctypes.c_size_t
        if not u.IsClipboardFormatAvailable(13):
            return None
        handle = u.GetClipboardData(13)
        if not handle:
            raise win_error("GetClipboardData")
        size = k.GlobalSize(handle)
        if not 2 <= size <= 16 * 1024 * 1024 or size % 2:
            raise ServiceError(
                "CLIPBOARD_FORMAT", "Unicode clipboard allocation is invalid or too large"
            )
        pointer = k.GlobalLock(handle)
        if not pointer:
            raise win_error("GlobalLock clipboard")
        try:
            raw = ctypes.string_at(pointer, size)
            end = next((i for i in range(0, len(raw) - 1, 2) if raw[i : i + 2] == b"\0\0"), None)
            if end is None:
                raise ServiceError("CLIPBOARD_FORMAT", "Unicode clipboard is not terminated")
            return raw[:end].decode("utf-16-le", "strict")
        finally:
            k.GlobalUnlock(handle)

    def clipboard_write(self, text):
        u, k = self.u, self.k
        u.EmptyClipboard.argtypes, u.EmptyClipboard.restype = [], W.BOOL
        u.SetClipboardData.argtypes, u.SetClipboardData.restype = [W.UINT, W.HANDLE], W.HANDLE
        k.GlobalAlloc.argtypes, k.GlobalAlloc.restype = [W.UINT, ctypes.c_size_t], W.HGLOBAL
        k.GlobalFree.argtypes, k.GlobalFree.restype = [W.HGLOBAL], W.HGLOBAL
        k.GlobalLock.argtypes, k.GlobalLock.restype = [W.HGLOBAL], ctypes.c_void_p
        k.GlobalUnlock.argtypes, k.GlobalUnlock.restype = [W.HGLOBAL], W.BOOL
        if text is None:
            if not u.EmptyClipboard():
                raise win_error("EmptyClipboard")
            return
        raw = text.encode("utf-16-le") + b"\0\0"
        handle = k.GlobalAlloc(0x42, len(raw))
        if not handle:
            raise win_error("GlobalAlloc clipboard")
        try:
            pointer = k.GlobalLock(handle)
            if not pointer:
                raise win_error("GlobalLock clipboard")
            ctypes.memmove(pointer, raw, len(raw))
            k.GlobalUnlock(handle)
            if not u.EmptyClipboard():
                raise win_error("EmptyClipboard")
            if not u.SetClipboardData(13, handle):
                raise win_error("SetClipboardData")
            handle = None  # ownership transferred to the OS
        finally:
            if handle:
                k.GlobalFree(handle)


@dataclass
class ClipboardToken:
    sequence: int
    restored: bool = False
    restore_skipped: bool = False
    restore_error: str | None = None


class AutomationServices:
    def __init__(
        self, allow_shell=False, allow_process_control=False, artifact_store=None, adapter=None
    ):
        self.allow_shell = allow_shell is True
        self.allow_process_control = allow_process_control is True
        self.artifact_store = artifact_store
        self._adapter = adapter
        self.jobs = JobRunner(artifact_store)

    @property
    def adapter(self):
        if self._adapter is None:
            self._adapter = WindowsAdapter()
        return self._adapter

    def _failure(self, operation, exc, *, acted=False):
        has_acted = acted or getattr(exc, "acted", False)
        code = getattr(
            exc, "code", "INVALID_ARGUMENT" if isinstance(exc, ValueError) else "SERVICE_ERROR"
        )
        # No exception text from an injected backend or clipboard ever crosses this boundary.
        message = str(exc) if isinstance(exc, ServiceError) else "Service operation failed"
        return result(
            "unknown"
            if has_acted
            else "refused"
            if code
            in {
                "UNAVAILABLE",
                "PERMISSION_REQUIRED",
                "AMBIGUOUS_TARGET",
                "INVALID_ARGUMENT",
                "INVALID_TARGET",
                "PROTECTED_PROCESS",
                "INPUT_IDLE_UNAVAILABLE",
                "UNSUPPORTED",
                "IDENTITY_UNAVAILABLE",
                "SHELL_REQUIRED",
            }
            else "error",
            operation,
            code=code,
            message=message,
            **({"may_have_acted": True} if has_acted else {}),
            **({"cancelled": True} if code == "CANCELLED" else {}),
        )

    def _gate(self, permission, call_flag):
        if not getattr(self, "allow_" + permission) or call_flag is not True:
            raise ServiceError(
                "PERMISSION_REQUIRED", "Startup and per-call permission are required"
            )

    def _protected(self):
        protected = {
            os.getpid(),
            0,
            *self.jobs.protected_pids,
            *(child.pid for child in multiprocessing.active_children() if child.pid),
        }
        try:
            cursor = os.getpid()
            by_pid = {item["pid"]: item for item in self.adapter.inventory()}
            worker_roots = protected - {os.getpid(), 0}
            for _ in range(128):
                children = {
                    pid for pid, entry in by_pid.items() if entry.get("parent_pid") in worker_roots
                } - worker_roots
                if not children:
                    break
                worker_roots.update(children)
            protected.update(worker_roots)
            for _ in range(128):
                parent = by_pid.get(cursor, {}).get("parent_pid", 0)
                if not parent or parent in protected:
                    break
                protected.add(parent)
                cursor = parent
        except Exception as exc:
            # Failure to enumerate ancestry cannot make destructive actions permissive.
            raise ServiceError(
                "IDENTITY_UNAVAILABLE", "Cannot establish protected process ancestry"
            ) from exc
        return protected

    def _process_matches(self, pid=None, name=None, target=None, all=False):
        if target is not None:
            if pid is not None or name is not None:
                raise ValueError("Specify one process target")
            if isinstance(target, int):
                pid = target
            elif isinstance(target, str) and target.startswith("pid:"):
                pid = int(target[4:])
            else:
                name = target
        if (pid is None) == (name is None):
            raise ValueError("Specify exactly one pid or name")
        if pid is not None and (type(pid) is not int or not 0 < pid <= 0xFFFFFFFF):
            raise ValueError("PID must be a positive DWORD")
        if name is not None and (not isinstance(name, str) or not name.strip()):
            raise ValueError("name must be nonempty")
        matches = [
            entry
            for entry in self.adapter.inventory()
            if (
                entry["pid"] == pid
                if pid is not None
                else entry["name"].casefold() == name.casefold()
            )
        ]
        if not matches:
            raise ServiceError("PROCESS_NOT_FOUND", "No process matches the requested identity")
        if len(matches) > 1 and not all:
            raise ServiceError("AMBIGUOUS_TARGET", "Multiple processes match; all=true is required")
        return matches

    def process(
        self,
        action="list",
        argv=None,
        *,
        exe=None,
        args=None,
        cwd=None,
        shell=False,
        command=None,
        timeout=10,
        pid=None,
        name=None,
        target=None,
        all=False,
        dir=None,
        glob=None,
        query=None,
        ready="exists",
        shell_kind="powershell",
        allow_shell=False,
        allow_process_control=False,
        cancel_event=None,
        deadline=None,
    ):
        operation, acted = "process." + str(action), False
        handles = []
        try:
            deadline = make_deadline(timeout, deadline)
            checkpoint(cancel_event, deadline)
            if action == "list":
                entries = self.adapter.inventory()
                if query is not None:
                    if not isinstance(query, str):
                        raise ValueError("Process list query must be a string")
                    entries = [
                        item for item in entries if query.casefold() in item["name"].casefold()
                    ]
                return result("ok", operation, data=entries, method="toolhelp_process_snapshot")
            if action == "launch":
                if command is not None:
                    raise ValueError("Raw commands require the separately gated shell action")
                if argv is not None and (exe is not None or args is not None):
                    raise ValueError("argv and exe/args are mutually exclusive")
                launch_argv = validate_argv(argv if argv is not None else [exe, *(args or [])])
                if args is not None and not isinstance(args, list):
                    raise ValueError("args must be an argument list")
                if cwd is not None and (not isinstance(cwd, str) or not Path(cwd).is_dir()):
                    raise ValueError("cwd must be an existing directory")
                # Launch must not be a spelling variant of the gated raw-command tool.
                interpreter = Path(launch_argv[0]).name.casefold()
                if interpreter in {
                    "cmd",
                    "cmd.exe",
                    "powershell",
                    "powershell.exe",
                    "pwsh",
                    "pwsh.exe",
                }:
                    raise ServiceError(
                        "SHELL_REQUIRED", "Command interpreters require shell action"
                    )
                if shell and interpreter.endswith((".cmd", ".bat", ".ps1")):
                    raise ServiceError(
                        "SHELL_REQUIRED", "Shell scripts require the gated shell action"
                    )
                if shell:
                    data = self.adapter.shell_execute(launch_argv[0], launch_argv[1:], cwd)
                else:
                    data = self.adapter.launch_detached(launch_argv, cwd)
                acted = True
                checkpoint(cancel_event, deadline, acted=True)
                return result(
                    "invoked",
                    operation,
                    data=data,
                    method="shell_execute" if shell else "detached_create_process",
                )
            if action == "shell":
                self._gate("shell", allow_shell)
                if argv is not None or exe is not None or args is not None or shell:
                    raise ValueError("shell action requires command, not launch arguments")
                if cwd is not None and (not isinstance(cwd, str) or not Path(cwd).is_dir()):
                    raise ValueError("cwd must be an existing directory")
                if (
                    not isinstance(command, str)
                    or not command
                    or "\0" in command
                    or len(command) > 30000
                ):
                    raise ValueError("command must be a bounded NUL-free string")
                if shell_kind == "powershell":
                    import base64

                    executable = shutil.which("powershell.exe")
                    if not executable:
                        raise ServiceError("UNAVAILABLE", "Windows PowerShell is not installed")
                    shell_argv = [
                        executable,
                        "-NoLogo",
                        "-NoProfile",
                        "-NonInteractive",
                        "-EncodedCommand",
                        base64.b64encode(command.encode("utf-16-le")).decode(),
                    ]
                elif shell_kind == "cmd":
                    executable = shutil.which("cmd.exe")
                    if not executable:
                        raise ServiceError(
                            "UNAVAILABLE", "Windows command processor is not installed"
                        )
                    shell_argv = [executable, "/d", "/s", "/c", command]
                else:
                    raise ValueError("shell_kind must be powershell or cmd")
                data = self.jobs.run(shell_argv, cwd, cancel_event, deadline)
                return result(
                    "ok" if data["exit_code"] == 0 else "error",
                    operation,
                    data=data,
                    method="contained_shell",
                    **(
                        {"code": "CHILD_EXIT", "message": "Command returned nonzero"}
                        if data["exit_code"]
                        else {}
                    ),
                )
            if action in {"kill", "wait"}:
                if action == "kill":
                    self._gate("process_control", allow_process_control)
                matches = self._process_matches(pid, name, target, all)
                if action == "kill" and any(item["pid"] in self._protected() for item in matches):
                    raise ServiceError(
                        "PROTECTED_PROCESS", "Cannot control the server or active workers"
                    )
                for item in matches:
                    if item.get("process_creation_time") is None:
                        raise ServiceError(
                            "IDENTITY_UNAVAILABLE", "Process creation identity is unavailable"
                        )
                    handles.append(
                        self.adapter.open_process(
                            item["pid"], item["process_creation_time"], terminate=action == "kill"
                        )
                    )
                # Acquire and validate ALL handles before any mutation (no partial name match).
                if action == "kill":
                    for handle in handles:
                        checkpoint(cancel_event, deadline, acted=acted)
                        handle.terminate()
                        acted = True
                exits = {}
                while len(exits) < len(handles):
                    checkpoint(cancel_event, deadline, acted=acted)
                    for handle in handles:
                        if handle.pid not in exits and (code := handle.poll()) is not None:
                            exits[handle.pid] = code
                    if len(exits) < len(handles):
                        self._pause(cancel_event, deadline, acted=acted)
                return result(
                    "verified",
                    operation,
                    data={
                        "processes": [
                            {
                                "pid": handle.pid,
                                "process_creation_time": handle.creation,
                                "exit_code": exits[handle.pid],
                            }
                            for handle in handles
                        ]
                    },
                    method="handle_bound_wait",
                )
            if action == "wait_for_file":
                return result(
                    "verified",
                    operation,
                    data=self._wait_file(dir, glob, cancel_event, deadline),
                    method="new_stable_file",
                )
            if action == "wait_for_window":
                return result(
                    "verified",
                    operation,
                    data=self._wait_window(
                        query if query is not None else target, ready, cancel_event, deadline
                    ),
                    method="window_readiness",
                )
            raise ServiceError("UNSUPPORTED", "Unsupported process action")
        except Exception as exc:
            return self._failure(operation, exc, acted=acted)
        finally:
            for handle in handles:
                handle.close()

    @staticmethod
    def _pause(cancel_event, deadline, *, acted=False):
        checkpoint(cancel_event, deadline, acted=acted)
        duration = min(0.05, max(0, deadline - time.monotonic()))
        if cancel_event is not None:
            cancel_event.wait(duration)
        else:
            time.sleep(duration)

    def _wait_file(self, directory, pattern, cancel_event, deadline):
        if not isinstance(directory, str) or not Path(directory).is_dir():
            raise ValueError("dir must name an existing directory")
        if (
            not isinstance(pattern, str)
            or not pattern
            or Path(pattern).is_absolute()
            or ".." in Path(pattern).parts
            or "/" in pattern
            or "\\" in pattern
        ):
            raise ValueError("glob must be a filename pattern, not a path")
        root = Path(directory)
        baseline = {path.name for path in root.glob(pattern)}
        samples = {}
        while True:
            checkpoint(cancel_event, deadline)
            for path in root.glob(pattern):
                if path.name in baseline or path.is_symlink() or not path.is_file():
                    continue
                info = path.stat()
                if getattr(info, "st_file_attributes", 0) & 0x400:
                    continue
                identity = (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns)
                previous = samples.get(path.name)
                now = time.monotonic()
                if previous and previous[0] == identity and now - previous[1] >= 0.2:
                    return {
                        "path": str(path.absolute()),
                        "size": info.st_size,
                        "stable_seconds": now - previous[1],
                        "new_since_baseline": True,
                    }
                if not previous or previous[0] != identity:
                    samples[path.name] = (identity, now)
            self._pause(cancel_event, deadline)

    def _query_windows(self, query):
        if isinstance(query, str):
            if not query:
                raise ValueError("query must not be empty")
            if query.startswith(("hwnd:", "pid:")) or query == "foreground":
                return [self.adapter.resolve_window(query)]
            windows = self.adapter.windows()
            exact = [item for item in windows if item["title"].casefold() == query.casefold()]
            return exact or [
                item for item in windows if query.casefold() in item["title"].casefold()
            ]
        if (
            not isinstance(query, dict)
            or not query
            or set(query) - {"title", "pid", "hwnd", "process"}
        ):
            raise ValueError("query must be a title/target or title,pid,hwnd,process object")
        windows = self.adapter.windows()
        if "process" in query:
            pids = {
                item["pid"]
                for item in self.adapter.inventory()
                if item["name"].casefold() == str(query["process"]).casefold()
            }
            windows = [item for item in windows if item["pid"] in pids]
        for key in {"title", "pid", "hwnd"} & query.keys():
            windows = [item for item in windows if item[key] == query[key]]
        return windows

    def _wait_window(self, query, ready, cancel_event, deadline):
        if ready not in {"exists", "visible", "input_idle"}:
            raise ValueError("ready must be exists, visible or input_idle")
        while True:
            checkpoint(cancel_event, deadline)
            try:
                matches = self._query_windows(query)
            except Exception as exc:
                if getattr(exc, "code", None) != "TARGET_NOT_FOUND":
                    raise
                matches = []
            if len(matches) > 1:
                raise ServiceError("AMBIGUOUS_TARGET", "Window readiness query is ambiguous")
            if matches:
                window = matches[0]
                handle = self.adapter.open_process(window["pid"])
                try:
                    self.adapter.validate_window(window, handle.creation)
                    satisfied = ready == "exists" or (ready == "visible" and window["visible"])
                    if ready == "input_idle":
                        satisfied = handle.input_idle(0)
                    if satisfied:
                        checkpoint(cancel_event, deadline)
                        self.adapter.validate_window(window, handle.creation)
                        return {
                            "window": window,
                            "actual_mode": ready,
                            "ready": True,
                            "process_creation_time": handle.creation,
                        }
                finally:
                    handle.close()
            self._pause(cancel_event, deadline)

    def system(self, action, text=None, *, cancel_event=None, deadline=None, **kwargs):
        operation, acted = "system." + str(action), False
        try:
            checkpoint(cancel_event, deadline)
            if kwargs:
                raise ValueError("Unknown system arguments")
            if action == "cursor":
                return result("ok", operation, data=self.adapter.cursor())
            if action == "displays":
                return result("ok", operation, data=self.adapter.displays())
            if action not in {"get_clipboard", "set_clipboard"}:
                raise ServiceError("UNSUPPORTED", "Unsupported system action")
            if action == "set_clipboard":
                self._validate_text(text)
            with self.adapter.clipboard_open(cancel_event, deadline):
                checkpoint(cancel_event, deadline)
                if action == "set_clipboard":
                    acted = True  # EmptyClipboard can succeed before SetClipboardData fails.
                    self.adapter.clipboard_write(text)
                value = self.adapter.clipboard_read()
                sequence = self.adapter.clipboard_sequence()
                if action == "set_clipboard" and value != text:
                    raise ServiceError(
                        "CLIPBOARD_VERIFY_FAILED", "Clipboard readback did not match", acted=True
                    )
            checkpoint(cancel_event, deadline, acted=acted)
            return result(
                "verified" if acted else "ok",
                operation,
                data={"text": value, "has_unicode_text": value is not None, "sequence": sequence},
                method="unicode_clipboard",
            )
        except (ServiceError, OSError, ValueError, TypeError, UnicodeError) as exc:
            return self._failure(operation, exc, acted=acted)

    @staticmethod
    def _validate_text(text):
        if (
            not isinstance(text, str)
            or "\0" in text
            or len(text.encode("utf-16-le")) > 8 * 1024 * 1024
        ):
            raise ValueError("Clipboard text must be a bounded NUL-free Unicode string")

    @contextmanager
    def clipboard_session(self, text, cancel_event=None, deadline=None):
        self._validate_text(text)
        checkpoint(cancel_event, deadline)
        with self.adapter.clipboard_open(cancel_event, deadline):
            previous = self.adapter.clipboard_read()
            before_sequence = self.adapter.clipboard_sequence()
            try:
                self.adapter.clipboard_write(text)
            except BaseException:
                # EmptyClipboard may already have changed formats before SetClipboardData failed.
                if self.adapter.clipboard_sequence() != before_sequence:
                    self.adapter.clipboard_write(previous)
                raise
            sequence = self.adapter.clipboard_sequence()
            token = ClipboardToken(sequence)
            try:
                if self.adapter.clipboard_read() != text:
                    raise ServiceError(
                        "CLIPBOARD_VERIFY_FAILED", "Clipboard readback did not match", acted=True
                    )
            except BaseException:
                # Still holding the clipboard lock: no user write can be overwritten.
                if self.adapter.clipboard_sequence() == sequence:
                    self.adapter.clipboard_write(previous)
                raise
        try:
            checkpoint(cancel_event, deadline, acted=True)
            yield token
        finally:
            try:
                # Cleanup is independent of cancellation/expired operation deadline.
                with self.adapter.clipboard_open(None, time.monotonic() + 1):
                    if self.adapter.clipboard_sequence() == token.sequence:
                        self.adapter.clipboard_write(previous)
                        token.restored = self.adapter.clipboard_read() == previous
                        if not token.restored:
                            token.restore_error = "CLIPBOARD_RESTORE_VERIFY_FAILED"
                    else:
                        token.restore_skipped = True
            except Exception:
                token.restore_error = "CLIPBOARD_RESTORE_FAILED"

    def window(
        self,
        action,
        target=None,
        selector=None,
        *,
        allow_process_control=False,
        cancel_event=None,
        deadline=None,
        **kwargs,
    ):
        if action == "ocr":
            return self.ocr(target, cancel_event=cancel_event, deadline=deadline, **kwargs)
        operation, acted = "window." + str(action), False
        handle = None
        try:
            checkpoint(cancel_event, deadline)
            if action != "close":
                raise ServiceError("UNSUPPORTED", "Unsupported service window action")
            self._gate("process_control", allow_process_control)
            if selector is not None or kwargs:
                raise ValueError("close accepts only an explicit window target")
            window = self.adapter.resolve_window(target)
            if window["pid"] in self._protected():
                raise ServiceError("PROTECTED_PROCESS", "Cannot close the server or active workers")
            handle = self.adapter.open_process(window["pid"])
            self.adapter.validate_window(window, handle.creation)
            checkpoint(cancel_event, deadline)
            self.adapter.close_window(window, handle.creation)
            acted = True
            checkpoint(cancel_event, deadline, acted=True)
            return result(
                "invoked",
                operation,
                method="post_wm_close",
                data={
                    "hwnd": window["hwnd"],
                    "pid": window["pid"],
                    "process_creation_time": handle.creation,
                },
            )
        except Exception as exc:
            return self._failure(operation, exc, acted=acted)
        finally:
            if handle:
                handle.close()

    def ocr(
        self,
        target="desktop",
        *,
        language=None,
        image_bytes=None,
        timeout=10,
        cancel_event=None,
        deadline=None,
    ):
        from .ocr import recognize

        return recognize(
            target,
            language=language,
            image_bytes=image_bytes,
            timeout=timeout,
            cancel_event=cancel_event,
            deadline=deadline,
            runner=self.jobs,
        )

    def close(self):
        self.jobs.close()
