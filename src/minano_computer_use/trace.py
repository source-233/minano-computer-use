"""Opt-in redacted call facts and ownership-scoped artifact retention."""

from __future__ import annotations

import json
import math
import os
import secrets
import sqlite3
import stat
import threading
import time
import uuid
from pathlib import Path


def _safe_directory(path: Path) -> Path:
    path = path.absolute()
    for component in (*reversed(path.parents), path):
        if component.exists() or component.is_symlink():
            info = component.lstat()
            if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
                raise ValueError("Artifact directory must not traverse symlinks or reparse points")
    path.mkdir(parents=True, exist_ok=True)
    return path


class ArtifactStore:
    """Only delete regular files allocated and registered in our own ledger."""

    def __init__(self, root: Path, max_bytes=256 * 1024 * 1024, max_files=200):
        if type(max_bytes) is not int or not 4096 <= max_bytes <= 16 * 1024**3:
            raise ValueError("artifact max_bytes must be an integer in [4096, 16GiB]")
        if type(max_files) is not int or not 1 <= max_files <= 100000:
            raise ValueError("artifact max_files must be an integer in [1, 100000]")
        self.root = _safe_directory(Path(root) / "minano-artifacts")
        self.max_bytes = max_bytes
        self.max_files = max_files
        self._lock = threading.RLock()
        self._pending: set[Path] = set()
        self._groups: dict[str, set[Path]] = {}
        self._latest_group: str | None = None
        self._group_open = False
        self._closed = False
        self._directory_handles = self._pin_directories(self.root)
        database = self.root / "ownership.sqlite3"
        try:
            descriptor = self._open_owned_file(database, exclusive=False)
            with os.fdopen(descriptor, "rb") as ledger:
                contents = ledger.read()
            # SQLite never reopens a pathname or creates path-based journals.
            # Persist its serialized bytes through the same atomic safe writer.
            self._database = sqlite3.connect(":memory:", check_same_thread=False)
            if contents:
                self._database.deserialize(contents)
        except BaseException:
            self._close_directory_handles()
            raise
        self._database.execute(
            "CREATE TABLE IF NOT EXISTS files(name TEXT PRIMARY KEY, created REAL NOT NULL)"
        )
        self._database.commit()
        self.prune()

    def _open_owned_file(self, path: Path, *, exclusive: bool = True) -> int:
        if os.name != "nt":
            flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
            if exclusive:
                flags |= os.O_EXCL
            return os.open(path.name, flags, 0o600, dir_fd=self._directory_handles[-1])
        import ctypes
        import msvcrt
        from ctypes import wintypes

        kernel, _ = self._directory_handles
        handle = kernel.CreateFileW(
            str(path), 0x80000000 | 0x40000000, 0x1, None, 1 if exclusive else 4, 0x00200000, None
        )
        if handle == wintypes.HANDLE(-1).value:
            raise OSError(ctypes.get_last_error(), "Unable to open owned file without following")
        attributes = (wintypes.DWORD * 2)()
        if not kernel.GetFileInformationByHandleEx(
            handle, 9, attributes, ctypes.sizeof(attributes)
        ) or attributes[0] & (0x400 | 0x10):
            kernel.CloseHandle(handle)
            raise ValueError("Owned file must not be a symlink/reparse point or directory")
        return msvcrt.open_osfhandle(handle, os.O_RDWR | os.O_BINARY)

    def _persist_ledger(self) -> None:
        self._atomic_write(self.root / "ownership.sqlite3", self._database.serialize())

    def _close_directory_handles(self) -> None:
        if os.name == "nt":
            kernel, handles = self._directory_handles
            for handle in handles:
                kernel.CloseHandle(handle)
        else:
            for descriptor in self._directory_handles:
                os.close(descriptor)

    def _check_root(self) -> None:
        for component in (*reversed(self.root.parents), self.root):
            info = component.lstat()
            if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
                raise ValueError("Artifact directory must not traverse symlinks or reparse points")
        if os.name != "nt":
            current = self.root.lstat()
            pinned = os.fstat(self._directory_handles[-1])
            if (current.st_dev, current.st_ino) != (pinned.st_dev, pinned.st_ino):
                raise ValueError("Artifact directory identity changed")

    def _unlink_owned(self, path: Path) -> None:
        if os.name == "nt":
            path.unlink(missing_ok=True)
        else:
            try:
                os.unlink(path.name, dir_fd=self._directory_handles[-1])
            except FileNotFoundError:
                pass

    @staticmethod
    def _pin_directories(root: Path):
        """Pin each existing path component; Windows handles deny delete/rename sharing."""
        if os.name != "nt":
            descriptors = [os.open(root.anchor, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))]
            for component in root.parts[1:]:
                try:
                    descriptor = os.open(
                        component,
                        os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
                        dir_fd=descriptors[-1],
                    )
                    descriptors.append(descriptor)
                except OSError as exc:
                    for descriptor in descriptors:
                        os.close(descriptor)
                    raise ValueError("Artifact directory could not be pinned") from exc
            return descriptors
        import ctypes
        from ctypes import wintypes

        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.CreateFileW.argtypes = [
            wintypes.LPCWSTR,
            wintypes.DWORD,
            wintypes.DWORD,
            ctypes.c_void_p,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.HANDLE,
        ]
        kernel.CreateFileW.restype = wintypes.HANDLE
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel.CloseHandle.restype = wintypes.BOOL
        kernel.GetFileInformationByHandleEx.argtypes = [
            wintypes.HANDLE,
            ctypes.c_int,
            ctypes.c_void_p,
            wintypes.DWORD,
        ]
        kernel.GetFileInformationByHandleEx.restype = wintypes.BOOL
        handles = []
        components = list(reversed(root.parents)) + [root]
        try:
            for component in components:
                handle = kernel.CreateFileW(
                    str(component), 0x1 | 0x80, 0x1 | 0x2, None, 3, 0x02000000 | 0x00200000, None
                )
                if handle == wintypes.HANDLE(-1).value:
                    raise OSError(ctypes.get_last_error(), "Unable to pin artifact directory")
                handles.append(handle)
                attributes = (wintypes.DWORD * 2)()
                if not kernel.GetFileInformationByHandleEx(
                    handle, 9, attributes, ctypes.sizeof(attributes)
                ):
                    raise OSError(ctypes.get_last_error(), "Unable to inspect artifact directory")
                if attributes[0] & 0x400 or not attributes[0] & 0x10:
                    raise ValueError("Artifact directory is a reparse point or not a directory")
            return (kernel, handles)
        except BaseException:
            for handle in handles:
                kernel.CloseHandle(handle)
            raise

    def begin_group(self) -> str:
        """Start an artifact-return group; prior group is eligible for quota pruning."""
        with self._lock:
            if self._group_open:
                raise RuntimeError("An artifact group is already open")
            self._groups.clear()
            token = secrets.token_hex(16)
            self._groups[token] = set()
            self._latest_group = token
            self._group_open = True
            self.prune()
            return token

    def end_group(self, token: str) -> None:
        """Close admissions to a group while keeping its artifacts pinned until next begin."""
        with self._lock:
            if token not in self._groups:
                raise ValueError("Unknown artifact group")
            if token == self._latest_group:
                self._group_open = False
            self.prune()

    @property
    def group_open(self) -> bool:
        return self._group_open

    def write_bytes(self, path: Path, data: bytes) -> None:
        """Atomically replace this pending reservation without following a swapped link."""
        path = Path(path).absolute()
        with self._lock:
            self._path(path)
            if path not in self._pending:
                raise ValueError("Artifact path is not a pending reservation")
            self._atomic_write(path, data)

    def _atomic_write(self, path: Path, data: bytes) -> None:
        temporary_name = f".write-{secrets.token_hex(16)}.tmp"
        temporary = self.root / temporary_name
        descriptor = self._open_owned_file(temporary)
        directory_fd = None if os.name == "nt" else self._directory_handles[-1]
        try:
            with os.fdopen(descriptor, "wb", closefd=True) as output:
                output.write(data)
                output.flush()
                os.fsync(output.fileno())
            # replace removes a final-component symlink itself, never follows it.
            if os.name == "nt":
                os.replace(temporary, path)
            else:
                os.replace(
                    temporary_name, path.name, src_dir_fd=directory_fd, dst_dir_fd=directory_fd
                )
        finally:
            if os.name == "nt":
                temporary.unlink(missing_ok=True)
            else:
                try:
                    os.unlink(temporary_name, dir_fd=directory_fd)
                except FileNotFoundError:
                    pass

    def write_text(self, path: Path, text: str, *, encoding: str = "utf-8") -> None:
        self.write_bytes(path, text.encode(encoding))

    def _path(self, path: Path) -> Path:
        path = Path(path).absolute()
        if path.parent != self.root or not path.name.startswith("artifact-"):
            raise ValueError("Not an owned artifact path")
        self._check_root()
        if path.is_symlink() or (
            path.exists() and getattr(path.lstat(), "st_file_attributes", 0) & 0x400
        ):
            raise ValueError("Artifact must not be a symlink/reparse point")
        return path

    def _pending_path(self, path: Path) -> Path:
        path = Path(path).absolute()
        if path.parent != self.root or not path.name.startswith("artifact-"):
            raise ValueError("Not an owned artifact path")
        return path

    def allocate(self, suffix: str) -> Path:
        if suffix not in {".png", ".jpg", ".jpeg", ".webp", ".mp4", ".jsonl", ".json"}:
            raise ValueError("Unsupported artifact suffix")
        with self._lock:
            if self._closed:
                raise RuntimeError("Artifact store closed")
            self._check_root()
            path = self.root / f"artifact-{uuid.uuid4().hex}{suffix}"
            # Exclusive reservation prevents accidental overwrite even before registration.
            descriptor = self._open_owned_file(path)
            os.close(descriptor)
            self._pending.add(path)
            return path

    def register(self, path: Path) -> None:
        with self._lock:
            path = self._path(path)
            if path not in self._pending:
                raise ValueError("Artifact was not allocated by this store instance")
            descriptor = self._open_owned_file(path, exclusive=False)
            with os.fdopen(descriptor, "rb") as artifact:
                info = os.fstat(artifact.fileno())
                if not stat.S_ISREG(info.st_mode):
                    raise ValueError("Artifact is not a regular file")
            if info.st_size > self.max_bytes:
                self._unlink_owned(path)
                self._pending.discard(path)
                raise ValueError("Artifact exceeds configured storage quota")
            with self._database:
                self._database.execute(
                    "INSERT INTO files(name,created) VALUES(?,?)", (path.name, time.time())
                )
            self._pending.discard(path)
            if self._latest_group is not None and self._group_open:
                self._groups[self._latest_group].add(path)
            self.prune(protect=path)

    def discard(self, path: Path) -> None:
        """Remove an unfinished reservation, never an arbitrary artifact."""
        with self._lock:
            path = self._pending_path(path)
            if path not in self._pending:
                raise ValueError("Only this instance's pending reservation can be discarded")
            # unlink removes a swapped symlink/reparse entry itself without opening its target.
            self._unlink_owned(path)
            self._pending.discard(path)

    def prune(self, protect: Path | None = None) -> dict:
        with self._lock:
            self._check_root()
            self._database.execute("BEGIN IMMEDIATE")
            removed = 0
            try:
                records = []
                for name, created in self._database.execute(
                    "SELECT name,created FROM files ORDER BY created,name"
                ).fetchall():
                    path = self._path(self.root / name)
                    if not path.exists():
                        self._database.execute("DELETE FROM files WHERE name=?", (name,))
                        continue
                    descriptor = self._open_owned_file(path, exclusive=False)
                    with os.fdopen(descriptor, "rb") as artifact:
                        info = os.fstat(artifact.fileno())
                    if not stat.S_ISREG(info.st_mode):
                        raise ValueError("Ledger contains a non-regular artifact")
                    records.append((path, info.st_size, created))
                total = sum(size for _, size, _ in records)
                count = len(records)
                for path, size, _ in records:
                    if total <= self.max_bytes and count <= self.max_files:
                        break
                    if path == protect or path in self._pending:
                        continue
                    if any(path in pinned for pinned in self._groups.values()):
                        continue
                    self._unlink_owned(path)
                    self._database.execute("DELETE FROM files WHERE name=?", (path.name,))
                    total -= size
                    count -= 1
                    removed += 1
                self._database.commit()
                self._persist_ledger()
                return {
                    "removed": removed,
                    "registered_files": count,
                    "registered_bytes": total,
                    "over_budget": total > self.max_bytes or count > self.max_files,
                }
            except BaseException:
                self._database.rollback()
                raise

    def close(self):
        with self._lock:
            if self._closed:
                return
            self._closed = True
            # Unregistered files may be partial writes. Only this instance's own
            # reservations are removed; never sweep the supplied output directory.
            for path in tuple(self._pending):
                try:
                    self._unlink_owned(path)
                except OSError:
                    pass
            self._pending.clear()
            self._database.close()
            self._close_directory_handles()


class TraceRecorder:
    def __init__(self, store: ArtifactStore, enabled=False):
        self.store = store
        self.enabled = bool(enabled)
        self._lock = threading.Lock()
        self._recorded = 0
        self._failures = 0
        self._closed = False

    def record(self, operation, request: dict, result: dict) -> None:
        if not self.enabled or self._closed:
            return
        path = None
        owned_group = None
        try:
            if not self.store.group_open:
                owned_group = self.store.begin_group()
            # Deliberately no recursive request/result serialization. Exceptions,
            # titles, selectors, values, text, filenames and screenshots stay out.
            known_operations = {
                "window",
                "act",
                "play",
                "record",
                "screenshot",
                "process.list",
                "process.launch",
                "process.shell",
                "process.kill",
                "process.wait",
                "process.wait_for_file",
                "process.wait_for_window",
                "system.displays",
                "system.cursor",
                "system.get_clipboard",
                "system.set_clipboard",
                "system.viewports",
                "system.clear_viewport",
            }
            event = {
                "schema": 1,
                "operation": operation if operation in known_operations else "other",
                "timestamp": time.time(),
                "monotonic": time.monotonic(),
                "pid": os.getpid(),
            }
            for key in ("call_id", "batch_id", "status", "method", "delivery"):
                if isinstance(result.get(key), str):
                    event[key] = result[key][:80]
            for key in ("may_have_acted", "cancelled", "replayed"):
                if isinstance(result.get(key), bool):
                    event[key] = result[key]
            for key in ("started_at", "finished_at"):
                if type(result.get(key)) in (float, int) and math.isfinite(result[key]):
                    event[key] = result[key]
            error = result.get("error")
            if isinstance(error, dict) and isinstance(error.get("code"), str):
                event["error_code"] = error["code"][:80]
            binding = result.get("target_binding")
            if isinstance(binding, dict):
                event["target_binding"] = {
                    key: binding[key] for key in ("pid", "hwnd") if type(binding.get(key)) is int
                }
            data = result.get("data")
            if isinstance(data, dict):
                for key in ("requested", "attempted", "completed", "stopped_early"):
                    if type(data.get(key)) in (int, bool):
                        event[key] = data[key]
                cleanup = data.get("cleanup")
                if isinstance(cleanup, dict) and isinstance(cleanup.get("status"), str):
                    event["cleanup_status"] = cleanup["status"][:32]
                items = data.get("results")
                if isinstance(items, list):
                    event["actions"] = [
                        {
                            "index": item.get("index"),
                            "status": str(item.get("result", {}).get("status", ""))[:32],
                            "method": str(item.get("result", {}).get("method", ""))[:32],
                        }
                        for item in items[:100]
                        if isinstance(item, dict)
                    ]
                    for action, item in zip(
                        event["actions"],
                        [x for x in items[:100] if isinstance(x, dict)],
                        strict=True,
                    ):
                        outcome = item.get("result", {})
                        error = outcome.get("error", {})
                        if isinstance(error, dict) and isinstance(error.get("code"), str):
                            action["error_code"] = error["code"][:80]
                        if isinstance(outcome.get("cancelled"), bool):
                            action["cancelled"] = outcome["cancelled"]
            path = self.store.allocate(".jsonl")
            self.store.write_text(path, json.dumps(event, ensure_ascii=True) + "\n")
            self.store.register(path)
            with self._lock:
                self._recorded += 1
        except Exception:
            if path is not None:
                try:
                    self.store.discard(path)
                except Exception:
                    pass
            with self._lock:
                self._failures += 1
        finally:
            if owned_group is not None:
                try:
                    self.store.end_group(owned_group)
                except Exception:
                    pass

    def status(self):
        with self._lock:
            return {
                "enabled": self.enabled,
                "recorded": self._recorded,
                "write_failures": self._failures,
            }

    def close(self):
        self._closed = True
