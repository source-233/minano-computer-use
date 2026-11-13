"""Membership evidence comes from the original check, without provider re-query."""

from types import SimpleNamespace as NS

import pytest
from test_uia_backend import Control, FakeWin32, backend

from minano_computer_use.uia_backend import Refusal

PRIVATE = "PRIVATE_PROVIDER_NAME_VALUE_SELECTION"


def _raise():
    raise RuntimeError(PRIVATE)


def _execute(service, **request):
    try:
        return service.execute(
            {
                "action": "state",
                "target": "App",
                "target_binding": FakeWin32().window_identity(10, 20),
                **request,
            }
        )
    finally:
        service.close()


def _assert_refused(response, stage, index, predicate, error_type=None):
    details = {"stage": stage, "node_index": index, "predicate": predicate}
    if error_type is not None:
        details["error_type"] = error_type
    assert response["status"] == "refused"
    assert response["error"] == {
        "code": "STALE_TARGET",
        "message": "UIA membership could not be established",
        "details": details,
    }
    assert "data" not in response
    assert PRIVATE not in str(response)


@pytest.mark.parametrize(
    "change,predicate",
    [
        ("root_runtime", "root_runtime_id_changed"),
        ("root_runtime_none", "root_runtime_id_unavailable"),
        ("root_pid", "root_pid_mismatch"),
        ("root_hwnd", "root_hwnd_mismatch"),
        ("root_hwnd_zero", "root_hwnd_zero_or_unavailable"),
        ("root_hwnd_none", "root_hwnd_unavailable"),
        ("candidate_runtime", "candidate_runtime_id_changed"),
        ("candidate_runtime_none", "candidate_runtime_id_unavailable"),
        ("candidate_pid", "candidate_pid_mismatch"),
        ("ancestry_none", "ancestry_unavailable"),
        ("ancestry_cycle", "ancestry_limit"),
        ("foreign_hwnd", "candidate_hwnd_membership_mismatch"),
        ("native_unavailable", "native_membership_unavailable"),
    ],
)
def test_each_live_predicate_retains_its_original_rejection(change, predicate):
    child = Control(PRIVATE)
    root = Control(PRIVATE, children=[child])
    service = backend(root)
    root_id, child_id = tuple(root.runtime_id), tuple(child.runtime_id)
    if change == "root_runtime":
        root.runtime_id = [99]
    elif change == "root_runtime_none":
        root.runtime_id = None
    elif change == "root_pid":
        root.ProcessId = 99
    elif change == "root_hwnd":
        root.NativeWindowHandle = 99
    elif change == "root_hwnd_zero":
        root.NativeWindowHandle = 0
    elif change == "root_hwnd_none":
        root.NativeWindowHandle = None
    elif change == "candidate_runtime":
        child.runtime_id = [99]
    elif change == "candidate_runtime_none":
        child.runtime_id = None
    elif change == "candidate_pid":
        child.ProcessId = 99
    elif change == "ancestry_none":
        child.parent = None
    elif change == "ancestry_cycle":
        child.parent = child
    elif change in {"foreign_hwnd", "native_unavailable"}:
        child.NativeWindowHandle = 99
        if change == "foreign_hwnd":
            service.win32.is_child_window = lambda *args: False
    try:
        with pytest.raises(Refusal) as refusal:
            service._require_state_candidate(
                child,
                child_id,
                root,
                root_id,
                FakeWin32().window_identity(10, 20),
                "focus_before_read",
                1,
            )
        assert refusal.value.code == "STALE_TARGET"
        assert str(refusal.value) == "UIA membership could not be established"
        assert refusal.value.details == {
            "stage": "focus_before_read",
            "node_index": 1,
            "predicate": predicate,
        }
        assert PRIVATE not in str(refusal.value.details)
    finally:
        service.close()


@pytest.mark.parametrize(
    "point,predicate",
    [
        ("root_runtime", "provider_property_unavailable"),
        ("root_pid", "provider_property_unavailable"),
        ("root_hwnd", "provider_property_unavailable"),
        ("candidate_runtime", "provider_property_unavailable"),
        ("candidate_pid", "provider_property_unavailable"),
        ("candidate_hwnd", "provider_property_unavailable"),
        ("parent", "ancestry_unavailable"),
        ("ancestor_runtime", "ancestry_unavailable"),
        ("native", "native_membership_unavailable"),
    ],
)
def test_provider_failure_is_bounded_and_not_requeried(point, predicate):
    reads = []

    class Broken(Control):
        broken_property = ""

        def __getattribute__(self, name):
            if name == object.__getattribute__(self, "broken_property"):
                reads.append(name)
                _raise()
            return super().__getattribute__(name)

    root, child = Broken(PRIVATE), Broken(PRIVATE)
    root.broken_property = child.broken_property = ""
    child.parent = root
    root.children = [child]
    service = backend(root)
    root_id, child_id = tuple(root.runtime_id), tuple(child.runtime_id)
    properties = {"runtime": "GetRuntimeId", "pid": "ProcessId", "hwnd": "NativeWindowHandle"}
    if point.startswith("root_") or point.startswith("candidate_"):
        who, what = point.split("_")
        (root if who == "root" else child).broken_property = properties[what]
    elif point == "parent":
        child.broken_property = "GetParentControl"
    elif point == "ancestor_runtime":
        ancestor = Broken(PRIVATE)
        ancestor.broken_property = "GetRuntimeId"
        child.parent = ancestor
    else:
        child.NativeWindowHandle = 99

        def native(*args):
            reads.append("native")
            _raise()

        service.win32.is_child_window = native
    try:
        with pytest.raises(Refusal) as refusal:
            service._require_state_candidate(
                child,
                child_id,
                root,
                root_id,
                FakeWin32().window_identity(10, 20),
                "focus_before_read",
                1,
            )
        assert refusal.value.details == {
            "stage": "focus_before_read",
            "node_index": 1,
            "predicate": predicate,
            "error_type": "RuntimeError",
        }
        assert len(reads) == 1
        assert PRIVATE not in str(refusal.value.details)
    finally:
        service.close()


def test_first_failure_short_circuits_later_checks():
    class Unreadable(Control):
        @property
        def ProcessId(self):
            raise AssertionError("must not read after RuntimeId mismatch")

        @ProcessId.setter
        def ProcessId(self, value):
            pass

    root = Unreadable(PRIVATE)
    child = Control(PRIVATE)
    service = backend(root)
    try:
        assert service._state_live_candidate(
            child, tuple(child.runtime_id), root, (999,), FakeWin32().window_identity(10, 20)
        ) == {"predicate": "root_runtime_id_changed"}
    finally:
        service.close()


@pytest.mark.parametrize("native_pid,is_child", [(20, True), (99, True), (20, False)])
def test_existing_user32_membership_path_and_short_circuit(native_pid, is_child):
    calls = []

    def process_id(hwnd, pointer):
        calls.append(("pid", hwnd))
        pointer._obj.value = native_pid
        return 1

    def child_check(hwnd, candidate):
        calls.append(("child", hwnd, candidate))
        return is_child

    service = backend(Control("safe"))
    service.win32._user32 = lambda: NS(GetWindowThreadProcessId=process_id, IsChild=child_check)
    child = Control("safe")
    child.parent = service.auto.root
    child.NativeWindowHandle = 99
    try:
        failure = service._state_live_candidate(
            child,
            tuple(child.runtime_id),
            service.auto.root,
            tuple(service.auto.root.runtime_id),
            FakeWin32().window_identity(10, 20),
        )
        assert failure == (
            None
            if native_pid == 20 and is_child
            else {"predicate": "candidate_hwnd_membership_mismatch"}
        )
        assert calls == ([("pid", 99), ("child", 10, 99)] if native_pid == 20 else [("pid", 99)])
    finally:
        service.close()


def test_user32_zero_thread_result_is_unknown_and_never_checks_child():
    calls = []

    def process_id(hwnd, pointer):
        calls.append(("pid", hwnd))
        return 0

    def child_check(*args):
        pytest.fail("failed process lookup must not call IsChild")

    service = backend(Control("safe"))
    service.win32._user32 = lambda: NS(GetWindowThreadProcessId=process_id, IsChild=child_check)
    child = Control(PRIVATE)
    child.parent = service.auto.root
    child.NativeWindowHandle = 99
    try:
        with pytest.raises(Refusal) as refusal:
            service._require_state_candidate(
                child,
                tuple(child.runtime_id),
                service.auto.root,
                tuple(service.auto.root.runtime_id),
                FakeWin32().window_identity(10, 20),
                "focus_before_read",
                1,
            )
        assert refusal.value.code == "STALE_TARGET"
        assert refusal.value.details == {
            "stage": "focus_before_read",
            "node_index": 1,
            "predicate": "native_membership_unavailable",
        }
        assert PRIVATE not in str(refusal.value.details)
        assert calls == [("pid", 99)]
    finally:
        service.close()


@pytest.mark.parametrize("point", ["factory", "pid", "child"])
def test_user32_membership_exception_is_unknown_not_mismatch(point):
    def process_id(hwnd, pointer):
        if point == "pid":
            _raise()
        pointer._obj.value = 20
        return 1

    service = backend(Control("safe"))
    service.win32._user32 = (
        _raise
        if point == "factory"
        else lambda: NS(GetWindowThreadProcessId=process_id, IsChild=lambda *args: _raise())
    )
    child = Control(PRIVATE)
    child.parent = service.auto.root
    child.NativeWindowHandle = 99
    try:
        assert service._state_live_candidate(
            child,
            tuple(child.runtime_id),
            service.auto.root,
            tuple(service.auto.root.runtime_id),
            FakeWin32().window_identity(10, 20),
        ) == {"predicate": "native_membership_unavailable", "error_type": "RuntimeError"}
    finally:
        service.close()


def test_live_provider_exception_preserves_stage_without_extra_reads_or_search():
    reads, lookups = [], []

    class Broken(Control):
        armed = False

        def __getattribute__(self, name):
            if name == "HasKeyboardFocus":
                self.armed = True
                return True
            if name == "ProcessId" and self.armed:
                reads.append(name)
                _raise()
            return super().__getattribute__(name)

    child = Broken(PRIVATE)
    root = Control(PRIVATE, children=[child])
    service = backend(root)
    service.auto.ControlFromHandle = lambda hwnd: lookups.append(hwnd) or root
    _assert_refused(
        _execute(service), "focus_after_read", 1, "provider_property_unavailable", "RuntimeError"
    )
    assert reads == ["ProcessId"]
    assert lookups == [10]


@pytest.mark.parametrize("hwnd", [None, 0, 10, 99])
def test_valid_same_root_candidates_preserve_native_handle_semantics(hwnd):
    child = Control("safe")
    child.NativeWindowHandle = hwnd
    root = Control("safe root", children=[child])
    service = backend(root)
    calls = []
    service.win32.is_child_window = lambda *args: calls.append(args) or True
    response = _execute(service)
    assert response["status"] == "ok"
    assert len(response["data"]["nodes"]) == 2
    assert bool(calls) is (hwnd == 99)
    assert all(call == (10, 99, 20) for call in calls)


@pytest.mark.parametrize(
    "hwnd,predicate", [(None, "root_hwnd_unavailable"), (0, "root_hwnd_zero_or_unavailable")]
)
def test_initial_root_none_does_not_silently_relax_live_root_check(hwnd, predicate):
    root = Control(PRIVATE)
    root.NativeWindowHandle = hwnd
    response = _execute(backend(root))
    stage = "focus_before_read" if hwnd is None else "state_initial_root"
    _assert_refused(response, stage, 0, predicate)


def test_missing_root_hwnd_keeps_original_live_default():
    root = Control("safe")
    del root.NativeWindowHandle
    assert _execute(backend(root))["status"] == "ok"


def test_initial_root_hwnd_exception_keeps_original_later_live_rejection():
    reads = []

    class Broken(Control):
        def __getattribute__(self, name):
            if name == "NativeWindowHandle":
                reads.append(name)
                _raise()
            return super().__getattribute__(name)

    _assert_refused(
        _execute(backend(Broken(PRIVATE))),
        "focus_before_read",
        0,
        "provider_property_unavailable",
        "RuntimeError",
    )
    # The initial exception was ignored by the existing policy. The second read
    # is the original live checkpoint, not a diagnostic retry of the first read.
    assert reads == ["NativeWindowHandle", "NativeWindowHandle"]


@pytest.mark.parametrize("mode", ["none", "pid", "throw", "pid_throw", "runtime_throw"])
def test_initial_root_failures_have_real_checkpoint(mode):
    root = Control(PRIVATE, pid=99 if mode == "pid" else 20)
    service = backend(root)
    if mode == "none":
        service.auto.ControlFromHandle = lambda hwnd: None
    elif mode == "throw":
        service.auto.ControlFromHandle = lambda hwnd: _raise()
    elif mode == "pid_throw":

        class BadPid:
            @property
            def ProcessId(self):
                _raise()

        service.auto.ControlFromHandle = lambda hwnd: BadPid()
    elif mode == "runtime_throw":
        root.GetRuntimeId = _raise
    response = _execute(service)
    _assert_refused(
        response,
        "initial_root_binding" if mode == "runtime_throw" else "initial_root",
        0,
        "root_pid_mismatch"
        if mode == "pid"
        else "provider_property_unavailable"
        if mode in {"pid_throw", "runtime_throw"}
        else "root_unavailable",
        "RuntimeError" if "throw" in mode else None,
    )


@pytest.mark.parametrize("key", ["hwnd", "pid", "process_creation_time"])
def test_initial_expected_binding_mismatch(key):
    expected = FakeWin32().window_identity(10, 20)
    expected[key] += 1
    service = backend(Control(PRIVATE))
    service.auto.ControlFromHandle = lambda hwnd: pytest.fail(
        "binding must fail before root lookup"
    )
    _assert_refused(
        _execute(service, target_binding=expected), "initial_binding", 0, f"binding_{key}_mismatch"
    )


@pytest.mark.parametrize("throws", [False, True])
def test_binding_identity_unknown_is_not_reported_as_mismatch(throws):
    service = backend(Control(PRIVATE))
    service.win32.window_identity = (
        (lambda *args: _raise())
        if throws
        else (lambda hwnd, pid: {"hwnd": hwnd, "pid": pid, "process_creation_time": None})
    )
    _assert_refused(
        _execute(service),
        "initial_binding",
        0,
        "binding_identity_unavailable",
        "RuntimeError" if throws else None,
    )


@pytest.mark.parametrize("which", ["root", "child"])
def test_unavailable_initial_runtime_id_diagnostic(which):
    child = Control(PRIVATE)
    root = Control(PRIVATE, children=[child])
    (root if which == "root" else child).runtime_id = None
    _assert_refused(
        _execute(backend(root)),
        "state_initial_root" if which == "root" else "traversal_identity",
        0 if which == "root" else 1,
        "root_runtime_id_unavailable" if which == "root" else "candidate_runtime_id_unavailable",
    )


def test_dynamic_exception_class_cannot_leak_private_data():
    private_error = type(PRIVATE + "\n" + "x" * 200, (Exception,), {})

    def broken():
        raise private_error(PRIVATE)

    root = Control(PRIVATE)
    root.GetRuntimeId = broken
    response = _execute(backend(root))
    _assert_refused(
        response, "initial_root_binding", 0, "provider_property_unavailable", "ProviderError"
    )


@pytest.mark.parametrize("operation", ["focus", "selection", "text", "metadata"])
def test_runtime_id_change_during_read_discards_all_private_content(operation):
    def change():
        child.runtime_id = [999]

    class Moving(Control):
        def __getattribute__(self, name):
            if (operation, name) in {("focus", "HasKeyboardFocus"), ("metadata", "ClassName")}:
                change()
            return super().__getattribute__(name)

    def selection():
        if operation == "selection":
            change()
        return [NS(GetText=text)]

    def text(maximum):
        if operation == "text":
            change()
        return PRIVATE

    child = Moving(PRIVATE, patterns={8: NS(GetSelection=selection)})
    child.HasKeyboardFocus = True
    root = Control(PRIVATE, children=[child])
    _assert_refused(
        _execute(backend(root)),
        {
            "focus": "focus_after_read",
            "selection": "selection_after_read",
            "text": "text_after_read",
            "metadata": "before_publication",
        }[operation],
        1,
        "candidate_runtime_id_changed",
    )
