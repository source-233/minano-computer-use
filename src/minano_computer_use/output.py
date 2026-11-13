"""Hard limits for JSON evidence, including its duplicated MCP text representation."""

import copy
import json

from .contracts import failure_details, receipt_facts

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
            "process_creation_time",
            "root_binding",
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


def _guard_receipts(value):
    """Copy only changed containers; full public receipts have bounded safe events."""
    guarded = value
    if isinstance(value, dict):
        for key, item in value.items():
            if key in {"target_binding", "root_binding", "selector_suggestion"}:
                continue
            if key == "receipt" and isinstance(item, dict):
                replacement = receipt_facts(item, include_recipients=True)
                if replacement == item:
                    replacement = item
            else:
                replacement = _guard_receipts(item)
            if replacement is not item:
                if guarded is value:
                    guarded = value.copy()
                guarded[key] = replacement
    elif isinstance(value, list):
        for index, item in enumerate(value):
            replacement = _guard_receipts(item)
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
                    summary = {"index": entry.get("index"), "result": _outcome_summary(outcome)}
                    found.append(summary)
            elif isinstance(item, dict):
                found.extend(_failure_summaries(item))
    return found


def _component_failure_summaries(value, *, source=None):
    """Keep state component facts, never provider text or arbitrary component keys."""
    found = []
    if not isinstance(value, dict):
        return found
    data = value.get("data")
    if not isinstance(data, dict):
        return found
    failures = data.get("component_errors")
    if value.get("operation") == "state" and isinstance(failures, dict):
        for component in ("uia", "screenshot", "binding"):
            failure = failures.get(component)
            if not isinstance(failure, dict) or failure.get("status") not in {
                "refused",
                "error",
                "unknown",
                "partial",
            }:
                continue
            summary = {"component": component, "status": failure["status"]}
            if source is not None:
                summary["source"] = source
            if failure.get("operation") in {"state", "uia", "screenshot"}:
                summary["operation"] = failure["operation"]
            error = failure.get("error")
            if isinstance(error, dict):
                # Codes are static identifiers, not an alternate provider-text channel.
                code = error.get("code")
                if not (
                    isinstance(code, str)
                    and 0 < len(code) <= 80
                    and all(char in "ABCDEFGHIJKLMNOPQRSTUVWXYZ_0123456789" for char in code)
                ):
                    code = "ERROR"
                summary["error"] = _error_summary({**error, "code": code})
            found.append(summary)
    # Follow result envelopes only: similarly named application data is not state evidence.
    for key in ("after", "actions"):
        found.extend(
            _component_failure_summaries(
                data.get(key), source="after" if key == "after" else source
            )
        )
    for entry in data.get("results", []) if isinstance(data.get("results"), list) else []:
        if isinstance(entry, dict):
            found.extend(_component_failure_summaries(entry.get("result"), source=source))
    # At most one record per fixed component; recursive wrappers cannot amplify output.
    unique = {}
    for summary in found:
        unique.setdefault(summary["component"], summary)
    return list(unique.values())


def _error_summary(error):
    summary = {"code": str(error.get("code", "ERROR"))[:80]}
    details = failure_details(error.get("details"))
    if details:
        summary["details"] = details
    return summary


def _outcome_summary(outcome, *, _include_cleanup=True):
    summary = {
        name: outcome[name]
        for name in (
            "status",
            "operation",
            "method",
            "may_have_acted",
            "cancelled",
            "consumption_verified",
            "release_unconfirmed",
        )
        if name in outcome
    }
    if isinstance(outcome.get("error"), dict):
        summary["error"] = _error_summary(outcome["error"])
    receipt = receipt_facts(outcome.get("receipt"))
    if receipt:
        summary["receipt"] = receipt
    cleanup = outcome.get("cleanup")
    if (
        _include_cleanup
        and isinstance(cleanup, dict)
        and cleanup.get("status")
        in ("ok", "invoked", "verified", "refused", "error", "unknown", "partial")
    ):
        summary["cleanup"] = _outcome_summary(cleanup, _include_cleanup=False)
    return summary


def _receipt_core(value):
    """Keep exact delivery totals, including explicitly unknown acceptance."""
    facts = receipt_facts(value)
    return {
        key: facts[key]
        for key in ("requested_events", "accepted_events", "unknown_events")
        if key in facts
    }


def _critical_outcome(outcome, *, _include_cleanup=True):
    """Smallest mutation evidence; optional method/phase/timing facts go first."""
    facts = {
        key: outcome[key]
        for key in ("status", "may_have_acted", "cancelled", "release_unconfirmed")
        if key in outcome
    }
    if isinstance(outcome.get("error"), dict):
        facts["error"] = _error_summary(outcome["error"])
    if isinstance(outcome.get("receipt"), dict):
        facts["receipt"] = _receipt_core(outcome["receipt"])
    cleanup = outcome.get("cleanup")
    if (
        _include_cleanup
        and isinstance(cleanup, dict)
        and cleanup.get("status")
        in ("ok", "invoked", "verified", "refused", "error", "unknown", "partial")
    ):
        facts["cleanup"] = _critical_outcome(cleanup, _include_cleanup=False)
    return facts


def _reduce_feedback(value, *, receipts=False):
    """Discard optional observations before delivery and failure evidence."""
    if isinstance(value, dict):
        data = value.get("data")
        if value.get("operation") == "play" and isinstance(data, dict) and "capture" in data:
            # Capture metadata is an independent auxiliary observation. The MCP
            # frame is handled separately; omit this component before discarding
            # successful action delivery evidence just to retain sample arrays.
            data.pop("capture")
            data["capture_omitted"] = True
        if isinstance(data, dict) and isinstance(data.get("results"), list) and "after" in data:
            data.pop("after")
            data["after_omitted"] = True
        if value.get("status") in {
            "ok",
            "invoked",
            "verified",
            "refused",
            "error",
            "unknown",
            "partial",
        }:
            if "input_context" in value:
                value.pop("input_context")
                value["input_context_omitted"] = True
            if receipts and isinstance(value.get("receipt"), dict):
                value["receipt"] = receipt_facts(value["receipt"])
                value["receipt_details_omitted"] = True
            cleanup = value.get("cleanup")
            if (
                receipts
                and isinstance(cleanup, dict)
                and cleanup.get("status")
                in ("ok", "invoked", "verified", "refused", "error", "unknown", "partial")
            ):
                value["cleanup"] = _outcome_summary(cleanup, _include_cleanup=False)
        for key, item in value.items():
            if key not in {"target_binding", "root_binding", "selector_suggestion"}:
                _reduce_feedback(item, receipts=receipts)
    elif isinstance(value, list):
        for item in value:
            _reduce_feedback(item, receipts=receipts)


def _cleanup_summaries(value):
    found = []
    if isinstance(value, dict):
        for key, item in value.items():
            if key in {"receipt", "events", "input_context"}:
                continue
            if (
                key == "cleanup"
                and isinstance(item, dict)
                and item.get("status")
                in {"ok", "invoked", "verified", "refused", "error", "unknown", "partial"}
            ):
                found.append(_outcome_summary(item))
            elif isinstance(item, (dict, list)):
                found.extend(_cleanup_summaries(item))
    elif isinstance(value, list):
        for item in value:
            found.extend(_cleanup_summaries(item))
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
            "root_binding",
            "process_creation_time",
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
    value = _guard_receipts(_guard_node_references(value))
    if _fits(value, chars, byte_limit):
        return value
    saved_failures = _failure_summaries(value)
    component_failures = _component_failure_summaries(value)
    saved_cleanup = _cleanup_summaries(value)
    bounded = copy.deepcopy(value)
    bounded["output_truncated"] = True
    bounded["output_limits"] = {"max_chars": chars, "max_bytes": byte_limit}
    if component_failures:
        bounded["component_failure_summary"] = component_failures
    _reduce_feedback(bounded)
    if _fits(bounded, chars, byte_limit):
        return bounded
    _reduce_feedback(bounded, receipts=True)
    if _fits(bounded, chars, byte_limit):
        return bounded
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
            summary = _outcome_summary(action)
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
            "consumption_verified",
            "release_unconfirmed",
        )
        if key in bounded
    }
    minimal["output_truncated"] = True
    minimal["data_omitted"] = True
    if isinstance(error, dict):
        minimal["error"] = _error_summary(error)
    if saved_cleanup:
        minimal["cleanup"] = next(
            (
                item
                for item in saved_cleanup
                if item.get("status") in {"error", "unknown", "partial", "refused"}
            ),
            saved_cleanup[0],
        )
    if isinstance(bounded.get("receipt"), dict):
        minimal["receipt"] = receipt_facts(bounded["receipt"])
    # Preserve aggregate counters when individual action records cannot fit.
    data = bounded.get("data")
    if bounded.get("operation") == "play" and isinstance(data, dict):
        actions = data.get("actions")
        data = actions.get("data") if isinstance(actions, dict) else None
    if isinstance(data, dict):
        counters = {
            key: data[key]
            for key in (
                "requested",
                "attempted",
                "completed",
                "stopped_early",
            )
            if type(data.get(key)) is (bool if key == "stopped_early" else int)
        }
        if counters:
            minimal["data"] = counters
    if saved_failures:
        minimal.setdefault("data", {})["first_failure"] = saved_failures[0]
    if component_failures:
        minimal["component_failure_summary"] = component_failures
    # Identity is indivisible: if the rich result can't fit, this summary omits
    # it entirely rather than returning a modified reusable Name/AutomationId.
    if not _fits(minimal, chars, byte_limit):
        _drop_identity_fields(minimal)
    if not _fits(minimal, chars, byte_limit):
        # Phase/event/timing detail is secondary, but accepted=null and explicit
        # unknown totals are primary evidence, not optional receipt decoration.
        if isinstance(minimal.get("receipt"), dict):
            minimal["receipt"] = _receipt_core(minimal["receipt"])
        first = minimal.get("data", {}).get("first_failure", {}).get("result", {})
        if isinstance(first.get("receipt"), dict):
            first["receipt"] = _receipt_core(first["receipt"])
        cleanup = minimal.get("cleanup", {})
        if isinstance(cleanup.get("receipt"), dict):
            cleanup["receipt"] = _receipt_core(cleanup["receipt"])
    if not _fits(minimal, chars, byte_limit):
        if isinstance(minimal.get("cleanup"), dict):
            minimal["cleanup"] = _critical_outcome(minimal["cleanup"])
        failure = minimal.get("data", {}).get("first_failure")
        if isinstance(failure, dict):
            failure["result"] = _critical_outcome(failure["result"])
            nested = failure["result"].get("cleanup")
            if nested is not None and minimal.get("cleanup") == nested:
                # The same release evidence already belongs to this action;
                # don't pay for a second copy in the minimum public envelope.
                minimal.pop("cleanup")
    if not _fits(minimal, chars, byte_limit):
        raise ValueError("Result envelope exceeds the configured output budget")
    return minimal
