import importlib.util
import io
import json
import subprocess
from fractions import Fraction
from pathlib import Path
from types import SimpleNamespace

import pytest

from minano_computer_use.trace import ArtifactStore

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "verify_pyav.py"
SPEC = importlib.util.spec_from_file_location("verify_pyav", SCRIPT)
verification = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(verification)


@pytest.fixture
def av():
    return pytest.importorskip("av")


def test_help_does_not_import_av(monkeypatch, capsys):
    def forbidden():
        raise AssertionError("--help must not load av")

    monkeypatch.setattr(verification, "load_av", forbidden)
    with pytest.raises(SystemExit) as exc:
        verification.main(["--help"])
    assert exc.value.code == 0
    assert "--no-videos" in capsys.readouterr().out


def test_missing_av_has_actionable_failure_report(tmp_path, monkeypatch):
    def missing(name):
        raise ModuleNotFoundError("No module named av", name=name)

    monkeypatch.setattr(verification.importlib, "import_module", missing)
    assert verification.main(["--output-dir", str(tmp_path)]) == 1
    report = json.loads((tmp_path / "pyav-smoke.json").read_text())
    assert report["status"] == "failed"
    assert report["error"]["code"] == "MISSING_PYAV"
    assert report["error"]["type"] == "ModuleNotFoundError"
    assert "uv run python scripts/verify_pyav.py" in report["error"]["message"]
    assert report["cases"] == []
    assert not list((tmp_path / "minano-artifacts").glob("artifact-*"))


@pytest.mark.parametrize("size", [(64, 48), (65, 49)])
def test_small_cases_round_trip_without_external_process(av, size, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("No external executable, including FFmpeg, is allowed")

    monkeypatch.setattr(subprocess, "Popen", forbidden)
    payload, case = verification.encode_case(av, *size)
    assert case["frame_count"] == 10
    assert Fraction(case["frame_rate"]) == 10
    assert case["logical_size"] == list(size)
    assert case["encoded_size"] == [value + value % 2 for value in size]
    assert case["timestamps_seconds"] == pytest.approx([i / 10 for i in range(10)])
    assert all(a < b for a, b in zip(case["pts"], case["pts"][1:], strict=False))
    assert case["duration_seconds"] == pytest.approx(1)
    assert case["byte_count"] == len(payload) > 0
    assert case["video_artifact_path"] is None


@pytest.mark.parametrize(
    "parameters",
    [
        (0, 48, 10, 10),
        (64, -1, 10, 10),
        (64, 48, 1, 10),
        (64, 48, 10, 0),
        (True, 48, 10, 10),
        (64.0, 48, 10, 10),
    ],
)
def test_invalid_parameters_fail_before_av_use(parameters):
    with pytest.raises(ValueError):
        verification.encode_case(None, *parameters)


def test_corrupt_input_fails_decode(av):
    with pytest.raises(av.error.FFmpegError):
        verification.decode_case(av, io.BytesIO(b"not an mp4"), 64, 48)


def test_false_readback_is_rejected(av):
    payload, _ = verification.encode_case(av, 64, 48)
    with pytest.raises(ValueError, match="frame rate"):
        verification.decode_case(av, io.BytesIO(payload), 64, 48, fps=20)


def test_padding_does_not_resize_or_crop_logical_content(av):
    observed = []

    def inspect(image):
        index = len(observed)
        color = (verification._red(index, 10), 70, 110)
        assert image.size == (66, 50)
        for point in ((0, 0), (64, 0), (0, 48), (64, 48)):
            assert image.getpixel(point) == color
        assert image.getpixel((65, 48)) == (0, 0, 0)
        assert image.getpixel((64, 49)) == (0, 0, 0)
        observed.append(index)
        return av.VideoFrame.from_image(image)

    proxy = SimpleNamespace(open=av.open, VideoFrame=SimpleNamespace(from_image=inspect))
    verification.encode_case(proxy, 65, 49)
    assert len(observed) == 10


def test_encode_failure_closes_anonymous_file(av, monkeypatch):
    real_temporary = verification.tempfile.TemporaryFile
    opened = []

    def track(*args, **kwargs):
        temporary = real_temporary(*args, **kwargs)
        opened.append(temporary)
        return temporary

    def fail(*args, **kwargs):
        raise RuntimeError("synthetic encoder failure")

    monkeypatch.setattr(verification.tempfile, "TemporaryFile", track)
    monkeypatch.setattr(av, "open", fail)
    with pytest.raises(RuntimeError, match="synthetic encoder failure"):
        verification.encode_case(av, 64, 48)
    assert len(opened) == 1 and opened[0].closed


def test_safe_artifact_write_failure_discards_reservation(tmp_path, monkeypatch):
    store = ArtifactStore(tmp_path)
    group = store.begin_group()

    def fail(*args):
        raise OSError("synthetic write failure")

    monkeypatch.setattr(store, "write_bytes", fail)
    try:
        with pytest.raises(OSError, match="synthetic write failure"):
            verification._save_video(store, b"data")
        assert not list(store.root.glob("artifact-*"))
    finally:
        store.end_group(group)
        store.close()


def test_script_real_even_and_odd_artifacts(av, tmp_path, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("Verification must not launch an external process")

    monkeypatch.setattr(subprocess, "Popen", forbidden)
    assert verification.main(["--output-dir", str(tmp_path)]) == 0
    report = json.loads((tmp_path / "pyav-smoke.json").read_text())
    assert report["status"] == "passed"
    assert report["av_version"] == av.__version__
    assert report["library_versions"]
    assert isinstance(report["numpy_available"], bool)
    assert [case["logical_size"] for case in report["cases"]] == [[64, 48], [1927, 1148]]
    assert [case["encoded_size"] for case in report["cases"]] == [[64, 48], [1928, 1148]]
    for case in report["cases"]:
        path = Path(case["video_artifact_path"])
        assert path.parent == tmp_path / "minano-artifacts"
        assert path.name.startswith("artifact-") and path.suffix == ".mp4"
        assert path.stat().st_size == case["byte_count"]
        with path.open("rb") as source:
            decoded = verification.decode_case(av, source, *case["logical_size"])
        assert decoded["frame_count"] == 10
        assert decoded["pts"] == case["pts"]


def test_encode_failure_report_is_not_passed(av, tmp_path, monkeypatch):
    def fail(*args):
        raise RuntimeError("synthetic encode failure")

    monkeypatch.setattr(verification, "encode_case", fail)
    assert verification.main(["--output-dir", str(tmp_path)]) == 1
    report = json.loads((tmp_path / "pyav-smoke.json").read_text())
    assert report["status"] == "failed"
    assert report["error"]["code"] == "ENCODE_DECODE"
    assert report["error"]["type"] == "RuntimeError"
    assert report["cases"] == []


def test_missing_codec_produces_failure_report(av, tmp_path, monkeypatch):
    def fail(*args):
        raise LookupError("libx264 unavailable")

    monkeypatch.setattr(av, "Codec", fail)
    assert verification.main(["--output-dir", str(tmp_path)]) == 1
    report = json.loads((tmp_path / "pyav-smoke.json").read_text())
    assert report["status"] == "failed"
    assert report["error"]["code"] == "MISSING_CODEC"
    assert report["error"]["type"] == "LookupError"


def test_no_videos_keeps_report_without_video_artifacts(av, tmp_path, monkeypatch):
    real_encode = verification.encode_case

    def small_case(module, width, height):
        return real_encode(module, 64, 48)

    monkeypatch.setattr(verification, "encode_case", small_case)
    assert verification.main(["--output-dir", str(tmp_path), "--no-videos"]) == 0
    report = json.loads((tmp_path / "pyav-smoke.json").read_text())
    assert report["status"] == "passed"
    assert all(case["video_artifact_path"] is None for case in report["cases"])
    assert not list((tmp_path / "minano-artifacts").glob("artifact-*"))
