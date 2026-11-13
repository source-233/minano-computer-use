"""Copy public JSON identities without changing exact internal/IPC bindings."""


def public_result(value):
    """Expose FILETIME as canonical decimal text (or explicitly unavailable).

    A float cannot recover lost low bits. Invalid provider values become null
    with a static reason; they never get rounded or silently blessed as identity.
    Existing trace files and caller-supplied bindings are not converted here.
    """
    if isinstance(value, dict):
        copied = {}
        for key, item in value.items():
            if key != "process_creation_time":
                copied[key] = public_result(item)
                continue
            if item is None:
                copied[key] = None
            elif type(item) is int and 0 < item <= 0xFFFFFFFFFFFFFFFF:
                copied[key] = str(item)
            elif (
                isinstance(item, str)
                and 1 <= len(item) <= 20
                and item.isascii()
                and item.isdecimal()
                and item[0] != "0"
                and int(item) <= 0xFFFFFFFFFFFFFFFF
            ):
                copied[key] = item
            else:
                copied[key] = None
                copied["process_creation_time_unavailable"] = "INVALID_IDENTITY_TIME"
        return copied
    if isinstance(value, (list, tuple)):
        return [public_result(item) for item in value]
    return value
