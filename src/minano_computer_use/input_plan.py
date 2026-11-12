"""Pure native action normalization shared by the API and input backend."""

from __future__ import annotations

import math
from typing import Any

NATIVE_NAMES = frozenset(
    {
        "key",
        "hold",
        "text",
        "paste",
        "move",
        "look",
        "click",
        "mouse_down",
        "mouse_up",
        "drag",
        "wheel",
        "key_down",
        "key_up",
        "focus",
        "force_focus",
    }
)
EXTENDED_KEYS = frozenset(
    {
        0x21,
        0x22,
        0x23,
        0x24,
        0x25,
        0x26,
        0x27,
        0x28,
        0x2D,
        0x2E,
        0x2C,
        0x5B,
        0x5C,
        0x5D,
        0x6F,
        0x90,
        0xA3,
        0xA5,
    }
)
_KEYS = {
    "BACKSPACE": 8,
    "BACK": 8,
    "TAB": 9,
    "CLEAR": 12,
    "ENTER": 13,
    "RETURN": 13,
    "SHIFT": 16,
    "CTRL": 17,
    "CONTROL": 17,
    "ALT": 18,
    "MENU": 18,
    "PAUSE": 19,
    "BREAK": 19,
    "CAPSLOCK": 20,
    "CAPS_LOCK": 20,
    "ESC": 27,
    "ESCAPE": 27,
    "SPACE": 32,
    "SPACEBAR": 32,
    "PAGEUP": 33,
    "PAGE_UP": 33,
    "PGUP": 33,
    "PAGEDOWN": 34,
    "PAGE_DOWN": 34,
    "PGDN": 34,
    "END": 35,
    "HOME": 36,
    "LEFT": 37,
    "UP": 38,
    "RIGHT": 39,
    "DOWN": 40,
    "PRINTSCREEN": 44,
    "PRINT_SCREEN": 44,
    "PRTSC": 44,
    "INSERT": 45,
    "INS": 45,
    "DELETE": 46,
    "DEL": 46,
    "WIN": 91,
    "WINDOWS": 91,
    "LWIN": 91,
    "SUPER": 91,
    "META": 91,
    "RWIN": 92,
    "APPS": 93,
    "CONTEXT_MENU": 93,
    "MULTIPLY": 106,
    "ADD": 107,
    "SEPARATOR": 108,
    "SUBTRACT": 109,
    "DECIMAL": 110,
    "DIVIDE": 111,
    "NUMLOCK": 144,
    "SCROLLLOCK": 145,
    "LSHIFT": 160,
    "RSHIFT": 161,
    "LCTRL": 162,
    "RCTRL": 163,
    "LCONTROL": 162,
    "RCONTROL": 163,
    "LALT": 164,
    "RALT": 165,
    "SHIFTLEFT": 160,
    "SHIFTRIGHT": 161,
    "CONTROLLEFT": 162,
    "CONTROLRIGHT": 163,
    "ALTLEFT": 164,
    "ALTRIGHT": 165,
    "ALTGR": 165,
    "LEFT_SHIFT": 160,
    "RIGHT_SHIFT": 161,
    "LEFT_CTRL": 162,
    "RIGHT_CTRL": 163,
    "LEFT_ALT": 164,
    "RIGHT_ALT": 165,
    "NUM_LOCK": 144,
    "SCROLL_LOCK": 145,
    "NUMPADMULTIPLY": 106,
    "NUMPADADD": 107,
    "NUMPADSUBTRACT": 109,
    "NUMPADDECIMAL": 110,
    "NUMPADDIVIDE": 111,
    "BROWSER_BACK": 166,
    "BROWSER_FORWARD": 167,
    "BROWSER_REFRESH": 168,
    "BROWSER_STOP": 169,
    "BROWSER_SEARCH": 170,
    "BROWSER_FAVORITES": 171,
    "BROWSER_HOME": 172,
    "VOLUME_MUTE": 173,
    "VOLUME_DOWN": 174,
    "VOLUME_UP": 175,
    "MEDIA_NEXT": 176,
    "MEDIA_PREV": 177,
    "MEDIA_STOP": 178,
    "MEDIA_PLAY_PAUSE": 179,
    ";": 186,
    "SEMICOLON": 186,
    "=": 187,
    "EQUAL": 187,
    "EQUALS": 187,
    ",": 188,
    "COMMA": 188,
    "-": 189,
    "MINUS": 189,
    ".": 190,
    "PERIOD": 190,
    "/": 191,
    "SLASH": 191,
    "`": 192,
    "BACKTICK": 192,
    "GRAVE": 192,
    "[": 219,
    "LEFTBRACKET": 219,
    "BRACKETLEFT": 219,
    "\\": 220,
    "BACKSLASH": 220,
    "]": 221,
    "RIGHTBRACKET": 221,
    "BRACKETRIGHT": 221,
    "'": 222,
    "QUOTE": 222,
    "APOSTROPHE": 222,
}
_SHIFTED = dict(
    zip(
        '!@#$%^&*()_+{}|:"<>?~',
        [
            49,
            50,
            51,
            52,
            53,
            54,
            55,
            56,
            57,
            48,
            189,
            187,
            219,
            221,
            220,
            186,
            222,
            188,
            190,
            191,
            192,
        ],
        strict=True,
    )
)
_KEYS.update(_SHIFTED)
_KEYS.update(PLUS=187, COLON=186, UNDERSCORE=189, TILDE=192)


def key_code(value: Any) -> int:
    if isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= 255:
        return value
    if not isinstance(value, str):
        raise ValueError("key must be a VK integer or supported name")
    name = value.strip().upper()
    if len(name) == 1 and name.isascii() and name.isalnum():
        return ord(name)
    if name.startswith("ARROW"):
        name = name[5:]
    if name.startswith("F") and name[1:].isdigit() and 1 <= int(name[1:]) <= 24:
        return 111 + int(name[1:])
    for prefix in ("NUMPAD", "NUM", "KP"):
        suffix = name.removeprefix(prefix).lstrip("_")
        if name.startswith(prefix) and len(suffix) == 1 and suffix.isdigit():
            return 96 + int(suffix)
    if name in _KEYS:
        return _KEYS[name]
    raise ValueError(f"unsupported key name: {value}")


def _int(value: Any, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not -(2**31) <= value < 2**31:
        raise ValueError(f"{name} must be a signed 32-bit integer")
    return value


def bounded_seconds(value: Any, name: str = "seconds", maximum: float = 10) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a number")
    try:
        value = float(value)
    except OverflowError as exc:
        raise ValueError(f"{name} exceeds bounds") from exc
    if not math.isfinite(value) or not 0 <= value <= maximum:
        raise ValueError(f"{name} must be 0..{maximum}")
    return value


def _chord(value: Any) -> list[int]:
    if isinstance(value, str) and "+" in value and value != "+":
        value = [part.strip() for part in value.split("+")]
    values = value if isinstance(value, list) else [value]
    if not values or len(values) > 32:
        raise ValueError("key chord must contain 1..32 keys")
    keys = [key_code(key) for key in values]
    if len(set(keys)) != len(keys):
        raise ValueError("key chord contains duplicates")
    if any(isinstance(key, str) and key.strip() in _SHIFTED for key in values):
        if not any(key in {16, 160, 161} for key in keys):
            keys.insert(0, 16)
    return keys


def _point(value: Any) -> dict:
    if isinstance(value, dict) and set(value) in ({"coordinate"}, {"coordinates"}):
        value = value.get("coordinate", value.get("coordinates"))
    if isinstance(value, (list, tuple)) and len(value) == 2:
        value = dict(zip(("x", "y"), value, strict=True))
    if not isinstance(value, dict) or set(value) != {"x", "y"}:
        raise ValueError("point must contain exactly x and y")
    return {key: _int(value[key], key) for key in ("x", "y")}


def normalize_native_action(action: dict) -> dict:
    """Validate the entire action without resolving targets or performing effects."""
    if not isinstance(action, dict):
        raise ValueError("action must be an object")
    if any(not isinstance(key, str) for key in action):
        raise ValueError("action field names must be strings")
    data = dict(action)

    def alias(canonical, *aliases):
        present = [key for key in (canonical, *aliases) if key in data]
        if len(present) > 1:
            raise ValueError(f"conflicting aliases for {canonical}")
        if present and present[0] != canonical:
            data[canonical] = data.pop(present[0])

    alias("action", "type")
    name = data.get("action")
    aliases = {
        "mouse_move": "move",
        "mouse_move_relative": "look",
        "hold_key": "hold",
        "type": "text",
        "scroll": "wheel",
        "left_click": "click",
        "right_click": "click",
        "middle_click": "click",
        "double_click": "click",
        "triple_click": "click",
        "left_mouse_down": "mouse_down",
        "left_mouse_up": "mouse_up",
        "right_mouse_down": "mouse_down",
        "right_mouse_up": "mouse_up",
        "middle_mouse_down": "mouse_down",
        "middle_mouse_up": "mouse_up",
    }
    if not isinstance(name, str):
        raise ValueError("action name is required")
    original = name
    name = aliases.get(name, name)
    if name not in NATIVE_NAMES:
        raise ValueError("unsupported native action")
    data["action"] = name
    if original.startswith(("left_", "right_", "middle_")):
        button = original.split("_")[0]
        if "button" in data and data["button"] != button:
            raise ValueError("conflicting button alias")
        data["button"] = button
    if original in {"double_click", "triple_click"}:
        clicks = 2 if original == "double_click" else 3
        if "clicks" in data and data["clicks"] != clicks:
            raise ValueError("conflicting click count")
        data["clicks"] = clicks
    alias("seconds", "duration")
    if name in {"key", "hold", "key_down", "key_up"}:
        alias("key", "keys", "text")
    else:
        alias("key", "keys")
        alias("text", "value")
    alias("coordinates", "coordinate")
    if "coordinates" in data:
        if "x" in data or "y" in data:
            raise ValueError("conflicting coordinate aliases")
        point = _point(data.pop("coordinates"))
        if name == "look":
            if "dx" in data or "dy" in data:
                raise ValueError("conflicting relative coordinates")
            data.update(dx=point["x"], dy=point["y"])
        else:
            data.update(point)
    if original == "mouse_move_relative" and ("x" in data or "y" in data):
        alias("dx", "x")
        alias("dy", "y")
    allowed = {"action"}
    if name in {"key", "hold", "key_down", "key_up"}:
        allowed |= {"key"}
        keys = _chord(data.get("key"))
        if name in {"key_down", "key_up"}:
            data["key"] = keys[0] if len(keys) == 1 else keys
        else:
            allowed.add("seconds")
            data["key"] = keys
            data["seconds"] = bounded_seconds(data.get("seconds", 0.1 if name == "hold" else 0))
    elif name in {"text", "paste"}:
        allowed |= {"text"}
        text = data.get("text")
        if not isinstance(text, str) or len(text) > 10000:
            raise ValueError("text must be a string of at most 10000 characters")
        if name == "paste" and "\0" in text:
            raise ValueError("paste text must not contain NUL characters")
        try:
            text.encode("utf-16-le")
        except UnicodeError as exc:
            raise ValueError("text contains invalid Unicode") from exc
        if name == "text":
            allowed.add("literal")
            if not isinstance(data.get("literal", False), bool):
                raise ValueError("literal must be boolean")
            data.setdefault("literal", False)
    elif name == "look":
        allowed |= {"dx", "dy"}
        data.update(dx=_int(data.get("dx"), "dx"), dy=_int(data.get("dy"), "dy"))
    elif name in {"move", "click", "mouse_down", "mouse_up", "drag"}:
        allowed |= {"x", "y"}
        if name in {"move", "drag"} or "x" in data or "y" in data:
            data.update(_point({"x": data.get("x"), "y": data.get("y")}))
        if name != "move":
            allowed.add("button")
            data.setdefault("button", "left")
            if not isinstance(data["button"], str) or data["button"] not in {
                "left",
                "right",
                "middle",
                "x1",
                "x2",
            }:
                raise ValueError("unsupported mouse button")
        if name == "click":
            allowed |= {"clicks", "modifiers"}
            clicks = _int(data.get("clicks", 1), "clicks")
            if not 1 <= clicks <= 3:
                raise ValueError("clicks must be 1..3")
            data["clicks"] = clicks
            if "modifiers" in data and not isinstance(data["modifiers"], (str, list)):
                raise ValueError("modifiers must be a chord string or list")
            data["modifiers"] = _chord(data["modifiers"]) if data.get("modifiers") else []
            if any(
                key not in {16, 17, 18, 91, 92, 160, 161, 162, 163, 164, 165}
                for key in data["modifiers"]
            ):
                raise ValueError("click modifiers must be modifier keys")
        if name == "drag":
            allowed |= {"to", "path", "seconds", "press_dwell_ms"}
            if ("to" in data) == ("path" in data):
                raise ValueError("drag requires exactly one endpoint or path")
            if "to" in data:
                data["to"] = _point(data["to"])
            else:
                path = data["path"]
                if not isinstance(path, list) or not 1 <= len(path) <= 1000:
                    raise ValueError("drag path must contain 1..1000 points")
                data["path"] = [_point(point) for point in path]
            data["seconds"] = bounded_seconds(data.get("seconds", 0.5))
            dwell = bounded_seconds(data.get("press_dwell_ms", 30), "press_dwell_ms", 1000)
            if dwell / 1000 > data["seconds"]:
                raise ValueError("press dwell exceeds total drag duration")
            data["press_dwell_ms"] = dwell
    elif name == "wheel":
        allowed |= {"amount", "horizontal"}
        alias("amount", "delta", "scroll_amount")
        if original == "scroll" or "scroll_amount" in action:
            data["amount"] = _int(data.get("amount"), "scroll_amount") * 120
        data["amount"] = _int(data.get("amount"), "amount")
        alias("scroll_direction", "direction")
        if "scroll_direction" in data:
            if "horizontal" in data:
                raise ValueError("conflicting wheel direction")
            direction = data.pop("scroll_direction")
            if not isinstance(direction, str) or direction not in {
                "vertical",
                "horizontal",
                "up",
                "down",
                "left",
                "right",
            }:
                raise ValueError("unsupported wheel direction")
            data["horizontal"] = direction in {"horizontal", "left", "right"}
            if direction in {"down", "left"}:
                data["amount"] = -data["amount"]
        data.setdefault("horizontal", False)
        if not isinstance(data["horizontal"], bool):
            raise ValueError("horizontal must be boolean")
    unknown = data.keys() - allowed
    if unknown:
        raise ValueError(f"unknown fields for {name}: {', '.join(sorted(unknown))}")
    return data
