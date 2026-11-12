"""Synthetic, in-process PyAV proof; does not change the production video backend."""

from __future__ import annotations

import argparse
import importlib
import importlib.util
import json
import os
import platform
import sys
import tempfile
from fractions import Fraction
from pathlib import Path

from PIL import Image

from minano_computer_use.trace import ArtifactStore


def load_av():
    """Load lazily so --help and isolated test collection do not need PyAV."""
    try:
        return importlib.import_module("av")
    except ModuleNotFoundError as exc:
        if exc.name != "av":
            raise
        raise ModuleNotFoundError(
            "PyAV is unavailable; run uv sync --python 3.13, then: "
            "uv run python scripts/verify_pyav.py",
            name="av",
        ) from exc


def _parameters(width, height, frame_count, fps):
    if any(type(value) is not int for value in (width, height, frame_count, fps)):
        raise ValueError("Dimensions, frame_count and fps must be integers")
    if width < 2 or height < 2 or frame_count < 4 or fps < 1:
        raise ValueError("Dimensions >= 2, frame_count >= 4 and fps >= 1 required")


def _red(index, frame_count):
    return 20 + 200 * index // (frame_count - 1)


def decode_case(av, source, width, height, frame_count=10, fps=10):
    """Read a seekable anonymous file and reject plausible but false readback."""
    _parameters(width, height, frame_count, fps)
    source.seek(0)
    encoded_size = (width + width % 2, height + height % 2)
    with av.open(source, mode="r", format="mp4") as container:
        if len(container.streams.video) != 1:
            raise ValueError("Expected exactly one video stream")
        stream = container.streams.video[0]
        codec = stream.codec_context.name
        rate = stream.average_rate
        if codec != "h264" or (stream.width, stream.height) != encoded_size:
            raise ValueError("Unexpected decoded codec or geometry")
        if rate != Fraction(fps, 1):
            raise ValueError("Unexpected decoded frame rate")
        pts, times, samples = [], [], []
        for index, frame in enumerate(container.decode(stream)):
            if index >= frame_count or frame.pts is None or frame.time_base is None:
                raise ValueError("Unexpected decoded frame count or missing timestamp")
            if (frame.width, frame.height) != encoded_size or frame.format.name != "yuv420p":
                raise ValueError("Unexpected decoded frame geometry or pixel format")
            timestamp = frame.pts * frame.time_base
            if timestamp != Fraction(index, fps):
                raise ValueError("Decoded timestamps do not match explicit input PTS")
            with frame.to_image() as image:
                colors = [image.getpixel((x, height // 2)) for x in (0, width // 2, width - 2)]
            expected = (_red(index, frame_count), 70, 110)
            if any(
                abs(a - b) > 8 for color in colors for a, b in zip(color, expected, strict=True)
            ):
                raise ValueError("Decoded synthetic color does not match expected content")
            pts.append(frame.pts)
            times.append(float(timestamp))
            samples.append(list(colors[1]))
        if len(pts) != frame_count or any(a >= b for a, b in zip(pts, pts[1:], strict=False)):
            raise ValueError("Missing frames or non-monotonic decoded PTS")
        reds = [color[0] for color in samples]
        if len(set(reds)) < 4 or any(a >= b for a, b in zip(reds, reds[1:], strict=False)):
            raise ValueError("Readback lacks distinct, monotonically increasing colors")
        duration = float(stream.duration * stream.time_base) if stream.duration else None
        if duration is None or abs(duration - frame_count / fps) > 1 / fps:
            raise ValueError("Unexpected decoded duration")
        return {
            "codec": codec,
            "pixel_format": "yuv420p",
            "logical_size": [width, height],
            "encoded_size": list(encoded_size),
            "padding_right_bottom": [width % 2, height % 2],
            "frame_count": len(pts),
            "frame_rate": str(rate),
            "time_base": str(stream.time_base),
            "pts": pts,
            "timestamps_seconds": times,
            "duration_seconds": duration,
            "sample_colors_rgb": samples,
        }


def encode_case(av, width, height, frame_count=10, fps=10):
    """Encode/flush/close, then independently decode; no filename or NumPy bridge."""
    _parameters(width, height, frame_count, fps)
    encoded_size = (width + width % 2, height + height % 2)
    with tempfile.TemporaryFile(mode="w+b") as temporary:
        with av.open(temporary, mode="w", format="mp4") as container:
            stream = container.add_stream("libx264", rate=fps)
            stream.width, stream.height = encoded_size
            stream.pix_fmt = "yuv420p"
            stream.time_base = stream.codec_context.time_base = Fraction(1, fps)
            stream.options = {"crf": "18", "preset": "fast"}
            for index in range(frame_count):
                with Image.new("RGB", encoded_size, (0, 0, 0)) as padded:
                    with Image.new(
                        "RGB", (width, height), (_red(index, frame_count), 70, 110)
                    ) as image:
                        padded.paste(image, (0, 0))
                    frame = av.VideoFrame.from_image(padded)
                frame.pts, frame.time_base = index, Fraction(1, fps)
                for packet in stream.encode(frame):
                    container.mux(packet)
            for packet in stream.encode():
                container.mux(packet)
        metadata = decode_case(av, temporary, width, height, frame_count, fps)
        temporary.seek(0)
        payload = temporary.read()
    metadata["byte_count"] = len(payload)
    metadata["video_artifact_path"] = None
    return payload, metadata


def _save_video(store, payload):
    path = store.allocate(".mp4")
    try:
        store.write_bytes(path, payload)
        store.register(path)
    except Exception:
        # close() also reclaims reservations if registration itself failed.
        try:
            store.discard(path)
        except ValueError:
            pass
        raise
    return str(path)


def _write_report(directory, report):
    """Atomic fixed-name summary; never open a possibly linked report target."""
    path = directory / "pyav-smoke.json"
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="wb", dir=directory, delete=False) as output:
            temporary = Path(output.name)
            output.write((json.dumps(report, indent=2, ensure_ascii=True) + "\n").encode())
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=Path(".artifacts"))
    parser.add_argument("--no-videos", action="store_true", help="Keep JSON evidence only")
    args = parser.parse_args(argv)
    report = {
        "status": "failed",
        "scope": "synthetic PyAV encode/decode only; not production or cancellation proof",
        "python_version": platform.python_version(),
        "python_implementation": platform.python_implementation(),
        "numpy_available": importlib.util.find_spec("numpy") is not None,
        "cases": [],
    }
    store, group, phase = None, None, "artifact_store"
    try:
        store = ArtifactStore(args.output_dir)
        group = store.begin_group()
        phase = "missing_pyav"
        av = load_av()
        report.update(av_version=av.__version__, library_versions=av.library_versions)
        phase = "missing_codec"
        av.Codec("libx264", "w")
        for width, height in ((64, 48), (1927, 1148)):
            phase = "encode_decode"
            payload, case = encode_case(av, width, height)
            if not args.no_videos:
                phase = "artifact_write"
                case["video_artifact_path"] = _save_video(store, payload)
            report["cases"].append(case)
        report["status"] = "passed"
    except Exception as exc:
        report["error"] = {
            "code": phase.upper(),
            "type": type(exc).__name__,
            "message": str(exc),
            "errno": getattr(exc, "errno", None),
        }
    finally:
        try:
            if store is not None:
                if group is not None:
                    store.end_group(group)
                _write_report(store.root.parent, report)
            else:
                print(json.dumps(report), file=sys.stderr)
        except Exception as exc:
            report["status"] = "failed"
            print(f"PyAV report write failed: {type(exc).__name__}", file=sys.stderr)
        finally:
            if store is not None:
                store.close()
    print(f"PyAV verification: {report['status']}")
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
