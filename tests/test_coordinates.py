from __future__ import annotations

import copy

import pytest

from minano_computer_use.coordinates import (
    CoordinateError,
    CoordinateManager,
    resolve_window_target,
)


def metadata(**overrides):
    return {
        "target": "desktop",
        "output_size": [100, 50],
        "physical_transform": {
            "origin": [-200, 20],
            "output_to_physical_scale": [2, 3],
        },
        **overrides,
    }


def window(**overrides):
    return {
        "hwnd": 10,
        "pid": 20,
        "process_creation_time": 1234,
        "rect": [-200, 20, 0, 170],
        "visible": True,
        "minimized": False,
        "dpi": 144,
        **overrides,
    }


def test_screen_is_default_and_is_not_implicitly_scaled():
    actions = [{"action": "click", "x": -4, "y": 9000}]
    manager = CoordinateManager()
    assert manager.map_actions(actions) == actions
    assert manager.map_actions(actions) is not actions


def test_remember_is_immutable_latest_and_scoped():
    manager = CoordinateManager(resolver=lambda _: copy.deepcopy(window()))
    source = metadata(target="desktop")
    first = manager.remember(source, scope="alice")
    source["physical_transform"]["origin"][0] = 999
    action = [{"action": "click", "x": 3, "y": 4}]
    assert manager.map_actions(action, "image", first, target="hwnd:10", scope="alice")[0] == {
        "action": "click",
        "x": -194,
        "y": 32,
    }
    with pytest.raises(CoordinateError, match="scope"):
        manager.map_actions(action, "image", first, target="hwnd:10", scope="bob")
    second = manager.remember(metadata(), scope="alice")
    assert second != first
    with pytest.raises(CoordinateError, match="stale"):
        manager.map_actions(action, "image", first, target="hwnd:10", scope="alice")
    assert manager.map_actions(action, "image", target="hwnd:10", scope="alice")[0]["x"] == -194


def test_map_all_absolute_points_without_mutating_plan_or_relative_look():
    manager = CoordinateManager(resolver=lambda _: copy.deepcopy(window()))
    capture_id = manager.remember(metadata(target="desktop"))
    actions = [
        {
            "action": "drag",
            "x": 1,
            "y": 2,
            "to": {"x": 5, "y": 6},
            "path": [[7, 8], {"x": 9, "y": 10}],
        },
        {"action": "look", "x": -3000, "y": 5000},
        {"action": "click", "coordinate": [10, 12]},
    ]
    result = manager.map_actions(actions, "image", capture_id, target="hwnd:10")
    assert result[0] == {
        "action": "drag",
        "x": -198,
        "y": 26,
        "to": {"x": -190, "y": 38},
        "path": [[-186, 44], {"x": -182, "y": 50}],
    }
    assert result[1] == actions[1]
    assert result[2]["coordinate"] == [-180, 56]
    assert actions[0]["x"] == 1
    assert type(result[0]["x"]) is int


@pytest.mark.parametrize(
    "point", [[-1, 0], [100, 0], [0, 50], [float("nan"), 0], [float("inf"), 0], [True, 0]]
)
def test_out_of_bounds_and_nonfinite_points_never_clamp(point):
    manager = CoordinateManager(resolver=lambda _: copy.deepcopy(window()))
    capture_id = manager.remember(metadata())
    with pytest.raises(CoordinateError):
        manager.map_actions(
            [{"action": "move", "x": point[0], "y": point[1]}],
            "image",
            capture_id,
            target="hwnd:10",
        )


@pytest.mark.parametrize(
    "transform",
    [
        None,
        {},
        {"origin": [0, 0], "output_to_physical_scale": [0, 1]},
        {"origin": [0, float("inf")], "output_to_physical_scale": [1, 1]},
    ],
)
def test_unknown_or_invalid_transform_refuses(transform):
    manager = CoordinateManager(resolver=lambda _: copy.deepcopy(window()))
    manager.remember(metadata(physical_transform=transform))
    with pytest.raises(CoordinateError):
        manager.map_actions([{"action": "click", "x": 0, "y": 0}], "image", target="hwnd:10")


@pytest.mark.parametrize(
    "change",
    [
        {"pid": 21},
        {"hwnd": 11},
        {"process_creation_time": 1235},
        {"rect": [-199, 20, 1, 170]},
        {"dpi": 96},
        {"visible": False},
        {"minimized": True},
    ],
)
def test_window_mapping_revalidates_identity_geometry_and_dpi(change):
    live = window()
    manager = CoordinateManager(resolver=lambda _: copy.deepcopy(live))
    manager.remember(metadata(target="hwnd:10", **window(), window_rect=window()["rect"]))
    live.update(change)
    with pytest.raises(CoordinateError):
        manager.map_actions([{"action": "click", "x": 1, "y": 1}], "image")


def test_window_target_binding_and_missing_creation_identity_refuse():
    manager = CoordinateManager(
        resolver=lambda target: window(hwnd=11 if target == "other" else 10)
    )
    manager.remember(metadata(target="title", **window(), window_rect=window()["rect"]))
    assert manager.map_actions([{"x": 1, "y": 1}], "image", target="hwnd:10")[0]["x"] == -198
    with pytest.raises(CoordinateError, match="target"):
        manager.map_actions([{"x": 1, "y": 1}], "image", target="other")
    manager.remember(metadata(target="title", **window(process_creation_time=None)))
    with pytest.raises(CoordinateError, match="creation"):
        manager.map_actions([{"x": 1, "y": 1}], "image")


def test_ratio_viewport_follows_same_window_move_and_resize_not_title():
    live = window(title="First title")
    calls = []

    def resolve(target):
        calls.append(target)
        assert target == "hwnd:10"
        return copy.deepcopy(live)

    manager = CoordinateManager(resolver=resolve)
    source = metadata(target="First title", **window(), window_rect=window()["rect"])
    manager.define_viewport("roi", source, region=[25, 10, 75, 40], scope="owner")
    source["hwnd"] = 999
    live.update(rect=[100, 200, 500, 500], title="Changed title", dpi=192)
    resolved = manager.resolve("viewport:roi", scope="owner")
    assert resolved["target"] == "hwnd:10"
    assert resolved["region_space"] == "screen"
    assert resolved["region"] == {"x": 200, "y": 260, "w": 200, "h": 180}
    assert manager.list_viewports("other") == []
    listed = manager.list_viewports("owner")
    listed[0]["ratios"][0] = 55
    assert manager.resolve("viewport:roi", "owner")["region"]["x"] == 200
    live["process_creation_time"] += 1
    with pytest.raises(CoordinateError, match="identity"):
        manager.resolve("viewport:roi", "owner")
    assert manager.clear_viewport("roi", "owner")
    assert not manager.clear_viewport("roi", "owner")
    assert calls and set(calls) == {"hwnd:10"}


def test_desktop_viewport_physical_crop_and_scope():
    manager = CoordinateManager()
    manager.define_viewport("crop", metadata(), region=[10, 10, 30, 20])
    assert manager.resolve("viewport:crop")["region"] == {"x": -180, "y": 50, "w": 40, "h": 30}
    with pytest.raises(CoordinateError, match="unknown"):
        manager.resolve("viewport:crop", "other")


@pytest.mark.parametrize(
    "region",
    [
        [10, 20, 30, 40],
        {"left": 10, "top": 20, "right": 30, "bottom": 40},
        {"x": 10, "y": 20, "w": 20, "h": 20},
        {"left": 10, "top": 20, "width": 20, "height": 20},
    ],
)
def test_map_region_accepts_rectangle_aliases_and_inclusive_edges(region):
    manager = CoordinateManager()
    capture_id = manager.remember(metadata())
    assert manager.map_region(region, capture_id=capture_id) == {
        "x": -180,
        "y": 80,
        "w": 40,
        "h": 60,
    }
    assert manager.map_region(
        {"left": 0, "top": 0, "right": 100, "bottom": 50}, capture_id=capture_id
    ) == {"x": -200, "y": 20, "w": 200, "h": 150}
    with pytest.raises(CoordinateError, match="out of bounds"):
        manager.map_region({"left": 0, "top": 0, "right": 101, "bottom": 50}, capture_id=capture_id)


def test_map_region_requires_known_transform_and_latest_capture():
    manager = CoordinateManager()
    with pytest.raises(CoordinateError, match="capture_id"):
        manager.map_region([0, 0, 1, 1])
    manager.remember(metadata(physical_transform=None))
    with pytest.raises(CoordinateError, match="physical"):
        manager.map_region([0, 0, 1, 1])
    old = manager.remember(metadata(), scope="a")
    manager.remember(metadata(), scope="a")
    with pytest.raises(CoordinateError, match="stale"):
        manager.map_region([0, 0, 1, 1], scope="a", capture_id=old)
    with pytest.raises(CoordinateError, match="scope"):
        manager.map_region([0, 0, 1, 1], scope="b")
    with pytest.raises(CoordinateError, match="target"):
        manager.map_region([0, 0, 1, 1], target="display:0", scope="a")


def test_map_region_window_must_remain_same_identity_geometry_and_known_transform():
    live = window()
    manager = CoordinateManager(resolver=lambda _: copy.deepcopy(live))
    manager.remember(metadata(target="hwnd:10", **window(), window_rect=window()["rect"]))
    assert manager.map_region([0, 0, 100, 50], target="hwnd:10") == {
        "x": -200,
        "y": 20,
        "w": 200,
        "h": 150,
    }
    live["rect"] = [-199, 20, 1, 170]
    with pytest.raises(CoordinateError, match="geometry"):
        manager.map_region([0, 0, 100, 50], target="hwnd:10")
    live = window()
    manager.remember(metadata(target="hwnd:10", **window(), physical_transform=None))
    with pytest.raises(CoordinateError, match="physical_transform"):
        manager.map_region([0, 0, 100, 50], target="hwnd:10")


@pytest.mark.parametrize("surface", ["desktop", "display:0", "region"])
def test_screen_surface_image_input_accepts_only_explicit_live_hwnd_recipient(surface):
    live = window()
    calls = []

    def resolver(target):
        calls.append(target)
        assert target == "hwnd:10"
        return copy.deepcopy(live)

    manager = CoordinateManager(resolver=resolver)
    capture_id = manager.remember(metadata(target=surface))
    mapped = manager.map_actions(
        [{"action": "click", "x": 1, "y": 2}],
        coordinate_space="image",
        capture_id=capture_id,
        target="hwnd:0xa",
    )
    assert mapped == [{"action": "click", "x": -198, "y": 26}]
    assert calls == ["hwnd:10", "hwnd:10"]
    for target in (None, "desktop", "display:0", "viewport:screen", "foreground"):
        with pytest.raises(CoordinateError, match="explicit concrete"):
            manager.map_actions(
                [{"action": "click", "x": 1, "y": 2}],
                coordinate_space="image",
                capture_id=capture_id,
                target=target,
            )


@pytest.mark.parametrize(
    "change",
    [
        {"pid": 21},
        {"hwnd": 11},
        {"process_creation_time": 1235},
        {"rect": [-199, 20, 0, 170]},
        {"dpi": 96},
        {"visible": False},
        {"minimized": True},
    ],
)
def test_screen_surface_recipient_identity_geometry_and_creation_are_revalidated(change):
    calls = 0

    def resolver(target):
        nonlocal calls
        assert target == "hwnd:10"
        calls += 1
        return window(**change) if calls > 1 else window()

    manager = CoordinateManager(resolver=resolver)
    capture_id = manager.remember(metadata(target="display:0"))
    with pytest.raises(CoordinateError):
        manager.map_actions([{"x": 1, "y": 2}], "image", capture_id, "hwnd:10")
    assert calls == 2


def test_screen_surface_roi_still_requires_source_target_alignment():
    manager = CoordinateManager(resolver=lambda _: copy.deepcopy(window()))
    capture_id = manager.remember(metadata(target="display:0"))
    with pytest.raises(CoordinateError, match="target"):
        manager.map_region([0, 0, 1, 1], target="hwnd:10", capture_id=capture_id)


def test_exact_title_process_and_hwnd_resolution_are_independent_of_services(monkeypatch):
    from minano_computer_use import coordinates, win32

    windows = [window(title="Exact"), window(hwnd=11, pid=21, title="Exact extra")]
    monkeypatch.setattr(win32, "list_windows", lambda: copy.deepcopy(windows))
    monkeypatch.setattr(
        win32,
        "resolve_window",
        lambda target: next(copy.deepcopy(w) for w in windows if target == f"hwnd:{w['hwnd']}"),
    )
    monkeypatch.setattr(
        coordinates, "_process_name", lambda pid: "app.exe" if pid == 20 else "other.exe"
    )
    assert resolve_window_target("window:Exact")["hwnd"] == 10
    assert resolve_window_target("window:process:app.exe")["hwnd"] == 10
    assert resolve_window_target("window:app.exe")["hwnd"] == 10
    assert resolve_window_target("window:hwnd:11")["hwnd"] == 11
    with pytest.raises(win32.TargetError, match="exactly one"):
        resolve_window_target("window:Exac")
    windows[1]["title"] = "Exact"
    with pytest.raises(win32.TargetError) as error:
        resolve_window_target("window:Exact")
    assert error.value.code == "AMBIGUOUS_TARGET"


def test_exact_process_ambiguity_refuses_multiple_windows(monkeypatch):
    from minano_computer_use import coordinates, win32

    monkeypatch.setattr(win32, "list_windows", lambda: [window(), window(hwnd=11)])
    monkeypatch.setattr(coordinates, "_process_name", lambda _: "app.exe")
    with pytest.raises(win32.TargetError) as error:
        resolve_window_target("window:process:APP.EXE")
    assert error.value.code == "AMBIGUOUS_TARGET"
