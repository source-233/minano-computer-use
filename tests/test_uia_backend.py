import json
import subprocess
import sys
from types import SimpleNamespace as NS

import pytest

from minano_computer_use.uia_backend import UIABackend


class FakeWin32:
    windows = [{"hwnd": 10, "pid": 20, "title": "App"}]

    def __init__(self):
        self.focus = 10
        self.cursor = [1, 2]
        self.creation = 1234

    def window_identity(self, hwnd, pid):
        return {"hwnd": hwnd, "pid": pid, "process_creation_time": self.creation}

    def resolve_window(self, target):
        assert target == "App"
        return self.windows[0]

    def list_windows(self):
        return self.windows.copy()

    def observations(self):
        return {"foreground_hwnd": self.focus, "cursor": self.cursor.copy()}


class Control:
    def __init__(self, name, *, pid=20, children=(), patterns=None, auto=None, control_type=1):
        self.Name, self.AutomationId, self.ControlType = name, name.lower(), control_type
        self.ControlTypeName, self.ProcessId = "ButtonControl", pid
        self.IsPassword = False
        self.ClassName, self.LocalizedControlType = "FakeControl", "button"
        self.NativeWindowHandle = 10
        self.IsEnabled, self.IsOffscreen = True, False
        self.HasKeyboardFocus, self.IsKeyboardFocusable = False, True
        self.BoundingRectangle = NS(left=1, top=2, right=30, bottom=40)
        self.children, self.patterns = list(children), patterns or {}
        self.auto = auto
        self.parent = None
        self.runtime_id = [20, id(self)]
        for i, child in enumerate(self.children):
            child.parent, child._index = self, i
            child.auto = auto

    def GetFirstChildControl(self):
        return self.children[0] if self.children else None

    def GetNextSiblingControl(self):
        siblings = self.parent.children
        return siblings[self._index + 1] if self._index + 1 < len(siblings) else None

    def GetPattern(self, pattern_id):
        return self.patterns.get(pattern_id)

    def GetRuntimeId(self):
        return self.runtime_id

    def GetParentControl(self):
        return self.parent


class FakeAuto:
    PatternId = NS(
        InvokePattern=1,
        ValuePattern=2,
        TogglePattern=3,
        SelectionItemPattern=4,
        ExpandCollapsePattern=5,
        ScrollItemPattern=6,
        ScrollPattern=7,
        TextPattern=8,
    )
    ControlType = NS(ButtonControl=1)
    ScrollAmount = NS(
        SmallIncrement=4, SmallDecrement=1, LargeIncrement=3, LargeDecrement=0, NoAmount=2
    )

    def __init__(self, root):
        self.root = root

    def ControlFromHandle(self, hwnd):
        assert hwnd == 10
        return self.root


def backend(root, win=None):
    win = win or FakeWin32()
    auto = FakeAuto(root)
    root.auto = auto
    for child in root.children:
        child.auto = auto
    return UIABackend(auto=auto, win32=win)


def run(root, action="inspect", **kwargs):
    service = backend(root)
    response = service.execute({"action": action, "target": "App", **kwargs})
    service.close()
    return response


def test_list_without_target_uses_win32_only():
    service = UIABackend(auto=object(), win32=FakeWin32())
    response = service.execute({"action": "list", "target": None})
    service.close()
    assert response["status"] == "ok"
    assert response["method"] == "win32"
    assert response["data"]["windows"] == FakeWin32.windows
    assert response["observations"]["foreground_changed"] is False
    json.dumps(response)


def test_ambiguous_exact_name_refused():
    root = Control("root", children=[Control("Save"), Control("Save")])
    assert run(root, selector="Save")["error"]["code"] == "AMBIGUOUS_SELECTOR"


def test_truncated_search_fails_closed():
    root = Control("root", children=[Control("wanted"), Control("more")])
    response = run(root, selector="wanted", max_nodes=2)
    assert response["status"] == "refused"
    assert response["error"]["code"] == "SEARCH_TRUNCATED"


def test_unsupported_pattern_never_falls_back_to_input():
    root = Control("root", children=[Control("press")])
    response = run(root, "invoke", selector="press")
    assert response["status"] == "refused"
    assert response["error"]["code"] == "UNSUPPORTED_PATTERN"


def test_set_value_mismatch_is_not_success():
    class Value:
        IsReadOnly = False
        Value = "old"

        def SetValue(self, value, waitTime=0):
            self.Value = "provider ignored"
            return True

    control = Control("edit", patterns={2: Value()})
    response = run(Control("root", children=[control]), "set_value", selector="edit", value="new")
    assert response["status"] == "unknown"
    assert response["error"]["code"] == "READBACK_TIMEOUT"
    assert response["may_have_acted"] is True
    assert response["data"]["before"] is response["data"]["after"] is None
    assert response["data"]["value_redacted"] is True


def test_invoke_is_invoked_not_verified_and_reports_focus_change():
    class Invoke:
        def Invoke(self, waitTime=0):
            win.focus = 99
            return True

    win = FakeWin32()
    auto = FakeAuto(Control("root", children=[Control("run", patterns={1: Invoke()})]))
    service = UIABackend(auto=auto, win32=win)
    response = service.execute({"action": "invoke", "target": "App", "selector": "run"})
    service.close()
    assert response["status"] == "invoked"
    assert response["observations"]["foreground_changed"] is True
    assert response["observations"]["cursor_changed"] is False


def test_duplicate_can_be_disambiguated_by_explicit_index():
    root = Control("root", children=[Control("Save"), Control("Save")])
    response = run(root, selector={"name": "Save", "index": 1})
    assert response["status"] == "ok"
    assert response["data"]["name"] == "Save"


def test_pattern_exception_after_mutation_is_unknown():
    class Invoke:
        def Invoke(self, waitTime=0):
            raise RuntimeError("provider disconnected")

    response = run(
        Control("root", children=[Control("run", patterns={1: Invoke()})]), "invoke", selector="run"
    )
    assert response["status"] == "unknown"
    assert response["may_have_acted"] is True


def test_bad_owner_thread_rejected():
    service = backend(Control("root"))
    service._owner = (service._owner[0], -1)
    with pytest.raises(RuntimeError):
        service.execute({"action": "inspect", "target": "App"})


@pytest.mark.parametrize("selector", ["save", {"name": "Save", "automation_id": "wrong"}])
def test_selectors_are_exact_case_sensitive_and_anded(selector):
    response = run(Control("root", children=[Control("Save")]), selector=selector)
    assert response["error"]["code"] == "ELEMENT_NOT_FOUND"


def test_control_type_maps_external_name_to_enum():
    response = run(
        Control("root", children=[Control("Save")]),
        selector={"name": "Save", "control_type": "Button"},
    )
    assert response["status"] == "ok"


def test_depth_truncation_fails_closed_even_with_explicit_index():
    root = Control("root", children=[Control("Save")])
    response = run(root, selector={"index": 0}, depth=0)
    assert response["error"]["code"] == "SEARCH_TRUNCATED"


def test_tree_can_return_honestly_truncated_json():
    root = Control("root", children=[Control("Save")])
    response = run(root, "tree", depth=0)
    assert response["status"] == "ok"
    assert response["data"]["truncated"] is True
    assert len(response["data"]["nodes"]) == 1
    json.dumps(response)


def test_read_exception_is_error_not_unknown():
    class BrokenControl(Control):
        def GetFirstChildControl(self):
            raise RuntimeError("provider unavailable")

    response = run(BrokenControl("root"), "tree")
    assert response["status"] == "error"
    assert response["may_have_acted"] is False


def test_stale_pid_refused():
    assert run(Control("root", pid=99))["error"]["code"] == "STALE_TARGET"


def test_set_value_exact_readback_verifies():
    class Value:
        IsReadOnly, Value = False, "old"

        def SetValue(self, value, waitTime=0):
            self.Value = value
            return True

    response = run(Control("edit", patterns={2: Value()}), "set_value", value="new")
    assert response["status"] == "verified"
    assert response["data"]["after"] == "new"


def test_toggle_reads_transition():
    class Toggle:
        ToggleState = 0

        def Toggle(self, waitTime=0):
            self.ToggleState = 1
            return True

    response = run(Control("box", patterns={3: Toggle()}), "toggle")
    assert response["status"] == "verified"


def test_select_reads_selection_state():
    class Selection:
        IsSelected = False

        def Select(self, waitTime=0):
            self.IsSelected = True
            return True

    response = run(Control("item", patterns={4: Selection()}), "select")
    assert response["status"] == "verified"


@pytest.mark.parametrize("action", ["expand", "collapse"])
def test_expand_collapse_false_does_not_mask_failed_state(action):
    pattern = NS(
        ExpandCollapseState=0 if action == "expand" else 1,
        Expand=lambda **kwargs: False,
        Collapse=lambda **kwargs: False,
    )
    response = run(Control("branch", patterns={5: pattern}), action)
    assert response["status"] == "unknown"


def test_scroll_into_view_is_only_invoked():
    pattern = NS(ScrollIntoView=lambda **kwargs: True)
    assert run(Control("item", patterns={6: pattern}), "scroll_into_view")["status"] == "invoked"


@pytest.mark.parametrize(
    "direction, expected",
    [
        ("down", (2, 4)),
        ("up", (2, 1)),
        ("left", (1, 2)),
        ("right", (4, 2)),
    ],
)
def test_scroll_uses_semantic_enum_and_verifies_direction(direction, expected):
    class Scroll:
        HorizontallyScrollable, VerticallyScrollable = True, True
        HorizontalScrollPercent, VerticalScrollPercent = 50.0, 50.0

        def Scroll(self, horizontal, vertical, waitTime=0):
            assert (horizontal, vertical) == expected
            self.HorizontalScrollPercent += 1 if horizontal == 4 else -1 if horizontal == 1 else 0
            self.VerticalScrollPercent += 1 if vertical == 4 else -1 if vertical == 1 else 0
            return True

    response = run(Control("pane", patterns={7: Scroll()}), "scroll", direction=direction)
    assert response["status"] == "verified"


def test_text_reads_text_pattern_without_input():
    pattern = NS(DocumentRange=NS(GetText=lambda maximum: "hello"))
    response = run(Control("document", patterns={8: pattern}), "text")
    assert response["data"]["nodes"][0]["text"] == "hello"


def test_import_is_lazy_in_fresh_interpreter():
    process = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys; import minano_computer_use.uia_backend; "
            "assert 'uiautomation' not in sys.modules; assert 'comtypes' not in sys.modules",
        ],
        capture_output=True,
        text=True,
    )
    assert process.returncode == 0, process.stderr
    assert process.stdout == ""


def test_mta_set_before_initialization_and_extra_init_balanced(monkeypatch):
    calls = []
    auto = FakeAuto(Control("root"))
    auto.InitializeUIAutomationInCurrentThread = lambda: calls.append(("init", sys.coinit_flags))
    auto.UninitializeUIAutomationInCurrentThread = lambda: calls.append(("close", sys.coinit_flags))
    monkeypatch.setitem(sys.modules, "uiautomation", auto)
    monkeypatch.delitem(sys.modules, "comtypes", raising=False)
    monkeypatch.setattr(sys, "coinit_flags", 2, raising=False)
    service = UIABackend(win32=FakeWin32())
    service.close()
    service.close()
    assert calls == [("init", 0), ("close", 0)]
