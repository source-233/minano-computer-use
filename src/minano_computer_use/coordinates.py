"""Caller-scoped immutable image coordinates and identity-bound viewports."""

from __future__ import annotations

import copy
import math
import os
import threading
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any


class CoordinateError(ValueError):
    """The requested mapping cannot be proven safe."""


class StaleCaptureError(CoordinateError):
    """An internally pinned image recipient changed before dispatch."""

    code = "STALE_CAPTURE"


def _process_name(pid: int) -> str | None:
    """Read an exact executable basename, without shell/process-service access."""
    if os.name != "nt":
        return None
    import ctypes
    from ctypes import wintypes

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.QueryFullProcessImageNameW.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        wintypes.LPWSTR,
        ctypes.POINTER(wintypes.DWORD),
    ]
    kernel.QueryFullProcessImageNameW.restype = wintypes.BOOL
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.restype = wintypes.BOOL
    handle = kernel.OpenProcess(0x1000, False, pid)
    if not handle:
        return None
    try:
        size = wintypes.DWORD(32768)
        buffer = ctypes.create_unicode_buffer(size.value)
        if not kernel.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size)):
            return None
        return buffer.value.replace("\\", "/").rsplit("/", 1)[-1]
    finally:
        kernel.CloseHandle(handle)


def resolve_window_target(target: str) -> dict[str, Any]:
    """Resolve capture grammar; prefixed names are exact, never fuzzy."""
    from . import win32

    prefixed = target.startswith("window:")
    query = target[7:] if prefixed else target
    if not query:
        raise CoordinateError("window target must be nonempty")
    if query == "foreground" or query.startswith(("hwnd:", "pid:")):
        return win32.resolve_window(query)
    process = query.startswith("process:")
    title = query.startswith("title:")
    if not prefixed and not process and not title:
        return win32.resolve_window(query)  # Preserve the original bare-title API.
    value = query.split(":", 1)[1] if process or title else query
    if not value:
        raise CoordinateError("window target must be nonempty")
    windows = win32.list_windows()
    matches = (
        [] if process else [w for w in windows if w.get("title", "").casefold() == value.casefold()]
    )
    if not title and not matches:
        names: dict[int, str | None] = {}
        for window in windows:
            pid = int(window["pid"])
            if pid not in names:
                names[pid] = (
                    window.get("process_name") or window.get("process") or _process_name(pid)
                )
            name = names[pid]
            if name and name.replace("\\", "/").rsplit("/", 1)[-1].casefold() == value.casefold():
                matches.append(window)
    if len(matches) != 1:
        code = "AMBIGUOUS_TARGET" if matches else "TARGET_NOT_FOUND"
        raise win32.TargetError(
            code, "capture target must match exactly one window title or process"
        )
    return win32.resolve_window(f"hwnd:{matches[0]['hwnd']}")


def window_snapshot(target: str) -> dict[str, Any]:
    """Read current HWND/PID/create identity and optional DPI, without focusing."""
    from . import win32

    window = dict(resolve_window_target(target))
    # Read-only capture still works when query-limited access is denied. Image
    # mappings and persistent viewports reject an unknown creation identity.
    try:
        creation = win32.process_creation_time(int(window["pid"]))
    except (OSError, AttributeError):
        creation = None
    window["process_creation_time"] = creation
    if os.name == "nt" and "dpi" not in window:
        import ctypes
        from ctypes import wintypes

        try:
            user = ctypes.WinDLL("user32", use_last_error=True)
            user.GetDpiForWindow.argtypes = [wintypes.HWND]
            user.GetDpiForWindow.restype = wintypes.UINT
            dpi = user.GetDpiForWindow(int(window["hwnd"]))
            if dpi:
                window["dpi"] = int(dpi)
        except (OSError, AttributeError):
            pass
    return window


def validate_window(
    expected: dict[str, Any], current: dict[str, Any], *, geometry: bool = True, strong: bool = True
) -> None:
    if expected.get("hwnd") is None:
        return
    if expected.get("hwnd") != current.get("hwnd"):
        raise CoordinateError("captured window HWND changed")
    if expected.get("pid") != current.get("pid"):
        raise CoordinateError("captured window PID changed")
    creation = expected.get("process_creation_time")
    if strong and creation is None:
        raise CoordinateError("window process creation identity is unavailable")
    if creation is not None and creation != current.get("process_creation_time"):
        raise CoordinateError("captured window process identity changed")
    if current.get("minimized") or not current.get("visible", True):
        raise CoordinateError("captured window is hidden or minimized")
    rect = expected.get("window_rect", expected.get("rect"))
    actual = current.get("window_rect", current.get("rect"))
    if geometry and (rect is None or actual is None or list(rect) != list(actual)):
        raise CoordinateError("captured window geometry changed")
    if geometry and expected.get("dpi") != current.get("dpi"):
        raise CoordinateError("captured window DPI changed")


@dataclass(frozen=True)
class _Frame:
    capture_id: str
    metadata: dict[str, Any]


def _numbers(value: Any, length: int, label: str) -> tuple[float, ...]:
    if not isinstance(value, (list, tuple)) or len(value) != length:
        raise CoordinateError(f"{label} must contain {length} finite numbers")
    if any(type(v) not in (float, int) or not math.isfinite(v) for v in value):
        raise CoordinateError(f"{label} must contain {length} finite numbers")
    return tuple(float(v) for v in value)


class CoordinateManager:
    def __init__(self, resolver: Callable[[str], dict[str, Any]] | None = None) -> None:
        self._resolver = resolver or window_snapshot
        self._latest: dict[str, _Frame] = {}
        self._viewports: dict[tuple[str, str], dict[str, Any]] = {}
        self._lock = threading.RLock()

    @staticmethod
    def _scope(scope: str) -> str:
        if not isinstance(scope, str) or not scope:
            raise CoordinateError("scope must be a nonempty string")
        return scope

    def remember(self, metadata: dict[str, Any], scope: str = "default") -> str:
        scope = self._scope(scope)
        snapshot = copy.deepcopy(metadata)
        capture_id = uuid.uuid4().hex
        snapshot["capture_id"] = capture_id
        with self._lock:
            self._latest[scope] = _Frame(capture_id, snapshot)
        return capture_id

    def _current(self, metadata: dict[str, Any], *, geometry: bool = True) -> dict[str, Any]:
        try:
            current = self._resolver(f"hwnd:{int(metadata['hwnd'])}")
        except Exception as exc:
            raise CoordinateError("captured window is stale or unavailable") from exc
        validate_window(metadata, current, geometry=geometry)
        return current

    def validate_binding(self, binding: dict[str, Any], hwnd: int) -> dict[str, Any]:
        """Recheck an internal dispatch pin, never the latest frame or its title."""
        if binding.get("hwnd") != hwnd:
            raise StaleCaptureError("input recipient does not match captured window HWND")
        try:
            return self._current(binding)
        except CoordinateError as exc:
            raise StaleCaptureError(str(exc)) from exc

    @staticmethod
    def _screen_surface(target: Any) -> bool:
        return isinstance(target, str) and (
            target in {"desktop", "region", "primary", "left", "right"}
            or target.startswith("display:")
        )

    def _validate_recipient(self, target: Any) -> dict[str, Any]:
        """Validate an explicit input window, not the image surface provenance.

        No foreground lookup or surface-to-window inference is permitted. The
        unique explicit target is bound to its HWND/PID/create identity and read
        again by handle. Native foreground/hit-test policy remains downstream.
        """
        if isinstance(target, str):
            query = target[7:] if target.startswith("window:") else target
            if (
                not query
                or query == "foreground"
                or self._screen_surface(query)
                or query.startswith("viewport:")
            ):
                raise CoordinateError("image input requires an explicit concrete window recipient")
            try:
                if query.startswith("hwnd:"):
                    hwnd = int(query[5:], 0)
                    if hwnd <= 0:
                        raise ValueError("invalid HWND")
                    recipient = copy.deepcopy(self._resolver(f"hwnd:{hwnd}"))
                    if not isinstance(recipient, dict) or recipient.get("hwnd") != hwnd:
                        raise CoordinateError("resolved input recipient HWND does not match target")
                else:
                    recipient = copy.deepcopy(self._resolver(target))
            except Exception as exc:
                raise CoordinateError("input recipient window is unavailable or ambiguous") from exc
        elif isinstance(target, dict):
            recipient = copy.deepcopy(target)
        else:
            raise CoordinateError("image input requires an explicit concrete window recipient")
        if not isinstance(recipient, dict) or any(
            type(recipient.get(key)) is not int or recipient[key] <= 0
            for key in ("hwnd", "pid", "process_creation_time")
        ):
            raise CoordinateError(
                "input recipient requires live HWND/PID/process creation identity"
            )
        rect = _numbers(recipient.get("window_rect", recipient.get("rect")), 4, "recipient rect")
        if rect[2] <= rect[0] or rect[3] <= rect[1]:
            raise CoordinateError("input recipient has invalid window geometry")
        self._current(recipient)
        return recipient

    @staticmethod
    def _region_values(region: Any, label: str = "region") -> tuple[float, float, float, float]:
        """Accept legacy xyxy and the two public rectangle spellings."""
        if isinstance(region, (list, tuple)):
            return _numbers(region, 4, label)
        if not isinstance(region, dict):
            raise CoordinateError(f"{label} must be [left,top,right,bottom] or a rectangle object")
        if any(type(v) not in (int, float) or not math.isfinite(v) for v in region.values()):
            raise CoordinateError(f"{label} must contain finite numbers")
        keys = set(region)
        if keys == {"left", "top", "right", "bottom"}:
            values = (region["left"], region["top"], region["right"], region["bottom"])
        elif keys == {"x", "y", "w", "h"}:
            values = (
                region["x"],
                region["y"],
                region["x"] + region["w"],
                region["y"] + region["h"],
            )
        elif keys == {"left", "top", "width", "height"}:
            values = (
                region["left"],
                region["top"],
                region["left"] + region["width"],
                region["top"] + region["height"],
            )
        else:
            raise CoordinateError(
                f"{label} must use left/top/right/bottom, x/y/w/h, or left/top/width/height"
            )
        return _numbers(values, 4, label)

    def map_region(
        self,
        region: Any,
        target: Any = None,
        scope: str = "default",
        capture_id: str | None = None,
    ) -> dict[str, int]:
        """Map an image ROI to a physical screen rectangle.

        Unlike point mapping, the right and bottom edges may equal the image
        dimensions. This is intentional for crop rectangles and never clamps
        coordinates outside the captured image.
        """
        with self._lock:
            frame = self._latest.get(self._scope(scope))
        if frame is None or capture_id is not None and frame.capture_id != capture_id:
            raise CoordinateError("unknown, stale, or wrong-scope capture_id")
        metadata = frame.metadata
        if metadata.get("hwnd") is not None:
            self._current(metadata)
            if target is not None:
                try:
                    if isinstance(target, str) and target.startswith("viewport:"):
                        target = self.resolve(target, scope=scope)["target"]
                    live = target if isinstance(target, dict) else self._resolver(target)
                    validate_window(metadata, live)
                except Exception as exc:
                    raise CoordinateError("target does not match the capture") from exc
        elif target is not None and target != metadata.get("target"):
            raise CoordinateError("target does not match the capture")
        transform = metadata.get("physical_transform")
        if not isinstance(transform, dict):
            raise CoordinateError("capture has no known physical_transform for image ROI")
        origin = _numbers(transform.get("origin"), 2, "physical origin")
        scales = _numbers(transform.get("output_to_physical_scale"), 2, "physical scale")
        size = _numbers(metadata.get("output_size"), 2, "image size")
        if min(*scales, *size) <= 0:
            raise CoordinateError("physical scale and image size must be positive")
        left, top, right, bottom = self._region_values(region)
        if not (0 <= left < right <= size[0] and 0 <= top < bottom <= size[1]):
            raise CoordinateError("image region is out of bounds")
        physical_left = round(origin[0] + left * scales[0])
        physical_top = round(origin[1] + top * scales[1])
        physical_right = round(origin[0] + right * scales[0])
        physical_bottom = round(origin[1] + bottom * scales[1])
        if physical_right <= physical_left or physical_bottom <= physical_top:
            raise CoordinateError("image region has empty physical geometry")
        return {
            "x": physical_left,
            "y": physical_top,
            "w": physical_right - physical_left,
            "h": physical_bottom - physical_top,
        }

    def map_actions(
        self,
        actions: list[dict[str, Any]],
        coordinate_space: str = "screen",
        capture_id: str | None = None,
        target: Any = None,
        scope: str = "default",
        *,
        bind: bool = False,
    ) -> list[dict[str, Any]]:
        if coordinate_space not in {"screen", "image"}:
            raise CoordinateError("coordinate_space must be screen or image")
        mapped = copy.deepcopy(actions)
        if coordinate_space == "screen":
            return mapped
        with self._lock:
            frame = self._latest.get(self._scope(scope))
        if frame is None or capture_id is not None and frame.capture_id != capture_id:
            raise CoordinateError("unknown, stale, or wrong-scope capture_id")
        metadata = frame.metadata
        binding = None
        if metadata.get("hwnd") is not None:
            self._current(metadata)
            if target is None:
                raise CoordinateError("window capture image input requires its bound window target")
            try:
                if isinstance(target, str) and target.startswith("viewport:"):
                    target = self.resolve(target, scope=scope)["target"]
                live = target if isinstance(target, dict) else self._resolver(target)
                validate_window(metadata, live)
            except Exception as exc:
                raise CoordinateError("target does not match the capture") from exc
            binding = metadata
        else:
            if not self._screen_surface(metadata.get("target")):
                raise CoordinateError("capture has no recognized screen surface provenance")
            if target is None:
                raise CoordinateError("image input requires an explicit concrete window recipient")
            binding = self._validate_recipient(target)
        transform = metadata.get("physical_transform")
        if not isinstance(transform, dict):
            raise CoordinateError("capture has no known physical_transform")
        origin = _numbers(transform.get("origin"), 2, "physical origin")
        scales = _numbers(transform.get("output_to_physical_scale"), 2, "physical scale")
        size = _numbers(metadata.get("output_size"), 2, "image size")
        if min(*scales, *size) <= 0:
            raise CoordinateError("physical scale and image size must be positive")

        def point(value: Any) -> Any:
            obj = isinstance(value, dict)
            xy = [value.get("x"), value.get("y")] if obj else value
            x, y = _numbers(xy, 2, "image coordinate")
            if not (0 <= x < size[0] and 0 <= y < size[1]):
                raise CoordinateError("image coordinate is out of bounds")
            # Native input consumes integral physical pixels. Never clamp.
            result = [round(origin[0] + x * scales[0]), round(origin[1] + y * scales[1])]
            if obj:
                return {**value, "x": result[0], "y": result[1]}
            return result

        for action in mapped:
            if not isinstance(action, dict):
                raise CoordinateError("actions must contain objects")
            if bind:
                # Generated only after the public whole-plan validation. Keep the
                # original physical surface/recipient, not a mutable latest frame.
                action["_coordinate_binding"] = copy.deepcopy(binding)
            if action.get("action", action.get("type")) in {"look", "mouse_move_relative"}:
                continue
            if "x" in action or "y" in action:
                converted = point({"x": action.get("x"), "y": action.get("y")})
                action.update(converted)
            for key in ("to", "coordinate"):
                if key in action:
                    action[key] = point(action[key])
            if "path" in action:
                if not isinstance(action["path"], list):
                    raise CoordinateError("path must be a list")
                action["path"] = [point(p) for p in action["path"]]
        return mapped

    def define_viewport(
        self, name: str, metadata: dict[str, Any], region: Any = None, scope: str = "default"
    ) -> dict[str, Any]:
        scope = self._scope(scope)
        if not isinstance(name, str) or not name or len(name) > 128:
            raise CoordinateError("viewport name must be 1..128 characters")
        snapshot = copy.deepcopy(metadata)
        transform = snapshot.get("physical_transform")
        if not isinstance(transform, dict):
            raise CoordinateError("viewport requires a known physical_transform")
        origin = _numbers(transform.get("origin"), 2, "physical origin")
        scale = _numbers(transform.get("output_to_physical_scale"), 2, "physical scale")
        width, height = _numbers(snapshot.get("output_size"), 2, "image size")
        if min(*scale, width, height) <= 0:
            raise CoordinateError("invalid viewport dimensions")
        # Optional region is [l,t,r,b] in the returned image, not guessed screen
        # units. Store its physical crop as ratios to the original window rect.
        left, top, right, bottom = (
            (0.0, 0.0, width, height) if region is None else _numbers(region, 4, "viewport region")
        )
        if not (0 <= left < right <= width and 0 <= top < bottom <= height):
            raise CoordinateError("viewport region is out of bounds")
        crop = [
            origin[0] + left * scale[0],
            origin[1] + top * scale[1],
            (right - left) * scale[0],
            (bottom - top) * scale[1],
        ]
        definition: dict[str, Any] = {"name": name, "metadata": snapshot, "crop": crop}
        if snapshot.get("hwnd") is not None:
            self._current(snapshot)
            wl, wt, wr, wb = _numbers(
                snapshot.get("window_rect", snapshot.get("rect")), 4, "window rect"
            )
            if wr <= wl or wb <= wt:
                raise CoordinateError("invalid window geometry")
            definition["ratios"] = [
                (crop[0] - wl) / (wr - wl),
                (crop[1] - wt) / (wb - wt),
                crop[2] / (wr - wl),
                crop[3] / (wb - wt),
            ]
            definition["target"] = f"hwnd:{snapshot['hwnd']}"
        else:
            definition["target"] = "desktop"
        with self._lock:
            self._viewports[(scope, name)] = definition
        return {"name": name, "target": definition["target"], "ratios": definition.get("ratios")}

    def resolve(self, target: str, scope: str = "default") -> dict[str, Any]:
        if not isinstance(target, str) or not target.startswith("viewport:"):
            raise CoordinateError("expected viewport:name target")
        with self._lock:
            definition = copy.deepcopy(self._viewports.get((self._scope(scope), target[9:])))
        if definition is None:
            raise CoordinateError("unknown viewport in this caller scope")
        metadata = definition["metadata"]
        binding = copy.deepcopy(metadata)
        crop = definition["crop"]
        if metadata.get("hwnd") is not None:
            current = self._current(metadata, geometry=False)
            left, top, right, bottom = _numbers(
                current.get("window_rect", current.get("rect")), 4, "window rect"
            )
            if right <= left or bottom <= top:
                raise CoordinateError("invalid viewport window geometry")
            x, y, w, h = definition["ratios"]
            crop = [
                left + (right - left) * x,
                top + (bottom - top) * y,
                (right - left) * w,
                (bottom - top) * h,
            ]
            binding.update(current)
            binding["window_rect"] = list(current.get("window_rect", current.get("rect")))
        region = dict(zip(("x", "y", "w", "h"), (round(v) for v in crop), strict=True))
        if region["w"] <= 0 or region["h"] <= 0:
            raise CoordinateError("viewport crop has empty geometry")
        return {
            "target": definition["target"],
            "region": region,
            "region_space": "screen",
            "binding": binding,
        }

    def list_viewports(self, scope: str = "default") -> list[dict[str, Any]]:
        scope = self._scope(scope)
        with self._lock:
            return [
                {
                    "name": name,
                    "target": item["target"],
                    "ratios": copy.deepcopy(item.get("ratios")),
                }
                for (owner, name), item in self._viewports.items()
                if owner == scope
            ]

    def clear_viewport(self, name: str, scope: str = "default") -> bool:
        with self._lock:
            return self._viewports.pop((self._scope(scope), name), None) is not None
