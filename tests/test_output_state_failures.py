import copy
import json

import pytest

from minano_computer_use.server import _response


def _state(status, errors):
    return {
        "status": status,
        "operation": "state",
        "data": {
            "component_errors": errors,
            "nodes": [{"name": "large" * 1000} for _ in range(20)],
        },
    }


@pytest.mark.parametrize("status", ["partial", "error"])
def test_state_component_failure_survives_minimal_server_envelope(status):
    value = _state(
        status,
        {
            "uia": {
                "status": "refused",
                "operation": "state",
                "error": {
                    "code": "STALE_TARGET",
                    "message": "private provider text" * 1000,
                    "details": {
                        "stage": "focus_before_read",
                        "predicate": "ancestry_unavailable",
                        "node_index": 17,
                        "value": "private",
                    },
                },
            },
            "screenshot": {
                "status": "error",
                "operation": "screenshot",
                "error": {"code": "CAPTURE_FAILED", "message": "private" * 1000},
            },
            "untrusted_component": {
                "status": "error",
                "error": {"code": "SECRET"},
            },
        },
    )
    original = copy.deepcopy(value)
    response = _response(value, "state", 2048, 2048)
    bounded = response.structuredContent

    assert bounded["status"] == status
    assert bounded["data_omitted"] is True
    assert bounded["component_failure_summary"] == [
        {
            "component": "uia",
            "status": "refused",
            "operation": "state",
            "error": {
                "code": "STALE_TARGET",
                "details": {
                    "stage": "focus_before_read",
                    "predicate": "ancestry_unavailable",
                    "node_index": 17,
                },
            },
        },
        {
            "component": "screenshot",
            "status": "error",
            "operation": "screenshot",
            "error": {"code": "CAPTURE_FAILED"},
        },
    ]
    assert "private" not in json.dumps(bounded)
    assert json.loads(response.content[0].text) == bounded
    envelope = json.dumps(response.model_dump(exclude_none=True), ensure_ascii=False)
    assert len(envelope) <= 2048 and len(envelope.encode("utf-8")) <= 2048
    assert value == original


def test_nested_after_state_failure_is_preserved_with_location():
    after = _state(
        "error",
        {
            "uia": {
                "status": "error",
                "operation": "state",
                "error": {"code": "STALE_TARGET", "details": {"stage": "focus_after_read"}},
            }
        },
    )
    value = {
        "status": "partial",
        "operation": "act",
        "data": {"results": [], "after": after, "nodes": ["x" * 5000] * 20},
    }
    original = copy.deepcopy(value)
    response = _response(value, "act", 2048, 2048)
    summary = response.structuredContent["component_failure_summary"]
    assert summary == [
        {
            "component": "uia",
            "status": "error",
            "source": "after",
            "operation": "state",
            "error": {"code": "STALE_TARGET", "details": {"stage": "focus_after_read"}},
        }
    ]
    assert response.structuredContent["status"] == "partial"
    assert json.loads(response.content[0].text) == response.structuredContent
    assert (
        len(json.dumps(response.model_dump(exclude_none=True), ensure_ascii=False).encode("utf-8"))
        <= 2048
    )
    assert value == original


def test_all_fixed_components_and_repeated_nested_failures_are_bounded():
    errors = {
        component: {
            "status": "refused",
            "operation": "state",
            "error": {
                "code": "STALE_TARGET",
                "details": {
                    "predicate": "binding_process_creation_time_mismatch",
                    "stage": "binding_before_publication",
                    "node_index": 5000,
                    "error_type": "ReferenceError",
                },
            },
        }
        for component in ("uia", "screenshot", "binding")
    }
    value = _state("error", errors)
    value["data"]["after"] = _state("error", errors)
    original = copy.deepcopy(value)
    response = _response(value, "state", 2048, 2048)
    summary = response.structuredContent["component_failure_summary"]
    assert [item["component"] for item in summary] == ["uia", "screenshot", "binding"]
    assert all(item["error"]["code"] == "STALE_TARGET" for item in summary)
    assert all(item["status"] == "refused" for item in summary)
    assert (
        len(json.dumps(response.model_dump(exclude_none=True), ensure_ascii=False).encode("utf-8"))
        <= 2048
    )
    assert value == original


def test_after_failure_does_not_replace_message_delivery_facts():
    receipt = {"requested_events": 2, "accepted_events": 1, "unknown_events": 1}
    value = {
        "status": "unknown",
        "operation": "act",
        "delivery": "message",
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
                        "error": {"code": "TIMEOUT"},
                    },
                }
            ],
            "after": _state(
                "partial",
                {
                    "screenshot": {
                        "status": "error",
                        "operation": "screenshot",
                        "error": {"code": "CAPTURE_FAILED"},
                    }
                },
            ),
            "other": "x" * 100000,
        },
    }
    original = copy.deepcopy(value)
    response = _response(value, "act", 2048, 2048)
    bounded = response.structuredContent
    assert bounded["status"] == "unknown" and bounded["may_have_acted"] is True
    assert bounded["delivery"] == "message"
    first = bounded["data"]["first_failure"]["result"]
    assert first["status"] == "unknown" and first["may_have_acted"] is True
    assert first["receipt"] == receipt
    assert bounded["component_failure_summary"][0]["error"]["code"] == "CAPTURE_FAILED"
    assert json.loads(response.content[0].text) == bounded
    assert (
        len(json.dumps(response.model_dump(exclude_none=True), ensure_ascii=False).encode("utf-8"))
        <= 2048
    )
    assert value == original


def test_unrelated_component_errors_are_not_promoted_to_state_evidence():
    value = _state("partial", {})
    value["operation"] = "inspect"
    value["data"]["component_errors"] = {"uia": {"status": "error", "error": {"code": "NOT_STATE"}}}
    bounded = _response(value, "inspect", 2048, 2048).structuredContent
    assert "component_failure_summary" not in bounded


def test_component_summary_survives_list_trimming_without_provider_text():
    value = _state(
        "partial",
        {
            "uia": {
                "status": "unknown",
                "operation": "private operation",
                "error": {
                    "code": "private provider code",
                    "message": "private message",
                    "details": {
                        "predicate": "private predicate",
                        "stage": "private stage",
                        "node_index": True,
                        "name": "private name",
                    },
                },
            }
        },
    )
    value["data"]["nodes"] = [{"name": '\\"你好' * 20} for _ in range(100)]
    original = copy.deepcopy(value)
    response = _response(value, "state", 4096, 4096)
    bounded = response.structuredContent
    assert 0 < len(bounded["data"]["nodes"]) < 100
    assert bounded["component_failure_summary"] == [
        {"component": "uia", "status": "unknown", "error": {"code": "ERROR"}}
    ]
    assert bounded["status"] == "partial"
    assert json.loads(response.content[0].text) == bounded
    envelope = json.dumps(response.model_dump(exclude_none=True), ensure_ascii=False)
    assert len(envelope) <= 4096 and len(envelope.encode("utf-8")) <= 4096
    assert value == original
