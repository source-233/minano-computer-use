import copy

import pytest

from minano_computer_use.input_plan import key_code, normalize_native_action


@pytest.mark.parametrize(
    "old,new,extra",
    [
        ("mouse_move", "move", {"coordinates": [-20, 30]}),
        ("mouse_move_relative", "look", {"coordinates": [-20, 30]}),
        ("hold_key", "hold", {"keys": "ctrl+a", "duration": 0.1}),
        ("type", "text", {"value": "hello"}),
        ("left_click", "click", {"x": 1, "y": 2}),
        ("right_click", "click", {"x": 1, "y": 2}),
        ("middle_click", "click", {"x": 1, "y": 2}),
        ("double_click", "click", {"x": 1, "y": 2}),
        ("triple_click", "click", {"x": 1, "y": 2}),
        ("left_mouse_down", "mouse_down", {}),
        ("left_mouse_up", "mouse_up", {}),
        ("right_mouse_down", "mouse_down", {}),
        ("right_mouse_up", "mouse_up", {}),
        ("middle_mouse_down", "mouse_down", {}),
        ("middle_mouse_up", "mouse_up", {}),
        ("scroll", "wheel", {"scroll_amount": 2, "scroll_direction": "horizontal"}),
    ],
)
def test_aliases_pure_and_idempotent(old, new, extra):
    action = {"type": old, **extra}
    before = copy.deepcopy(action)
    normalized = normalize_native_action(action)
    assert normalized["action"] == new
    assert normalize_native_action(normalized) == normalized
    assert action == before


@pytest.mark.parametrize(
    "action",
    [
        {"action": "key", "key": "A", "keys": "A"},
        {"action": "focus", "type": "focus"},
        {"action": "focus", "native": True},
        {"action": "key", "key": ["ctrl", "control"]},
        {"action": "hold", "key": "A", "seconds": float("nan")},
        {"action": "hold", "key": "A", "seconds": True},
        {"action": "look", "dx": 2**31, "dy": 0},
        {"action": "move", "x": 1, "y": 2, "coordinates": [1, 2]},
        {"action": "click", "x": 1},
        {"action": "click", "clicks": 4},
        {"action": "click", "modifiers": ["a"]},
        {"action": "click", "button": "wat"},
        {"action": "drag", "x": 1, "y": 2, "to": [3, 4], "path": [[3, 4]]},
        {"action": "drag", "x": 1, "y": 2, "path": []},
        {"action": "drag", "x": 1, "y": 2, "to": [3, 4], "seconds": 11},
        {"action": "wheel", "amount": 1, "delta": 1},
        {"action": "wheel", "scroll_amount": 2**30},
        {"action": "text", "text": "secret", "literal": 1},
        {"action": "paste", "text": "secret", "literal": True},
        {"action": "text", "text": "\ud800"},
        {"action": "unknown"},
    ],
)
def test_refused_before_effects(action):
    with pytest.raises(ValueError):
        normalize_native_action(action)


def test_raw_wheel_delta_and_legacy_notches():
    assert normalize_native_action({"action": "wheel", "delta": -7})["amount"] == -7
    assert normalize_native_action(
        {"action": "scroll", "scroll_amount": 2, "scroll_direction": "left"}
    ) == {"action": "wheel", "amount": -240, "horizontal": True}


@pytest.mark.parametrize(
    "alias,code",
    [
        ("pgup", 33),
        ("page_down", 34),
        ("home", 36),
        ("end", 35),
        ("insert", 45),
        ("del", 46),
        ("arrowleft", 37),
        ("rctrl", 163),
        ("ralt", 165),
        ("F24", 135),
        ("numpad9", 105),
        ("kp_1", 97),
        (";", 186),
        ("=", 187),
        ("minus", 189),
        ("/", 191),
        ("backslash", 220),
        ("[", 219),
        ("apostrophe", 222),
        ("printscreen", 44),
        ("win", 91),
    ],
)
def test_broad_key_names(alias, code):
    assert key_code(alias) == code


def test_drag_and_chords_canonical_shape():
    assert normalize_native_action({"action": "key_down", "key": "CTRL+SHIFT"})["key"] == [17, 16]
    action = normalize_native_action(
        {"action": "drag", "x": -1, "y": 2, "path": [[4, 5], {"x": 6, "y": 7}], "duration": 1}
    )
    assert action["path"] == [{"x": 4, "y": 5}, {"x": 6, "y": 7}]
    assert action["seconds"] == 1
    assert action["press_dwell_ms"] == 30


@pytest.mark.parametrize("symbol,vk", [("+", 187), ("?", 191), ("!", 49), ("{", 219)])
def test_shifted_punctuation_is_a_real_chord(symbol, vk):
    assert normalize_native_action({"action": "key", "key": symbol})["key"] == [16, vk]


@pytest.mark.parametrize(
    "action",
    [
        {"action": "click", "button": []},
        {"action": "click", "modifiers": 0},
        {"action": "scroll", "scroll_amount": 1, "scroll_direction": []},
        {"action": "focus", 2: "value"},
    ],
)
def test_malformed_json_shapes_raise_valueerror(action):
    with pytest.raises(ValueError):
        normalize_native_action(action)


@pytest.mark.parametrize("name", ["key", "hold_key", "key_down", "key_up"])
def test_exact_old_text_key_payload(name):
    normalized = normalize_native_action({"action": name, "text": "ctrl+s"})
    assert normalized["key"] == [17, 83]
    assert "text" not in normalized
    with pytest.raises(ValueError, match="conflicting aliases"):
        normalize_native_action({"action": name, "key": "ctrl+s", "text": "ctrl+s"})


def test_exact_old_singular_coordinate_and_drag_endpoint():
    assert normalize_native_action({"action": "mouse_move", "coordinate": [-10, 20]}) == {
        "action": "move",
        "x": -10,
        "y": 20,
    }
    drag = normalize_native_action(
        {"action": "drag", "coordinate": [10, 20], "to": {"coordinate": [30, 40]}}
    )
    assert drag["x"] == 10 and drag["y"] == 20
    assert drag["to"] == {"x": 30, "y": 40}
    with pytest.raises(ValueError):
        normalize_native_action({"action": "move", "coordinate": [1, 2], "coordinates": [1, 2]})
    with pytest.raises(ValueError):
        normalize_native_action(
            {"action": "drag", "coordinate": [1, 2], "to": {"coordinate": [3, 4], "x": 3, "y": 4}}
        )


@pytest.mark.parametrize("amount_field", ["amount", "scroll_amount"])
@pytest.mark.parametrize("direction_field", ["direction", "scroll_direction"])
def test_exact_old_scroll_payloads_are_notches(amount_field, direction_field):
    assert normalize_native_action(
        {"action": "scroll", amount_field: 2, direction_field: "left"}
    ) == {"action": "wheel", "amount": -240, "horizontal": True}
    assert normalize_native_action({"action": "wheel", "amount": 2})["amount"] == 2
    assert normalize_native_action({"action": "wheel", "delta": 2})["amount"] == 2
    assert normalize_native_action({"action": "wheel", "scroll_amount": 2})["amount"] == 240


@pytest.mark.parametrize(
    "payload",
    [
        {"action": "scroll", "amount": 1, "scroll_amount": 1},
        {"action": "scroll", "amount": 1, "direction": "up", "scroll_direction": "up"},
        {"action": "scroll"},
    ],
)
def test_old_scroll_conflicts_are_refused(payload):
    with pytest.raises(ValueError):
        normalize_native_action(payload)


@pytest.mark.parametrize("field", ["text", "value"])
def test_paste_nul_rejected_during_pure_normalization_without_disclosing_text(field):
    with pytest.raises(ValueError, match="NUL") as error:
        normalize_native_action({"action": "paste", field: "private\0clipboard"})
    assert "private" not in str(error.value) and "clipboard" not in str(error.value)


def test_complete_batch_normalization_refuses_nul_paste_before_dispatch():
    dispatched = []
    actions = [{"action": "key", "key": "A"}, {"action": "paste", "text": "a\0b"}]
    with pytest.raises(ValueError, match="NUL"):
        plan = [normalize_native_action(action) for action in actions]
        dispatched.extend(plan)
    assert dispatched == []
    # NUL refusal is clipboard-specific, not a change to Unicode input semantics.
    assert normalize_native_action({"action": "text", "text": "a\0b"})["text"] == "a\0b"
