import json

import pytest

from minano_computer_use.output import bound_result, validate_budget


def wire(value):
    text = json.dumps(value, ensure_ascii=False)
    return json.dumps(
        {"content": [{"type": "text", "text": text}], "structuredContent": value, "isError": True},
        ensure_ascii=False,
    )


def test_small_result_unchanged():
    value = {"status": "ok", "operation": "tree", "data": {"nodes": []}}
    assert bound_result(value) is value


def test_escaped_unicode_and_nodes_budget_with_parent_integrity():
    nodes = [{"name": '"你好\\' * 3000, "parent_index": i - 1 if i else None} for i in range(40)]
    value = {"status": "ok", "operation": "tree", "data": {"nodes": nodes}}
    bounded = bound_result(value, 4096, 4096)
    assert bounded["output_truncated"]
    assert len(wire(bounded)) <= 4096
    assert len(wire(bounded).encode("utf-8")) <= 4096
    for index, item in enumerate(bounded.get("data", {}).get("nodes", [])):
        assert item["parent_index"] is None or item["parent_index"] < index
    assert len(nodes[0]["name"]) > 1000


def test_uncertainty_error_code_and_token_never_partially_truncated():
    token = "123456789" * 30
    value = {
        "status": "unknown",
        "operation": "set_value",
        "may_have_acted": True,
        "cancelled": True,
        "error": {"code": "CANCELLED", "message": "x" * 4000},
        "data": {"token": token, "nested": {"large": "x" * 10000}},
    }
    bounded = bound_result(value, 2048, 2048)
    assert bounded["status"] == "unknown"
    assert bounded["may_have_acted"] and bounded["cancelled"]
    assert bounded["error"]["code"] == "CANCELLED"
    assert bounded.get("data", {}).get("token", token) == token
    assert len(wire(bounded).encode()) <= 2048


def test_identity_not_cut_and_late_failure_keeps_original_index():
    binding = {"root_runtime_id": [1, 2, 3], "element_runtime_id": [4, 5, 6]}
    value = {
        "status": "partial",
        "operation": "act",
        "target_binding": binding,
        "data": {
            "requested": 20,
            "attempted": 20,
            "completed": 19,
            "results": [
                {
                    "index": i,
                    "result": {
                        "status": "ok" if i < 19 else "unknown",
                        "operation": "invoke",
                        "data": "x" * 2000,
                        **(
                            {"error": {"code": "TIMEOUT"}, "may_have_acted": True}
                            if i == 19
                            else {}
                        ),
                    },
                }
                for i in range(20)
            ],
        },
    }
    bounded = bound_result(value, 4096, 4096)
    assert bounded["target_binding"] == binding
    actions = bounded["data"]["results"]
    assert actions[0]["index"] == 19
    assert actions[0]["result"]["error"]["code"] == "TIMEOUT"
    assert bounded["data"]["completed"] == 19


def test_nested_play_failure_survives_tiny_budget_and_large_capture_metadata():
    value = {
        "status": "partial",
        "operation": "play",
        "data": {
            "actions": {
                "status": "unknown",
                "data": {
                    "results": [
                        {
                            "index": 7,
                            "result": {
                                "status": "unknown",
                                "operation": "invoke",
                                "error": {"code": "TIMEOUT"},
                                "may_have_acted": True,
                            },
                        }
                    ]
                },
            },
            "capture": {"metadata": {"timestamps": [0.123456] * 300}},
        },
    }
    bounded = bound_result(value, 2048, 2048)
    failure = bounded.get("failure_summary", bounded.get("data", {}).get("first_failure"))
    assert failure["index"] == 7
    assert failure["result"]["may_have_acted"]
    assert failure["result"]["error"]["code"] == "TIMEOUT"
    assert len(wire(bounded).encode()) <= 2048


def test_name_and_automation_id_whole_or_omitted():
    value = {
        "status": "ok",
        "operation": "inspect",
        "data": {"name": "a" * 2400, "automation_id": "b" * 2400},
    }
    bounded = bound_result(value, 2048, 2048)
    data = bounded.get("data", {})
    assert "name" not in data or data["name"] == value["data"]["name"]
    assert "automation_id" not in data or data["automation_id"] == value["data"]["automation_id"]


def test_large_window_listing_retains_a_bounded_prefix():
    value = {
        "status": "ok",
        "operation": "list",
        "data": [
            {"hwnd": index, "pid": 123, "title": "Window " + str(index) + "x" * 100}
            for index in range(300)
        ],
    }
    bounded = bound_result(value)
    assert bounded["status"] == "ok"
    assert 0 < len(bounded["data"]) < 300
    assert bounded["output_truncated"]
    assert len(wire(bounded)) <= 24000


@pytest.mark.parametrize("value", [1, 2047, 1048577, True, 5000.5])
def test_impossible_or_untyped_budget_refuses(value):
    with pytest.raises(ValueError):
        validate_budget(value, 65536)
