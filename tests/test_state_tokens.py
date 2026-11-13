from types import SimpleNamespace as NS

import pytest
from test_uia_backend import Control, backend
from test_uia_queries import Clock

from minano_computer_use.contracts import failure_details


def state(service, **kwargs):
    return service.execute(
        {
            "action": "state",
            "target": "App",
            "target_binding": service.win32.window_identity(10, 20),
            "include_tokens": True,
            **kwargs,
        }
    )


@pytest.mark.parametrize("rich", [False, True])
@pytest.mark.parametrize("compact", [False, True])
@pytest.mark.parametrize("action", ["invoke", "set_value"])
def test_state_tokens_operate_without_name_rediscovery(rich, compact, action):
    calls = []
    value = NS(Value="old", IsReadOnly=False)

    def write(text, **kwargs):
        calls.append(text)
        value.Value = text

    value.SetValue = write
    child = Control(
        "same",
        patterns={
            1: NS(Invoke=lambda **kwargs: calls.append("invoke")),
            2: value,
        },
    )
    root = Control("root", children=[child, Control("same")])
    service = backend(root)
    response = state(service, rich=rich, compact=compact)
    assert response["status"] == "ok"
    expected = service.win32.window_identity(10, 20)
    assert response["target_binding"] == response["data"]["target_binding"] == expected
    token = response["data"]["nodes"][1]["token"]
    assert response["data"]["nodes"][1]["token_ttl_seconds"] == 60
    root.GetFirstChildControl = lambda: pytest.fail("must not rediscover token target")
    service._select = lambda *args: pytest.fail("must not search by selector")
    response = service.execute(
        {
            "action": action,
            "target": "App",
            "token": token,
            "value": " new ",
        }
    )
    assert response["status"] == ("invoked" if action == "invoke" else "verified")
    assert calls == (["invoke"] if action == "invoke" else [" new "])


def test_state_default_does_not_issue_tokens():
    service = backend(Control("root"))
    response = state(service, include_tokens=False)
    assert "token" not in response["data"]["nodes"][0]
    assert not service._tokens


@pytest.mark.parametrize(
    "change", ["expiry", "worker", "root", "control", "creation", "detach", "com"]
)
def test_state_token_uses_existing_invalidation(change):
    calls = []
    child = Control("child", patterns={1: NS(Invoke=lambda **kw: calls.append(1))})
    root = Control("root", children=[child])
    service = backend(root)
    clock = Clock()
    service._clock = clock
    token = state(service, rich=False)["data"]["nodes"][1]["token"]
    if change == "expiry":
        clock.now += 60
    elif change == "worker":
        service = backend(root)
    elif change == "root":
        root.runtime_id = [20, 999]
    elif change == "control":
        child.runtime_id = [20, 999]
    elif change == "creation":
        service.win32.creation += 1
    elif change == "detach":
        child.parent = None
    else:
        child._element = None
    response = service.execute({"action": "invoke", "target": "App", "token": token})
    assert response["status"] == "refused"
    assert response["error"]["code"] == {
        "expiry": "EXPIRED_TOKEN",
        "worker": "INVALID_TOKEN",
    }.get(change, "STALE_TOKEN")
    assert not calls


def test_state_token_cap_never_evicts_same_response():
    service = backend(Control("root", children=[Control(str(i)) for i in range(260)]))
    response = state(service, rich=False, max_output_chars=1048576, max_output_bytes=1048576)
    entries = response["data"]["nodes"]
    tokens = [entry["token"] for entry in entries if "token" in entry]
    assert len(tokens) == len(service._tokens) == 256
    assert all(token in service._tokens for token in tokens)
    assert entries[-1]["token_unavailable"]


@pytest.mark.parametrize("budget", [2048, 7000, 1048576])
def test_state_tokens_truncated_tree_and_output_budget(budget):
    root = Control("root", children=[Control(str(i)) for i in range(8)])
    root.children[1].HasKeyboardFocus = True
    service = backend(root)
    response = state(
        service, rich=False, max_nodes=3, max_output_chars=budget, max_output_bytes=budget
    )
    assert response["status"] == "ok"
    nodes = response.get("data", {}).get("nodes", [])
    for entry in nodes:
        assert entry["token"] in service._tokens
        assert len(entry["token"]) == 36
    for key in ("focus", "selection"):
        reference = response.get("data", {}).get(key, {})
        assert "node_index" not in reference or reference["node_index"] < len(nodes)
    if budget == 1048576:
        assert len(nodes) == 3 and response["data"]["truncated"] is True


@pytest.mark.parametrize(
    "phase", ["metadata", "issue", "after_issue", "binding", "later_issue", "metadata_after_issue"]
)
def test_failed_state_never_commits_tokens_for_changed_identity(phase):
    child = Control("child")
    root = Control("root", children=[child])
    service = backend(root)
    old = service.execute({"action": "inspect", "target": "App"})["data"]["token"]
    previous = service._tokens.copy()
    original_metadata = service._rich_metadata
    original_issue = service._issue_token

    def metadata(control, *args, **kwargs):
        data = original_metadata(control, *args, **kwargs)
        if control is child and phase == "metadata":
            child.parent = None
        return data

    def issue(entry, window, live_root, control):
        if phase == "issue":
            control.runtime_id = [20, 999]
        original_issue(entry, window, live_root, control)
        if phase == "after_issue":
            control.runtime_id = [20, 888]
        elif phase == "binding":
            service.win32.creation += 1
        elif phase == "later_issue" and control is child:
            root.runtime_id = [20, 777]
        elif phase == "metadata_after_issue":
            control.AutomationId = "replacement"

    service._rich_metadata = metadata
    service._issue_token = issue
    response = state(service)
    assert response["status"] == "refused"
    assert "data" not in response
    details = response["error"]["details"]
    assert failure_details(details) == details
    assert service._tokens == previous
    assert old in service._tokens


@pytest.mark.parametrize("compact", [False, True])
@pytest.mark.parametrize("changed_node", ["root", "child"])
@pytest.mark.parametrize("field", ["Name", "AutomationId"])
def test_metadata_callback_change_cannot_bind_token_to_unpublished_metadata(
    compact, changed_node, field
):
    child = Control("child")
    root = Control("root", children=[child])
    service = backend(root)
    old_token = service.execute({"action": "inspect", "target": "App"})["data"]["token"]
    previous = service._tokens.copy()
    metadata = service._rich_metadata

    def change_after_read(control, *args, **kwargs):
        entry = metadata(control, *args, **kwargs)
        if control is (root if changed_node == "root" else child):
            setattr(control, field, "replacement")
        return entry

    service._rich_metadata = change_after_read
    response = state(service, compact=compact)
    assert response["status"] == "refused"
    assert response["error"]["code"] == "STALE_TARGET"
    assert response["error"]["details"] == {
        "stage": "token_after_issue",
        "node_index": 0 if changed_node == "root" else 1,
        "predicate": "token_binding_changed",
    }
    assert "data" not in response
    assert service._tokens == previous
    assert old_token in service._tokens
