"""Real window capture of our owned fixture only; no activation or native input."""

import argparse
import ctypes
import io
import json
import queue
import subprocess
import sys
import threading
from ctypes import wintypes as w
from pathlib import Path

from PIL import Image

from minano_computer_use.capture import CaptureService


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--backend", choices=["printwindow", "wgc", "both"], default="both")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    artifacts = root / ".artifacts"
    artifacts.mkdir(exist_ok=True)
    user = ctypes.WinDLL("user32", use_last_error=True)
    user.PostMessageW.argtypes = [w.HWND, w.UINT, w.WPARAM, w.LPARAM]
    user.PostMessageW.restype = w.BOOL
    child = subprocess.Popen(
        [sys.executable, str(root / "tests/windows_fixture.py")],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    response = queue.Queue(maxsize=1)

    def read_ready():
        response.put(child.stdout.readline())

    reader = threading.Thread(target=read_ready)
    reader.start()
    report = {"native_input_used": False, "own_fixture_only": True, "captures": {}}
    service = CaptureService(artifacts)
    hwnd = 0
    try:
        info = json.loads(response.get(timeout=5))
        hwnd = info["hwnd"]
        target = f"hwnd:{hwnd:#x}"
        modes = ("printwindow", "wgc") if args.backend == "both" else (args.backend,)
        for mode in modes:
            frame = service.screenshot(target=target, capture_mode=mode, timeout=8)
            extension = ".jpg" if frame.mime_type == "image/jpeg" else ".png"
            path = artifacts / f"window-capture-{mode}{extension}"
            path.write_bytes(frame.image_bytes)
            with Image.open(path) as image:
                assert image.width > 100 and image.height > 100
                extrema = image.convert("RGB").getextrema()
                assert any(high > low for low, high in extrema), extrema
            assert frame.metadata["capture_backend"] == mode, frame.metadata
            report["captures"][mode] = {"path": str(path), "metadata": frame.metadata}
        cropped = service.screenshot(
            target=target,
            capture_mode=modes[0],
            region=[10, 10, 110, 60],
            max_dim=100,
            timeout=5,
        )
        with Image.open(io.BytesIO(cropped.image_bytes)) as image:
            assert image.size == (100, 50), image.size
        report["roi"] = cropped.metadata
        report["passed"] = True
    finally:
        service.close()
        if hwnd:
            user.PostMessageW(hwnd, 0x0010, 0, 0)
        try:
            child.wait(timeout=3)
        except subprocess.TimeoutExpired:
            child.kill()
            child.wait(timeout=3)
        reader.join(timeout=3)
        report["fixture_exit_code"] = child.returncode
        report["fixture_stderr"] = child.stderr.read()
        report["requested_backend"] = args.backend
        output = artifacts / f"window-capture-{args.backend}-smoke.json"
        output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps({"report": str(output), "passed": report.get("passed", False)}))


if __name__ == "__main__":
    main()
