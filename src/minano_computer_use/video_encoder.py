"""Bounded PyAV encoding in a disposable, isolated spawn worker.

Injected workers have signature ``(manifest, raw, output, result, stop_event)``.
Files are already-open anonymous binary files, never paths. Write at most 8192
UTF-8 JSON bytes to result: {ok: true, metadata: {...}} or
{ok: false, code: str, message: str}. Only the parent persists artifacts.
"""

from __future__ import annotations

import json
import math
import multiprocessing
import os
import tempfile
import threading
import time
from dataclasses import dataclass
from fractions import Fraction
from multiprocessing import context, reduction

from PIL import Image

from . import isolated_spawn
from .control import OperationCancelled

_RAW_LIMIT = 512 * 1024**2
_RESULT_LIMIT = 8192


class VideoEncodingError(Exception):
    """An encoder failure; no fallback video has been produced."""

    def __init__(self, code: str, message: str):
        self.code, self.message = code, message
        super().__init__(message)


@dataclass(frozen=True)
class EncodedVideo:
    data: bytes
    metadata: dict


class _TransferFile:
    """Duplicate directly into the spawned child, avoiding parent-side leaks."""

    def __init__(self, file, mode):
        self.file, self.mode = file, mode

    def __reduce__(self):
        if os.name == "nt":
            import msvcrt

            popen = context.get_spawning_popen()
            handle = reduction.DupHandle(
                msvcrt.get_osfhandle(self.file.fileno()), 0xC0000000, pid=popen.pid
            )
        else:
            handle = reduction.DupFd(self.file.fileno())
        return _receive_file, (handle, self.mode)


def _receive_file(handle, mode):
    descriptor = handle.detach()
    if os.name == "nt":
        import msvcrt

        descriptor = msvcrt.open_osfhandle(descriptor, os.O_RDWR | os.O_BINARY)
    return os.fdopen(descriptor, mode, buffering=0)


class _LimitedOutput:
    def __init__(self, file, limit):
        self.file, self.limit = file, limit

    def tell(self):
        return self.file.tell()

    def seek(self, offset, whence=0):
        if whence not in (os.SEEK_SET, os.SEEK_CUR, os.SEEK_END):
            raise ValueError("Invalid seek origin")
        origin = 0 if whence == os.SEEK_SET else self.tell()
        if whence == os.SEEK_END:
            origin = os.fstat(self.file.fileno()).st_size
        if not 0 <= origin + offset <= self.limit:
            raise VideoEncodingError("VIDEO_SIZE_LIMIT", "Encoded video exceeds byte limit")
        return self.file.seek(offset, whence)

    def write(self, data):
        if self.tell() + len(data) > self.limit:
            raise VideoEncodingError("VIDEO_SIZE_LIMIT", "Encoded video exceeds byte limit")
        return self.file.write(data)

    def writable(self):
        return True

    def seekable(self):
        return True

    def flush(self):
        self.file.flush()


def _encode_worker(manifest, raw, output, result, stop_event):
    try:
        import av

        if av.__version__ != "19.0.2":
            raise VideoEncodingError("VIDEO_BACKEND", "PyAV 19.0.2 is required")
        width, height = manifest["width"], manifest["height"]
        encoded = (width + width % 2, height + height % 2)
        rate = Fraction(*manifest["rate"])
        frame_bytes = width * height * 3
        if os.fstat(raw.fileno()).st_size != manifest["expected_bytes"]:
            raise VideoEncodingError("VIDEO_INPUT", "Raw frame byte count mismatch")
        raw.seek(0)
        output.seek(0)
        with av.open(_LimitedOutput(output, manifest["max_bytes"]), "w", format="mp4") as mux:
            stream = mux.add_stream("libx264", rate=rate)
            stream.width, stream.height = encoded
            stream.pix_fmt = "yuv420p"
            stream.time_base = stream.codec_context.time_base = 1 / rate
            stream.codec_context.thread_count = 1
            for index in range(manifest["count"]):
                if stop_event.is_set():
                    raise OperationCancelled()
                pixels = raw.read(frame_bytes)
                if len(pixels) != frame_bytes:
                    raise VideoEncodingError("VIDEO_INPUT", "Incomplete raw frame")
                image = Image.frombytes("RGB", (width, height), pixels)
                if encoded != (width, height):
                    padded = Image.new("RGB", encoded)
                    padded.paste(image, (0, 0))
                    image = padded
                frame = av.VideoFrame.from_image(image)
                frame.pts, frame.time_base = index, 1 / rate
                for packet in stream.encode(frame):
                    mux.mux(packet)
            for packet in stream.encode():
                mux.mux(packet)
        metadata = {
            "backend": "pyav",
            "pyav_version": av.__version__,
            "libversions": {name: list(version) for name, version in av.library_versions.items()},
            "codec": "h264",
            "encoder": "libx264",
            "pix_fmt": "yuv420p",
            "logical_size": [width, height],
            "encoded_size": list(encoded),
            "framecount": manifest["count"],
            "fps": float(rate),
        }
        response = {"ok": True, "metadata": metadata}
    except BaseException as exc:
        response = {
            "ok": False,
            "code": exc.code if isinstance(exc, VideoEncodingError) else "VIDEO_BACKEND",
            # Never expose codec diagnostics containing paths or frame contents.
            "message": exc.message if isinstance(exc, VideoEncodingError) else type(exc).__name__,
        }
    try:
        payload = json.dumps(response).encode("utf-8")
        if len(payload) <= _RESULT_LIMIT:
            result.write(payload)
    finally:
        for file in (raw, output, result):
            file.close()


class VideoEncoder:
    def __init__(self, timeout=30.0, worker_target=None):
        if (
            isinstance(timeout, bool)
            or not isinstance(timeout, (int, float))
            or not math.isfinite(timeout)
            or timeout <= 0
        ):
            raise ValueError("timeout must be finite and positive")
        self.timeout, self._worker = timeout, worker_target or _encode_worker
        self._condition = threading.Condition()
        self._active, self._closed = False, False
        self._stop = threading.Event()
        self._failed_child = None

    def _reap(self, child):
        self._failed_child = child  # Keep ownership on failure; never close a live process.
        try:
            if child.is_alive():
                try:
                    child.terminate()
                except OSError:
                    pass
            child.join(timeout=1)
            if child.is_alive():
                try:
                    child.kill()
                except OSError:
                    pass
                child.join(timeout=1)
            if child.is_alive():
                raise VideoEncodingError(
                    "VIDEO_CLEANUP_FAILED", "Encoder worker could not be reaped"
                )
            child.close()
        except (OSError, RuntimeError) as exc:
            raise VideoEncodingError(
                "VIDEO_CLEANUP_FAILED", "Encoder worker cleanup failed"
            ) from exc
        self._failed_child = None

    def encode(
        self,
        images: list[Image.Image],
        fps: float,
        *,
        cancel_event=None,
        deadline=None,
        max_bytes=256 * 1024**2,
    ) -> EncodedVideo:
        started = time.monotonic()
        if deadline is not None and (
            isinstance(deadline, bool)
            or not isinstance(deadline, (int, float))
            or not math.isfinite(deadline)
        ):
            raise ValueError("deadline must be finite")
        expires = min(started + self.timeout, deadline if deadline is not None else math.inf)

        def checkpoint():
            if self._stop.is_set() or (cancel_event is not None and cancel_event.is_set()):
                raise OperationCancelled()
            if time.monotonic() >= expires:
                if deadline is not None and deadline <= started + self.timeout:
                    raise TimeoutError("Video encoding caller deadline expired")
                raise VideoEncodingError("VIDEO_TIMEOUT", "Video encoding timed out")

        with self._condition:
            if self._failed_child is not None:
                raise VideoEncodingError(
                    "VIDEO_CLEANUP_FAILED", "Encoder worker cleanup is pending"
                )
            if self._closed or self._active:
                raise VideoEncodingError("VIDEO_UNAVAILABLE", "Encoder is closed or already active")
            self._active = True
        child = None
        try:
            checkpoint()
            if (
                isinstance(fps, bool)
                or not isinstance(fps, (int, float))
                or not math.isfinite(fps)
                or not 0 < fps <= 1000
            ):
                raise ValueError("fps must be finite in (0, 1000]")
            if type(max_bytes) is not int or not 1 <= max_bytes <= 256 * 1024**2:
                raise ValueError("max_bytes must be an integer in [1, 256MiB]")
            if not isinstance(images, list) or not 1 <= len(images) <= 100000:
                raise ValueError("images must contain 1 to 100000 PIL frames")
            if not isinstance(images[0], Image.Image):
                raise ValueError("all frames must be PIL images")
            width, height = images[0].size
            if not 1 <= width <= 16384 or not 1 <= height <= 16384:
                raise ValueError("frame geometry exceeds encoder limits")
            expected = width * height * 3 * len(images)
            if expected > _RAW_LIMIT:
                raise ValueError("raw frames exceed 512MiB")
            rate = Fraction(str(fps)).limit_denominator(1_000_000)
            if rate <= 0:
                raise ValueError("fps is below supported time-base precision")
            manifest = {
                "count": len(images),
                "width": width,
                "height": height,
                "rate": [rate.numerator, rate.denominator],
                "expected_bytes": expected,
                "max_bytes": max_bytes,
            }
            with (
                tempfile.TemporaryFile() as raw,
                tempfile.TemporaryFile() as output,
                tempfile.TemporaryFile() as result,
            ):
                try:
                    for image in images:
                        checkpoint()
                        if not isinstance(image, Image.Image) or image.size != (width, height):
                            raise ValueError("all frames must be PIL images of identical size")
                        pixels = image.convert("RGB").tobytes()
                        if len(pixels) != width * height * 3:
                            raise ValueError("RGB frame byte count mismatch")
                        raw.write(pixels)
                    raw.flush()
                    checkpoint()
                    ctx = multiprocessing.get_context("spawn")
                    stop = ctx.Event()
                    child = isolated_spawn.process(
                        ctx,
                        self._worker,
                        (
                            manifest,
                            _TransferFile(raw, "rb"),
                            _TransferFile(output, "r+b"),
                            _TransferFile(result, "r+b"),
                            stop,
                        ),
                    )
                    child.start()
                    while child.is_alive():
                        checkpoint()
                        child.join(min(0.02, max(0, expires - time.monotonic())))
                    child.join(timeout=1)
                    checkpoint()
                    if child.exitcode != 0:
                        raise VideoEncodingError(
                            "VIDEO_BACKEND", "Encoder worker exited abnormally"
                        )
                    result.seek(0)
                    payload = result.read(_RESULT_LIMIT + 1)
                    try:
                        if len(payload) > _RESULT_LIMIT:
                            raise ValueError()
                        response = json.loads(payload)
                        if not isinstance(response, dict) or type(response.get("ok")) is not bool:
                            raise ValueError()
                    except (ValueError, UnicodeError, RecursionError) as exc:
                        raise VideoEncodingError(
                            "VIDEO_PROTOCOL", "Invalid encoder result"
                        ) from exc
                    if not response["ok"]:
                        code = response.get("code", "VIDEO_BACKEND")
                        if not isinstance(code, str) or code not in {
                            "VIDEO_BACKEND",
                            "VIDEO_SIZE_LIMIT",
                            "VIDEO_INPUT",
                        }:
                            code = "VIDEO_BACKEND"
                        raise VideoEncodingError(code, "PyAV encoder failed")
                    metadata = response.get("metadata")
                    if (
                        not isinstance(metadata, dict)
                        or any(
                            metadata.get(key) != value
                            for key, value in {
                                "backend": "pyav",
                                "pyav_version": "19.0.2",
                                "codec": "h264",
                                "encoder": "libx264",
                                "pix_fmt": "yuv420p",
                                "logical_size": [width, height],
                                "encoded_size": [width + width % 2, height + height % 2],
                                "framecount": len(images),
                                "fps": float(rate),
                            }.items()
                        )
                        or not isinstance(metadata.get("libversions"), dict)
                    ):
                        raise VideoEncodingError("VIDEO_PROTOCOL", "Invalid encoder metadata")
                    output.seek(0)
                    data = output.read(max_bytes + 1)
                    checkpoint()
                    if not 12 <= len(data) <= max_bytes or data[4:8] != b"ftyp":
                        raise VideoEncodingError(
                            "VIDEO_PROTOCOL", "Invalid or oversized MP4 output"
                        )
                    return EncodedVideo(data, metadata)
                finally:
                    # Owner alone reaps the child before any shared file closes.
                    if child is not None and child.pid is not None:
                        stop.set()
                        self._reap(child)
        except (OSError, EOFError, RuntimeError) as exc:
            if isinstance(exc, TimeoutError):
                raise
            raise VideoEncodingError(
                "VIDEO_BACKEND", "Encoder resource or startup failure"
            ) from exc
        finally:
            with self._condition:
                self._active = False
                self._condition.notify_all()

    def close(self):
        """Signal cancellation, then wait for the owner to reap and clean up."""
        with self._condition:
            self._closed = True
            self._stop.set()
            if not self._condition.wait_for(lambda: not self._active, timeout=3):
                raise VideoEncodingError("VIDEO_CLEANUP_FAILED", "Encoder owner cleanup is pending")
            if self._failed_child is not None:
                self._reap(self._failed_child)
