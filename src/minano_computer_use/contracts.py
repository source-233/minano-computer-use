"""Small JSON-only contract shared by the broker and effect backends."""

from typing import Any

READ_ACTIONS = frozenset({"list", "tree", "text", "inspect", "find", "wait_for", "state"})
UIA_ACTIONS = READ_ACTIONS | frozenset(
    {
        "invoke",
        "set_value",
        "toggle",
        "select",
        "expand",
        "collapse",
        "scroll_into_view",
        "scroll",
    }
)
NATIVE_ACTIONS = frozenset(
    {
        "focus",
        "force_focus",
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
    }
)
STOP_STATUSES = frozenset({"refused", "error", "unknown", "partial"})


def result(
    status: str,
    operation: str,
    *,
    method: str | None = None,
    data: Any = None,
    code: str | None = None,
    message: str | None = None,
    **extra: Any,
) -> dict[str, Any]:
    """Build a JSON result without conflating invocation and observed effect."""
    value: dict[str, Any] = {"status": status, "operation": operation}
    if method is not None:
        value["method"] = method
    if data is not None:
        value["data"] = data
    if code is not None or message is not None:
        value["error"] = {"code": code or "ERROR", "message": message or ""}
    value.update(extra)
    return value
