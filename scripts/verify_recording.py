"""Synthetic MP4 integration: no desktop reads/input or external FFmpeg executables."""

import json
from pathlib import Path

import av
from PIL import Image

from minano_computer_use.capture import CaptureService


class SyntheticGrabber:
    monitors = [{"left": 0, "top": 0, "width": 64, "height": 48}]

    def __init__(self, width=64, height=48) -> None:
        self.index = 0
        self.width, self.height = width, height
        self.monitors = [{"left": 0, "top": 0, "width": width, "height": height}]

    def grab(self, _rect: dict) -> Image.Image:
        self.index += 1
        return Image.new("RGB", (self.width, self.height), (self.index * 30 % 255, 40, 60))


def verify_size(output, width, height):
    service = CaptureService(output, grabber=SyntheticGrabber(width, height))
    try:
        frame = service.record(seconds=0.2, fps=10, montage_frames=6)
        assert frame.image_bytes.startswith(b"\xff\xd8")
        assert frame.mime_type == "image/jpeg"
        assert max(frame.metadata["output_size"]) <= 800
        assert frame.metadata["capture_backend"] == "injected"
        assert frame.metadata["encoder_backend"] == "pyav"
        assert frame.metadata["montage_timestamps"] == frame.metadata["timestamps"]
        video = Path(frame.metadata["mp4_path"])
        assert video.exists() and video.stat().st_size > 0
        with av.open(str(video)) as container:
            streams = [stream for stream in container.streams if stream.type == "video"]
            assert len(streams) == 1
            stream = streams[0]
            decoded = list(container.decode(stream))
        assert stream.codec_context.name == "h264"
        assert stream.width == width + width % 2
        assert stream.height == height + height % 2
        assert len(decoded) == frame.metadata["frames"]
        assert all(item.pts is not None for item in decoded)
        assert all(decoded[i].pts <= decoded[i + 1].pts for i in range(len(decoded) - 1))
        assert [item.width for item in decoded] == [width + width % 2] * len(decoded)
        assert [item.height for item in decoded] == [height + height % 2] * len(decoded)
        colors = [list(item.to_image().getpixel((0, 0))) for item in decoded]
        assert all(
            abs(color[0] - ((index + 1) * 30 % 255)) <= 8
            and abs(color[1] - 40) <= 8
            and abs(color[2] - 60) <= 8
            for index, color in enumerate(colors)
        )
        return {
            "logical_size": [width, height],
            "metadata": frame.metadata,
            "decoded_verified": {
                "backend": "pyav",
                "codec": stream.codec_context.name,
                "width": stream.width,
                "height": stream.height,
                "frames": len(decoded),
                "pts": [item.pts for item in decoded],
                "upper_left_colors": colors,
                "padding": "right/bottom only; logical upper-left retained",
            },
            "decoded": True,
        }
    finally:
        service.close()


def main() -> None:
    output = Path(__file__).resolve().parents[1] / ".artifacts"
    cases = [
        verify_size(output, 64, 48),
        verify_size(output, 1927, 1148),
    ]
    report = {"passed": True, "desktop_captured": False, "native_input_used": False, "cases": cases}
    report_path = output / "recording-smoke.json"
    report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps({"report": str(report_path), "cases": cases, "passed": True}))


if __name__ == "__main__":
    main()
