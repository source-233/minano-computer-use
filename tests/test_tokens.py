import uuid
from types import SimpleNamespace as NS

import pytest
from test_uia_backend import Control, FakeAuto, FakeWin32, backend
from test_uia_queries import Clock

from minano_computer_use.uia_backend import UIABackend


def issue(service, **kwargs):
    response = service.execute({"action": "inspect", "target": "App", **kwargs})
    assert response["status"] == "ok"
    token = response["data"]["token"]
    assert str(uuid.UUID(token)) == token
    return token


def use(service, token, **kwargs):
    return service.execute({"action": "inspect", "target": "App", "token": token, **kwargs})


def test_token_targets_cached_element_without_name_research():
    calls = []
    button = Control("same", patterns={1: NS(Invoke=lambda **kw: calls.append(1))})
    root = Control("root", children=[button, Control("same")])
    service = backend(root)
    token = issue(service, selector={"name": "same", "index": 0})
    root.GetFirstChildControl = lambda: pytest.fail("token must not re-search the tree")
    response = use(service, token, action="invoke")
    assert response["status"] == "invoked" and calls == [1]


def test_token_rejects_selector_and_different_target_binding():
    service = backend(Control("root"))
    token = issue(service)
    assert use(service, token, selector="root")["error"]["code"] == "INVALID_SELECTOR"
    service.win32.windows = [{"hwnd": 10, "pid": 99, "title": "App"}]
    assert use(service, token)["status"] == "refused"


def test_token_ttl_never_slides_on_use():
    clock = Clock()
    service = UIABackend(auto=FakeAuto(Control("root")), win32=FakeWin32(), clock=clock)
    token = issue(service)
    clock.now += 59
    assert use(service, token)["status"] == "ok"
    clock.now += 1
    assert use(service, token)["error"]["code"] == "EXPIRED_TOKEN"
    assert use(service, token)["error"]["code"] == "INVALID_TOKEN"


@pytest.mark.parametrize("change", ["element_id", "root_id", "metadata", "creation", "detach"])
def test_token_changed_live_binding_invalidates_without_fallback(change):
    child = Control("edit")
    root = Control("root", children=[child])
    service = backend(root)
    token = issue(service, selector="edit")
    if change == "element_id":
        child.runtime_id = [20, 777]
    elif change == "root_id":
        root.runtime_id = [20, 888]
    elif change == "metadata":
        child.AutomationId = "replacement"
    elif change == "creation":
        service.win32.creation += 1
    else:
        child.parent = None
    assert use(service, token)["error"]["code"] == "STALE_TOKEN"
    assert use(service, token)["error"]["code"] == "INVALID_TOKEN"


def test_token_replacement_root_same_pid_name_rejected():
    service = backend(Control("root"))
    token = issue(service)
    service.auto.root = Control("root")
    assert use(service, token)["error"]["code"] == "STALE_TOKEN"


def test_new_worker_and_unknown_uuid_never_auto_rebind():
    service = backend(Control("root"))
    token = issue(service)
    replacement = backend(Control("root"))
    assert use(replacement, token)["error"]["code"] == "INVALID_TOKEN"
    assert use(service, str(uuid.uuid4()))["error"]["code"] == "INVALID_TOKEN"
    service.close()
    assert not service._tokens


def test_token_cache_bounded_256_oldest_evicted():
    service = backend(Control("root"))
    first = issue(service)
    for _ in range(256):
        issue(service)
    assert len(service._tokens) == 256
    assert use(service, first)["error"]["code"] == "INVALID_TOKEN"


def test_missing_runtime_id_read_works_but_no_unsafe_token():
    root = Control("root")
    root.GetRuntimeId = None
    service = backend(root)
    response = service.execute({"action": "inspect", "target": "App"})
    assert response["status"] == "ok"
    assert "token" not in response["data"]
    assert response["data"]["token_unavailable"]


def test_token_wait_cannot_turn_stale_into_exists_false():
    root = Control("root")
    service = backend(root)
    token = issue(service)
    root.runtime_id = [20, 888]
    response = use(service, token, action="wait_for", condition={"exists": False})
    assert response["error"]["code"] == "STALE_TOKEN"


def test_token_lost_underlying_com_reference_must_not_trigger_library_refind():
    child = Control("edit")
    child._element = object()
    service = backend(Control("root", children=[child]))
    token = issue(service, selector="edit")
    child._element = None
    child.GetRuntimeId = lambda: pytest.fail("missing live pointer must not auto-refind")
    assert use(service, token)["error"]["code"] == "STALE_TOKEN"


def test_large_tree_token_response_never_evicts_tokens_issued_in_same_response():
    root = Control("root", children=[Control(f"child {i}") for i in range(260)])
    service = backend(root)
    response = service.execute(
        {
            "action": "tree",
            "target": "App",
            "include_tokens": True,
            "max_output_chars": 1048576,
            "max_output_bytes": 1048576,
        }
    )
    entries = response["data"]["nodes"]
    tokens = [entry["token"] for entry in entries if "token" in entry]
    assert len(tokens) == 256
    assert all(token in service._tokens for token in tokens)
    assert "token_unavailable" in entries[-1]
