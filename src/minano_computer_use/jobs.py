"""Owned Windows process trees: contain a suspended child before its first instruction.

No subprocess shell=True, global taskkill, or external process-control permission is
used here. Closing an owned job is cleanup, not authority to kill an arbitrary PID.
"""

from __future__ import annotations

import ctypes
import json
import os
import shutil
import subprocess
import threading
import time
from ctypes import wintypes as W

OUTPUT_LIMIT = 1024 * 1024


class ServiceError(Exception):
    def __init__(self, code, message, *, acted=False):
        super().__init__(message)
        self.code, self.acted = code, acted


def checkpoint(cancel_event=None, deadline=None, *, acted=False):
    if cancel_event is not None and cancel_event.is_set():
        raise ServiceError("CANCELLED", "Operation cancelled", acted=acted)
    if deadline is not None and time.monotonic() >= deadline:
        raise ServiceError("TIMEOUT", "Operation deadline expired", acted=acted)


def make_deadline(timeout, deadline):
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)):
        raise ValueError("timeout must be a finite number in (0, 300]")
    if not 0 < timeout <= 300:
        raise ValueError("timeout must be a finite number in (0, 300]")
    return (
        min(time.monotonic() + timeout, deadline)
        if deadline is not None
        else (time.monotonic() + timeout)
    )


def kernel32():
    if os.name != "nt":
        raise ServiceError("UNAVAILABLE", "Windows process APIs are unavailable")
    k = ctypes.WinDLL("kernel32", use_last_error=True)
    signatures = {
        "CloseHandle": ([W.HANDLE], W.BOOL),
        "CreateJobObjectW": ([ctypes.c_void_p, W.LPCWSTR], W.HANDLE),
        "SetInformationJobObject": ([W.HANDLE, ctypes.c_int, ctypes.c_void_p, W.DWORD], W.BOOL),
        "AssignProcessToJobObject": ([W.HANDLE, W.HANDLE], W.BOOL),
        "TerminateJobObject": ([W.HANDLE, W.UINT], W.BOOL),
        "TerminateProcess": ([W.HANDLE, W.UINT], W.BOOL),
        "ResumeThread": ([W.HANDLE], W.DWORD),
        "WaitForSingleObject": ([W.HANDLE, W.DWORD], W.DWORD),
        "GetExitCodeProcess": ([W.HANDLE, ctypes.POINTER(W.DWORD)], W.BOOL),
        "OpenProcess": ([W.DWORD, W.BOOL, W.DWORD], W.HANDLE),
        "GetProcessId": ([W.HANDLE], W.DWORD),
        "GetProcessTimes": ([W.HANDLE] + [ctypes.POINTER(W.FILETIME)] * 4, W.BOOL),
        "CreateProcessW": (
            [
                W.LPCWSTR,
                W.LPWSTR,
                ctypes.c_void_p,
                ctypes.c_void_p,
                W.BOOL,
                W.DWORD,
                ctypes.c_void_p,
                W.LPCWSTR,
                ctypes.c_void_p,
                ctypes.c_void_p,
            ],
            W.BOOL,
        ),
        "InitializeProcThreadAttributeList": (
            [ctypes.c_void_p, W.DWORD, W.DWORD, ctypes.POINTER(ctypes.c_size_t)],
            W.BOOL,
        ),
        "UpdateProcThreadAttribute": (
            [
                ctypes.c_void_p,
                W.DWORD,
                ctypes.c_size_t,
                ctypes.c_void_p,
                ctypes.c_size_t,
                ctypes.c_void_p,
                ctypes.c_void_p,
            ],
            W.BOOL,
        ),
        "DeleteProcThreadAttributeList": ([ctypes.c_void_p], None),
    }
    for name, (args, returns) in signatures.items():
        function = getattr(k, name)
        function.argtypes, function.restype = args, returns
    return k


def win_error(operation):
    # Never include arguments/commands/clipboard data in diagnostics.
    return ServiceError("WIN32_ERROR", f"{operation} failed (Win32 {ctypes.get_last_error()})")


class _BasicLimits(ctypes.Structure):
    _fields_ = [
        ("process_time", ctypes.c_int64),
        ("job_time", ctypes.c_int64),
        ("flags", W.DWORD),
        ("min_working", ctypes.c_size_t),
        ("max_working", ctypes.c_size_t),
        ("active_processes", W.DWORD),
        ("affinity", ctypes.c_size_t),
        ("priority", W.DWORD),
        ("scheduling", W.DWORD),
    ]


class _JobLimits(ctypes.Structure):
    _fields_ = [
        ("basic", _BasicLimits),
        ("io", ctypes.c_uint64 * 6),
        ("process_memory", ctypes.c_size_t),
        ("job_memory", ctypes.c_size_t),
        ("peak_process", ctypes.c_size_t),
        ("peak_job", ctypes.c_size_t),
    ]


class _Startup(ctypes.Structure):
    _fields_ = [
        ("cb", W.DWORD),
        ("reserved", W.LPWSTR),
        ("desktop", W.LPWSTR),
        ("title", W.LPWSTR),
        ("x", W.DWORD),
        ("y", W.DWORD),
        ("xsize", W.DWORD),
        ("ysize", W.DWORD),
        ("xchars", W.DWORD),
        ("ychars", W.DWORD),
        ("fill", W.DWORD),
        ("flags", W.DWORD),
        ("show", W.WORD),
        ("reserved2size", W.WORD),
        ("reserved2", ctypes.c_void_p),
        ("stdin", W.HANDLE),
        ("stdout", W.HANDLE),
        ("stderr", W.HANDLE),
    ]


class _StartupEx(ctypes.Structure):
    _fields_ = [("startup", _Startup), ("attributes", ctypes.c_void_p)]


class _ProcessInfo(ctypes.Structure):
    _fields_ = [("process", W.HANDLE), ("thread", W.HANDLE), ("pid", W.DWORD), ("tid", W.DWORD)]


def validate_argv(argv):
    if not isinstance(argv, (list, tuple)) or not argv or len(argv) > 1024:
        raise ValueError("argv must be a nonempty argument list (maximum 1024 arguments)")
    if any(not isinstance(arg, str) or "\0" in arg for arg in argv) or not argv[0]:
        raise ValueError("Arguments must be NUL-free strings and executable must not be empty")
    if sum(len(arg) + 3 for arg in argv) > 30000:
        raise ValueError("Command line is too long")
    return list(argv)


def command_line(argv):
    """cmd /s /c uses cmd quoting, not the C runtime backslash-quote convention."""
    if (
        os.path.basename(argv[0]).casefold() == "cmd.exe"
        and len(argv) == 5
        and [arg.casefold() for arg in argv[1:4]] == ["/d", "/s", "/c"]
    ):
        return subprocess.list2cmdline(argv[:4]) + ' "' + argv[4] + '"'
    return subprocess.list2cmdline(argv)


def launch_detached(argv, cwd=None):
    """Launch a normal application without a kill-on-close job association."""
    argv = validate_argv(argv)
    k = kernel32()
    executable = shutil.which(argv[0])
    if executable is None:
        raise ServiceError("EXECUTABLE_NOT_FOUND", "Executable was not found")
    if executable.lower().endswith((".bat", ".cmd")):
        raise ServiceError("SHELL_REQUIRED", "Batch files require the gated shell action")
    info = _ProcessInfo()
    commandline = ctypes.create_unicode_buffer(subprocess.list2cmdline(argv))
    startup = _Startup()
    startup.cb = ctypes.sizeof(startup)
    try:
        if not k.CreateProcessW(
            executable,
            commandline,
            None,
            None,
            False,
            0x4 | 0x08000000,
            None,
            cwd,
            ctypes.byref(startup),
            ctypes.byref(info),
        ):
            raise win_error("CreateProcess")
        pid = int(info.pid)
        if k.ResumeThread(info.thread) == 0xFFFFFFFF:
            k.TerminateProcess(info.process, 1)
            k.WaitForSingleObject(info.process, 5000)
            raise win_error("ResumeThread")
        return {"pid": pid, "owned": False, "contained": False, "mode": "detached_create_process"}
    finally:
        if info.thread:
            k.CloseHandle(info.thread)
        if info.process:
            k.CloseHandle(info.process)


class OwnedJob:
    def __init__(self, argv, cwd=None, *, capture=False):
        argv = validate_argv(argv)
        self.k = k = kernel32()
        self.job = self.process = None
        self.pid = None
        self.readers, self.threads = [], []
        self.buffers = [bytearray(), bytearray()]
        self.totals = [0, 0]
        self.reader_errors = []
        executable = shutil.which(argv[0])
        if executable is None:
            raise ServiceError("EXECUTABLE_NOT_FOUND", "Executable was not found")
        # .bat/.cmd are interpreters, not safe CreateProcess executables.
        if executable.lower().endswith((".bat", ".cmd")):
            raise ServiceError("SHELL_REQUIRED", "Batch files require the gated shell action")
        files = []
        attribute_list = None
        attributes_initialized = False
        info = _ProcessInfo()
        try:
            self.job = k.CreateJobObjectW(None, None)
            if not self.job:
                raise win_error("CreateJobObject")
            limits = _JobLimits()
            limits.basic.flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            if not k.SetInformationJobObject(
                self.job, 9, ctypes.byref(limits), ctypes.sizeof(limits)
            ):
                raise win_error("SetInformationJobObject")
            import msvcrt

            stdin = open(os.devnull, "rb")
            files.append(stdin)
            handles = [msvcrt.get_osfhandle(stdin.fileno())]
            for _ in range(2):
                if capture:
                    read_fd, write_fd = os.pipe()
                    reader = os.fdopen(read_fd, "rb", buffering=0)
                    output = os.fdopen(write_fd, "wb", buffering=0)
                    self.readers.append(reader)
                else:
                    output = open(os.devnull, "wb")
                files.append(output)
                handles.append(msvcrt.get_osfhandle(output.fileno()))
            for handle in handles:
                os.set_handle_inheritable(handle, True)
            size = ctypes.c_size_t()
            k.InitializeProcThreadAttributeList(None, 1, 0, ctypes.byref(size))
            attribute_list = ctypes.create_string_buffer(size.value)
            if not k.InitializeProcThreadAttributeList(attribute_list, 1, 0, ctypes.byref(size)):
                raise win_error("InitializeProcThreadAttributeList")
            attributes_initialized = True
            handle_list = (W.HANDLE * 3)(*handles)
            if not k.UpdateProcThreadAttribute(
                attribute_list, 0, 0x20002, handle_list, ctypes.sizeof(handle_list), None, None
            ):
                raise win_error("UpdateProcThreadAttribute")
            startup = _StartupEx()
            startup.startup.cb = ctypes.sizeof(startup)
            startup.startup.flags = 0x100  # STARTF_USESTDHANDLES
            startup.startup.stdin, startup.startup.stdout, startup.startup.stderr = handles
            startup.attributes = ctypes.cast(attribute_list, ctypes.c_void_p)
            commandline = ctypes.create_unicode_buffer(command_line(argv))
            if not k.CreateProcessW(
                executable,
                commandline,
                None,
                None,
                True,
                0x4 | 0x80000 | 0x08000000,
                None,
                cwd,
                ctypes.byref(startup),
                ctypes.byref(info),
            ):
                raise win_error("CreateProcess")
            self.process, self.pid = info.process, int(info.pid)
            if not k.AssignProcessToJobObject(self.job, self.process):
                raise win_error("AssignProcessToJobObject")
            # No child instruction can execute before containment succeeds.
            if k.ResumeThread(info.thread) == 0xFFFFFFFF:
                raise win_error("ResumeThread")
        except BaseException:
            if self.process:
                k.TerminateProcess(self.process, 1)
                k.WaitForSingleObject(self.process, 5000)
            self.close()
            raise
        finally:
            if info.thread:
                k.CloseHandle(info.thread)
            if attributes_initialized:
                k.DeleteProcThreadAttributeList(attribute_list)
            for file in files:
                file.close()
        for index, reader in enumerate(self.readers):
            thread = threading.Thread(target=self._drain, args=(index, reader), daemon=True)
            self.threads.append(thread)
            thread.start()

    def _drain(self, index, reader):
        try:
            while chunk := reader.read(16384):
                self.totals[index] += len(chunk)
                remaining = OUTPUT_LIMIT // 2 - len(self.buffers[index])
                if remaining > 0:
                    self.buffers[index].extend(chunk[:remaining])
        except OSError:
            self.reader_errors.append(index)
        finally:
            reader.close()

    def poll(self):
        state = self.k.WaitForSingleObject(self.process, 0)
        if state == 258:
            return None
        if state != 0:
            raise win_error("WaitForSingleObject")
        code = W.DWORD()
        if not self.k.GetExitCodeProcess(self.process, ctypes.byref(code)):
            raise win_error("GetExitCodeProcess")
        return int(code.value)

    def stop(self):
        if self.job and not self.k.TerminateJobObject(self.job, 1):
            raise win_error("TerminateJobObject")
        if self.process:
            if self.k.WaitForSingleObject(self.process, 5000) != 0:
                raise win_error("WaitForSingleObject cleanup")

    def close(self):
        if self.job:
            self.k.CloseHandle(self.job)
            self.job = None
        for thread in self.threads:
            thread.join(5)
        for reader in self.readers:
            if not reader.closed and not self.threads:
                reader.close()
        if self.process:
            self.k.CloseHandle(self.process)
            self.process = None


class JobRunner:
    def __init__(self, artifact_store=None, factory=OwnedJob):
        self.artifact_store, self.factory = artifact_store, factory
        self.owned = []

    @property
    def protected_pids(self):
        return {job.pid for job in self.owned if job.pid}

    def launch(self, argv, cwd=None, cancel_event=None, deadline=None):
        checkpoint(cancel_event, deadline)
        data = launch_detached(argv, cwd)
        checkpoint(cancel_event, deadline, acted=True)
        return data

    def run(self, argv, cwd=None, cancel_event=None, deadline=None):
        checkpoint(cancel_event, deadline)
        job = self.factory(argv, cwd, capture=True)
        self.owned.append(job)
        try:
            while (exit_code := job.poll()) is None:
                checkpoint(cancel_event, deadline, acted=True)
                if cancel_event is not None:
                    cancel_event.wait(0.02)
                else:
                    time.sleep(0.02)
            # A grandchild may still hold stdout; terminate the owned remainder.
            job.stop()
            job.close()
            data = {"pid": job.pid, "exit_code": exit_code, "contained": True}
            data["output_encoding"] = "utf-8-replacement"
            for index, name in enumerate(("stdout", "stderr")):
                content = bytes(job.buffers[index])
                data[name] = content[:16384].decode("utf-8-sig", errors="replace")
                data[name + "_bytes"] = job.totals[index]
                data[name + "_truncated"] = job.totals[index] > 16384
                data[name + "_retained_bytes"] = len(content)
                if len(content) > 16384 and self.artifact_store is not None:
                    path = self.artifact_store.allocate(".json")
                    self.artifact_store.write_text(
                        path,
                        json.dumps(
                            {
                                "encoding": "utf-8-replacement",
                                "text": content.decode("utf-8", "replace"),
                                "total_bytes": job.totals[index],
                                "truncated": job.totals[index] > len(content),
                            },
                            ensure_ascii=True,
                        ),
                    )
                    self.artifact_store.register(path)
                    data[name + "_artifact"] = str(path)
            if job.reader_errors:
                raise ServiceError(
                    "OUTPUT_READ_FAILED", "Unable to collect child output", acted=True
                )
            return data
        except ServiceError as exc:
            exc.acted = True
            raise
        except Exception as exc:
            raise ServiceError(
                "CHILD_JOB_FAILED", "Owned process operation failed", acted=True
            ) from exc
        finally:
            try:
                if job.process:
                    try:
                        job.stop()
                    finally:
                        job.close()
            except ServiceError as exc:
                exc.acted = True
                raise
            finally:
                self.owned.remove(job)

    def close(self):
        for job in self.owned[:]:
            try:
                job.stop()
            finally:
                job.close()
                self.owned.remove(job)
