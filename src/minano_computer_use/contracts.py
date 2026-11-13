"""Small JSON-only contract shared by the broker and effect backends."""

import math
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

# Provider diagnostics are facts, never a channel for exception text or values.
FAILURE_PREDICATES = frozenset(
    {
        "binding_identity_unavailable",
        "binding_hwnd_mismatch",
        "binding_pid_mismatch",
        "binding_process_creation_time_mismatch",
        "root_unavailable",
        "root_runtime_id_unavailable",
        "root_runtime_id_changed",
        "root_pid_mismatch",
        "root_hwnd_mismatch",
        "root_hwnd_unavailable",
        "root_hwnd_zero_or_unavailable",
        "candidate_runtime_id_unavailable",
        "candidate_runtime_id_changed",
        "candidate_pid_mismatch",
        "ancestry_unavailable",
        "ancestry_limit",
        "candidate_hwnd_membership_mismatch",
        "native_membership_unavailable",
        "provider_property_unavailable",
        "token_binding_changed",
        "token_metadata_changed",
    }
)
FAILURE_STAGES = frozenset(
    {
        "initial_binding",
        "binding_before_publication",
        "initial_root",
        "initial_root_binding",
        "state_initial_root",
        "traversal_identity",
        "focus_before_read",
        "focus_after_read",
        "selection_before_pattern",
        "selection_before_read",
        "selection_after_read",
        "text_before_read",
        "text_after_read",
        "metadata_before_read",
        "before_publication",
        "root_before_publication",
        "token_before_issue",
        "token_after_issue",
        "token_before_publication",
        "token_after_metadata",
        "token_binding_before_publication",
        "set_value_initial_control",
        "set_value_verification",
    }
)
FAILURE_ERROR_TYPES = frozenset(
    {
        "COMError",
        "OSError",
        "RuntimeError",
        "ValueError",
        "TypeError",
        "AttributeError",
        "TimeoutError",
        "PermissionError",
        "ReferenceError",
        "ProviderError",
    }
)


def failure_details(value):
    """Retain only frozen diagnostic vocabulary and bounded node evidence."""
    if not isinstance(value, dict):
        return {}
    facts = {}
    for key, allowed in (
        ("predicate", FAILURE_PREDICATES),
        ("stage", FAILURE_STAGES),
        ("error_type", FAILURE_ERROR_TYPES),
    ):
        item = value.get(key)
        if isinstance(item, str) and item in allowed:
            facts[key] = item
    if type(value.get("node_index")) is int and -1 <= value["node_index"] <= 5000:
        facts["node_index"] = value["node_index"]
    return facts


def _receipt_event_facts(value):
    """Fixed dispatch fields only; recipient identity is not a target binding."""
    if not isinstance(value, dict):
        return {}
    facts = {}
    for key, maximum in (
        ("requested_vk", 0xFFFF),
        ("w_vk", 0xFFFF),
        ("w_scan", 0xFFFF),
        ("flags", 0xFFFFFFFF),
    ):
        item = value.get(key)
        if type(item) is int and 0 <= item <= maximum:
            facts[key] = item
        elif key == "requested_vk" and key in value and item is None:
            facts[key] = None
    if value.get("phase") in ("down", "up", "other", "cleanup"):
        facts["phase"] = value["phase"]
    if "accepted" in value and (value["accepted"] is None or type(value["accepted"]) is bool):
        facts["accepted"] = value["accepted"]
    for key, maximum in (("recipient_hwnd", 0xFFFFFFFFFFFFFFFF), ("recipient_pid", 0xFFFFFFFF)):
        item = value.get(key)
        if type(item) is int and 0 < item <= maximum:
            facts[key] = item
    return facts


def receipt_facts(value, *, include_recipients=False, _phase=False):
    """Aggregate facts by default; opt-in public dispatch events never include text/context."""
    if not isinstance(value, dict):
        return {}
    facts = {}
    counts = (
        ("requested", "accepted", "unknown")
        if _phase
        else ("requested_events", "accepted_events", "unknown_events", "events_omitted")
    )
    for key in counts:
        item = value.get(key)
        if key in value and (item is None or type(item) is int and 0 <= item <= 1048576):
            facts[key] = item
    for key in ("requested_seconds", "dispatch_interval_seconds"):
        item = value.get(key)
        if key in value and (
            item is None
            or type(item) in (int, float)
            and math.isfinite(item)
            and 0 <= item <= 86400
        ):
            facts[key] = item
    for key in ("may_have_acted", "cancelled", "consumption_verified", "release_unconfirmed"):
        if type(value.get(key)) is bool:
            facts[key] = value[key]
    phases = value.get("phases")
    if isinstance(phases, dict) and not _phase:
        facts["phases"] = {
            key: receipt_facts(phases[key], _phase=True)
            for key in ("down", "up", "other", "cleanup")
            if isinstance(phases.get(key), dict)
        }
    events = value.get("events")
    if include_recipients and not _phase and isinstance(events, list):
        facts["events"] = [
            _receipt_event_facts(event) for event in events[:64] if isinstance(event, dict)
        ]
        if len(events) > 64:
            facts["events_omitted"] = facts.get("events_omitted", 0) or 0
            facts["events_omitted"] += len(events) - 64
    return facts


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
