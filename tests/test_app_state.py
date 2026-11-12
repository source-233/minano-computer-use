from types import SimpleNamespace as NS

import pytest
from test_uia_backend import Control, FakeWin32, backend


def _bound(win):
    return win.window_identity(10, 20)


def test_state_is_scoped_and_returns_one_traversal_with_timing():
    focused = Control("focused")
    focused.HasKeyboardFocus = True
    root = Control("root", children=[focused])
    service = backend(root)
    response = service.execute(
        {
            "action": "state",
            "target": "App",
            "target_binding": _bound(FakeWin32()),
        }
    )
    service.close()
    data = response["data"]
    assert response["status"] == "ok"
    assert len(data["nodes"]) == 2
    assert data["focus"] == {"status": "available", "node_index": 1}
    assert data["selection"]["status"] == "unavailable"
    assert data["target_binding"] == _bound(FakeWin32())
    assert set(data["componenttimes"]) == {"traversal", "focus", "selection"}
    assert data["consistency"] == "non_atomic"


def test_state_refuses_missing_or_stale_binding_before_traversal():
    root = Control("root")
    service = backend(root)
    missing = service.execute({"action": "state", "target": "App"})
    stale = service.execute(
        {
            "action": "state",
            "target": "App",
            "target_binding": {"hwnd": 10, "pid": 20, "process_creation_time": 999},
        }
    )
    service.close()
    assert missing["error"]["code"] == "INVALID_TARGET_BINDING"
    assert stale["error"]["code"] == "STALE_TARGET"


def test_state_password_and_unknown_password_suppress_selection_and_text():
    class Text:
        def GetSelection(self):
            return [NS(GetText=lambda maximum: "private selection")]

        DocumentRange = NS(GetText=lambda maximum: "private document")

    password = Control("secret", patterns={8: Text()})
    password.IsPassword = True
    password.HasKeyboardFocus = True
    root = Control("root", children=[password])
    service = backend(root)
    response = service.execute(
        {"action": "state", "target": "App", "target_binding": _bound(FakeWin32())}
    )
    service.close()
    assert response["data"]["selection"]["status"] == "redacted"
    text_entry = response["data"]["nodes"][1]
    assert text_entry["is_password"] is True
    assert text_entry["text_redacted"] is True
    assert "private" not in str(response)


def test_selection_caps_ranges_and_text_total():
    ranges = [NS(GetText=lambda maximum: "z" * maximum) for _ in range(20)]

    class Text:
        def GetSelection(self):
            return ranges

    focused = Control("focused", patterns={8: Text()})
    focused.HasKeyboardFocus = True
    root = Control("root", children=[focused])
    service = backend(root)
    response = service.execute(
        {"action": "state", "target": "App", "target_binding": _bound(FakeWin32())}
    )
    service.close()
    selection = response["data"]["selection"]
    assert len(selection["ranges"]) <= 16
    assert sum(map(len, selection["ranges"])) <= 4096
    assert selection["truncated"] is True


def test_state_never_reads_global_out_of_target_focus():
    root = Control("root")
    service = backend(root)
    service.win32.focus = 999
    service.auto.GetFocusedControl = lambda: (_ for _ in ()).throw(
        AssertionError("must not fetch unrelated focus")
    )
    response = service.execute(
        {"action": "state", "target": "App", "target_binding": _bound(FakeWin32())}
    )
    service.close()
    assert response["status"] == "ok"
    assert response["data"]["focus"]["status"] == "unavailable"
    assert response["observations"]["before"]["foreground_hwnd"] == 999
    assert "foreground_hwnd" not in response["data"]["focus"]


def test_state_rejects_dynamically_stale_window_after_reads():
    class Stale(Control):
        def GetFirstChildControl(self):
            win.creation += 1
            return None

    win = FakeWin32()
    service = backend(Stale("root"), win)
    response = service.execute({"action": "state", "target": "App", "target_binding": _bound(win)})
    service.close()
    assert response["status"] == "refused"
    assert response["error"]["code"] == "STALE_TARGET"
    assert "data" not in response


def test_state_explicit_selector_reads_selection_without_focus():
    focused = Control("edit", patterns={8: NS(GetSelection=lambda: [NS(GetText=lambda n: "abc")])})
    root = Control("root", children=[focused])
    service = backend(root)
    response = service.execute(
        {
            "action": "state",
            "target": "App",
            "target_binding": _bound(FakeWin32()),
            "selector": "edit",
        }
    )
    service.close()
    assert response["data"]["focus"]["status"] == "unavailable"
    assert response["data"]["selection"]["ranges"] == ["abc"]


def test_state_unknown_password_redacts_selection_without_provider_read():
    class Text:
        def GetSelection(self):
            raise AssertionError("unknown password selection must not be read")

    root = Control("root", patterns={8: Text()})
    root.HasKeyboardFocus = True
    del root.IsPassword
    service = backend(root)
    response = service.execute(
        {"action": "state", "target": "App", "target_binding": _bound(FakeWin32())}
    )
    service.close()
    assert response["status"] == "ok"
    assert response["data"]["selection"]["status"] == "redacted"
    assert response["data"]["nodes"][0]["is_password"] is None


def test_state_focus_getter_error_is_unavailable_not_false():
    class Broken(Control):
        def __getattribute__(self, name):
            if name == "HasKeyboardFocus":
                raise RuntimeError("PRIVATE_PROVIDER_TEXT")
            return super().__getattribute__(name)

    service = backend(Broken("root"))
    response = service.execute(
        {"action": "state", "target": "App", "target_binding": _bound(FakeWin32())}
    )
    service.close()
    assert response["status"] == "ok"
    assert response["data"]["focus"]["status"] == "unavailable"
    assert response["data"]["nodes"][0]["has_keyboard_focus"] is None
    assert "PRIVATE_PROVIDER_TEXT" not in str(response)


def test_selection_range_count_is_capped_and_never_invokes_focus():
    reads = []
    ranges = [NS(GetText=lambda maximum: reads.append(maximum) or "x") for _ in range(30)]
    root = Control("root", patterns={8: NS(GetSelection=lambda: ranges)})
    root.HasKeyboardFocus = True
    service = backend(root)
    response = service.execute(
        {"action": "state", "target": "App", "target_binding": _bound(FakeWin32())}
    )
    service.close()
    assert len(response["data"]["selection"]["ranges"]) == 16
    assert len(reads) == 16
    assert response["data"]["selection"]["truncated"] is True


@pytest.mark.parametrize("stage", ["focus", "selection", "text", "metadata"])
def test_state_rejects_candidate_detaching_during_provider_reads(stage):
    def detach():
        child.parent = None
        child.NativeWindowHandle = 99

    class Moving(Control):
        def __getattribute__(self, name):
            if name == "HasKeyboardFocus" and stage == "focus":
                detach()
                return True
            if name == "ClassName" and stage == "metadata":
                detach()
                return "OTHER_ROOT_METADATA"
            return super().__getattribute__(name)

    def read_text(maximum):
        if stage == "text":
            detach()
        return "OTHER_ROOT_SELECTION"

    def selection():
        if stage == "selection":
            detach()
        return [NS(GetText=read_text)]

    child = Moving("edit", patterns={8: NS(GetSelection=selection)})
    child.HasKeyboardFocus = True
    root = Control("root", children=[child])
    service = backend(root)
    response = service.execute(
        {"action": "state", "target": "App", "target_binding": _bound(FakeWin32())}
    )
    service.close()
    assert response["status"] == "refused"
    assert response["error"]["code"] == "STALE_TARGET"
    assert "OTHER_ROOT" not in str(response)
    assert "data" not in response


def test_state_rejects_foreign_hwnd_even_if_provider_keeps_original_parent():
    child = Control("edit")
    child.NativeWindowHandle = 99
    child.HasKeyboardFocus = True
    root = Control("root", children=[child])
    service = backend(root)
    service.win32.is_child_window = lambda hwnd, candidate, pid: False
    response = service.execute(
        {"action": "state", "target": "App", "target_binding": _bound(FakeWin32())}
    )
    service.close()
    assert response["error"]["code"] == "STALE_TARGET"
    assert "data" not in response
