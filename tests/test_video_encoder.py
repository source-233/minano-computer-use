"""Synthetic codec evidence only: these tests never touch a real desktop."""

import io
import json
import multiprocessing
import os
import threading
import time

import pytest
from PIL import Image

from minano_computer_use.control import OperationCancelled
from minano_computer_use.video_encoder import (
    VideoEncoder,
    VideoEncodingError,
    _encode_worker,
    _LimitedOutput,
)


def _hang(manifest, raw, output, result, stop):
    # Ignore even the stop event, modelling a blocked native codec.
    while True:
        time.sleep(1)


def _corrupt(manifest, raw, output, result, stop):
    result.write(b'{"ok":')


def _oversized_result(manifest, raw, output, result, stop):
    result.write(b"x" * 9000)


def _backend_error(manifest, raw, output, result, stop):
    result.write(json.dumps({"ok": False, "code": "VIDEO_BACKEND"}).encode())


def _crash(manifest, raw, output, result, stop):
    os._exit(7)


def _bad_error(manifest, raw, output, result, stop):
    result.write(b'{"ok":false,"code":[]}')


def _corrupt_video(manifest, raw, output, result, stop):
    metadata = {
        "backend": "pyav",
        "pyav_version": "19.0.2",
        "libversions": {},
        "codec": "h264",
        "encoder": "libx264",
        "pix_fmt": "yuv420p",
        "logical_size": [17, 13],
        "encoded_size": [18, 14],
        "framecount": 4,
        "fps": 5.0,
    }
    output.write(b"not a real video")
    result.write(json.dumps({"ok": True, "metadata": metadata}).encode())


def _frames():
    return [Image.new("RGB", (17, 13), color) for color in ("red", "green", "blue", "white")]


def _children():
    return {child.pid for child in multiprocessing.active_children()}


def test_real_pyav_mp4_round_trip():
    import av

    before = _children()
    encoder = VideoEncoder()
    try:
        encoded = encoder.encode(_frames(), 5)
    finally:
        encoder.close()
        encoder.close()
    assert _children() == before
    assert encoded.data[4:8] == b"ftyp"
    assert encoded.metadata["backend"] == "pyav"
    assert encoded.metadata["pyav_version"] == "19.0.2"
    assert encoded.metadata["libversions"]
    assert encoded.metadata["logical_size"] == [17, 13]
    assert encoded.metadata["encoded_size"] == [18, 14]
    assert encoded.metadata["framecount"] == 4
    with av.open(io.BytesIO(encoded.data)) as container:
        stream = container.streams.video[0]
        assert stream.codec_context.name == "h264"
        assert stream.codec_context.pix_fmt == "yuv420p"
        assert float(stream.average_rate) == 5
        frames = list(container.decode(stream))
        assert len(frames) == 4
        assert [(frame.width, frame.height) for frame in frames] == [(18, 14)] * 4
        assert [float(frame.pts * frame.time_base) for frame in frames] == [0, 0.2, 0.4, 0.6]
        # The top-left logical area remains in place; padding is only right/bottom.
        red = frames[0].to_image().getpixel((4, 4))
        assert red[0] > 180 and red[1] < 50 and red[2] < 50


def test_hang_timeout_reaped_and_same_encoder_recovers():
    before = _children()
    encoder = VideoEncoder(timeout=0.4, worker_target=_hang)
    with pytest.raises(VideoEncodingError) as failure:
        encoder.encode(_frames(), 5)
    assert failure.value.code == "VIDEO_TIMEOUT"
    assert _children() == before
    encoder._worker = _encode_worker
    encoder.timeout = 10
    assert encoder.encode(_frames(), 5).metadata["framecount"] == 4
    encoder.close()
    assert _children() == before


def test_hang_caller_deadline_is_timeout_error():
    before = _children()
    encoder = VideoEncoder(worker_target=_hang)
    with pytest.raises(TimeoutError):
        encoder.encode(_frames(), 5, deadline=time.monotonic() + 0.4)
    encoder.close()
    assert _children() == before


@pytest.mark.parametrize("action", ["cancel", "close"])
def test_hang_cancel_and_concurrent_close_join_owner(action):
    before = _children()
    encoder = VideoEncoder(worker_target=_hang)
    cancel = threading.Event()
    failures = []

    def run():
        try:
            encoder.encode(_frames(), 5, cancel_event=cancel)
        except BaseException as exc:
            failures.append(exc)

    owner = threading.Thread(target=run)
    owner.start()
    end = time.monotonic() + 10
    while _children() == before and time.monotonic() < end:
        time.sleep(0.01)
    assert _children() != before
    if action == "cancel":
        cancel.set()
    encoder.close()
    owner.join(5)
    assert not owner.is_alive()
    assert len(failures) == 1 and isinstance(failures[0], OperationCancelled)
    assert _children() == before
    encoder.close()


@pytest.mark.parametrize(
    "worker", [_corrupt, _oversized_result, _corrupt_video, _backend_error, _crash, _bad_error]
)
def test_worker_errors_never_produce_video(worker):
    before = _children()
    encoder = VideoEncoder(worker_target=worker)
    with pytest.raises(VideoEncodingError):
        encoder.encode(_frames(), 5)
    encoder.close()
    assert _children() == before


def test_real_pyav_byte_quota():
    encoder = VideoEncoder()
    with pytest.raises(VideoEncodingError) as failure:
        encoder.encode(_frames(), 5, max_bytes=32)
    assert failure.value.code == "VIDEO_SIZE_LIMIT"
    encoder.close()


def test_write_boundary_quota_covers_seeks_and_rewrites():
    target = io.BytesIO()
    writer = _LimitedOutput(target, 10)
    writer.write(b"12345")
    writer.seek(9)
    writer.write(b"x")
    with pytest.raises(VideoEncodingError):
        writer.write(b"y")
    with pytest.raises(VideoEncodingError):
        writer.seek(11)
    writer.seek(0)
    writer.write(b"abcd")
    assert len(target.getvalue()) == 10


@pytest.mark.parametrize("fps", [0, -1, float("nan"), float("inf"), True])
def test_invalid_fps(fps):
    encoder = VideoEncoder()
    with pytest.raises(ValueError):
        encoder.encode(_frames(), fps)
    encoder.close()


def test_input_validation_and_pre_cancel_do_not_spawn():
    before = _children()
    encoder = VideoEncoder()
    for images in (
        [],
        [None],
        [Image.new("RGB", (1, 1)), Image.new("RGB", (2, 2))],
        [Image.new("RGB", (1024, 1024))] * 171,
    ):
        with pytest.raises(ValueError):
            encoder.encode(images, 5)
    cancel = threading.Event()
    cancel.set()
    with pytest.raises(OperationCancelled):
        encoder.encode(_frames(), 5, cancel_event=cancel)
    with pytest.raises(TimeoutError):
        encoder.encode(_frames(), 5, deadline=time.monotonic() - 1)
    encoder.close()
    with pytest.raises(VideoEncodingError):
        encoder.encode(_frames(), 5)
    assert _children() == before


def test_spooling_uses_same_deadline_and_does_not_start_child(monkeypatch):
    before = _children()
    convert = Image.Image.convert

    def delayed_convert(self, *args, **kwargs):
        time.sleep(0.02)
        return convert(self, *args, **kwargs)

    monkeypatch.setattr(Image.Image, "convert", delayed_convert)
    encoder = VideoEncoder()
    with pytest.raises(TimeoutError):
        encoder.encode(_frames(), 5, deadline=time.monotonic() + 0.01)
    encoder.close()
    assert _children() == before


@pytest.mark.parametrize("kill_works", [True, False])
def test_cleanup_escalates_bounded_join_and_retains_unreaped_child(monkeypatch, kill_works):
    from minano_computer_use import isolated_spawn

    class FakeProcess:
        pid = None
        alive = True

        def __init__(self):
            self.events = []
            self.kill_works = kill_works

        def start(self):
            self.pid = 42

        def is_alive(self):
            return self.alive

        def join(self, timeout=None):
            assert timeout is not None
            self.events.append(("join", timeout))

        def terminate(self):
            self.events.append("terminate")

        def kill(self):
            self.events.append("kill")
            if self.kill_works:
                self.alive = False

        def close(self):
            assert not self.alive
            self.events.append("close")

    child = FakeProcess()
    files = []

    def process(ctx, worker, args):
        files.extend(transfer.file for transfer in args[1:4])
        return child

    monkeypatch.setattr(isolated_spawn, "process", process)
    encoder = VideoEncoder(timeout=0.02)
    started = time.monotonic()
    with pytest.raises(VideoEncodingError) as failure:
        encoder.encode(_frames(), 5)
    assert time.monotonic() - started < 1
    assert failure.value.code == ("VIDEO_TIMEOUT" if kill_works else "VIDEO_CLEANUP_FAILED")
    assert all(file.closed for file in files)
    assert not encoder._active
    escalation = ["terminate", ("join", 1), "kill", ("join", 1)]
    assert child.events[-(5 if kill_works else 4) :] == escalation + (
        ["close"] if kill_works else []
    )
    if not kill_works:
        assert encoder._failed_child is child
        with pytest.raises(VideoEncodingError, match="cleanup is pending"):
            encoder.encode(_frames(), 5)
        with pytest.raises(VideoEncodingError) as close_failure:
            encoder.close()
        assert close_failure.value.code == "VIDEO_CLEANUP_FAILED"
        assert encoder._failed_child is child and child.alive
        child.kill_works = True
    encoder.close()
    assert encoder._failed_child is None
