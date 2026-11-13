import copy
import json
import shutil
import subprocess

import pytest

from minano_computer_use.server import _response
from minano_computer_use.wire import public_result

TIME = 134387468188212291


def test_recursive_public_time_copy_keeps_internal_numbers_exact():
    original = {
        "status": "ok",
        "process_creation_time": TIME,
        "data": [
            {"target_binding": {"process_creation_time": TIME, "pid": 123, "hwnd": 456}},
            {"process_creation_time": None},
            {"process_creation_time": str(TIME)},
        ],
        "unrelated": TIME,
    }
    saved = copy.deepcopy(original)
    wire = public_result(original)
    assert wire["process_creation_time"] == str(TIME)
    assert wire["data"][0]["target_binding"] == {
        "process_creation_time": str(TIME),
        "pid": 123,
        "hwnd": 456,
    }
    assert wire["data"][1]["process_creation_time"] is None
    assert wire["data"][2]["process_creation_time"] == str(TIME)
    assert wire["unrelated"] == TIME
    assert original == saved


@pytest.mark.parametrize(
    "value", [float(TIME), 0, -1, True, "001", "1.5", "NaN", "秘密", "9" * 10000, 2**64, [], {}]
)
def test_invalid_time_is_bounded_unknown_never_rounded(value):
    wire = public_result({"process_creation_time": value})
    assert wire == {
        "process_creation_time": None,
        "process_creation_time_unavailable": "INVALID_IDENTITY_TIME",
    }


def test_server_text_structured_budget_and_javascript_roundtrip():
    result = _response(
        {
            "status": "ok",
            "data": {
                "process_creation_time": TIME,
                "nested": [{"process_creation_time": None}, {"process_creation_time": TIME}],
            },
        },
        "window",
    )
    parsed = json.loads(result.content[0].text)
    assert parsed == result.structuredContent
    assert parsed["data"]["process_creation_time"] == str(TIME)
    javascript = shutil.which("bun") or shutil.which("node")
    if javascript is None:
        pytest.skip("Bun/Node.js unavailable for JS parse/stringify evidence")
    code = (
        "const fs=require('fs');"
        "process.stdout.write(JSON.stringify(JSON.parse(fs.readFileSync(0,'utf8'))))"
    )
    completed = subprocess.run(
        [javascript, "-e", code],
        input=result.content[0].text,
        text=True,
        capture_output=True,
        check=True,
        timeout=20,
    )
    assert json.loads(completed.stdout) == parsed


def test_budget_does_not_prefix_clip_time_or_binding():
    binding = {"process_creation_time": TIME, "root_runtime_id": [1, 2, 3]}
    result = _response(
        {
            "status": "unknown",
            "may_have_acted": True,
            "target_binding": binding,
            "data": {"process_creation_time": TIME, "large": ["x" * 5000] * 300},
        },
        "act",
        2048,
        2048,
    ).structuredContent
    assert result["may_have_acted"]
    assert "target_binding" not in result or result["target_binding"] == public_result(binding)
    assert result.get("data", {}).get("process_creation_time", str(TIME)) == str(TIME)
