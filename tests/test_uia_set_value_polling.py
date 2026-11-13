import pytest
from test_uia_backend import Control, FakeAuto, FakeWin32
from test_uia_queries import Clock

from minano_computer_use.contracts import failure_details
from minano_computer_use.uia_backend import UIABackend


class Value:
    IsReadOnly = False

    def __init__(self, clock, delay=0):
        self.clock, self.delay = clock, delay
        self.writes, self.reads = [], []
        self.start = None
        self.on_read = self.on_write = None

    @property
    def Value(self):
        self.reads.append(self.clock())
        if self.on_read:
            self.on_read()
        if self.start is not None and self.clock() - self.start >= self.delay:
            return self.writes[0]
        return "old"

    def SetValue(self, value, waitTime=0):
        assert waitTime == 0
        self.writes.append(value)
        self.start = self.clock()
        if self.on_write:
            self.on_write()
        return True


def setup(delay=0, *, child=True):
    clock = Clock()
    pattern = Value(clock, delay)
    edit = Control("edit", patterns={2: pattern})
    root = Control("root", children=[edit]) if child else edit
    service = UIABackend(auto=FakeAuto(root), win32=FakeWin32(), clock=clock, sleep=clock.sleep)
    return service, root, edit, pattern, clock


def write(service, **kwargs):
    return service.execute(
        {
            "action": "set_value",
            "target": "App",
            "selector": "edit",
            "value": "new",
            **kwargs,
        }
    )


@pytest.mark.parametrize("value", ["new", "", " new ", "\n\t"])
@pytest.mark.parametrize("child", [False, True])
def test_delayed_same_element_confirms_exact_string_once(value, child):
    service, root, edit, pattern, clock = setup(0.1, child=child)

    def forbid_rediscovery():
        service.win32.resolve_window = lambda *a: pytest.fail("must not resolve again")
        service.auto.ControlFromHandle = lambda *a: pytest.fail("must not reacquire root")
        service._select = lambda *a, **kw: pytest.fail("must not re-search")

    pattern.on_write = forbid_rediscovery
    response = write(service, value=value)
    assert response["status"] == "verified"
    assert response["data"]["after"] == value
    assert pattern.writes == [value]
    assert 100.1 <= clock.now <= 100.2
    assert not service._tokens


def test_immediate_success_never_sleeps():
    service, root, edit, pattern, clock = setup()
    service._sleep = lambda *args: pytest.fail("immediate readback must not sleep")
    assert write(service)["status"] == "verified"
    assert len(pattern.reads) == 2 and pattern.writes == ["new"]
    assert clock.now == 100


@pytest.mark.parametrize(
    "timeout,absolute,duration", [(5, None, 0.6), (0.12, None, 0.12), (5, 100.08, 0.08)]
)
def test_poll_timeout_is_bounded_and_not_worker_timeout(timeout, absolute, duration):
    service, root, edit, pattern, clock = setup(10)
    kwargs = {} if absolute is None else {"_deadline_monotonic": absolute}
    response = write(service, timeout=timeout, **kwargs)
    assert response["status"] == "unknown" and response["may_have_acted"] is True
    assert response["error"]["code"] == "READBACK_TIMEOUT"
    assert clock.now == pytest.approx(100 + duration)
    assert pattern.writes == ["new"]
    assert response["data"]["before"] is response["data"]["after"] is None
    assert response["data"]["value_redacted"] is True


@pytest.mark.parametrize("spent", [0.15, 0.3])
def test_deadline_includes_target_search_and_prewrite_reads(spent):
    service, root, edit, pattern, clock = setup(10)
    resolve = service.win32.resolve_window

    def slow_resolve(target):
        clock.now += spent
        return resolve(target)

    service.win32.resolve_window = slow_resolve
    response = write(service, timeout=0.2)
    if spent >= 0.2:
        assert response["status"] == "refused"
        assert response["error"]["code"] == "DEADLINE_EXCEEDED"
        assert not pattern.writes
    else:
        assert response["error"]["code"] == "READBACK_TIMEOUT"
        assert pattern.writes == ["new"]
        assert clock.now == pytest.approx(100.2)


def change_identity(kind, service, root, edit):
    if kind == "root":
        root.runtime_id = [20, 777]
    elif kind == "control":
        edit.runtime_id = [20, 888]
    elif kind == "creation":
        service.win32.creation += 1
    elif kind == "ancestry":
        edit.parent = None
    elif kind == "native":
        edit.NativeWindowHandle = 99
        service.win32.is_child_window = lambda *args: False
    elif kind == "root_hwnd":
        root.NativeWindowHandle = 99
    elif kind == "pid":
        edit.ProcessId = 99
    elif kind == "com":
        edit._element = None
    else:
        edit._element = object()


@pytest.mark.parametrize(
    "kind",
    ["root", "control", "creation", "ancestry", "native", "root_hwnd", "pid", "com", "pointer"],
)
@pytest.mark.parametrize("when", ["before", "write", "sleep", "getter"])
def test_identity_change_stops_without_rewrite_or_another_read(kind, when):
    service, root, edit, pattern, clock = setup(0.1)
    root._element, edit._element = object(), object()

    def change():
        change_identity(kind, service, root, edit)

    if when == "before":
        pattern.on_read = change
    elif when == "write":
        pattern.on_write = change
    elif when == "sleep":
        clock.on_sleep = change
    else:
        pattern.on_read = lambda: change() if pattern.writes else None
    response = write(service)
    assert response["status"] == ("refused" if when == "before" else "unknown")
    assert response["error"]["code"] == "STALE_TARGET"
    assert len(pattern.writes) == (0 if when == "before" else 1)
    assert len(pattern.reads) == (1 if when in {"before", "write"} else 2)
    if when != "before":
        assert response["may_have_acted"] is True


@pytest.mark.parametrize("missing", ["root_id", "control_id", "creation", "com"])
def test_unpinnable_identity_never_writes(missing):
    service, root, edit, pattern, clock = setup()
    if missing == "root_id":
        root.GetRuntimeId = lambda: None
    elif missing == "control_id":
        edit.GetRuntimeId = lambda: None
    elif missing == "creation":
        service.win32.creation = None
    else:
        edit._element = None
    response = write(service)
    assert response["status"] == "refused"
    assert not pattern.writes


@pytest.mark.parametrize("when", ["write", "read"])
def test_postwrite_provider_exception_stops_unknown_and_sanitized(when):
    service, root, edit, pattern, clock = setup(0.1)

    def fail():
        raise RuntimeError("PRIVATE_PROVIDER_VALUE")

    if when == "write":
        pattern.on_write = fail
    else:
        pattern.on_read = lambda: fail() if pattern.writes else None
    response = write(service)
    assert response["status"] == "unknown" and response["may_have_acted"] is True
    assert response["error"]["code"] == "UIA_MUTATION_UNCERTAIN"
    assert pattern.writes == ["new"]
    assert len(pattern.reads) <= 2 and clock.now == 100
    assert "PRIVATE_PROVIDER_VALUE" not in str(response)


@pytest.mark.parametrize("when", ["before", "write", "getter", "sleep"])
@pytest.mark.parametrize("scope", ["self", "descendant", "unknown", "truncated"])
def test_privacy_changes_redact_all_before_after_samples(when, scope):
    service, root, edit, pattern, clock = setup(0.1, child=False)
    secret = Control("secret")
    if scope in {"descendant", "truncated"}:
        edit.children = [secret]
        secret.parent, secret._index = edit, 0

    def private():
        if scope == "unknown":
            edit.IsPassword = None
        elif scope == "self":
            edit.IsPassword = True
        else:
            secret.IsPassword = True

    if when == "before":
        private()
    elif when == "write":
        pattern.on_write = private
    elif when == "getter":
        pattern.on_read = lambda: private() if pattern.writes else None
    else:
        clock.on_sleep = private
    kwargs = {"depth": 0} if scope == "truncated" else {}
    if scope == "truncated":
        kwargs["selector"] = None
    response = write(service, **kwargs)
    assert response["status"] == "verified"
    assert response["data"]["value_redacted"] is True
    assert response["data"]["before"] is response["data"]["after"] is None
    assert "old" not in str(response) and "new" not in str(response)


def test_identity_mutated_by_successful_getter_cannot_be_verified():
    service, root, edit, pattern, clock = setup()
    pattern.on_read = lambda: setattr(edit, "runtime_id", [20, 999]) if pattern.writes else None
    response = write(service)
    assert response["status"] == "unknown"
    assert response["error"]["code"] == "STALE_TARGET"
    details = response["error"]["details"]
    assert details == {
        "stage": "set_value_verification",
        "node_index": 0,
        "predicate": "candidate_runtime_id_changed",
    }
    assert failure_details(details) == details
    assert pattern.writes == ["new"] and len(pattern.reads) == 2


def test_expiry_does_not_start_final_privacy_or_identity_queries():
    service, root, edit, pattern, clock = setup(10)
    privacy = service._aggregate_value_safe
    queries = []

    def slow_if_expired(*args, **kwargs):
        queries.append(clock())
        if clock() >= 100.12:
            clock.now += 0.2
        return privacy(*args, **kwargs)

    service._aggregate_value_safe = slow_if_expired
    response = write(service, timeout=0.12)
    assert response["status"] == "unknown"
    assert response["error"]["code"] == "READBACK_TIMEOUT"
    assert response["may_have_acted"] is True
    assert clock.now == pytest.approx(100.12)
    assert all(start < 100.12 for start in queries)
    assert response["data"]["before"] is response["data"]["after"] is None
    assert response["data"]["value_redacted"] is True
    assert pattern.writes == ["new"]


@pytest.mark.parametrize("stage", ["setter", "getter", "identity", "walk", "password"])
def test_provider_call_crossing_expiry_starts_no_followup_query(stage, monkeypatch):
    service, root, edit, pattern, clock = setup()
    events = []
    crossed = False

    def event(name):
        nonlocal crossed
        events.append((name, clock()))
        if pattern.writes and name == stage and not crossed:
            crossed = True
            clock.now += 0.2

    original_id = Control.GetRuntimeId
    original_child = Control.GetFirstChildControl
    original_password = service._password

    def runtime_id(control):
        event("identity")
        return original_id(control)

    def children(control):
        event("walk")
        return original_child(control)

    def password(control):
        event("password")
        return original_password(control)

    monkeypatch.setattr(Control, "GetRuntimeId", runtime_id)
    monkeypatch.setattr(Control, "GetFirstChildControl", children)
    service._password = password
    pattern.on_write = lambda: event("setter")
    pattern.on_read = lambda: event("getter")
    response = write(service, timeout=0.12)
    assert crossed
    assert response["status"] == "unknown"
    assert response["error"]["code"] == "READBACK_TIMEOUT"
    assert response["may_have_acted"] is True
    assert clock.now == pytest.approx(100.2)
    assert all(start < 100.12 for name, start in events)
    assert response["data"]["before"] is response["data"]["after"] is None
    assert response["data"]["value_redacted"] is True
    assert response["data"]["pattern_returned"] is True
    assert pattern.writes == ["new"]
    assert len(pattern.reads) == (2 if stage == "getter" else 1)


@pytest.mark.parametrize(
    "stage",
    [
        "resolve",
        "handle",
        "root_pid",
        "binding_identity",
        "binding_runtime",
        "walk",
        "match",
        "token_runtime",
        "token_metadata",
    ],
)
def test_prewrite_discovery_expiry_starts_no_followup_provider_read(stage, monkeypatch):
    service, root, edit, pattern, clock = setup()
    token = None
    if stage.startswith("token"):
        token = service.execute({"action": "inspect", "target": "App", "selector": "edit"})["data"][
            "token"
        ]
        pattern.reads.clear()
    events = []
    counts = {}
    crossed = False
    trigger = {
        "resolve": ("resolve", 1),
        "handle": ("handle", 1),
        "root_pid": ("ProcessId", 1),
        "binding_identity": ("identity", 1),
        "binding_runtime": ("runtime", 1),
        "walk": ("children", 1),
        "match": ("Name", 1),
        "token_runtime": ("runtime", 3),
        "token_metadata": ("Name", 1),
    }[stage]

    def event(name):
        nonlocal crossed
        events.append((name, clock()))
        counts[name] = counts.get(name, 0) + 1
        if (name, counts[name]) == trigger:
            crossed = True
            clock.now += 0.2

    original_getattribute = Control.__getattribute__

    def properties(control, name):
        if name in {"ProcessId", "Name", "AutomationId", "ControlTypeName"}:
            event(name)
        return original_getattribute(control, name)

    def instrument(owner, method, name):
        original = getattr(owner, method)

        def call(*args, **kwargs):
            event(name)
            return original(*args, **kwargs)

        monkeypatch.setattr(owner, method, call)

    instrument(service.win32, "resolve_window", "resolve")
    instrument(service.auto, "ControlFromHandle", "handle")
    instrument(service, "_window_identity", "identity")
    instrument(root, "GetRuntimeId", "runtime")
    instrument(root, "GetFirstChildControl", "children")
    instrument(edit, "GetFirstChildControl", "children")
    monkeypatch.setattr(Control, "__getattribute__", properties)
    kwargs = {"token": token, "selector": None} if token else {}
    response = write(service, timeout=0.12, **kwargs)
    assert crossed
    assert response["status"] == "refused"
    assert response["error"]["code"] == "DEADLINE_EXCEEDED"
    assert response.get("may_have_acted", False) is False
    assert all(start < 100.12 for name, start in events)
    assert clock.now == pytest.approx(100.2)
    assert not pattern.writes and not pattern.reads
    if token:
        assert token in service._tokens  # Deadline is not stale-token evidence.
