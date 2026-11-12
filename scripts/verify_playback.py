"""Synthetic play/frame export: no real desktop/input or external FFmpeg executables."""

import io
import json
import threading
import time
from pathlib import Path

import av
from PIL import Image

from minano_computer_use.capture import CaptureService
from minano_computer_use.contracts import result
from minano_computer_use.coordinates import CoordinateManager
from minano_computer_use.runtime import Runtime


class ControlledSurface:
    monitors = [{"left": 0, "top": 0, "width": 1927, "height": 1148}]

    def __init__(self):
        self.lock = threading.Lock()
        self.level = 0
        self.inputs = []
        self.cancel_event = None

    def grab(self, _rect):
        with self.lock:
            color = (20 + self.level * 60, 40, 100)
        return Image.new("RGB", (1927, 1148), color)

    def execute(self, action, target, key_method="scancode"):
        # Deliberately no Windows/native API. Target is the injected synthetic desktop.
        with self.lock:
            self.level += 1
            self.inputs.append({"action": action["action"], "at": time.monotonic()})
        return result("invoked", action["action"], method="synthetic_input")

    def set_cancel_event(self, event):
        self.cancel_event = event

    def release_all(self):
        return result("ok", "release_all", data={"released": 0})

    def close(self):
        pass


def inspect_video(metadata):
    path = Path(metadata["mp4_path"])
    assert path.is_file() and path.stat().st_size > 0
    assert metadata["encoder_backend"] == "pyav"
    with av.open(str(path)) as container:
        streams = [stream for stream in container.streams if stream.type == "video"]
        assert len(streams) == 1
        stream = streams[0]
        assert stream.codec_context.name == "h264"
        assert [stream.width, stream.height] == [1928, 1148]
        decoded = list(container.decode(stream))
    assert len(decoded) == metadata["frames"]
    assert all(
        item.pts is not None and (item.width, item.height) == (1928, 1148) for item in decoded
    )
    pts = [item.pts for item in decoded]
    assert all(a < b for a, b in zip(pts, pts[1:], strict=False))
    colors = [list(item.to_image().getpixel((0, 0))) for item in decoded]
    assert len(colors) == metadata["frames"]
    return {
        "decoded_verified": {
            "backend": "pyav",
            "codec": stream.codec_context.name,
            "width": stream.width,
            "height": stream.height,
            "frames": len(decoded),
            "pts": pts,
            "padding": "right only; logical upper-left retained",
        },
        "decoded_frame_colors": colors,
        "decoded": True,
    }


def main():
    output = Path(__file__).resolve().parents[1] / ".artifacts"
    surface = ControlledSurface()
    # Surface provenance and input recipient are independent. This inventory is
    # synthetic too; no OS window is resolved or targeted by the verification.
    window = {
        "hwnd": 12345,
        "pid": 54321,
        "process_creation_time": 1,
        "rect": [0, 0, 1927, 1148],
        "dpi": 96,
        "visible": True,
        "minimized": False,
    }

    def resolve_fixture(target):
        if target != "hwnd:12345":
            raise ValueError("Only the synthetic fixture recipient is supported")
        return dict(window)

    coordinates = CoordinateManager(resolver=resolve_fixture)
    runtime = Runtime(output_dir=output, native=surface, coordinate_manager=coordinates)
    camera = CaptureService(
        output,
        grabber=surface,
        artifact_store=runtime._artifacts(),
        coordinate_manager=coordinates,
    )
    runtime._capture = camera
    try:
        baseline = runtime.screenshot("desktop")
        capture_id = (
            baseline["frame"].metadata["capture_id"]
            if isinstance(baseline, dict)
            else (baseline.metadata["capture_id"])
        )
        response = runtime.play(
            target="desktop",
            native=True,
            seconds=0.9,
            fps=10,
            actions=[
                {"at": 0.15, "action": "key", "key": "A"},
                {"at": 0.40, "action": "look", "dx": 10, "dy": 0},
                {"at": 0.65, "action": "key", "key": "D"},
            ],
        )
        assert response["status"] == "ok", response
        frame = response["frame"]
        metadata = frame.metadata
        assert frame.mime_type == "image/jpeg" and len(frame.image_bytes) < 100_000
        assert max(metadata["output_size"]) <= 800
        assert metadata["timestamps"] == sorted(metadata["timestamps"])
        assert metadata["frames"] >= 8 and len(surface.inputs) == 3
        decoded = inspect_video(metadata)
        assert len({tuple(color) for color in decoded["decoded_frame_colors"]}) >= 3
        # Neither the play montage nor recording frames replaces the click context.
        coordinates.map_actions([], "image", capture_id, "hwnd:12345")
        exported = runtime.record("desktop", seconds=0.2, fps=10, keep_frames=True)
        assert exported["status"] == "ok", exported
        exported_metadata = exported["frame"].metadata
        paths = exported_metadata["frame_paths"]
        assert len(paths) == exported_metadata["frames"]
        for path in paths:
            with Image.open(path) as source:
                assert source.format == "PNG" and source.size == (1927, 1148)
        index = json.loads(Path(exported_metadata["frame_index_path"]).read_text())
        assert [entry["timestamp"] for entry in index["frames"]] == exported_metadata["timestamps"]
        script = runtime.play(
            target="desktop",
            script="wait 0.15\nshot\nwait 0.15",
            seconds=1,
            fps=10,
        )
        assert script["status"] == "ok", script
        assert script["frame"].metadata["extra_frames"] == 1
        coordinates.map_actions([], "image", capture_id, "hwnd:12345")
        with Image.open(io.BytesIO(frame.image_bytes)) as montage:
            assert montage.format == "JPEG" and max(montage.size) <= 800
        report = {
            "passed": True,
            "desktop_captured": False,
            "native_input_used": False,
            "fake_inputs": surface.inputs,
            "play_call_id": response["call_id"],
            "play": metadata,
            "video": decoded,
            "frame_export": exported_metadata,
            "script_shot": script["frame"].metadata,
            "capture_context_preserved": True,
            "pairing": "Independent timestamps; no strict action/game-frame claim",
        }
        path = output / "playback-smoke.json"
        path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(
            json.dumps(
                {
                    "report": str(path),
                    "passed": True,
                    "video_frames": metadata["frames"],
                    "montage_size": metadata["output_size"],
                    "image_bytes": len(frame.image_bytes),
                    "source_frame_exports": len(paths),
                    "decoded": True,
                }
            )
        )
    finally:
        runtime.close()


if __name__ == "__main__":
    main()
