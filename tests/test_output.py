import copy
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
    if failure is None:
        failure = bounded["data"]["actions"]["data"]["results"][0]
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


def state_result(node_index=24):
    return {
        "status": "ok",
        "operation": "state",
        "method": "uia",
        "data": {
            "nodes": [
                {"name": "n" * 500, "parent_index": index - 1 if index else None}
                for index in range(25)
            ],
            "focus": {"status": "available", "node_index": node_index},
            "selection": {
                "status": "available",
                "node_index": node_index,
                "ranges": ["selection plaintext"],
                "element": {"name": "selected element"},
                "path": "selected path",
            },
        },
    }


@pytest.mark.parametrize("chars,byte_limit", [(24000, 65536), (65536, 24000), (2048, 2048)])
def test_state_budget_invalidates_omitted_node_references(chars, byte_limit):
    value = state_result()
    original = copy.deepcopy(value)
    bounded = bound_result(value, chars, byte_limit)
    assert bounded["status"] == "ok"
    assert bounded["method"] == "uia"
    assert bounded["output_truncated"]
    assert 0 < len(bounded["data"]["nodes"]) < 25
    for field in ("focus", "selection"):
        assert bounded["data"][field] == {
            "status": "unavailable",
            "reason": "REFERENCED_NODE_OMITTED",
        }
    assert "selection plaintext" not in wire(bounded)
    assert len(wire(bounded)) + 32 <= chars
    assert len(wire(bounded).encode("utf-8")) + 32 <= byte_limit
    assert value == original


def test_state_budget_preserves_retained_node_references():
    value = state_result(0)
    bounded = bound_result(value)
    assert len(bounded["data"]["nodes"]) < 25
    for field in ("focus", "selection"):
        assert bounded["data"][field] == value["data"][field]
    assert bound_result(bounded) is bounded


@pytest.mark.parametrize("wrapper", ["uia", "after"])
def test_nested_state_references_rechecked_by_final_budget(wrapper):
    worker = bound_result(state_result(), 65536, 65536)
    assert worker["data"]["selection"]["status"] == "available"
    value = {"status": "partial", "method": "compose", "may_have_acted": True, "data": {}}
    value["data"][wrapper] = worker["data"] if wrapper == "uia" else worker
    original = copy.deepcopy(value)
    bounded = bound_result(value)
    state = bounded["data"][wrapper]
    if wrapper == "after":
        state = state["data"]
    assert len(state["nodes"]) < 25
    assert state["focus"]["status"] == state["selection"]["status"] == "unavailable"
    assert "ranges" not in state["selection"]
    assert bounded["status"] == "partial"
    assert bounded["method"] == "compose"
    assert bounded["may_have_acted"] is True
    assert len(wire(bounded)) + 32 <= 24000
    assert value == original


@pytest.mark.parametrize("node_index", [-1, 1, True, False, "0", None])
def test_invalid_state_reference_sanitized_without_mutating_input(node_index):
    value = state_result(node_index)
    value["data"]["nodes"] = [{"name": "retained"}]
    original = copy.deepcopy(value)
    bounded = bound_result(value)
    assert bounded is not value
    for field in ("focus", "selection"):
        assert bounded["data"][field] == {
            "status": "unavailable",
            "reason": "REFERENCED_NODE_OMITTED",
        }
    assert value == original


def test_unindexed_and_unrelated_statuses_unchanged():
    value = {
        "status": "ok",
        "data": {
            "nodes": [],
            "focus": {"status": "unavailable", "reason": "No scoped focus"},
            "selection": {"status": "redacted", "reason": "Password"},
            "actions": [{"status": "ok", "node_index": 24}],
            "target_binding": {"nodes": [], "focus": {"node_index": 24}},
        },
    }
    assert bound_result(value) is value


@pytest.mark.parametrize("value", [1, 2047, 1048577, True, 5000.5])
def test_impossible_or_untyped_budget_refuses(value):
    with pytest.raises(ValueError):
        validate_budget(value, 65536)


@pytest.mark.parametrize("wrapper", [False, True])
def test_failure_predicate_survives_worker_and_server_budget(wrapper):
    value = {
        "status": "unknown",
        "operation": "invoke",
        "may_have_acted": True,
        "error": {
            "code": "STALE_TARGET",
            "message": "x" * 10000,
            "details": {
                "stage": "focus_before_read",
                "node_index": 12,
                "predicate": "ancestry_unavailable",
                "error_type": "COMError",
                "value": "private",
            },
        },
        "target_binding": {"process_creation_time": 134387468188212291},
        "data": {"large": ["x" * 10000] * 100},
    }
    worker = bound_result(value, 4096, 4096)
    if wrapper:
        worker = {
            "status": "partial",
            "operation": "act",
            "may_have_acted": True,
            "data": {
                "results": [{"index": 3, "result": worker}],
                "after": {"metadata": ["x" * 1000] * 100},
            },
        }
    bounded = bound_result(worker, 2048, 2048)
    if wrapper:
        failure = bounded.get("data", {}).get("first_failure", bounded.get("failure_summary"))
        if failure is None:
            failure = bounded["data"]["results"][0]
        bounded_error = failure["result"]["error"]
    else:
        bounded_error = bounded["error"]
    assert bounded["may_have_acted"]
    assert bounded_error["code"] == "STALE_TARGET"
    assert bounded_error["details"]["predicate"] == "ancestry_unavailable"
    assert len(wire(bounded).encode()) + 32 <= 2048


def test_input_context_and_after_dropped_before_delivery_facts():
    receipt = {
        "requested_events": 2,
        "accepted_events": 1,
        "unknown_events": 1,
        "phases": {
            "down": {"requested": 1, "accepted": 1},
            "up": {"requested": 1, "accepted": None, "unknown": 1},
        },
    }
    value = {
        "status": "unknown",
        "operation": "act",
        "may_have_acted": True,
        "data": {
            "results": [
                {
                    "index": 0,
                    "result": {
                        "status": "unknown",
                        "operation": "key",
                        "may_have_acted": True,
                        "receipt": receipt,
                        "input_context": {"huge": ["x" * 5000] * 100},
                    },
                }
            ],
            "cleanup": {"status": "unknown", "may_have_acted": True},
            "after": {"metadata": ["x" * 1000] * 100},
        },
    }
    bounded = bound_result(value, 4096, 4096)
    assert bounded["data"]["results"][0]["result"]["receipt"] == receipt
    assert "input_context" not in bounded["data"]["results"][0]["result"]
    assert bounded["data"]["cleanup"]["status"] == "unknown"
    assert "after" not in bounded["data"]


def test_tiny_budget_retains_cleanup_and_first_failure_over_receipt_events():
    value = {
        "status": "partial",
        "operation": "play",
        "may_have_acted": True,
        "data": {
            "actions": {
                "status": "unknown",
                "data": {
                    "results": [
                        {
                            "index": 7,
                            "result": {
                                "status": "unknown",
                                "may_have_acted": True,
                                "receipt": {
                                    "events": [{"flags": 8}] * 500,
                                    "requested_events": 500,
                                    "accepted_events": None,
                                },
                                "error": {
                                    "code": "STALE_TARGET",
                                    "details": {
                                        "stage": "focus_after_read",
                                        "predicate": "ancestry_unavailable",
                                    },
                                },
                            },
                        }
                    ],
                    "cleanup": {"status": "unknown", "may_have_acted": True},
                },
            },
            "capture": {"metadata": ["x" * 2000] * 500},
        },
    }
    bounded = bound_result(value, 2048, 2048)
    assert bounded["may_have_acted"]
    if "first_failure" in bounded["data"]:
        cleanup = bounded["cleanup"]
        failure = bounded["data"]["first_failure"]["result"]
    else:
        actions = bounded["data"]["actions"]["data"]
        cleanup = actions["cleanup"]
        failure = actions["results"][0]["result"]
    assert cleanup["status"] == "unknown"
    assert failure["error"]["details"]["predicate"] == "ancestry_unavailable"
    assert failure["may_have_acted"]
    assert len(wire(bounded).encode()) + 32 <= 2048


@pytest.mark.parametrize("operation", ["act", "play"])
def test_server_minimal_budget_preserves_unknown_receipt_totals_and_batch_counters(operation):
    from minano_computer_use.server import _response

    receipt = {
        "requested_events": 2,
        "accepted_events": None,
        "unknown_events": 1,
        "phases": {
            "down": {"requested": 1, "accepted": 1, "unknown": 0},
            "up": {"requested": 1, "accepted": 0, "unknown": 1},
            "other": {"requested": 0, "accepted": 0, "unknown": 0},
        },
        "requested_seconds": 0.125,
        "dispatch_interval_seconds": 0.126,
        "events": [
            {
                "requested_vk": 65,
                "w_vk": 0,
                "w_scan": 30,
                "flags": 8,
                "phase": "down",
                "accepted": True,
            }
        ]
        * 64,
        "events_omitted": 1000,
        "consumption_verified": False,
    }
    failure = {
        "status": "unknown",
        "operation": "key",
        "method": "SendInput",
        "may_have_acted": True,
        "cancelled": True,
        "receipt": receipt,
        "error": {
            "code": "CANCELLED",
            "details": {
                "predicate": "ancestry_unavailable",
                "stage": "focus_before_read",
                "node_index": 12,
                "error_type": "COMError",
            },
        },
        "input_context": {"aux": ['私人\\"' * 500] * 10},
    }
    cleanup = {
        "status": "unknown",
        "operation": "release_all",
        "method": "SendInput",
        "may_have_acted": True,
        "receipt": receipt,
        "error": {"code": "RELEASE_UNCONFIRMED"},
    }
    counters = {"requested": 20, "attempted": 18, "completed": 17, "stopped_early": True}
    batch = {
        **counters,
        "cleanup": cleanup,
        "results": [
            {"index": i, "result": {"status": "invoked", "receipt": receipt}} for i in range(17)
        ]
        + [{"index": 17, "result": failure}],
    }
    data = (
        batch
        if operation == "act"
        else {
            "actions": {"status": "unknown", "data": batch},
            "capture": {"metadata": ['私人\\"' * 500] * 10},
            "requested": 999,
            "completed": 999,
        }
    )
    response = _response(
        {
            "status": "unknown",
            "operation": operation,
            "delivery": "native",
            "may_have_acted": True,
            "cancelled": True,
            "data": data,
        },
        operation,
        2048,
        2048,
    )
    bounded = response.structuredContent
    assert json.loads(response.content[0].text) == bounded
    assert response.isError and bounded["status"] == "unknown"
    assert bounded["operation"] == operation and bounded["delivery"] == "native"
    assert bounded["may_have_acted"] and bounded["cancelled"] and bounded["data_omitted"]
    assert {key: bounded["data"][key] for key in counters} == counters
    first = bounded["data"]["first_failure"]
    assert first["index"] == 17 and first["result"]["may_have_acted"]
    assert first["result"]["error"]["details"]["predicate"] == "ancestry_unavailable"
    core = {"requested_events": 2, "accepted_events": None, "unknown_events": 1}
    assert first["result"]["receipt"] == core
    assert bounded["cleanup"]["receipt"] == core
    assert bounded["cleanup"]["status"] == "unknown"
    assert bounded["cleanup"]["error"]["code"] == "RELEASE_UNCONFIRMED"
    encoded = json.dumps(response.model_dump(exclude_none=True), ensure_ascii=False)
    assert len(encoded) <= 2048 and len(encoded.encode("utf-8")) <= 2048
    assert len(wire(bounded)) + 32 <= 2048


@pytest.mark.parametrize("budget", [2048, 4096])
def test_successful_play_omits_capture_metadata_before_action_receipt(budget):
    from minano_computer_use.server import _response

    receipt = {"requested_events": 2, "accepted_events": 2, "unknown_events": 0}
    value = {
        "status": "ok",
        "operation": "play",
        "delivery": "native",
        "may_have_acted": True,
        "data": {
            "actions": {
                "status": "ok",
                "operation": "act",
                "data": {
                    "requested": 1,
                    "attempted": 1,
                    "completed": 1,
                    "stopped_early": False,
                    "results": [
                        {
                            "index": 0,
                            "result": {"status": "invoked", "operation": "key", "receipt": receipt},
                        }
                    ],
                    "cleanup": {"status": "ok"},
                },
            },
            "capture": {"metadata": {"sample_times": [0.123456789] * 600}},
        },
    }
    response = _response(value, "play", budget, budget)
    bounded = response.structuredContent
    assert bounded["status"] == "ok" and not response.isError
    assert bounded["data"]["actions"]["data"]["results"][0]["result"]["receipt"] == receipt
    assert bounded["data"]["actions"]["data"]["cleanup"]["status"] == "ok"
    assert bounded["data"]["capture_omitted"] is True
    assert "capture" not in bounded["data"]
    assert json.loads(response.content[0].text) == bounded
    assert len(wire(bounded).encode()) + 32 <= budget
    assert "capture" in value["data"]


@pytest.mark.parametrize("cleanup_status", ["ok", "unknown"])
def test_receipt_phase_cleanup_counts_do_not_mask_real_cleanup_status(cleanup_status):
    from minano_computer_use.server import _response

    value = {
        "status": "unknown",
        "operation": "act",
        "may_have_acted": True,
        "receipt": {"phases": {"cleanup": {"requested": 1, "accepted": 1, "unknown": 0}}},
        "data": {
            "results": [
                {
                    "index": 0,
                    "result": {
                        "status": "unknown",
                        "may_have_acted": True,
                        "error": {"code": "TIMEOUT"},
                    },
                }
            ],
            "cleanup": {"status": cleanup_status},
            "unrelated_large": ["x" * 3000] * 100,
        },
    }
    response = _response(value, "act", 2048, 2048)
    bounded = response.structuredContent
    assert bounded["data_omitted"] is True
    assert bounded["cleanup"]["status"] == cleanup_status
    assert bounded["status"] == "unknown" and bounded["may_have_acted"]
    assert len(wire(bounded).encode()) + 32 <= 2048


@pytest.mark.parametrize("budget", [2048, 4096, 6144, 8192])
def test_cancelled_action_local_release_survives_all_summary_budgets(budget):
    from minano_computer_use.server import _response

    def receipt(down, up):
        return {
            "requested_events": down + up,
            "accepted_events": down + up,
            "unknown_events": 0,
            "phases": {
                "down": {"requested": down, "accepted": down, "unknown": 0},
                "up": {"requested": up, "accepted": up, "unknown": 0},
                "other": {"requested": 0, "accepted": 0, "unknown": 0},
            },
            "requested_seconds": 0.2,
            "dispatch_interval_seconds": 0.201,
            "consumption_verified": False,
            "events": [
                {
                    "phase": "down",
                    "w_vk": 0,
                    "w_scan": 30,
                    "flags": 8,
                    "accepted": True,
                    "requested_vk": 65,
                }
            ]
            * 64,
        }

    local_cleanup = {
        "status": "ok",
        "operation": "release_all",
        "method": "SendInput",
        "receipt": receipt(0, 1),
        "may_have_acted": True,
        "input_context": {"text": "SECRET"},
    }
    local_cleanup["cleanup"] = {"status": "ok", "text": "SECRET"}
    failed = {
        "status": "unknown",
        "operation": "key",
        "method": "SendInput",
        "cancelled": True,
        "may_have_acted": True,
        "error": {"code": "CANCELLED"},
        "receipt": receipt(1, 0),
        "cleanup": local_cleanup,
    }
    value = {
        "status": "unknown",
        "operation": "act",
        "delivery": "native",
        "cancelled": True,
        "may_have_acted": True,
        "data": {
            "requested": 5,
            "attempted": 5,
            "completed": 4,
            "stopped_early": False,
            "results": [
                {
                    "index": i,
                    "result": {"status": "invoked", "operation": "key", "receipt": receipt(1, 1)},
                }
                for i in range(4)
            ]
            + [{"index": 4, "result": failed}],
            "cleanup": {"status": "ok", "receipt": receipt(0, 0)},
        },
    }
    response = _response(value, "act", budget, budget)
    bounded = response.structuredContent
    data = bounded["data"]
    if "first_failure" in data:
        failure = data["first_failure"]
    else:
        failure = next(item for item in data["results"] if item["index"] == 4)
    outcome = failure["result"]
    assert outcome["status"] == "unknown" and outcome["cancelled"]
    assert outcome["receipt"]["accepted_events"] == 1
    cleanup = outcome["cleanup"]
    assert cleanup["status"] == "ok" and cleanup["receipt"]["accepted_events"] == 1
    if "phases" in cleanup["receipt"]:
        assert cleanup["receipt"]["phases"]["up"]["accepted"] == 1
    assert "input_context" not in cleanup and "events" not in cleanup["receipt"]
    assert "cleanup" not in cleanup
    assert bounded["may_have_acted"] and response.isError
    assert json.loads(response.content[0].text) == bounded
    assert len(wire(bounded).encode()) + 32 <= budget
