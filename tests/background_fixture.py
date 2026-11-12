"""Opt-in owned Win32 background fixture. Never run from the default test suite.

Private WM_APP commands return scalars only (no cross-process pointer payloads).
There is deliberately no focus command: even SetFocus can activate a parent.
"""

import ctypes
import json
import os
import sys
from ctypes import wintypes as w
from pathlib import Path

WM_APP_QUERY = 0x8001
WM_APP_SELECT = 0x8003
COUNTERS = (
    "clicks",
    "move",
    "down",
    "up",
    "drag",
    "wheel",
    "hwheel",
    "key_down",
    "key_up",
    "char",
    "last_point",
    "last_wheel",
)


def main() -> None:
    if sys.platform != "win32":
        raise SystemExit("Windows required")
    user = ctypes.WinDLL("user32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    callback = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, w.HWND, w.UINT, w.WPARAM, w.LPARAM)

    class WindowClass(ctypes.Structure):
        _fields_ = [
            ("style", w.UINT),
            ("wndproc", callback),
            ("extra_class", ctypes.c_int),
            ("extra_window", ctypes.c_int),
            ("instance", w.HINSTANCE),
            ("icon", w.HICON),
            ("cursor", w.HANDLE),
            ("background", w.HBRUSH),
            ("menu", w.LPCWSTR),
            ("name", w.LPCWSTR),
        ]

    def api(dll, name, arguments, returns):
        function = getattr(dll, name)
        function.argtypes, function.restype = arguments, returns
        return function

    module = api(kernel, "GetModuleHandleW", [w.LPCWSTR], w.HMODULE)
    default = api(user, "DefWindowProcW", [w.HWND, w.UINT, w.WPARAM, w.LPARAM], ctypes.c_ssize_t)
    register = api(user, "RegisterClassW", [ctypes.POINTER(WindowClass)], w.ATOM)
    create = api(
        user,
        "CreateWindowExW",
        [
            w.DWORD,
            w.LPCWSTR,
            w.LPCWSTR,
            w.DWORD,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            w.HWND,
            w.HMENU,
            w.HINSTANCE,
            w.LPVOID,
        ],
        w.HWND,
    )
    show = api(user, "ShowWindow", [w.HWND, ctypes.c_int], w.BOOL)
    destroy = api(user, "DestroyWindow", [w.HWND], w.BOOL)
    quit_message = api(user, "PostQuitMessage", [ctypes.c_int], None)
    get_message = api(
        user, "GetMessageW", [ctypes.POINTER(w.MSG), w.HWND, w.UINT, w.UINT], ctypes.c_int
    )
    translate = api(user, "TranslateMessage", [ctypes.POINTER(w.MSG)], w.BOOL)
    dispatch = api(user, "DispatchMessageW", [ctypes.POINTER(w.MSG)], ctypes.c_ssize_t)
    get_focus = api(user, "GetFocus", [], w.HWND)
    foreground = api(user, "GetForegroundWindow", [], w.HWND)
    send = api(user, "SendMessageW", [w.HWND, w.UINT, w.WPARAM, w.LPARAM], ctypes.c_ssize_t)
    set_proc = api(
        user,
        "SetWindowLongPtrW" if ctypes.sizeof(ctypes.c_void_p) == 8 else "SetWindowLongW",
        [w.HWND, ctypes.c_int, ctypes.c_ssize_t],
        ctypes.c_ssize_t,
    )
    call_proc = api(
        user,
        "CallWindowProcW",
        [
            ctypes.c_void_p,
            w.HWND,
            w.UINT,
            w.WPARAM,
            w.LPARAM,
        ],
        ctypes.c_ssize_t,
    )
    setter = api(user, "SetThreadDpiAwarenessContext", [ctypes.c_void_p], ctypes.c_void_p)
    awareness = api(user, "GetAwarenessFromDpiAwarenessContext", [ctypes.c_void_p], ctypes.c_int)
    get_context = api(user, "GetThreadDpiAwarenessContext", [], ctypes.c_void_p)
    if not setter(ctypes.c_void_p(-4)) or awareness(get_context()) != 2:
        raise RuntimeError("Cannot establish physical DPI coordinates for owned fixture")

    roots = {}
    counters = {}
    owners = {}
    button_procs = {}
    pressed_buttons = set()

    @callback
    def button_proc(hwnd, message, wp, lp):
        # Standard BUTTON normally calls SetFocus/SetCapture on mouse-down.
        # Keep its real class/provider, but give this owned test app explicit,
        # nonactivating message-click behavior and no real capture side effects.
        if message == 0x0021:
            return 3
        if message == 0x0201:
            pressed_buttons.add(hwnd)
            return 0
        if message == 0x0202:
            if hwnd in pressed_buttons:
                pressed_buttons.remove(hwnd)
                send(owners[hwnd], 0x0111, 1002, hwnd)
            return 0
        return int(call_proc(button_procs[hwnd], hwnd, message, wp, lp))

    @callback
    def wndproc(hwnd, message, wp, lp):
        root = owners.get(hwnd, hwnd)
        data = counters.get(root)
        if message == 0x0021:  # WM_MOUSEACTIVATE: do not activate even if touched.
            return 3  # MA_NOACTIVATE
        if message == WM_APP_QUERY and root in roots:
            if wp < len(COUNTERS):
                return data[COUNTERS[wp]]
            if wp == 100:
                return int(get_focus() or 0)
            if wp == 101:
                return int(foreground() or 0)
            return -1
        if message == WM_APP_SELECT and root in roots:
            send(roots[root]["edit"], 0x00B1, wp, lp)  # EM_SETSEL, local scalar only.
            return int(send(roots[root]["edit"], 0x00B0, 0, 0))  # EM_GETSEL packed.
        if data is not None:
            field = {
                0x0200: "move",
                0x0201: "down",
                0x0202: "up",
                0x020A: "wheel",
                0x020E: "hwheel",
                0x0100: "key_down",
                0x0101: "key_up",
                0x0102: "char",
            }.get(message)
            if field:
                data[field] += 1
                if message == 0x0200 and wp & 1:
                    data["drag"] += 1
                if message in {0x0200, 0x0201, 0x0202}:
                    data["last_point"] = lp & 0xFFFFFFFF
                if message in {0x020A, 0x020E}:
                    data["last_wheel"] = ctypes.c_short(wp >> 16).value
                return 0
            if message == 0x0111 and wp & 0xFFFF == 1002 and wp >> 16 == 0:
                data["clicks"] += 1
                return 0
        if message == 0x0010 and hwnd in roots:
            for window in list(roots):
                destroy(window)
            quit_message(0)
            return 0
        return int(default(hwnd, message, wp, lp))

    instance = module(None)
    class_name = f"MinanoBackgroundFixture{os.getpid()}"
    wc = WindowClass(0, wndproc, 0, 0, instance, None, None, w.HBRUSH(6), None, class_name)
    if not register(ctypes.byref(wc)):
        raise ctypes.WinError(ctypes.get_last_error())

    def checked_create(*args):
        handle = create(*args)
        if not handle:
            raise ctypes.WinError(ctypes.get_last_error())
        return int(handle)

    for index, label in enumerate(("target", "unrelated")):
        title = f"Minano background {label} {os.getpid()}"
        hwnd = checked_create(
            0x08000000,
            class_name,
            title,
            0x00CF0000,
            70 + index * 480,
            70,
            460,
            310,
            None,
            None,
            instance,
            None,
        )
        roots[hwnd] = {"hwnd": hwnd, "title": title, "role": label}
        counters[hwnd] = dict.fromkeys(COUNTERS, 0)
        for kind, text, identifier, y, height, style, key in (
            ("EDIT", "initial", 1001, 15, 26, 0x00810080, "edit"),
            ("BUTTON", "Apply background", 1002, 55, 30, 0, "button"),
            (class_name, "Message surface", 1003, 100, 125, 0x00800000, "surface"),
        ):
            child = checked_create(
                0x08000000,
                kind,
                text,
                0x50000000 | style,
                20,
                y,
                390,
                height,
                hwnd,
                w.HMENU(identifier),
                instance,
                None,
            )
            roots[hwnd][key] = child
            owners[child] = hwnd
            if key == "button":
                previous = set_proc(child, -4, ctypes.cast(button_proc, ctypes.c_void_p).value)
                if not previous:
                    raise ctypes.WinError(ctypes.get_last_error())
                button_procs[child] = previous
        show(hwnd, 4)  # SW_SHOWNOACTIVATE; no initial SetFocus.

    info = {
        "pid": os.getpid(),
        "windows": list(roots.values()),
        "dpi_awareness": "per_monitor_v2",
        "default_focus": int(get_focus() or 0),
        "button_behavior": "standard BUTTON class; owned subclass consumes down/up without "
        "SetFocus/SetCapture and emits parent BN_CLICKED",
    }
    # Readiness uses an owned path instead of an unbounded pipe.readline().
    destination = Path(sys.argv[1])
    temporary = destination.with_suffix(".tmp")
    temporary.write_text(json.dumps(info), encoding="utf-8")
    temporary.replace(destination)
    msg = w.MSG()
    while (code := get_message(ctypes.byref(msg), None, 0, 0)) > 0:
        translate(ctypes.byref(msg))
        dispatch(ctypes.byref(msg))
    if code == -1:
        raise ctypes.WinError(ctypes.get_last_error())


if __name__ == "__main__":
    main()
