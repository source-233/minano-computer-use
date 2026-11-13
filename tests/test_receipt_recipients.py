import copy
import json

import pytest

from minano_computer_use.contracts import receipt_facts
from minano_computer_use.output import _receipt_core, bound_result
from minano_computer_use.trace import _delivery_facts


def receipt():
    return {
        "requested_events": 1,
        "accepted_events": 1,
        "unknown_events": 0,
        "events": [
            {
                "requested_vk": 65,
                "w_vk": 0,
                "w_scan": 30,
                "flags": 8,
                "phase": "up",
                "accepted": True,
                "recipient_hwnd": 0xFEDCBA9876543210,
                "recipient_pid": 0xFEDCBA98,
            }
        ],
    }


def test_public_receipt_keeps_exact_actual_recipient_not_requested_target():
    value = {
        "status": "invoked",
        "operation": "key_up",
        "target_binding": {"hwnd": 11, "pid": 22, "process_creation_time": 123456789},
        "receipt": receipt(),
    }
    original = copy.deepcopy(value)
    value["receipt"]["path"] = "SECRET"
    value["receipt"]["recipients"] = [{"hwnd": 1, "pid": 2, "path": "SECRET"}]
    value["receipt"]["events"][0].update(
        path="SECRET", text="SECRET", process_creation_time=987654321, target_binding={"hwnd": 1}
    )
    bounded = bound_result(value)
    assert bounded == original
    assert bounded["receipt"]["events"][0]["recipient_hwnd"] == 0xFEDCBA9876543210
    assert bounded["target_binding"]["hwnd"] == 11
    assert "SECRET" not in json.dumps(bounded)
    assert "path" in value["receipt"]  # Sanitization never mutates backend evidence.


@pytest.mark.parametrize(
    "field,maximum", [("recipient_hwnd", 2**64 - 1), ("recipient_pid", 2**32 - 1)]
)
@pytest.mark.parametrize("invalid", [None, True, False, 0, -1, 1.5, "123", [], {}])
def test_recipient_identity_rejects_non_positive_or_non_exact_int(field, maximum, invalid):
    value = receipt()
    value["events"][0][field] = invalid
    assert field not in receipt_facts(value, include_recipients=True)["events"][0]
    value["events"][0][field] = maximum + 1
    assert field not in receipt_facts(value, include_recipients=True)["events"][0]
    value["events"][0][field] = maximum
    assert receipt_facts(value, include_recipients=True)["events"][0][field] == maximum


def test_detailed_event_allowlist_is_bounded_without_identity_prefixes():
    value = receipt()
    value["events"] *= 70
    value["events_omitted"] = 3
    facts = receipt_facts(value, include_recipients=True)
    assert len(facts["events"]) == 64
    assert facts["events_omitted"] == 9
    assert all(event == value["events"][0] for event in facts["events"])
    assert receipt_facts(facts, include_recipients=True) == facts
    assert "events" not in receipt_facts(value)


def test_trace_and_core_never_keep_recipient_events_or_context():
    value = receipt()
    outcome = {
        "status": "unknown",
        "receipt": value,
        "input_context": {"path": "SECRET"},
        "cleanup": {"status": "ok", "receipt": value},
    }
    facts = _delivery_facts(outcome)
    core = {"requested_events": 1, "accepted_events": 1, "unknown_events": 0}
    assert facts["receipt"] == core
    assert facts["cleanup"]["receipt"] == core
    assert _receipt_core(value) == core
    assert "recipient" not in json.dumps(facts)
    assert '"events"' not in json.dumps(facts)
    assert "SECRET" not in json.dumps(facts)


def test_budget_reduction_drops_recipient_detail_after_preserving_aggregate():
    value = {"status": "unknown", "operation": "key_up", "receipt": receipt()}
    value["receipt"]["events"] *= 64
    original = copy.deepcopy(value)
    bounded = bound_result(value, 2048, 2048)
    assert bounded["receipt"] == receipt_facts(original["receipt"])
    assert bounded["receipt_details_omitted"] is True
    assert "recipient" not in json.dumps(bounded)
    assert value == original
