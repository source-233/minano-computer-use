"""Bounded cross-process UIA evidence, collected only on the owning UIA thread.

A foreign PID alone is not permission. A live UIA path must contain a native
descendant owned by that element's process. Pins survive as token evidence; a
later validation may never replace an anchor or accept a reused process PID.
"""


def cross_process_pin(
    backend, control, root, expected, original_id, root_id, *, previous=None, checkpoint=None
):
    check = checkpoint or (lambda: None)
    if any(
        type(expected.get(key)) is not int or expected[key] <= 0
        for key in ("hwnd", "pid", "process_creation_time")
    ):
        return None, "binding_identity_unavailable"
    pointer = getattr(control, "_element", None)
    if previous is not None and "pointer" in previous and pointer is not previous["pointer"]:
        return None, "element_process_or_anchor_changed"

    def read(node, name):
        check()
        # This is a local pointer check, not another COM RuntimeId query for
        # every property. RuntimeIds are sampled once per ancestry pass below.
        if hasattr(node, "_element") and not node._element:
            raise ValueError("Live UIA pointer unavailable")
        value = getattr(node, name)
        check()
        return value

    check()
    pid = int(read(control, "ProcessId"))
    if pid <= 0:
        return None, "element_process_unavailable"
    if previous is not None and pid != previous["pid"]:
        return None, "element_process_changed"
    reader = getattr(backend.win32, "child_window_identity", None)
    if reader is None:
        return None, "cross_process_anchor_unavailable"
    path, anchor, references = [], None, []
    ancestor = control
    visited = set()
    for _ in range(64):
        check()
        runtime_id = backend._runtime_id(ancestor)
        check()
        if runtime_id is None or runtime_id in visited:
            return None, "ancestry_unavailable"
        visited.add(runtime_id)
        node_pid = int(read(ancestor, "ProcessId"))
        hwnd = read(ancestor, "NativeWindowHandle")
        if type(hwnd) is not int or node_pid <= 0:
            return None, "cross_process_anchor_unavailable"
        path.append((runtime_id, node_pid, hwnd))
        references.append(ancestor)
        if anchor is None and hwnd and hwnd != expected["hwnd"] and node_pid == pid:
            anchor = (runtime_id, hwnd)
        if runtime_id == root_id:
            break
        check()
        ancestor = ancestor.GetParentControl()
        check()
        if ancestor is None:
            return None, "ancestry_unavailable"
    else:
        return None, "ancestry_limit"
    if path[0][0] != original_id or anchor is None:
        return None, "cross_process_anchor_unavailable"
    # If the candidate has its own HWND, that HWND itself must be the native
    # anchor; a different same-PID ancestor cannot legitimize a foreign handle.
    if path[0][2] and path[0][2] != anchor[1]:
        return None, "candidate_hwnd_membership_mismatch"
    check()
    if backend._window_identity(expected) != expected:
        return None, "binding_identity_unavailable"
    check()
    identity = reader(expected["hwnd"], expected["pid"], anchor[1], pid)
    check()
    if (
        identity.get("hwnd") != anchor[1]
        or identity.get("pid") != pid
        or type(identity.get("process_creation_time")) is not int
        or identity["process_creation_time"] <= 0
    ):
        return None, "element_process_unavailable"
    pin = {
        "pid": pid,
        "process_creation_time": identity["process_creation_time"],
        "anchor_hwnd": anchor[1],
        "anchor_id": anchor[0],
        "path": tuple(path),
        "pointer": pointer,
    }
    if previous is not None and any(pin[key] != previous[key] for key in pin if key != "pointer"):
        return None, "element_process_or_anchor_changed"
    # A native query can itself race provider churn: prove the collected UIA
    # path still consists of the same live nodes AFTER the HWND/process checks.
    for index, node in enumerate(references):
        check()
        if (
            backend._runtime_id(node) != path[index][0]
            or int(read(node, "ProcessId")) != path[index][1]
            or read(node, "NativeWindowHandle") != path[index][2]
        ):
            return None, "element_process_or_anchor_changed"
        if index + 1 < len(references):
            check()
            parent = node.GetParentControl()
            check()
            if parent is None or backend._runtime_id(parent) != path[index + 1][0]:
                return None, "ancestry_unavailable"
    check()
    if reader(expected["hwnd"], expected["pid"], anchor[1], pid) != identity:
        return None, "element_process_or_anchor_changed"
    check()
    if backend._window_identity(expected) != expected:
        return None, "binding_identity_unavailable"
    check()
    if backend._runtime_id(control) != original_id:
        return None, "candidate_runtime_id_changed"
    check()
    if backend._runtime_id(root) != root_id:
        return None, "root_runtime_id_changed"
    check()
    if (
        int(read(root, "ProcessId")) != expected["pid"]
        or read(root, "NativeWindowHandle") != expected["hwnd"]
    ):
        return None, "root_hwnd_mismatch"
    if int(read(control, "ProcessId")) != pid:
        return None, "element_process_changed"
    if hasattr(control, "_element") and control._element is not pointer:
        return None, "element_process_or_anchor_changed"
    return pin, None
