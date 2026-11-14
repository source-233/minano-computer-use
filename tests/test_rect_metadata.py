from types import SimpleNamespace as NS

import pytest
from PIL import Image
from test_capture import FakeGrabber
from test_win32 import Api

from minano_computer_use import win32
from minano_computer_use.capture import CaptureService
from minano_computer_use.coordinates import CoordinateError, validate_window


@pytest.mark.parametrize(
    "awareness,space",
    [
        (0, "dpi_virtualized_screen"),
        (1, "system_dpi_screen"),
        (2, "physical_screen"),
        (-1, "win32_screen_unknown"),
    ],
)
def test_window_rect_labels_unchanged_ltrb_with_observed_thread_awareness(awareness, space):
    def rectangle(hwnd, out):
        out._obj.left, out._obj.top, out._obj.right, out._obj.bottom = 79, 64, 414, 604
        return True

    def pid(hwnd, out):
        out._obj.value = 20
        return 1

    user = NS(
        IsWindow=lambda _: True,
        GetWindowTextLengthW=lambda _: 0,
        GetWindowTextW=lambda *a: 0,
        GetWindowThreadProcessId=pid,
        GetWindowRect=rectangle,
        IsWindowVisible=lambda _: True,
        IsIconic=lambda _: False,
        GetThreadDpiAwarenessContext=Api(lambda: -1),
        GetAwarenessFromDpiAwarenessContext=Api(lambda _: awareness),
    )
    entry = win32._window_entry(10, user)
    assert entry["rect"] == [79, 64, 414, 604]
    assert entry["rect"][2] - entry["rect"][0] == 335
    assert entry["rect"][3] - entry["rect"][1] == 540
    assert entry["rect_format"] == "left_top_right_bottom"
    assert entry["rect_space"] == space
    assert entry["rect_source"] == "GetWindowRect"


def test_unknown_thread_awareness_never_claims_physical_or_dip():
    assert win32.rect_coordinate_space(NS()) == "win32_screen_unknown"
    user = NS(
        GetThreadDpiAwarenessContext=Api(lambda: 0),
        GetAwarenessFromDpiAwarenessContext=Api(lambda _: pytest.fail("Null context")),
    )
    assert win32.rect_coordinate_space(user) == "win32_screen_unknown"


def test_equal_rect_numbers_in_different_spaces_do_not_validate_as_same_geometry():
    current = {
        "hwnd": 10,
        "pid": 20,
        "process_creation_time": 100,
        "rect": [1, 2, 3, 4],
        "rect_space": "dpi_virtualized_screen",
    }
    expected = {**current, "rect_space": "physical_screen"}
    with pytest.raises(CoordinateError, match="coordinate space changed"):
        validate_window(expected, current)
    validate_window(expected, current, geometry=False)  # Cleanup does not use coordinates.


def test_surface_region_and_resize_scaling_are_explicit_and_do_not_rescale_rect():
    camera = CaptureService(grabber=FakeGrabber())
    try:
        frame = camera.screenshot(region=[10, 20, 110, 70], max_dim=50)
        m = frame.metadata
        assert m["surface_bounds"] == [0, 0, 400, 200]
        assert m["surface_bounds_space"] == m["coordinate_space"] == "surface_pixels"
        assert m["surface_bounds_format"] == m["region_format"] == "left_top_right_bottom"
        assert m["region_result_space"] == "surface_pixels"
        assert m["scale_from"] == "cropped_surface_pixels" and m["scale_to"] == "output_pixels"
        assert m["scale"] == [0.5, 0.5]
        assert m["physical_transform"]["origin"] == [10, 20]
    finally:
        camera.close()


@pytest.mark.parametrize("capture_mode", ["printwindow", "screen"])
def test_window_capture_distinguishes_window_rect_space_from_surface(monkeypatch, capture_mode):
    from minano_computer_use import coordinates, window_capture

    window = {
        "hwnd": 10,
        "pid": 20,
        "process_creation_time": 1234,
        "rect": [79, 64, 414, 604],
        "rect_format": "left_top_right_bottom",
        "rect_space": "dpi_virtualized_screen",
        "visible": True,
        "dpi": 144,
    }
    monkeypatch.setattr(coordinates, "window_snapshot", lambda _: dict(window))
    monkeypatch.setattr(
        window_capture,
        "capture_window",
        lambda *a: {"size": [502, 810], "data": bytes(502 * 810 * 4)},
    )

    class Grabber:
        monitors = [{"left": 0, "top": 0, "width": 2000, "height": 2000}]

        def grab(self, region):
            return Image.new("RGB", (region["width"], region["height"]))

    camera = CaptureService(grabber=Grabber())
    try:
        m = camera.screenshot(target="App", capture_mode=capture_mode).metadata
        assert m["window_rect"] == [79, 64, 414, 604]
        assert m["window_rect_format"] == "left_top_right_bottom"
        assert m["window_rect_space"] == "dpi_virtualized_screen"
        assert m["surface_bounds_space"] == "surface_pixels"
        assert m["physical_transform"] is None
        if capture_mode == "printwindow":
            assert m["surface_size"] == [502, 810]
            assert m["physical_transform"] is None  # Do not invent a DPI ratio.
    finally:
        camera.close()


@pytest.mark.parametrize("stage", ["before", "after", "timestamp", "missing"])
def test_native_descendant_identity_rejects_changed_or_unverifiable_relation(monkeypatch, stage):
    checks = []

    def child(parent, handle):
        checks.append(handle)
        return not (stage == "before" or stage == "after" and len(checks) > 1)

    def pid(hwnd, out):
        out._obj.value = 20
        return 1

    user = NS(IsWindow=lambda _: True, GetWindowThreadProcessId=pid, IsChild=Api(child))
    monkeypatch.setattr(win32, "_user32", lambda: user)
    monkeypatch.setattr(
        win32,
        "window_identity",
        lambda hwnd, pid: {
            "hwnd": hwnd,
            "pid": pid,
            "process_creation_time": None if stage == "timestamp" else 100,
        },
    )
    if stage == "missing":
        user.GetWindowThreadProcessId = lambda *a: 0
    with pytest.raises(win32.TargetError):
        win32.child_window_identity(10, 20, 30, 40)


def test_native_child_identity_allows_a_different_process_with_same_native_tree(monkeypatch):
    def pid(hwnd, out):
        out._obj.value = 20
        return 1

    user = NS(
        IsWindow=lambda _: True,
        GetWindowThreadProcessId=pid,
        IsChild=Api(lambda parent, child: (parent, child) == (10, 30)),
    )
    monkeypatch.setattr(win32, "_user32", lambda: user)
    monkeypatch.setattr(
        win32,
        "window_identity",
        lambda hwnd, pid: {"hwnd": hwnd, "pid": pid, "process_creation_time": 5678},
    )
    assert win32.child_window_identity(10, 20, 30, 40) == {
        "hwnd": 30,
        "pid": 40,
        "process_creation_time": 5678,
    }
