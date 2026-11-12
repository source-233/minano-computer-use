from __future__ import annotations

import io
import json
from dataclasses import is_dataclass
from pathlib import Path
from threading import Event

import pytest
from PIL import Image

from minano_computer_use.capture import CaptureService
from minano_computer_use.coordinates import CoordinateError, CoordinateManager
from minano_computer_use.video_encoder import EncodedVideo, VideoEncodingError


class FakeVideoEncoder:
    """No process or desktop work for tests unrelated to the encoding backend."""

    def __init__(self, error=None, data=b"test-mp4-data"):
        self.error = error
        self.data = data
        self.closed = False
        self.calls = []

    def encode(self, images, fps, **kwargs):
        self.calls.append((images, fps, kwargs))
        if self.error is not None:
            raise self.error
        return EncodedVideo(data=self.data, metadata={"backend": "pyav", "codec": "h264"})

    def close(self):
        self.closed = True


def unavailable_encoder():
    return FakeVideoEncoder(VideoEncodingError("VIDEO_CODEC_UNAVAILABLE", "H.264 unavailable"))


class FakeGrabber:
    monitors = [{"left": 0, "top": 0, "width": 400, "height": 200}]

    def __init__(self) -> None:
        self.closed = False
        self.calls = 0

    def grab(self, rect) -> Image.Image:
        assert rect == {"left": 0, "top": 0, "width": 400, "height": 200}
        self.calls += 1
        return Image.new("RGB", (400, 200), (self.calls % 255, 10, 20))

    def close(self) -> None:
        self.closed = True


def test_screenshot_encoding_scale_and_backend(tmp_path: Path) -> None:
    grabber = FakeGrabber()
    frame = CaptureService(tmp_path, grabber=grabber).screenshot(max_dim=100, format="png")
    assert frame.mime_type == "image/png"
    assert frame.metadata["width"] == 100
    assert frame.metadata["height"] == 50
    assert frame.metadata["scale"] == [0.25, 0.25]
    assert frame.metadata["capture_backend"] == "injected"
    assert frame.metadata["occlusion_free"] is False


def test_screenshot_defaults_to_compact_jpeg_and_png_quality_is_ignored():
    jpeg = CaptureService(grabber=FakeGrabber()).screenshot()
    assert jpeg.mime_type == "image/jpeg"
    assert jpeg.metadata["format"] == "jpeg"
    assert jpeg.metadata["quality"] == 75
    assert jpeg.metadata["quality_applied"] is True
    assert jpeg.metadata["output_size"] == [400, 200]
    png = CaptureService(grabber=FakeGrabber()).screenshot(format="png", quality=1)
    assert png.mime_type == "image/png"
    assert png.metadata["quality"] is None
    assert png.metadata["quality_applied"] is False


@pytest.mark.parametrize("kwargs", [{"quality": 0}, {"quality": 96}, {"quality": True}])
def test_jpeg_quality_is_validated_before_capture(kwargs):
    grabber = FakeGrabber()
    with pytest.raises(ValueError, match="quality"):
        CaptureService(grabber=grabber).screenshot(**kwargs)
    assert grabber.calls == 0


def test_record_montage_samples_and_missing_codec_warning(tmp_path: Path) -> None:
    grabber = FakeGrabber()
    frame = CaptureService(tmp_path, grabber=grabber, encoder=unavailable_encoder()).record(
        seconds=0.05, fps=10, montage_frames=2, format="png"
    )
    assert frame.image_bytes.startswith(b"\x89PNG")
    assert frame.metadata["frames"] >= 1
    assert len(frame.metadata["montage_timestamps"]) <= 2
    assert frame.metadata["mp4_path"] is None
    assert "VIDEO_CODEC_UNAVAILABLE" in frame.metadata["warning"]
    assert frame.metadata["encoder_backend"] == "pyav"
    assert frame.metadata["encoder_diagnostics"]["message"] == "H.264 unavailable"
    assert Path(frame.metadata["montage_path"]).exists()
    assert frame.metadata["format"] == "png"


def test_record_composes_then_scales_montage_and_keeps_original_frame_dimensions(tmp_path):
    frame = CaptureService(tmp_path, grabber=FakeGrabber(), encoder=unavailable_encoder()).record(
        seconds=0.05, fps=1, montage_frames=1, max_dim=80
    )
    assert frame.mime_type == "image/jpeg"
    assert frame.metadata["original_size"][0] > 80
    assert max(frame.metadata["output_size"]) == 80
    assert frame.metadata["scale"][0] < 1
    assert frame.metadata["format"] == "jpeg"
    assert frame.metadata["quality"] == 75


@pytest.mark.parametrize("method", ["screenshot", "record"])
@pytest.mark.parametrize(
    "kwargs",
    [
        {"max_dim": -1},
        {"max_dim": 4097},
        {"max_dim": 0.5},
        {"max_dim": True},
        {"format": "gif"},
        {"format": None},
        {"quality": 0},
        {"quality": 96},
        {"format": "webp", "quality": 96},
    ],
)
def test_image_options_reject_before_sampling_or_artifact_creation(tmp_path, method, kwargs):
    grabber = FlexibleGrabber()
    camera = CaptureService(tmp_path, grabber=grabber)
    with pytest.raises(ValueError):
        getattr(camera, method)(**kwargs)
    assert not grabber.rects
    assert camera._artifact_store is None


@pytest.mark.parametrize(
    "format,mime,suffix",
    [
        ("jpeg", "image/jpeg", ".jpg"),
        ("jpg", "image/jpeg", ".jpg"),
        ("png", "image/png", ".png"),
        ("webp", "image/webp", ".webp"),
    ],
)
def test_record_caps_whole_multicell_montage_and_preserves_fullsize_frames(
    tmp_path, monkeypatch, format, mime, suffix
):
    class LargeGrabber:
        monitors = [{"left": 0, "top": 0, "width": 1200, "height": 600}]

        def grab(self, rect):
            return Image.new("RGB", (1200, 600), "red")

    camera = CaptureService(tmp_path, grabber=LargeGrabber())
    encoded_sizes = []

    def fake_encoder(images, fps, **kwargs):
        encoded_sizes.extend(image.size for image in images)
        return None

    monkeypatch.setattr(camera, "_write_video", fake_encoder)
    frame = camera.record(seconds=0.1, fps=30, montage_frames=6, format=format, keep_frames=True)
    assert frame.mime_type == mime
    assert max(frame.metadata["output_size"]) == 800
    assert max(frame.metadata["montage_original_size"]) > 800
    assert Image.open(io.BytesIO(frame.image_bytes)).size == tuple(frame.metadata["output_size"])
    artifact = Path(frame.metadata["montage_path"])
    assert artifact.suffix == suffix
    assert artifact.read_bytes() == frame.image_bytes
    assert frame.metadata["image_bytes"] == len(frame.image_bytes)
    assert frame.metadata["mime_type"] == mime
    assert encoded_sizes and set(encoded_sizes) == {(1200, 600)}
    assert frame.metadata["logical_size"] == [1200, 600]
    assert all(Image.open(path).size == (1200, 600) for path in frame.metadata["frame_paths"])
    camera.close()


def test_record_max_dim_zero_keeps_full_composed_montage(tmp_path, monkeypatch):
    camera = CaptureService(tmp_path, grabber=FakeGrabber())
    monkeypatch.setattr(camera, "_write_video", lambda *args, **kwargs: None)
    frame = camera.record(seconds=0.1, fps=30, max_dim=0)
    assert frame.metadata["output_size"] == frame.metadata["montage_original_size"]
    assert max(frame.metadata["output_size"]) > 800
    assert frame.metadata["scale"] == [1.0, 1.0]
    camera.close()


def test_screenshot_image_roi_uses_scaled_immutable_pixels_and_full_edges():
    manager = CoordinateManager()
    camera = CaptureService(grabber=FakeGrabber(), coordinate_manager=manager)
    first = camera.screenshot(max_dim=100, scope="a")
    first.metadata["physical_transform"]["origin"][0] = 10000
    frame = camera.screenshot(
        max_dim=0,
        region={"left": 25, "top": 10, "width": 75, "height": 40},
        region_space="image",
        scope="a",
    )
    assert frame.metadata["region"] == [100, 40, 400, 200]
    assert frame.metadata["output_size"] == [300, 160]
    assert frame.metadata["requested_region_space"] == "image"
    assert frame.metadata["physical_transform"]["origin"] == [100, 40]
    with pytest.raises(CoordinateError, match="scope"):
        camera.screenshot(region=[0, 0, 1, 1], region_space="image", scope="b")


def test_record_image_roi_maps_once_without_updating_latest(tmp_path, monkeypatch):
    manager = CoordinateManager()
    camera = CaptureService(tmp_path, grabber=FakeGrabber(), coordinate_manager=manager)
    monkeypatch.setattr(camera, "_write_video", lambda *args, **kwargs: None)
    first = camera.screenshot(max_dim=100)
    frame = camera.record(
        seconds=0.05,
        fps=1,
        region_space="image",
        region={"left": 10, "top": 5, "width": 90, "height": 45},
    )
    assert frame.metadata["logical_size"] == [360, 180]
    assert manager.map_region([0, 0, 100, 50], capture_id=first.metadata["capture_id"]) == {
        "x": 0,
        "y": 0,
        "w": 400,
        "h": 200,
    }
    camera.close()


def test_auto_window_failure_never_implicitly_falls_back_to_screen(monkeypatch):
    from minano_computer_use import coordinates
    from minano_computer_use.window_capture import WindowCaptureError

    current = {
        "hwnd": 10,
        "pid": 20,
        "process_creation_time": 1234,
        "rect": [0, 0, 100, 100],
        "visible": True,
    }
    monkeypatch.setattr(coordinates, "window_snapshot", lambda _: dict(current))
    attempts = []

    def unavailable(hwnd, pid, backend, *args):
        attempts.append(backend)
        raise RuntimeError("synthetic backend unavailable")

    monkeypatch.setattr("minano_computer_use.window_capture.capture_window", unavailable)
    grabber = FlexibleGrabber()
    camera = CaptureService(grabber=grabber)
    with pytest.raises(WindowCaptureError):
        camera.screenshot("hwnd:10", capture_mode="auto")
    assert attempts == ["printwindow", "wgc"]
    assert not grabber.rects
    frame = camera.screenshot("hwnd:10", capture_mode="auto", allow_fallback=True)
    assert frame.metadata["capture_surface"] == "screen_composite"
    assert grabber.rects


def test_owned_grabber_closes_when_target_resolution_fails(tmp_path: Path) -> None:
    class OwnedService(CaptureService):
        def _make_grabber(self, supplied=None):
            return grabber, True

    grabber = FakeGrabber()
    service = OwnedService(tmp_path)
    try:
        service.record(target="missing-window", seconds=0.05, fps=1)
    except Exception:
        pass
    assert grabber.closed


def test_screenshot_region_is_relative_to_negative_origin_and_black_is_valid() -> None:
    class NegativeGrabber(FakeGrabber):
        monitors = [{"left": -1920, "top": -100, "width": 400, "height": 200}]

        def grab(self, rect) -> Image.Image:
            assert rect == {"left": -1920, "top": -100, "width": 400, "height": 200}
            return Image.new("RGB", (400, 200), "black")

    frame = CaptureService(grabber=NegativeGrabber()).screenshot(region=[10, 20, 110, 70])
    assert frame.metadata["original_size"] == [400, 200]
    assert frame.metadata["output_size"] == [100, 50]
    assert frame.metadata["region"] == [10, 20, 110, 70]
    assert frame.metadata["origin"] == [-1920, -100]
    assert frame.metadata["physical_transform"]["origin"] == [-1910, -80]
    assert Image.open(io.BytesIO(frame.image_bytes)).getpixel((0, 0)) == (0, 0, 0)


def test_screenshot_region_must_be_contained() -> None:
    import pytest

    with pytest.raises(ValueError, match="contained"):
        CaptureService(grabber=FakeGrabber()).screenshot(region=[-1, 0, 5, 5])


def test_record_cancellation_interrupts_sampling_wait(tmp_path: Path) -> None:
    import pytest

    from minano_computer_use.control import OperationCancelled

    class CancellingEvent(Event):
        def wait(self, timeout=None):
            self.set()
            return True

    with pytest.raises(OperationCancelled):
        CaptureService(tmp_path, grabber=FakeGrabber()).record(
            seconds=1, fps=1, cancel_event=CancellingEvent()
        )


def test_encoder_cancellation_propagates_and_close_reaps_owned_encoder(tmp_path: Path) -> None:
    import pytest

    from minano_computer_use.control import OperationCancelled

    cancel = Event()

    encoder = FakeVideoEncoder(OperationCancelled("encoder cancelled"))
    service = CaptureService(tmp_path, encoder=encoder)
    with pytest.raises(OperationCancelled):
        service._write_video([Image.new("RGB", (2, 2))], 1, cancel_event=cancel)
    assert encoder.calls[0][2]["cancel_event"] is cancel
    assert not service._artifact_store._pending
    service.close()
    assert encoder.closed


def test_encoder_cancellation_after_success_never_publishes_mp4(tmp_path):
    from minano_computer_use.control import OperationCancelled

    cancelled = Event()

    class LateCancellingEncoder(FakeVideoEncoder):
        def encode(self, *args, **kwargs):
            encoded = super().encode(*args, **kwargs)
            cancelled.set()
            return encoded

    camera = CaptureService(tmp_path, grabber=FakeGrabber(), encoder=LateCancellingEncoder())
    try:
        with pytest.raises(OperationCancelled):
            camera.record(seconds=0.05, fps=1, cancel_event=cancelled)
        assert not camera._artifact_store._pending
        assert not list(tmp_path.glob("*.mp4"))
    finally:
        camera.close()


def test_failed_encoder_reservations_are_discarded(tmp_path: Path) -> None:
    from minano_computer_use.trace import ArtifactStore

    store = ArtifactStore(tmp_path)
    service = CaptureService(artifact_store=store, encoder=unavailable_encoder())
    for _ in range(3):
        assert service._write_video([Image.new("RGB", (2, 2))], 1) is None
        assert not store._pending
    service.close()
    # Injected stores belong to the caller, not CaptureService.close().
    path = store.allocate(".png")
    store.discard(path)
    store.close()


@pytest.mark.parametrize("store_limit", [4096, 512 * 1024**2])
def test_encoder_receives_deadline_and_byte_budget_and_success_metadata(tmp_path, store_limit):
    import time

    from minano_computer_use.trace import ArtifactStore

    store = ArtifactStore(tmp_path, max_bytes=store_limit)
    encoder = FakeVideoEncoder()
    camera = CaptureService(artifact_store=store, encoder=encoder)
    deadline = time.monotonic() + 10
    path = camera._write_video([Image.new("RGB", (3, 5))], 2.5, deadline=deadline)
    assert path.read_bytes() == encoder.data
    assert encoder.calls[0][1] == 2.5
    assert encoder.calls[0][2]["deadline"] == deadline
    assert encoder.calls[0][2]["max_bytes"] == min(store_limit, 256 * 1024**2)
    assert camera._last_encoder_diagnostics == {"backend": "pyav", "codec": "h264"}
    assert not store._pending
    camera.close()
    store.close()


@pytest.mark.parametrize("during_encode", [False, True])
def test_encoder_deadline_propagates_without_pending_reservation(tmp_path, during_encode):
    import time

    encoder = FakeVideoEncoder(TimeoutError("inherited deadline expired"))
    camera = CaptureService(tmp_path, encoder=encoder)
    with pytest.raises(TimeoutError):
        camera._write_video(
            [Image.new("RGB", (2, 2))],
            1,
            deadline=time.monotonic() + (10 if during_encode else -1),
        )
    assert camera._artifact_store is None or not camera._artifact_store._pending
    assert len(encoder.calls) == int(during_encode)
    camera.close()


@pytest.mark.parametrize("data", [b"", b"x" * 4097])
def test_encoder_rejects_empty_or_overbudget_output_and_discards_reservation(tmp_path, data):
    from minano_computer_use.trace import ArtifactStore

    store = ArtifactStore(tmp_path, max_bytes=4096)
    camera = CaptureService(artifact_store=store, encoder=FakeVideoEncoder(data=data))
    assert camera._write_video([Image.new("RGB", (2, 2))], 1) is None
    assert camera._last_encoder_diagnostics["code"] in {"VIDEO_OUTPUT_LIMIT", "VIDEO_EMPTY_OUTPUT"}
    assert not store._pending
    assert not list(tmp_path.glob("*.mp4"))
    camera.close()
    store.close()


def test_encoder_reservation_swap_never_overwrites_unowned_file(tmp_path):
    from minano_computer_use.trace import ArtifactStore

    store = ArtifactStore(tmp_path / "owned")
    victim = tmp_path / "unowned.mp4"
    victim.write_bytes(b"keep-unowned-data")

    class SwappingEncoder(FakeVideoEncoder):
        def encode(self, *args, **kwargs):
            reserved = next(iter(store._pending))
            reserved.unlink()
            try:
                reserved.symlink_to(victim)
            except OSError:
                pytest.skip("OS does not permit symlinks for artifact race fixture")
            return super().encode(*args, **kwargs)

    camera = CaptureService(artifact_store=store, encoder=SwappingEncoder())
    try:
        assert camera._write_video([Image.new("RGB", (2, 2))], 1) is None
        assert victim.read_bytes() == b"keep-unowned-data"
        assert not store._pending
    finally:
        camera.close()
        store.close()


class FlexibleGrabber:
    monitors = [
        {"left": -100, "top": -50, "width": 500, "height": 300},
        {"left": 0, "top": 0, "width": 400, "height": 200},
        {"left": -100, "top": -50, "width": 100, "height": 150},
    ]

    def __init__(self, callback=None):
        self.rects = []
        self.callback = callback

    def grab(self, rect):
        self.rects.append(dict(rect))
        if self.callback:
            self.callback()
        return Image.new("RGB", (rect["width"], rect["height"]), "red")


def test_snapshot_full_resolution_save_is_original_not_upscaled_response(tmp_path):
    class PatternGrabber(FakeGrabber):
        def grab(self, rect):
            image = Image.new("RGB", (400, 200), "black")
            image.putpixel((11, 21), (1, 2, 3))
            return image

    camera = CaptureService(tmp_path, grabber=PatternGrabber())
    frame = camera.screenshot(max_dim=10, save=True, region=[10, 20, 110, 70])
    saved = Image.open(frame.metadata["saved_path"])
    assert saved.size == (100, 50)
    assert saved.getpixel((1, 1)) == (1, 2, 3)
    assert frame.metadata["output_size"] == [10, 5]
    assert frame.metadata["saved_size"] == [100, 50]
    assert is_dataclass(frame)
    full = camera.screenshot(max_dim=0)
    assert full.metadata["output_size"] == [400, 200]
    assert full.metadata["scale"] == [1.0, 1.0]
    camera.close()


def test_snapshot_remember_false_and_record_never_replace_click_context(tmp_path):
    manager = CoordinateManager()
    camera = CaptureService(
        tmp_path,
        grabber=FakeGrabber(),
        coordinate_manager=manager,
        encoder=unavailable_encoder(),
    )
    first = camera.screenshot(max_dim=100, scope="client")
    camera.screenshot(max_dim=200, scope="client", remember=False)
    camera.record(seconds=0.05, fps=1, scope="client")
    assert manager.map_region(
        [10, 10, 11, 11], capture_id=first.metadata["capture_id"], scope="client"
    ) == {"x": 40, "y": 40, "w": 4, "h": 4}
    first.metadata["physical_transform"]["origin"][0] = 10000
    assert manager.map_region([0, 0, 1, 1], scope="client") == {"x": 0, "y": 0, "w": 4, "h": 4}
    camera.close()


@pytest.mark.parametrize(
    "target,index,origin",
    [
        ("display:0", 0, [0, 0]),
        ("display:1", 1, [-100, -50]),
        ("display:primary", 0, [0, 0]),
        ("display:left", 1, [-100, -50]),
        ("display:right", 0, [0, 0]),
    ],
)
def test_display_grammar_is_zero_based_and_never_focuses(target, index, origin):
    frame = CaptureService(grabber=FlexibleGrabber()).screenshot(target, max_dim=0)
    assert frame.metadata["display_index"] == index
    assert frame.metadata["origin"] == origin


@pytest.mark.parametrize("target", ["display:-1", "display:2", "display:wat"])
def test_invalid_display_refuses_without_capture(target):
    grabber = FlexibleGrabber()
    with pytest.raises(ValueError):
        CaptureService(grabber=grabber).screenshot(target)
    assert grabber.rects == []


def test_screen_rect_object_surface_object_and_legacy_list_are_explicit():
    camera = CaptureService(grabber=FlexibleGrabber())
    frame = camera.screenshot(
        "region", region={"x": -90, "y": -30, "w": 40, "h": 30}, region_space="screen"
    )
    assert frame.metadata["region"] == [10, 20, 50, 50]
    assert frame.metadata["physical_transform"]["origin"] == [-90, -30]
    surface = camera.screenshot(region={"x": 10, "y": 20, "w": 40, "h": 30})
    legacy = camera.screenshot(region=[10, 20, 50, 50])
    assert surface.image_bytes == legacy.image_bytes == frame.image_bytes
    with pytest.raises(ValueError, match="screen region"):
        camera.screenshot(region=[0, 0, 5, 5], region_space="screen")
    with pytest.raises(ValueError, match="integer"):
        camera.screenshot(region={"x": 0.2, "y": 0, "w": 4, "h": 4})


def test_image_region_uses_latest_context_and_accepts_legacy_aliases():
    manager = CoordinateManager()
    camera = CaptureService(grabber=FlexibleGrabber(), coordinate_manager=manager)
    first = camera.screenshot(max_dim=0, scope="caller")
    frame = camera.screenshot(
        max_dim=0,
        region={"left": 10, "top": 20, "width": 20, "height": 20},
        region_space="image",
        scope="caller",
    )
    assert first.metadata["output_size"] == [500, 300]
    assert frame.metadata["region_space"] == "screen"
    assert frame.metadata["region"] == [10, 20, 30, 40]
    assert frame.metadata["physical_transform"]["origin"] == [-90, -30]
    with pytest.raises(ValueError, match="image region"):
        camera.screenshot(region=[0, 0, 501, 300], region_space="image", scope="caller")


def test_capture_mode_auto_is_screen_for_desktop_and_window_alias_for_window(monkeypatch):
    from minano_computer_use import coordinates

    assert (
        CaptureService(grabber=FakeGrabber())
        .screenshot(capture_mode="auto")
        .metadata["capture_surface"]
        == "screen_composite"
    )
    current = {
        "hwnd": 10,
        "pid": 20,
        "process_creation_time": 1234,
        "rect": [0, 0, 2, 2],
        "visible": True,
        "minimized": False,
    }
    monkeypatch.setattr(coordinates, "window_snapshot", lambda _: dict(current))
    monkeypatch.setattr(
        "minano_computer_use.window_capture.capture_window",
        lambda *args: {"data": b"\0" * 16, "size": [2, 2], "rect": [0, 0, 2, 2]},
    )
    frame = CaptureService().screenshot("hwnd:10", capture_mode="auto")
    assert frame.metadata["capture_backend"] == "printwindow"


@pytest.mark.parametrize("method", ["screenshot", "record"])
def test_foreground_true_refuses_instead_of_implicitly_focusing(method):
    grabber = FlexibleGrabber()
    with pytest.raises(ValueError, match="native force_focus"):
        getattr(CaptureService(grabber=grabber), method)(foreground=True)
    assert not grabber.rects


def test_screenshot_define_viewport_and_later_capture_are_scoped():
    manager = CoordinateManager()
    camera = CaptureService(grabber=FlexibleGrabber(), coordinate_manager=manager)
    first = camera.screenshot(region=[10, 20, 110, 70], define_viewport="roi", scope="a")
    later = camera.screenshot("viewport:roi", scope="a")
    assert first.image_bytes == later.image_bytes
    assert later.metadata["physical_transform"]["origin"] == [-90, -30]
    with pytest.raises(CoordinateError, match="unknown"):
        camera.screenshot("viewport:roi", scope="b")


def test_window_capture_viewport_binds_creation_and_tracks_resize(tmp_path, monkeypatch):
    from minano_computer_use import coordinates

    current = {
        "hwnd": 10,
        "pid": 20,
        "process_creation_time": 1234,
        "rect": [0, 0, 100, 100],
        "visible": True,
        "dpi": 96,
    }
    monkeypatch.setattr(coordinates, "window_snapshot", lambda _: dict(current))
    manager = CoordinateManager()
    camera = CaptureService(tmp_path, grabber=FlexibleGrabber(), coordinate_manager=manager)
    camera.screenshot("window:original", region=[25, 25, 75, 75], define_viewport="win")
    current["rect"] = [100, 0, 300, 200]
    frame = camera.screenshot("viewport:win")
    assert frame.metadata["output_size"] == [100, 100]
    assert frame.metadata["physical_transform"]["origin"] == [150, 50]
    current["process_creation_time"] += 1
    with pytest.raises(CoordinateError, match="identity"):
        camera.screenshot("viewport:win")


def test_keep_frames_exports_png_and_timestamp_index_in_quota_group(tmp_path):
    from minano_computer_use.trace import ArtifactStore

    store = ArtifactStore(tmp_path, max_files=1)
    camera = CaptureService(
        grabber=FlexibleGrabber(), artifact_store=store, encoder=unavailable_encoder()
    )
    first = camera.screenshot()
    frame = camera.record(
        seconds=0.05,
        fps=1,
        keep_frames=True,
        region={"x": -90, "y": -30, "w": 31, "h": 11},
        region_space="screen",
    )
    index = json.loads(Path(frame.metadata["frame_index_path"]).read_text())
    assert len(index["frames"]) == frame.metadata["frames"]
    for item in index["frames"]:
        assert type(item["index"]) is int
        assert type(item["timestamp"]) is float
        assert Image.open(item["path"]).size == (31, 11)
    assert frame.metadata["logical_size"] == [31, 11]
    assert frame.metadata["encoded_size"] is None
    assert frame.metadata["physical_transform"] is None
    assert frame.metadata["artifact_retention"]["over_budget"]
    assert frame.metadata["encoder_error"]["code"] == "VIDEO_CODEC_UNAVAILABLE"
    assert camera._coordinate_manager.map_region(
        [1, 1, 2, 2], capture_id=first.metadata["capture_id"]
    ) == {"x": -99, "y": -49, "w": 1, "h": 1}
    assert not store._pending
    store.close()


def test_record_static_warning_and_achieved_fps(tmp_path):
    camera = CaptureService(tmp_path, grabber=FlexibleGrabber(), encoder=unavailable_encoder())
    frame = camera.record(seconds=0.1, fps=30)
    assert frame.metadata["frames"] > 1
    assert frame.metadata["achieved_fps"] > 0
    assert frame.metadata["static_frames"] is True
    assert frame.metadata["warnings"][0]["code"] == "STATIC_FRAMES"
    camera.close()


def test_record_stop_event_and_extra_shots_are_bounded_and_not_clickable(tmp_path):
    stop = Event()
    grabber = FlexibleGrabber(callback=stop.set)
    camera = CaptureService(tmp_path, grabber=grabber, encoder=unavailable_encoder())
    shot = camera.screenshot(remember=False)
    frame = camera.record(seconds=10, fps=1, stop_event=stop, extra_frames=[shot])
    assert frame.metadata["frames"] == 1
    assert frame.metadata["extra_frames"] == 1
    assert frame.metadata["elapsed_seconds"] < 1
    with pytest.raises(CoordinateError):
        camera._coordinate_manager.map_actions([{"x": 1, "y": 1}], "image")
    camera.close()


def test_record_target_geometry_change_never_returns_success(tmp_path, monkeypatch):
    from minano_computer_use import coordinates
    from minano_computer_use.capture import CaptureTargetChanged

    current = {
        "hwnd": 10,
        "pid": 20,
        "process_creation_time": 1234,
        "rect": [0, 0, 100, 100],
        "visible": True,
    }
    monkeypatch.setattr(coordinates, "window_snapshot", lambda _: dict(current))
    grabber = FlexibleGrabber(callback=lambda: current.update(rect=[1, 0, 101, 100]))
    with pytest.raises(CaptureTargetChanged, match="geometry"):
        CaptureService(tmp_path, grabber=grabber).record("hwnd:10", seconds=0.05, fps=1)


def test_record_preserves_inherited_deadline_before_sampling(tmp_path):
    import time

    grabber = FlexibleGrabber()
    with pytest.raises(TimeoutError):
        CaptureService(tmp_path, grabber=grabber).record(deadline=time.monotonic() - 1)
    assert not grabber.rects


def test_encoder_errors_are_returned_as_bounded_backend_diagnostics(tmp_path):
    encoder = FakeVideoEncoder(
        VideoEncodingError("VIDEO_ENCODING_FAILED", "synthetic encoder failure\n" + "x" * 6000)
    )
    camera = CaptureService(tmp_path, grabber=FlexibleGrabber(), encoder=encoder)
    frame = camera.record(seconds=0.05, fps=1)
    assert frame.metadata["mp4_path"] is None
    diagnostic = frame.metadata["encoder_error"]
    assert diagnostic["backend"] == "pyav"
    assert diagnostic["code"] == "VIDEO_ENCODING_FAILED"
    assert diagnostic["message"].startswith("synthetic encoder failure\n")
    assert len(diagnostic["message"]) <= 4096
    assert not camera._artifact_store._pending
    camera.close()


def test_pyav_real_odd_1927x1148_is_padded_and_decodes(tmp_path, monkeypatch):
    import av

    monkeypatch.setenv("PATH", "")
    monkeypatch.setenv("FFMPEG", "invalid-must-not-be-read")

    class OddGrabber:
        monitors = [{"left": 0, "top": 0, "width": 1927, "height": 1148}]

        def grab(self, rect):
            return Image.new("RGB", (1927, 1148), "red")

    camera = CaptureService(tmp_path, grabber=OddGrabber())
    frame = camera.record(seconds=0.05, fps=1)
    assert frame.metadata["mp4_path"], frame.metadata.get("encoder_error")
    assert frame.metadata["logical_size"] == [1927, 1148]
    assert frame.metadata["encoded_size"] == [1928, 1148]
    assert frame.metadata["encoder_backend"] == "pyav"
    with av.open(frame.metadata["mp4_path"]) as container:
        assert container.streams.video[0].codec_context.name == "h264"
        decoded = list(container.decode(video=0))
    assert len(decoded) == frame.metadata["frames"]
    assert all(item.pts is not None for item in decoded)
    image = decoded[0].to_image()
    assert image.size == (1928, 1148)
    # Padding is right/bottom only: original upper-left image is still red.
    assert image.getpixel((0, 0))[0] > 200
    camera.close()


def test_failed_montage_write_discards_reservation(tmp_path: Path, monkeypatch) -> None:
    import pytest

    from minano_computer_use.trace import ArtifactStore

    store = ArtifactStore(tmp_path)
    service = CaptureService(grabber=FakeGrabber(), artifact_store=store)

    def failed_write(*args):
        raise OSError("simulated disk failure")

    monkeypatch.setattr(store, "write_bytes", failed_write)
    with pytest.raises(OSError, match="disk failure"):
        service.record(seconds=0.05, fps=1)
    assert not store._pending
    store.close()


def test_retention_finally_never_masks_recording_result_or_cancel(tmp_path: Path, monkeypatch):
    import threading

    import pytest

    from minano_computer_use.control import OperationCancelled
    from minano_computer_use.trace import ArtifactStore

    store = ArtifactStore(tmp_path)
    camera = CaptureService(grabber=FakeGrabber(), artifact_store=store)
    monkeypatch.setattr(camera, "_write_video", lambda *args, **kwargs: None)

    def fail_retention(_token):
        raise OSError("synthetic ledger flush failure")

    monkeypatch.setattr(store, "end_group", fail_retention)
    frame = camera.record(seconds=0.05, fps=1)
    assert frame.metadata["warnings"][0]["code"] == "ARTIFACT_RETENTION_FAILED"
    # Force another group that exits through a cancellation path.
    store._group_open = False
    cancelled = threading.Event()

    def cancel_video(*args, **kwargs):
        cancelled.set()
        raise OperationCancelled("original cancellation")

    monkeypatch.setattr(camera, "_write_video", cancel_video)
    with pytest.raises(OperationCancelled, match="original cancellation"):
        camera.record(seconds=0.05, fps=1, cancel_event=cancelled)
    store.close()


def test_record_and_trace_group_survive_quota_until_next_call(tmp_path: Path) -> None:
    from minano_computer_use.trace import ArtifactStore, TraceRecorder

    store = ArtifactStore(tmp_path, max_files=1)

    token = store.begin_group()
    frame = CaptureService(
        tmp_path,
        grabber=FakeGrabber(),
        encoder=FakeVideoEncoder(),
        artifact_store=store,
    ).record(seconds=0.05, fps=10, montage_frames=1)
    trace = TraceRecorder(store, enabled=True)
    trace.record("record", {}, {"status": "ok"})
    montage = Path(frame.metadata["montage_path"])
    video = Path(frame.metadata["mp4_path"])
    trace_path = next(store.root.glob("*.jsonl"))
    assert montage.exists() and video.exists() and trace_path.exists()
    assert frame.metadata["artifact_retention"]["over_budget"]
    assert store.prune()["over_budget"]
    store.end_group(token)
    assert montage.exists() and video.exists() and trace_path.exists()
    next_token = store.begin_group()
    assert not montage.exists() or not trace_path.exists()
    store.end_group(next_token)
    trace.close()
    store.close()
