import json
from types import SimpleNamespace as NS

import pytest
from test_uia_backend import Control, FakeAuto, FakeWin32, backend, run

from minano_computer_use.uia_backend import UIABackend


class Clock:
    now = 100.0

    def __init__(self, on_sleep=None):
        self.on_sleep = on_sleep

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds
        if self.on_sleep:
            self.on_sleep()


def waiting(root, clock=None, **kwargs):
    clock = clock or Clock()
    service = UIABackend(auto=FakeAuto(root), win32=FakeWin32(), clock=clock, sleep=clock.sleep)
    return service.execute(
        {
            "action": "wait_for",
            "target": "App",
            "selector": "edit",
            "condition": {"value": "ready"},
            "timeout": 0.2,
            **kwargs,
        }
    )


def test_find_contains_case_insensitive_discovery_exact_selector_unchanged():
    root = Control("root", children=[Control("Save"), Control("Save as"), Control("other")])
    response = run(root, "find", query="sAvE", limit=1)
    data = response["data"]
    assert data["matched_total"] == 2 and data["matched_total_known"] is True
    assert data["return_limited"] is True and data["truncated"] is False
    entry = data["matches"][0]
    assert entry["path"] == [0, 1]
    assert entry["ancestry"][0]["name"] == "root"
    assert entry["token"]
    assert run(root, "find", query="save", match="exact")["data"]["matched_total"] == 1
    assert run(root, selector="save")["error"]["code"] == "ELEMENT_NOT_FOUND"


def test_find_duplicate_suggestions_explicit_index_and_truncation_not_hidden_by_limit():
    root = Control("root", children=[Control("Save"), Control("Save")])
    response = run(root, "find", selector="Save", limit=1, max_nodes=2)
    assert response["data"]["matched_total_known"] is False
    assert response["data"]["truncated"] is True
    response = run(root, "find", selector="Save")
    assert [entry["selector_suggestion"]["index"] for entry in response["data"]["matches"]] == [
        0,
        1,
    ]


def test_find_compact_omits_ancestry_and_tree_explicit_tokens():
    root = Control("root", children=[Control("Save")])
    response = run(root, "find", query="save", compact=True)
    assert "ancestry" not in response["data"]["matches"][0]
    response = run(root, "tree", compact=True, include_tokens=True)
    assert all("token" in entry and "pid" not in entry for entry in response["data"]["nodes"])


@pytest.mark.parametrize(
    "kwargs", [{}, {"query": "a", "match": "regex"}, {"query": ""}, {"query": "a", "limit": True}]
)
def test_find_invalid_discovery_refused(kwargs):
    assert run(Control("root"), "find", **kwargs)["status"] == "refused"


def test_wait_delayed_value_read_only_and_absolute_deadline():
    pattern = NS(Value="old")
    root = Control("root", children=[Control("edit", patterns={2: pattern})])
    clock = Clock(lambda: setattr(pattern, "Value", "ready"))
    response = waiting(root, clock, _deadline_monotonic=100.15)
    assert response["status"] == "verified"
    assert response["last_observation"]["value"] == "ready"
    assert clock.now == 100.05
    assert response["delivery"] == "read_only" and response["fallbacks"] == []


def test_wait_deadline_expiry_retains_last_observation_and_no_fresh_timeout():
    root = Control("root", children=[Control("edit", patterns={2: NS(Value="old")})])
    clock = Clock()
    response = waiting(root, clock, _deadline_monotonic=100.08)
    assert response["error"]["code"] == "CONDITION_TIMEOUT"
    assert response["last_observation"] == {"exists": True, "value": "old"}
    assert clock.now == 100.08


def test_wait_already_expired_performs_no_pattern_read():
    response = waiting(Control("root"), _deadline_monotonic=99)
    assert response["error"]["code"] == "CONDITION_TIMEOUT"
    assert response["last_observation"] is None


def test_wait_disappearance_requires_complete_search():
    root = Control("root", children=[Control("other")])
    assert waiting(root, condition={"exists": False})["status"] == "verified"
    assert waiting(root, condition={"exists": False}, max_nodes=1)["error"]["code"] == (
        "SEARCH_TRUNCATED"
    )


def test_wait_provider_failure_not_disappearance():
    class Broken(Control):
        def GetFirstChildControl(self):
            raise RuntimeError("disconnected")

    assert waiting(Broken("root"), condition={"exists": False})["status"] == "error"


def test_wait_missing_value_times_out_and_ambiguous_refuses():
    assert waiting(Control("root"))["error"]["code"] == "CONDITION_TIMEOUT"
    root = Control("root", children=[Control("edit"), Control("edit")])
    assert waiting(root)["error"]["code"] == "AMBIGUOUS_SELECTOR"


@pytest.mark.parametrize(
    "condition",
    [
        {"exists": 1},
        {"value": 1},
        {},
        {"exists": True, "selected": False},
        {"toggle_state": True},
        {"expand_state": 4},
    ],
)
def test_wait_invalid_conditions_refused(condition):
    assert waiting(Control("root"), condition=condition)["error"]["code"] == "INVALID_CONDITION"


@pytest.mark.parametrize(
    "condition,patterns",
    [
        ({"toggle_state": 2}, {3: NS(ToggleState=2)}),
        ({"selected": True}, {4: NS(IsSelected=True)}),
        ({"expand_state": 1}, {5: NS(ExpandCollapseState=1)}),
    ],
)
def test_wait_state_predicates(condition, patterns):
    root = Control("root", children=[Control("edit", patterns=patterns)])
    assert waiting(root, condition=condition)["status"] == "verified"


def test_output_budget_covers_utf8_json_and_preserves_status():
    root = Control("root", children=[Control("界" * 10000) for _ in range(30)])
    response = run(root, "tree", max_output_chars=2048, max_output_bytes=2048)
    serialized = json.dumps(response, ensure_ascii=False)
    escaped = json.dumps(response)
    assert len(serialized.encode("utf-8")) <= 2048
    assert len(escaped) <= 2048
    assert response["status"] == "ok"


def test_output_invalid_budget_refused_before_mutation():
    response = run(Control("root"), "invoke", max_output_chars=1)
    assert response["error"]["code"] == "INVALID_BUDGET"


def test_metadata_and_pattern_values_bounded_early():
    root = Control("x" * 10000, patterns={2: NS(Value="x" * 10000, IsReadOnly=True)})
    response = run(root, max_output_chars=100000, max_output_bytes=100000)
    assert response["data"]["name"] is None
    assert response["data"]["automation_id"] is None
    assert response["data"]["fields_omitted"] == ["name", "automation_id"]
    assert response["data"]["metadata_truncated"] is True
    assert len(response["data"]["patterns"]["Value"]["Value"]) == 4096
    assert "token" not in response["data"]


@pytest.mark.parametrize("compact", [False, True])
def test_oversized_identity_is_whole_or_absent_never_partial_selector(compact):
    name = "prefix" + "x" * 5000
    child = Control(name)
    root = Control("root", children=[child])
    budgets = {"max_output_chars": 100000, "max_output_bytes": 100000}
    response = run(root, "find", selector=name, compact=compact, **budgets)
    assert response["data"]["matched_total"] == 1
    entry = response["data"]["matches"][0]
    assert entry.get("name") in (None, name)
    assert entry.get("automation_id") in (None, name.lower())
    assert entry["metadata_truncated"] is True
    assert "selector_suggestion" not in entry and "token" not in entry
    assert entry["token_unavailable"]
    tree = run(root, "tree", compact=compact, include_tokens=True, **budgets)
    entry = tree["data"]["nodes"][1]
    assert entry.get("name") in (None, name)
    assert entry.get("automation_id") in (None, name.lower())
    assert "token" not in entry
    assert run(root, selector=name, **budgets)["status"] == "ok"
    assert run(root, selector=name[:4096], **budgets)["error"]["code"] == "ELEMENT_NOT_FOUND"


def test_provider_changes_process_lifetime_during_wait_refuses():
    root = Control("root", children=[Control("edit", patterns={2: NS(Value="old")})])
    win = FakeWin32()
    clock = Clock(lambda: setattr(win, "creation", 9999))
    service = UIABackend(auto=FakeAuto(root), win32=win, clock=clock, sleep=clock.sleep)
    response = service.execute(
        {
            "action": "wait_for",
            "target": "App",
            "selector": "edit",
            "condition": {"value": "new"},
            "timeout": 0.2,
        }
    )
    assert response["error"]["code"] == "STALE_TARGET"


def test_delivery_and_binding_present_for_single_accessibility_mutation():
    calls = []
    service = backend(Control("root", patterns={1: NS(Invoke=lambda **kw: calls.append(1))}))
    response = service.execute({"action": "invoke", "target": "App"})
    assert response["status"] == "invoked" and calls == [1]
    assert response["delivery"] == "accessibility"
    assert response["target_binding"]["pid"] == 20


def test_async_value_write_is_not_replayed_by_explicit_wait():
    class Value:
        IsReadOnly = False
        Value = "old"
        calls = 0

        def SetValue(self, value, waitTime=0):
            self.calls += 1
            return True  # Completion is deliberately deferred until the next poll.

    pattern = Value()
    root = Control("root", children=[Control("edit", patterns={2: pattern})])
    clock = Clock(lambda: setattr(pattern, "Value", "new"))
    service = UIABackend(auto=FakeAuto(root), win32=FakeWin32(), clock=clock, sleep=clock.sleep)
    write = service.execute(
        {"action": "set_value", "target": "App", "selector": "edit", "value": "new"}
    )
    assert write["status"] == "verified"
    observed = service.execute(
        {
            "action": "wait_for",
            "target": "App",
            "selector": "edit",
            "condition": {"value": "new"},
            "timeout": 0.2,
        }
    )
    assert observed["status"] == "verified"
    assert pattern.calls == 1
