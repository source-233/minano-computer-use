import base64
import io
import os
import threading

from PIL import Image

from minano_computer_use.ocr import SCRIPT, recognize


class Runner:
    def __init__(self, payload):
        self.payload, self.calls = payload, []

    def run(self, argv, **kwargs):
        self.calls.append((argv, kwargs))
        return {"exit_code": 0, "stdout": self.payload, "stdout_truncated": False}


def png():
    output = io.BytesIO()
    Image.new("RGB", (30, 20), "white").save(output, format="PNG")
    return output.getvalue()


def test_fixed_script_and_separate_data_arguments(monkeypatch):
    monkeypatch.setattr("minano_computer_use.ocr.shutil.which", lambda _: "powershell.exe")
    runner = Runner('{"text":"中文","language":"zh-CN"}')
    language = "x';throw 'never'"
    outcome = recognize(image_bytes=png(), language=language, runner=runner)
    assert outcome["status"] == "ok"
    argv = runner.calls[0][0]
    assert "-File" in argv and "-Command" not in argv
    assert base64.b64decode(argv[-1]).decode() == language
    assert language not in SCRIPT
    assert outcome["data"]["text"] == "中文"


def test_language_package_refusal(monkeypatch):
    monkeypatch.setattr("minano_computer_use.ocr.shutil.which", lambda _: "powershell.exe")
    outcome = recognize(
        image_bytes=png(), language="zz-ZZ", runner=Runner('{"error":"OCR_LANGUAGE_UNAVAILABLE"}')
    )
    assert outcome["status"] == "refused"
    assert outcome["error"]["code"] == "OCR_LANGUAGE_UNAVAILABLE"


def test_cancel_before_capture_or_launch():
    event = threading.Event()
    event.set()
    runner = Runner("{}")
    outcome = recognize(image_bytes=png(), cancel_event=event, runner=runner)
    assert outcome["error"]["code"] == "CANCELLED"
    assert not runner.calls


def test_real_owned_winrt_fixture_when_available():
    if os.name != "nt":
        return
    image = Image.new("RGB", (900, 180), "white")
    from PIL import ImageDraw, ImageFont

    draw = ImageDraw.Draw(image)
    draw.text(
        (25, 35),
        "MINANO OCR 12345",
        fill="black",
        font=ImageFont.truetype("C:/Windows/Fonts/arial.ttf", 54),
    )
    output = io.BytesIO()
    image.save(output, format="PNG")
    outcome = recognize(image_bytes=output.getvalue(), timeout=20)
    if outcome["status"] == "ok":
        assert "MINANO OCR 12345" in outcome["data"]["text"]
    else:
        assert outcome["error"]["code"] in {"OCR_UNAVAILABLE", "OCR_LANGUAGE_UNAVAILABLE"}
