"""A narrowly scoped Windows spawn process with isolated child stdio.

The MCP server owns inherited stdio handles.  CPython's normal Windows spawn
bootstrap inherits those handles, and a replacement interpreter can block
before unpickling its process object after the first worker is terminated.  The
custom Popen below preserves multiprocessing's pipe-duplication lifecycle but
passes only three explicit NUL handles to the child interpreter.

This deliberately does not patch ``_winapi`` or global multiprocessing state.
It follows CPython 3.13's ``popen_spawn_win32.Popen`` shape; changes to that
private stdlib protocol require revisiting this adapter.
"""

from __future__ import annotations

import os
import sys

if sys.platform == "win32":
    import _winapi
    import msvcrt
    from multiprocessing import popen_spawn_win32, reduction, spawn, util
    from multiprocessing.context import SpawnProcess, set_spawning_popen
    from subprocess import STARTF_FORCEOFFFEEDBACK, STARTF_USESTDHANDLES, STARTUPINFO

    _create_process = _winapi.CreateProcess

    class _IsolatedPopen(popen_spawn_win32.Popen):
        """stdlib spawn Popen with an explicit, minimal inherited handle list."""

        def __init__(self, process_obj):
            if sys.implementation.name != "cpython" or sys.version_info[:2] != (3, 13):
                raise RuntimeError("Isolated Windows spawn requires CPython 3.13")
            prep_data = spawn.get_preparation_data(process_obj._name)
            rhandle, whandle = _winapi.CreatePipe(None, 0)
            try:
                wfd = msvcrt.open_osfhandle(whandle, 0)
            except BaseException:
                _close_handles(rhandle, whandle)
                raise
            cmd = spawn.get_command_line(parent_pid=os.getpid(), pipe_handle=rhandle)
            python_exe = spawn.get_executable()
            winenv = not _path_eq(sys.executable, sys._base_executable)
            if winenv and _path_eq(python_exe, sys.executable):
                cmd[0] = python_exe = sys._base_executable
                env = os.environ.copy()
                env["__PYVENV_LAUNCHER__"] = sys.executable
            else:
                env = None
            cmd = " ".join(f'"{arg}"' for arg in cmd)

            stdin_fd = stdout_fd = stderr_fd = None
            self.finalizer = None
            try:
                with open(wfd, "wb", closefd=True) as to_child:
                    stdin_fd = os.open("NUL", os.O_RDONLY)
                    stdout_fd = os.open("NUL", os.O_WRONLY)
                    stderr_fd = os.open("NUL", os.O_WRONLY)
                    std_handles = [
                        msvcrt.get_osfhandle(fd) for fd in (stdin_fd, stdout_fd, stderr_fd)
                    ]
                    for handle in std_handles:
                        os.set_handle_inheritable(handle, True)
                    startup = STARTUPINFO()
                    startup.dwFlags = STARTF_FORCEOFFFEEDBACK | STARTF_USESTDHANDLES
                    startup.hStdInput, startup.hStdOutput, startup.hStdError = std_handles
                    startup.lpAttributeList = {"handle_list": std_handles}
                    hp, ht, pid, _ = _create_process(
                        python_exe, cmd, None, None, True, 0, env, None, startup
                    )
                    self.pid = pid
                    self.returncode = None
                    self._handle = hp
                    self.sentinel = int(hp)
                    self.finalizer = util.Finalize(
                        self, _close_handles, (self.sentinel, int(rhandle))
                    )
                    _winapi.CloseHandle(ht)
                    set_spawning_popen(self)
                    try:
                        reduction.dump(prep_data, to_child)
                        reduction.dump(process_obj, to_child)
                    finally:
                        set_spawning_popen(None)
            except BaseException:
                if self.finalizer is None:
                    _winapi.CloseHandle(rhandle)
                else:
                    # BaseProcess.start has not registered this child yet.
                    # Do not orphan it if pickling/preparation fails mid-start.
                    try:
                        self.terminate()
                        self.wait()
                    finally:
                        self.finalizer()
                raise
            finally:
                for fd in (stdin_fd, stdout_fd, stderr_fd):
                    if fd is not None:
                        os.close(fd)

    class IsolatedSpawnProcess(SpawnProcess):
        """SpawnProcess using only the local isolated Popen implementation."""

        @staticmethod
        def _Popen(process_obj):
            return _IsolatedPopen(process_obj)

    def _path_eq(first, second):
        return first == second or os.path.normcase(first) == os.path.normcase(second)

    def _close_handles(*handles):
        for handle in handles:
            _winapi.CloseHandle(handle)


else:

    class IsolatedSpawnProcess:  # pragma: no cover - Windows-only adapter
        pass


def process(context, target, args, *, daemon=True):
    """Create a process with isolated stdio, using normal spawn elsewhere."""
    if sys.platform == "win32":
        child = IsolatedSpawnProcess(target=target, args=args, daemon=daemon)
    else:  # pragma: no cover - project is Windows-first, retained for imports
        child = context.Process(target=target, args=args, daemon=daemon)
    return child
