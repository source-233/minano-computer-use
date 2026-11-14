from types import SimpleNamespace as NS

import pytest
from test_uia_backend import Control, FakeWin32, backend


class HostWin32(FakeWin32):
    def __init__(self):
        super().__init__()
        self.element_creation = 5678
        self.member = True
        self.anchor = 30
        self.native_pid = 40
        self.on_native = None

    def child_window_identity(self, host, host_pid, child, child_pid):
        assert (host, host_pid) == (10, 20)
        if self.on_native:
            self.on_native()
        if not self.member or child != self.anchor or child_pid != self.native_pid:
            raise ValueError("PRIVATE native membership failure")
        return {"hwnd": child, "pid": child_pid, "process_creation_time": self.element_creation}


class Value:
    IsReadOnly = False

    def __init__(self):
        self.value, self.writes, self.on_write = "old", [], None

    @property
    def Value(self):
        return self.value

    def SetValue(self, value, waitTime=0):
        self.writes.append(value)
        self.value = value
        if self.on_write:
            self.on_write()


def fixture():
    value = Value()
    leaf = Control("edit", pid=40, patterns={2: value})
    leaf.NativeWindowHandle = 0
    bridge = Control("windowless", pid=40, children=[leaf])
    bridge.NativeWindowHandle = 0
    anchor = Control("embedded", pid=40, children=[bridge])
    anchor.NativeWindowHandle = 30
    root = Control("host", children=[anchor])
    win = HostWin32()
    service = backend(root, win)
    return service, win, root, anchor, bridge, leaf, value


def state(service, **kwargs):
    return service.execute(
        {
            "action": "state",
            "target": "App",
            "target_binding": service.win32.window_identity(10, 20),
            **kwargs,
        }
    )


def test_hosted_state_and_token_set_value_use_element_process_anchor():
    service, win, root, anchor, bridge, leaf, value = fixture()
    try:
        response = state(service, include_tokens=True)
        assert response["status"] == "ok", response
        data = response["data"]
        assert data["target_binding"]["pid"] == 20  # Host identity must not be rewritten.
        node = next(item for item in data["nodes"] if item["name"] == "edit")
        assert node["pid"] == 40 and node["token"]
        token = node["token"]
        cached = service._tokens[token]["membership"]
        assert cached["anchor_hwnd"] == 30 and cached["process_creation_time"] == 5678
        response = service.execute(
            {"action": "set_value", "target": "App", "token": token, "value": "new"}
        )
        assert response["status"] == "verified", response
        assert value.writes == ["new"]
        assert response["target_binding"]["pid"] == 20
    finally:
        service.close()


@pytest.mark.parametrize("action", ["set_value", "invoke"])
def test_direct_cross_process_mutation_is_verified_without_token(action):
    service, win, root, anchor, bridge, leaf, value = fixture()
    invoked = []
    leaf.patterns[1] = NS(Invoke=lambda waitTime=0: invoked.append(True))
    try:
        response = service.execute(
            {
                "action": action,
                "target": "App",
                "selector": "edit",
                **({"value": "new"} if action == "set_value" else {}),
            }
        )
        assert response["status"] == ("verified" if action == "set_value" else "invoked"), response
        assert (value.writes if action == "set_value" else invoked) == (
            ["new"] if action == "set_value" else [True]
        )
    finally:
        service.close()


@pytest.mark.parametrize(
    "failure",
    [
        "unrelated",
        "native_pid",
        "no_anchor",
        "sibling_anchor",
        "no_creation",
        "pid_zero",
        "detached",
        "cycle",
        "own_hwnd",
    ],
)
def test_unproven_cross_process_state_refuses_and_publishes_no_tokens(failure):
    service, win, root, anchor, bridge, leaf, value = fixture()
    if failure == "unrelated":
        win.member = False
    elif failure == "native_pid":
        win.native_pid = 99
    elif failure in {"no_anchor", "sibling_anchor"}:
        anchor.NativeWindowHandle = 0
        if failure == "sibling_anchor":
            sibling = Control("unrelated anchor", pid=40)
            sibling.NativeWindowHandle = 30
            root.children.append(sibling)
            sibling.parent, sibling._index = root, 1
    elif failure == "no_creation":
        win.element_creation = None
    elif failure == "pid_zero":
        leaf.ProcessId = 0
    elif failure == "detached":
        leaf.parent = None
        leaf.GetNextSiblingControl = lambda: None
    elif failure == "cycle":
        leaf.parent = leaf
    else:
        leaf.NativeWindowHandle = 99
    try:
        response = state(service, include_tokens=True)
        assert response["status"] == "refused", response
        assert response["error"]["code"] == "STALE_TARGET"
        assert "data" not in response and not service._tokens
        assert not value.writes and "PRIVATE" not in str(response)
    finally:
        service.close()


@pytest.mark.parametrize(
    "change",
    [
        "host_reuse",
        "element_reuse",
        "anchor_reparent",
        "anchor_replaced",
        "candidate_detached",
        "pointer",
    ],
)
def test_token_never_repins_changed_cross_process_identity(change):
    service, win, root, anchor, bridge, leaf, value = fixture()
    leaf._element = object()
    try:
        response = state(service, include_tokens=True)
        assert response["status"] == "ok", response
        token = response["data"]["nodes"][-1]["token"]
        if change == "host_reuse":
            win.creation += 1
        elif change == "element_reuse":
            win.element_creation += 1
        elif change == "anchor_reparent":
            win.member = False
        elif change == "anchor_replaced":
            anchor.runtime_id = [40, 999]
        elif change == "candidate_detached":
            leaf.parent = root
        else:
            leaf._element = object()
        response = service.execute(
            {"action": "set_value", "target": "App", "token": token, "value": "new"}
        )
        assert response["status"] == "refused" and response["error"]["code"] == "STALE_TOKEN"
        assert not value.writes
    finally:
        service.close()


@pytest.mark.parametrize("action", ["set_value", "invoke", "toggle"])
def test_cross_process_loss_after_write_is_unknown_and_never_replayed(action):
    service, win, root, anchor, bridge, leaf, value = fixture()
    calls = []

    def mutate(waitTime=0):
        calls.append(True)
        win.element_creation += 1

    leaf.patterns[1] = NS(Invoke=mutate)
    leaf.patterns[3] = NS(Toggle=mutate, ToggleState=0)
    value.on_write = lambda: setattr(win, "element_creation", 9999)
    try:
        response = service.execute(
            {
                "action": action,
                "target": "App",
                "selector": "edit",
                **({"value": "new"} if action == "set_value" else {}),
            }
        )
        assert response["status"] == "unknown", response
        assert response["may_have_acted"] is True
        assert len(value.writes) + len(calls) == 1
    finally:
        service.close()


def test_root_process_mismatch_is_still_an_explicit_unsupported_binding():
    service, win, root, anchor, bridge, leaf, value = fixture()
    root.ProcessId = 40
    try:
        response = state(service)
        assert response["error"]["details"]["predicate"] == "root_pid_mismatch"
    finally:
        service.close()


def test_provider_churn_during_native_membership_query_is_rejected():
    service, win, root, anchor, bridge, leaf, value = fixture()
    win.on_native = lambda: setattr(leaf, "parent", root)
    try:
        response = state(service)
        assert response["status"] == "refused"
        assert "data" not in response
    finally:
        service.close()


@pytest.mark.parametrize("stage", ["before_write", "after_write"])
def test_cross_process_deadline_starts_no_queries_after_expiry(stage):
    from test_uia_queries import Clock

    service, win, root, anchor, bridge, leaf, value = fixture()
    clock = Clock()
    service._clock, service._sleep = clock, clock.sleep
    native = win.child_window_identity
    calls = []

    def slow_native(*args):
        calls.append(clock())
        assert clock() < 100.12
        reply = native(*args)
        if stage == "before_write" or value.writes:
            clock.now += 0.2
        return reply

    win.child_window_identity = slow_native
    try:
        reply = service.execute(
            {
                "action": "set_value",
                "target": "App",
                "selector": "edit",
                "value": "new",
                "timeout": 0.12,
            }
        )
        assert reply["error"]["code"] == (
            "DEADLINE_EXCEEDED" if stage == "before_write" else "READBACK_TIMEOUT"
        )
        assert len(value.writes) == (0 if stage == "before_write" else 1)
        assert calls and all(t < 100.12 for t in calls)
    finally:
        service.close()


def test_cross_process_lost_pointer_is_not_refound_during_token_or_write():
    service, win, root, anchor, bridge, leaf, value = fixture()
    leaf._element = None
    try:
        response = service.execute(
            {"action": "set_value", "target": "App", "selector": "edit", "value": "new"}
        )
        assert response["status"] in {"refused", "error"}
        assert not value.writes
    finally:
        service.close()
