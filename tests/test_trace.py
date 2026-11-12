import json
from pathlib import Path

import pytest

from minano_computer_use.trace import ArtifactStore, TraceRecorder


def test_trace_allowlist_no_text_title_or_exception(tmp_path):
    store = ArtifactStore(tmp_path)
    trace = TraceRecorder(store, enabled=True)
    trace.record(
        "act",
        {"text": "SECRET", "selector": "SECRET"},
        {
            "status": "unknown",
            "call_id": "safeid",
            "error": {"code": "TIMEOUT", "message": "SECRET"},
            "data": {
                "value": "SECRET",
                "results": [{"index": 0, "result": {"status": "unknown", "value": "SECRET"}}],
            },
            "observations": {"title": "SECRET"},
        },
    )
    files = list(store.root.glob("*.jsonl"))
    assert len(files) == 1
    content = files[0].read_text()
    assert "SECRET" not in content
    assert json.loads(content)["error_code"] == "TIMEOUT"
    trace.close()
    store.close()


@pytest.mark.parametrize(
    "operation",
    ["process.shell", "process.kill", "system.set_clipboard", "system.get_clipboard"],
)
def test_parity_trace_never_serializes_commands_or_clipboard(tmp_path, operation):
    store = ArtifactStore(tmp_path)
    trace = TraceRecorder(store, enabled=True)
    secret = "PRIVATE-COMMAND-CLIPBOARD-TEXT"
    try:
        trace.record(
            operation,
            {"command": secret, "text": secret, "argv": [secret], "probe": secret},
            {
                "status": "unknown",
                "delivery": "native",
                "error": {"code": "TIMEOUT", "message": secret},
                "data": {"stdout": secret, "stderr": secret, "clipboard": secret},
            },
        )
        records = list(store.root.glob("*.jsonl"))
        assert len(records) == 1
        wire = records[0].read_text()
        assert secret not in wire
        parsed = json.loads(wire)
        assert parsed["operation"] == operation
        assert parsed["error_code"] == "TIMEOUT"
        assert "data" not in parsed and "command" not in parsed
    finally:
        trace.close()
        store.close()


def test_quota_removes_only_registered_owned_files(tmp_path):
    untouched = tmp_path / "important.mp4"
    untouched.write_text("do not delete")
    store = ArtifactStore(tmp_path, max_files=1, max_bytes=4096)
    unknown = store.root / "artifact-not-ours.png"
    unknown.write_bytes(b"unknown")
    active = store.allocate(".png")
    active.write_bytes(b"active")
    first = store.allocate(".png")
    first.write_bytes(b"first")
    store.register(first)
    second = store.allocate(".png")
    second.write_bytes(b"second")
    store.register(second)
    assert not first.exists() and second.exists() and active.exists()
    assert unknown.exists() and untouched.exists()
    store.close()
    assert not active.exists()
    assert second.exists()


def test_disabled_trace_never_writes_and_failures_do_not_raise(tmp_path):
    store = ArtifactStore(tmp_path)
    trace = TraceRecorder(store)
    trace.record("test", {}, {})
    assert not list(store.root.glob("*.jsonl"))
    trace.enabled = True
    store.close()
    trace.record("test", {}, {})
    assert trace.status()["write_failures"] == 1


def test_registered_files_survive_reopen_with_quota(tmp_path):
    store = ArtifactStore(tmp_path)
    for _ in range(2):
        path = store.allocate(".json")
        path.write_bytes(b"data")
        store.register(path)
    store.close()
    reopened = ArtifactStore(tmp_path, max_files=1)
    assert len(list(reopened.root.glob("*.json"))) == 1
    reopened.close()


def test_arbitrary_path_and_single_large_artifact_refused(tmp_path):
    store = ArtifactStore(tmp_path, max_bytes=4096)
    with pytest.raises(ValueError):
        store.register(tmp_path / "not-ours.mp4")
    path = store.allocate(".mp4")
    path.write_bytes(b"x" * 4097)
    with pytest.raises(ValueError, match="quota"):
        store.register(path)
    assert not path.exists()
    store.close()


def test_discard_cannot_remove_registered_or_foreign_file(tmp_path):
    store = ArtifactStore(tmp_path)
    path = store.allocate(".png")
    path.write_bytes(b"partial")
    store.discard(path)
    assert not path.exists()
    path = store.allocate(".json")
    path.write_text("{}")
    store.register(path)
    with pytest.raises(ValueError):
        store.discard(path)
    assert path.exists()
    store.close()


def test_symlink_artifact_rejected_without_following(tmp_path):
    store = ArtifactStore(tmp_path)
    path = store.allocate(".png")
    path.unlink()
    outside = tmp_path / "private.txt"
    outside.write_text("keep")
    try:
        path.symlink_to(outside)
    except OSError:
        store.close()
        pytest.skip("Creating symlinks requires Windows permission")
    with pytest.raises(ValueError, match="symlink"):
        store.register(path)
    assert outside.read_text() == "keep"
    path.unlink()
    store.close()


def test_atomic_writer_does_not_follow_replaced_reservation_symlink(tmp_path):
    store = ArtifactStore(tmp_path)
    path = store.allocate(".png")
    outside = tmp_path / "private.bin"
    outside.write_bytes(b"original")
    path.unlink()
    try:
        path.symlink_to(outside)
    except OSError:
        store.close()
        pytest.skip("Creating symlinks requires Windows permission")
    with pytest.raises(ValueError, match="symlink"):
        store.write_bytes(path, b"attacker")
    assert outside.read_bytes() == b"original"
    store.discard(path)
    assert outside.read_bytes() == b"original"
    store.close()


def test_atomic_writer_replaces_symlink_swapped_after_validation(tmp_path, monkeypatch):
    import minano_computer_use.trace as trace_module

    store = ArtifactStore(tmp_path)
    path = store.allocate(".png")
    outside = tmp_path / "private-late.bin"
    outside.write_bytes(b"original")
    real_replace = trace_module.os.replace
    swapped = False

    def swap_then_replace(src, dst, **kwargs):
        nonlocal swapped
        if Path(dst) == path and not swapped:
            path.unlink()
            try:
                path.symlink_to(outside)
            except OSError:
                pytest.skip("Creating symlinks requires Windows permission")
            swapped = True
        return real_replace(src, dst, **kwargs)

    monkeypatch.setattr(trace_module.os, "replace", swap_then_replace)
    store.write_bytes(path, b"owned-data")
    assert swapped and path.read_bytes() == b"owned-data"
    assert outside.read_bytes() == b"original"
    store.register(path)
    store.close()


def test_active_group_keeps_capture_and_trace_paths_live_over_tiny_quota(tmp_path):
    store = ArtifactStore(tmp_path, max_files=1)
    group = store.begin_group()
    capture = store.allocate(".png")
    store.write_bytes(capture, b"capture")
    store.register(capture)
    trace = store.allocate(".jsonl")
    store.write_text(trace, '{"op":"screenshot"}\n')
    store.register(trace)
    stats = store.prune()
    assert stats["over_budget"]
    assert capture.exists() and trace.exists()
    store.end_group(group)
    assert capture.exists() and trace.exists()
    next_group = store.begin_group()
    assert len(list(store.root.glob("artifact-*"))) == 1
    store.end_group(next_group)
    store.close()


def test_begin_group_prunes_prior_group_but_retains_newest(tmp_path):
    store = ArtifactStore(tmp_path, max_files=1)
    first_group = store.begin_group()
    first = store.allocate(".png")
    store.write_bytes(first, b"first")
    store.register(first)
    store.end_group(first_group)
    second_group = store.begin_group()
    second = store.allocate(".png")
    store.write_bytes(second, b"second")
    store.register(second)
    store.end_group(second_group)
    assert not first.exists()
    assert second.exists()
    store.close()


def test_root_directory_is_pinned_against_replacement(tmp_path):
    import os

    store = ArtifactStore(tmp_path)
    moved = store.root.with_name("moved-artifacts")
    if os.name == "nt":
        try:
            store.root.rename(moved)
        except OSError:
            path = store.allocate(".png")
            store.write_bytes(path, b"safe")
            store.register(path)
        else:
            store.root.mkdir()
            with pytest.raises(ValueError, match="reparse points"):
                store.allocate(".png")
            store.root.rmdir()
            moved.rename(store.root)
        store.close()
    else:
        store.root.rename(moved)
        store.root.mkdir()
        with pytest.raises(ValueError, match="identity changed"):
            store.allocate(".png")
        store.close()


def test_ledger_swapped_to_symlink_is_replaced_without_external_write(tmp_path):
    store = ArtifactStore(tmp_path)
    ledger = store.root / "ownership.sqlite3"
    outside = tmp_path / "private-ledger.txt"
    outside.write_text("keep external bytes")
    ledger.unlink()
    try:
        ledger.symlink_to(outside)
    except OSError:
        store.close()
        pytest.skip("Creating symlinks requires Windows permission")
    path = store.allocate(".png")
    store.write_bytes(path, b"pixels")
    store.register(path)
    assert outside.read_text() == "keep external bytes"
    assert not ledger.is_symlink()
    store.close()


def test_runtime_record_and_trace_share_return_group(tmp_path, monkeypatch):
    from PIL import Image

    from minano_computer_use.capture import CaptureService
    from minano_computer_use.runtime import Runtime

    class Grabber:
        monitors = [{"left": 0, "top": 0, "width": 4, "height": 4}]

        def grab(self, _rect):
            return Image.new("RGB", (4, 4), (30, 40, 50))

    runtime = Runtime(output_dir=tmp_path, trace_enabled=True, artifact_max_files=1)
    store = runtime._artifacts()
    camera = CaptureService(artifact_store=store, grabber=Grabber())

    def fake_video(_images, _fps, *, cancel_event=None, deadline=None):
        path = store.allocate(".mp4")
        store.write_bytes(path, b"synthetic-test-video")
        store.register(path)
        return path

    monkeypatch.setattr(camera, "_write_video", fake_video)
    runtime._capture = camera
    try:
        result = runtime.record(seconds=0.05, fps=1)
        assert result["status"] == "ok", result
        metadata = result["frame"].metadata
        assert Path(metadata["montage_path"]).exists()
        assert Path(metadata["mp4_path"]).exists()
        assert len(list(store.root.glob("*.jsonl"))) == 1
        assert store.prune()["over_budget"]
        assert not store.group_open
    finally:
        runtime.close()
