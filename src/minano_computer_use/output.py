"""Hard limits for JSON evidence, including its duplicated MCP text representation."""

import copy
import json

DEFAULT_CHARS = 24000
DEFAULT_BYTES = 65536


def validate_budget(chars=DEFAULT_CHARS, bytes=DEFAULT_BYTES):
    for name, value in (("max_output_chars", chars), ("max_output_bytes", bytes)):
        if type(value) is not int or not 2048 <= value <= 1048576:
            raise ValueError(f"{name} must be an integer in [2048, 1048576]")
    return chars, bytes


def _encoded(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False)


def _fits(value, chars, byte_limit):
    text = _encoded(value)
    # The server emits JSON once as text and again as structuredContent. Account
    # for text escaping too; images have a separate resolution/allocation budget.
    wire = _encoded(
        {"content": [{"type": "text", "text": text}], "structuredContent": value, "isError": True}
    )
    return len(wire) + 32 <= chars and len(wire.encode("utf-8")) + 32 <= byte_limit


def _clip(value, string_limit):
    if isinstance(value, str):
        return value if len(value) <= string_limit else value[:string_limit] + "…"
    if isinstance(value, list):
        return [_clip(item, string_limit) for item in value]
    if isinstance(value, dict):
        identity = {
            "name",
            "automation_id",
            "control_type",
            "token",
            "element_token",
            "target_binding",
            "selector_suggestion",
        }
        return {
            key: (item if key in identity else _clip(item, string_limit))
            for key, item in value.items()
        }
    return value


def _lists(value):
    found = []
    if isinstance(value, dict):
        for key, item in value.items():
            if key in {"target_binding", "selector_suggestion", "token", "element_token"}:
                continue
            if isinstance(item, list) and key in {"data", "nodes", "matches", "results", "windows"}:
                found.append((value, key, item))
            if isinstance(item, dict):
                found.extend(_lists(item))
    return found


def _guard_node_references(value):
    """Invalidate omitted sibling-node evidence, copying only changed containers."""
    guarded = value
    if isinstance(value, dict):
        nodes = value.get("nodes")
        for key, item in value.items():
            if key in {"target_binding", "selector_suggestion", "token", "element_token"}:
                continue
            if (
                isinstance(nodes, list)
                and key in {"focus", "selection"}
                and isinstance(item, dict)
                and "node_index" in item
                and (
                    type(item["node_index"]) is not int or not 0 <= item["node_index"] < len(nodes)
                )
            ):
                replacement = {
                    "status": "unavailable",
                    "reason": "REFERENCED_NODE_OMITTED",
                }
            else:
                replacement = _guard_node_references(item)
            if replacement is not item:
                if guarded is value:
                    guarded = value.copy()
                guarded[key] = replacement
    elif isinstance(value, list):
        for index, item in enumerate(value):
            replacement = _guard_node_references(item)
            if replacement is not item:
                if guarded is value:
                    guarded = value.copy()
                guarded[index] = replacement
    return guarded


def _failure_summaries(value):
    """Extract compact failure facts before any large list is reduced."""
    found = []
    if isinstance(value, dict):
        for key, item in value.items():
            if key == "results" and isinstance(item, list):
                for entry in item:
                    if not isinstance(entry, dict) or not isinstance(entry.get("result"), dict):
                        continue
                    outcome = entry["result"]
                    if outcome.get("status") not in {"refused", "error", "unknown", "partial"}:
                        continue
                    summary = {
                        "index": entry.get("index"),
                        "result": {
                            name: outcome[name]
                            for name in (
                                "status",
                                "operation",
                                "method",
                                "may_have_acted",
                                "cancelled",
                            )
                            if name in outcome
                        },
                    }
                    error = outcome.get("error")
                    if isinstance(error, dict):
                        summary["result"]["error"] = {"code": str(error.get("code", "ERROR"))[:80]}
                    found.append(summary)
            elif isinstance(item, dict):
                found.extend(_failure_summaries(item))
    return found


def _drop_identity_fields(value):
    """Drop whole identity values only; never manufacture a partial selector."""
    if isinstance(value, dict):
        for key in (
            "selector_suggestion",
            "target_binding",
            "name",
            "automation_id",
            "control_type",
            "token",
            "element_token",
        ):
            value.pop(key, None)
        for item in value.values():
            _drop_identity_fields(item)
    elif isinstance(value, list):
        for item in value:
            _drop_identity_fields(item)


def bound_result(value: dict, max_output_chars=DEFAULT_CHARS, max_output_bytes=DEFAULT_BYTES):
    """Never use output truncation to decide selector uniqueness or mutation success."""
    chars, byte_limit = validate_budget(max_output_chars, max_output_bytes)
    if not isinstance(value, dict):
        raise TypeError("Result must be a JSON object")
    value = _guard_node_references(value)
    if _fits(value, chars, byte_limit):
        return value
    saved_failures = _failure_summaries(value)
    bounded = copy.deepcopy(value)
    bounded["output_truncated"] = True
    bounded["output_limits"] = {"max_chars": chars, "max_bytes": byte_limit}
    if saved_failures:
        bounded["failure_summary"] = saved_failures[0]
    # Shorten data strings before dropping evidence. Opaque tokens are never cut.
    bounded = _clip(bounded, min(512, chars // 16))
    error = bounded.get("error")
    if isinstance(error, dict) and isinstance(error.get("message"), str):
        error["message"] = error["message"][:256]
    # Keep the action index and failure reason instead of dropping a late failure
    # merely because its data is larger than the earlier successful entries.
    for owner, key, items in _lists(bounded):
        if key != "results":
            continue
        summaries = []
        for item in items:
            if not isinstance(item, dict):
                continue
            action = item.get("result", {})
            if not isinstance(action, dict):
                continue
            summary = {
                name: action[name]
                for name in (
                    "status",
                    "operation",
                    "method",
                    "may_have_acted",
                    "cancelled",
                )
                if name in action
            }
            if isinstance(action.get("error"), dict):
                summary["error"] = {"code": action["error"].get("code", "ERROR")}
            summaries.append({"index": item.get("index"), "result": summary})
        failure_statuses = {"error", "refused", "unknown", "partial"}
        owner[key] = [
            item for item in summaries if item["result"].get("status") in failure_statuses
        ] + [item for item in summaries if item["result"].get("status") not in failure_statuses]
        owner["action_details_omitted"] = True
    while not _fits(bounded, chars, byte_limit):
        candidates = [item for item in _lists(bounded) if len(item[2]) > 1]
        if not candidates:
            break
        owner, key, items = max(candidates, key=lambda entry: len(_encoded(entry[2])))
        # Tree nodes are DFS parent-before-child: retaining a prefix cannot
        # introduce a parent_index pointing to an omitted node.
        if key == "results":
            # A compact first failure is more useful than a prefix of successful
            # actions. The complete failure list was saved before reduction.
            local_failures = _failure_summaries({"results": items})
            owner[key] = local_failures[:1] or items[:1]
            owner["action_details_omitted"] = True
            bounded = _guard_node_references(bounded)
            continue
        keep = max(1, len(items) // 2)
        owner[key] = items[:keep]
        owner["output_items_omitted"] = owner.get("output_items_omitted", 0) + len(items) - keep
        if key in {"nodes", "matches"}:
            owner["output_truncated"] = True
        bounded = _guard_node_references(bounded)
    if _fits(bounded, chars, byte_limit):
        return bounded
    minimal = {
        key: bounded[key]
        for key in (
            "status",
            "operation",
            "method",
            "delivery",
            "call_id",
            "may_have_acted",
            "cancelled",
            "replayed",
        )
        if key in bounded
    }
    minimal["output_truncated"] = True
    minimal["data_omitted"] = True
    if isinstance(error, dict):
        minimal["error"] = {
            "code": str(error.get("code", "ERROR"))[:64],
            "message": str(error.get("message", ""))[:128],
        }
    # Preserve aggregate counters when individual action records cannot fit.
    data = bounded.get("data")
    if isinstance(data, dict):
        counters = {
            key: data[key]
            for key in (
                "requested",
                "attempted",
                "completed",
                "stopped_early",
            )
            if isinstance(data.get(key), (int, bool))
        }
        if counters:
            minimal["data"] = counters
    if saved_failures:
        minimal.setdefault("data", {})["first_failure"] = saved_failures[0]
    # Identity is indivisible: if the rich result can't fit, this summary omits
    # it entirely rather than returning a modified reusable Name/AutomationId.
    if not _fits(minimal, chars, byte_limit):
        _drop_identity_fields(minimal)
    if not _fits(minimal, chars, byte_limit):
        raise ValueError("Result envelope exceeds the configured output budget")
    return minimal
