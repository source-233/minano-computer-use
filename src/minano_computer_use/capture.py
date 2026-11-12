"""Screen-composite capture with bounded screenshots and recordings."""

from __future__ import annotations

import io
import json
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class CaptureFrame:
    image_bytes: bytes
    mime_type: str
    metadata: dict[str, Any]


class CaptureTargetChanged(RuntimeError):
    """A captured window no longer matches its original identity or geometry."""


def _image_from_grab(value: Any) -> Any:
    if hasattr(value, "save") and hasattr(value, "size"):
        return value.copy() if hasattr(value, "copy") else value
    from PIL import Image

    if hasattr(value, "rgb") and hasattr(value, "size"):
        return Image.frombytes("RGB", tuple(value.size), value.rgb)
    if isinstance(value, (bytes, bytearray)):
        return Image.open(io.BytesIO(value)).convert("RGB")
    raise TypeError("capture adapter must return a PIL image, MSS screenshot, or encoded bytes")


def _image_options(max_dim: int, fmt: str, quality: int | None) -> tuple[str, int | None]:
    if type(max_dim) is not int or not 0 <= max_dim <= 4096:
        raise ValueError("max_dim must be an integer between 0 and 4096")
    if not isinstance(fmt, str) or fmt.lower() not in {"png", "jpeg", "jpg", "webp"}:
        raise ValueError("format must be png, jpeg, or webp")
    fmt = "jpeg" if fmt.lower() == "jpg" else fmt.lower()
    if (
        fmt != "png"
        and quality is not None
        and (type(quality) is not int or not 1 <= quality <= 95)
    ):
        raise ValueError("quality must be an integer between 1 and 95 for JPEG or WebP")
    return fmt, None if fmt == "png" else quality


def _encode(image: Any, fmt: str, quality: int | None = None) -> tuple[bytes, str]:
    fmt, quality = _image_options(0, fmt, quality)
    pil_fmt = fmt.upper()
    output = io.BytesIO()
    options = {} if quality is None else {"quality": quality}
    if pil_fmt == "JPEG" and image.mode not in {"RGB", "L"}:
        image = image.convert("RGB")
    image.save(output, format=pil_fmt, **options)
    return output.getvalue(), f"image/{fmt}"


class CaptureService:
    def __init__(
        self,
        output_dir: Path | None = None,
        grabber: Any = None,
        encoder: Any = None,
        artifact_store: Any = None,
        coordinate_manager: Any = None,
    ) -> None:
        self.output_dir = Path(output_dir or ".artifacts")
        self._grabber = grabber
        self._encoder = encoder
        self._artifact_store = artifact_store
        if coordinate_manager is None:
            from .coordinates import CoordinateManager

            coordinate_manager = CoordinateManager()
        self._coordinate_manager = coordinate_manager
        self._owns_artifact_store = False
        self._closed = False
        self._lock = threading.Lock()
        self._last_encoder_diagnostics: dict[str, Any] = {}

    @staticmethod
    def _checkpoint(cancel_event: Any = None, deadline: float | None = None) -> None:
        if cancel_event is not None and cancel_event.is_set():
            from .control import OperationCancelled

            raise OperationCancelled("capture cancelled")
        if deadline is not None and time.monotonic() >= deadline:
            raise TimeoutError("capture timed out")

    def _allocate(self, suffix: str) -> Path:
        self._ensure_store()
        return self._artifact_store.allocate(suffix)

    def _ensure_store(self) -> None:
        if self._artifact_store is None:
            from .trace import ArtifactStore

            self._artifact_store = ArtifactStore(self.output_dir)
            self._owns_artifact_store = True

    def _register(self, path: Path) -> None:
        self._artifact_store.register(path)
        self._artifact_store.prune()

    def _write_artifact(self, path: Path, data: bytes) -> None:
        self._artifact_store.write_bytes(path, data)

    def _discard_artifact(self, path: Path) -> None:
        if self._artifact_store is None:
            path.unlink(missing_ok=True)
            return
        try:
            self._artifact_store.discard(path)
        except ValueError:
            # register() can consume a reservation before reporting a later
            # quota/prune error; never discard a registered artifact.
            pass

    def _make_grabber(self, supplied: Any = None) -> tuple[Any, bool]:
        if supplied is not None:
            return supplied, False
        if self._grabber is not None:
            return self._grabber, False
        import mss

        return mss.mss(), True

    @staticmethod
    def _monitors(grabber: Any) -> list[dict[str, int]]:
        monitors = getattr(grabber, "monitors", None)
        if monitors:
            return [dict(m) for m in monitors]
        return [{"left": 0, "top": 0, "width": 1, "height": 1}]

    def _target_rect(self, target: str, grabber: Any) -> tuple[int, int, int, int, dict[str, Any]]:
        monitors = self._monitors(grabber)
        desktop = monitors[0]
        if target.startswith("display:") or target in {"primary", "left", "right"}:
            target = target[8:] if target.startswith("display:") else target
            if target == "primary":
                index = 1 if len(monitors) > 1 else 0
            elif target == "left":
                index = (
                    min(
                        range(1, len(monitors)),
                        key=lambda i: (monitors[i]["left"], monitors[i]["top"]),
                    )
                    if len(monitors) > 1
                    else 0
                )
            elif target == "right":
                index = (
                    max(
                        range(1, len(monitors)),
                        key=lambda i: (monitors[i]["left"], monitors[i]["top"]),
                    )
                    if len(monitors) > 1
                    else 0
                )
            else:
                try:
                    if not target.isdecimal():
                        raise ValueError
                    index = int(target) + 1
                except ValueError as exc:
                    raise ValueError("display index must be an integer") from exc
            if not 0 <= index < len(monitors):
                raise ValueError("display index is out of range")
            monitor = monitors[index]
            display_index = 0 if len(monitors) == 1 else index - 1
            return (
                monitor["left"],
                monitor["top"],
                monitor["width"],
                monitor["height"],
                {
                    "target": f"display:{target}",
                    "origin": [monitor["left"], monitor["top"]],
                    "scale": [1.0, 1.0],
                    "display_index": display_index,
                },
            )
        if target == "desktop":
            return (
                desktop["left"],
                desktop["top"],
                desktop["width"],
                desktop["height"],
                {
                    "target": "desktop",
                    "origin": [desktop["left"], desktop["top"]],
                    "scale": [1.0, 1.0],
                },
            )
        from .coordinates import window_snapshot

        window = window_snapshot(target)
        if window.get("minimized") or not window.get("visible", True):
            raise ValueError("target window is minimized or hidden")
        left, top, right, bottom = (int(v) for v in window["rect"])
        dleft, dtop = desktop["left"], desktop["top"]
        dright, dbottom = dleft + desktop["width"], dtop + desktop["height"]
        if (
            right <= left
            or bottom <= top
            or right <= dleft
            or left >= dright
            or bottom <= dtop
            or top >= dbottom
        ):
            raise ValueError("target window is offscreen")
        left, top = max(left, dleft), max(top, dtop)
        right, bottom = min(right, dright), min(bottom, dbottom)
        return (
            left,
            top,
            right - left,
            bottom - top,
            {
                "target": target,
                "hwnd": int(window["hwnd"]),
                "pid": int(window["pid"]),
                "window_rect": list(window["rect"]),
                "process_creation_time": window.get("process_creation_time"),
                "dpi": window.get("dpi"),
                "origin": [left, top],
                "scale": [1.0, 1.0],
            },
        )

    @staticmethod
    def _grab(grabber: Any, rect: tuple[int, int, int, int]) -> Any:
        if hasattr(grabber, "grab"):
            try:
                return _image_from_grab(
                    grabber.grab(
                        {"left": rect[0], "top": rect[1], "width": rect[2], "height": rect[3]}
                    )
                )
            except TypeError:
                return _image_from_grab(grabber.grab())
        if callable(grabber):
            return _image_from_grab(grabber(rect))
        raise TypeError("invalid capture adapter")

    def _resolve_capture_args(self, target, region, region_space, scope):
        binding = None
        if target.startswith("viewport:"):
            if region is not None:
                raise ValueError("viewport target and an additional region conflict")
            resolved = self._coordinate_manager.resolve(target, scope=scope)
            target, region, region_space = (
                resolved["target"],
                resolved["region"],
                resolved["region_space"],
            )
            binding = resolved["binding"]
        elif target == "region":
            if region is None:
                raise ValueError("region target requires region")
            target = "desktop"
        if region_space == "image":
            if region is None:
                raise ValueError("image region requires an image rectangle")
            if self._window_target(target):
                from .coordinates import window_snapshot

                binding = window_snapshot(target)
                region = self._coordinate_manager.map_region(region, target=binding, scope=scope)
                target = f"hwnd:{binding['hwnd']}"
            else:
                region = self._coordinate_manager.map_region(region, target=target, scope=scope)
            region_space = "screen"
        return target, region, region_space, binding

    @staticmethod
    def _window_target(target):
        return (
            target != "desktop"
            and not target.startswith("display:")
            and target not in {"primary", "left", "right", "region"}
        )

    @staticmethod
    def _crop_region(region, region_space, image, base):
        if region_space not in {"surface", "screen"}:
            raise ValueError("region_space must be surface, screen, or image")
        if region is None:
            return [0, 0, image.width, image.height]
        if isinstance(region, dict):
            if any(type(v) is not int for v in region.values()):
                raise ValueError("region rectangle values must be integers")
            keys = set(region)
            if keys == {"x", "y", "w", "h"}:
                x, y, w, h = (region[k] for k in ("x", "y", "w", "h"))
            elif keys == {"left", "top", "width", "height"}:
                x, y = region["left"], region["top"]
                w, h = region["width"], region["height"]
            elif keys == {"left", "top", "right", "bottom"}:
                x, y = region["left"], region["top"]
                w, h = region["right"] - x, region["bottom"] - y
            else:
                raise ValueError(
                    "region object must use x/y/w/h, left/top/width/height, "
                    "or left/top/right/bottom"
                )
            if any(type(v) is not int for v in (x, y, w, h)):
                raise ValueError("region rectangle values must be integers")
            if region_space == "screen":
                origin = base.get("origin")
                if origin is None:
                    raise ValueError("screen region requires a known physical origin")
                x, y = x - origin[0], y - origin[1]
            region = [x, y, x + w, y + h]
        elif region_space != "surface":
            raise ValueError(
                "screen region uses a rectangle object; surface uses "
                "[left,top,right,bottom] or rectangle aliases"
            )
        if (
            not isinstance(region, (list, tuple))
            or len(region) != 4
            or any(type(v) is not int for v in region)
            or not 0 <= region[0] < region[2] <= image.width
            or not 0 <= region[1] < region[3] <= image.height
        ):
            raise ValueError("region must be contained integer [left,top,right,bottom]")
        return list(region)

    def screenshot(
        self,
        target: str = "desktop",
        max_dim: int = 800,
        format: str = "jpeg",
        *,
        quality: int | None = 75,
        grabber: Any = None,
        region: list[int] | dict[str, int] | None = None,
        region_space: str = "surface",
        capture_mode: str = "screen",
        allow_fallback: bool = False,
        timeout: float = 5,
        cancel_event: Any = None,
        scope: str = "default",
        save: bool = False,
        define_viewport: str | None = None,
        foreground: bool = False,
        remember: bool = True,
        deadline: float | None = None,
    ) -> CaptureFrame:
        if self._closed:
            raise RuntimeError("capture service is closed")
        format, effective_quality = _image_options(max_dim, format, quality)
        if foreground:
            raise ValueError("foreground capture requires explicit native force_focus")
        if region_space not in {"surface", "screen", "image"}:
            raise ValueError("region_space must be surface, screen, or image")
        if capture_mode not in {"auto", "screen", "window", "printwindow", "wgc"}:
            raise ValueError("capture_mode must be auto, screen, window, printwindow, or wgc")
        if not 0 < float(timeout) <= 120:
            raise ValueError("timeout must be greater than zero and at most 120 seconds")
        if define_viewport and not remember:
            raise ValueError("define_viewport requires remember=True")
        requested_region_space = region_space
        target, region, region_space, binding = self._resolve_capture_args(
            target, region, region_space, scope
        )
        if capture_mode == "auto":
            is_window = (
                target != "desktop"
                and not target.startswith("display:")
                and target not in {"primary", "left", "right", "region"}
            )
            capture_mode = "window" if is_window else "screen"
        deadline = (
            min(deadline, time.monotonic() + timeout)
            if deadline is not None
            else time.monotonic() + timeout
        )
        self._checkpoint(cancel_event, deadline)
        started = time.monotonic()
        wall_time = time.time()
        fallbacks = []
        adapter, owned = None, False
        group_token = None
        returned_metadata = None
        try:
            image, base = None, {}
            if capture_mode != "screen":
                if target == "desktop":
                    raise ValueError("window capture requires an explicit window target")
                from .coordinates import window_snapshot
                from .window_capture import WindowCaptureError, capture_window

                window = window_snapshot(target)
                if binding:
                    from .coordinates import validate_window

                    validate_window(binding, window)
                self._check_window(window)
                backends = ["printwindow", "wgc"] if capture_mode == "window" else [capture_mode]
                for backend in backends:
                    self._checkpoint(cancel_event, deadline)
                    try:
                        self._revalidate(window)
                        captured = capture_window(
                            int(window["hwnd"]),
                            int(window["pid"]),
                            backend,
                            deadline - time.monotonic(),
                            cancel_event,
                        )
                        from PIL import Image

                        size = tuple(captured["size"])
                        image = Image.frombytes("RGB", size, captured["data"], "raw", "BGRX")
                        self._revalidate(window)
                        base = {
                            "target": target,
                            "hwnd": int(window["hwnd"]),
                            "pid": int(window["pid"]),
                            "window_rect": list(window["rect"]),
                            "process_creation_time": window.get("process_creation_time"),
                            "dpi": window.get("dpi"),
                            "origin": None,
                            "capture_backend": backend,
                            "capture_surface": "window",
                            "coordinate_space": "surface_pixels",
                            "occluded": None,
                            "occlusion_free": None,
                        }
                        fallbacks.append({"backend": backend, "success": True})
                        if backend == "printwindow" and captured.get("physical_origin") is not None:
                            base["origin"] = captured["physical_origin"]
                        break
                    except InterruptedError:
                        self._checkpoint(cancel_event)
                        raise
                    except CaptureTargetChanged:
                        raise
                    except (RuntimeError, OSError, ValueError, TimeoutError) as exc:
                        image = None
                        fallbacks.append(
                            {
                                "backend": backend,
                                "reason": str(exc),
                                "success": False,
                                "code": getattr(exc, "code", "WINDOW_CAPTURE_UNAVAILABLE"),
                            }
                        )
                if image is None and not allow_fallback:
                    self._checkpoint(cancel_event, deadline)
                    raise WindowCaptureError(
                        f"window capture unavailable: {fallbacks}", fallbacks[-1]["code"]
                    )
            if image is None:
                self._checkpoint(cancel_event, deadline)
                if capture_mode != "screen":
                    self._revalidate(window)
                adapter, owned = self._make_grabber(grabber)
                screen_target = f"hwnd:{window['hwnd']}" if capture_mode != "screen" else target
                rect = self._target_rect(screen_target, adapter)
                if binding:
                    from .coordinates import validate_window

                    validate_window(binding, rect[4])
                if "hwnd" in rect[4]:
                    self._revalidate(rect[4])
                image = self._grab(adapter, rect[:4])
                base = {
                    **rect[4],
                    "capture_backend": "mss" if owned else "injected",
                    "capture_surface": "screen_composite",
                    "coordinate_space": "surface_pixels",
                    "occluded": None,
                    "occlusion_free": False,
                }
                if "hwnd" in base:
                    self._revalidate(base)
                if capture_mode != "screen":
                    base["target"] = target
                    fallbacks.append(
                        {
                            "backend": base["capture_backend"],
                            "success": True,
                            "reason": "explicit screen fallback allowed",
                        }
                    )
            self._checkpoint(cancel_event, deadline)
            original_size = list(image.size)
            region = self._crop_region(region, region_space, image, base)
            image = image.crop(tuple(region))
            source_image = image
            width, height = image.size
            scale = 1.0 if int(max_dim) == 0 else min(1.0, max_dim / max(width, height))
            if scale < 1:
                image = image.resize((max(1, round(width * scale)), max(1, round(height * scale))))
            actual_scale = [image.width / width, image.height / height]
            encoded, mime = _encode(image, format, quality=effective_quality)
            metadata = {
                **base,
                "frame_id": uuid.uuid4().hex,
                "timestamp": wall_time,
                "timestamp_monotonic": started,
                "completed_monotonic": time.monotonic(),
                "original_size": original_size,
                "surface_size": original_size,
                "output_size": list(image.size),
                "region": list(region),
                "scale": actual_scale,
                "width": image.width,
                "height": image.height,
                "fallbacks": fallbacks,
                "capture_id": uuid.uuid4().hex,
                "region_space": region_space,
                "requested_region_space": requested_region_space,
                "format": format,
                "quality": effective_quality,
                "quality_applied": format != "png" and effective_quality is not None,
                "image_bytes": len(encoded),
                "mime_type": mime,
            }
            origin = base.get("origin")
            metadata["physical_transform"] = (
                None
                if origin is None
                else {
                    "origin": [origin[0] + region[0], origin[1] + region[1]],
                    "output_to_physical_scale": [1 / actual_scale[0], 1 / actual_scale[1]],
                }
            )
            self._checkpoint(cancel_event, deadline)
            if save:
                self._ensure_store()
                if not self._artifact_store.group_open:
                    group_token = self._artifact_store.begin_group()
                save_path = self._allocate(".png")
                try:
                    # Save the unscaled, unencoded source as requested.  This is
                    # distinct from the response image and remains quota-owned.
                    source_bytes, _ = _encode(source_image, "png")
                    self._write_artifact(save_path, source_bytes)
                    self._register(save_path)
                    metadata["saved_path"] = str(save_path)
                    metadata["saved_size"] = [width, height]
                    metadata["artifact_retention"] = self._artifact_store.prune()
                except BaseException:
                    self._discard_artifact(save_path)
                    raise
            self._checkpoint(cancel_event, deadline)
            if define_viewport:
                self._coordinate_manager.define_viewport(define_viewport, metadata, scope=scope)
            if remember:
                metadata["capture_id"] = self._coordinate_manager.remember(metadata, scope=scope)
            returned_metadata = metadata
            return CaptureFrame(encoded, mime, metadata)
        finally:
            if group_token is not None:
                try:
                    self._artifact_store.end_group(group_token)
                except Exception as exc:
                    if returned_metadata is not None:
                        returned_metadata.setdefault("warnings", []).append(
                            {"code": "ARTIFACT_RETENTION_FAILED", "detail": type(exc).__name__}
                        )
            if owned and hasattr(adapter, "close"):
                adapter.close()

    @staticmethod
    def _check_window(window: dict[str, Any]) -> None:
        if window.get("minimized") or not window.get("visible", True):
            raise ValueError("target window is minimized or hidden")

    def _revalidate(self, window: dict[str, Any]) -> None:
        from . import win32
        from .coordinates import CoordinateError, validate_window, window_snapshot

        try:
            current = window_snapshot(f"hwnd:{window['hwnd']}")
            self._check_window(current)
        except (win32.TargetError, ValueError, OSError) as exc:
            raise CaptureTargetChanged("target window became unavailable during capture") from exc
        try:
            validate_window(window, current, strong=False)
        except CoordinateError as exc:
            raise CaptureTargetChanged(str(exc)) from exc

    def record(
        self,
        target: str = "desktop",
        seconds: float = 3,
        fps: float = 10,
        montage_frames: int = 6,
        *,
        max_dim: int = 800,
        format: str = "jpeg",
        quality: int | None = 75,
        grabber: Any = None,
        cancel_event: Any = None,
        region: list[int] | dict[str, int] | None = None,
        region_space: str = "surface",
        keep_frames: bool = False,
        capture_mode: str = "screen",
        scope: str = "default",
        foreground: bool = False,
        stop_event: Any = None,
        extra_frames: list[Any] | None = None,
        deadline: float | None = None,
    ) -> CaptureFrame:
        if self._closed:
            raise RuntimeError("capture service is closed")
        format, effective_quality = _image_options(max_dim, format, quality)
        if not 0.05 <= float(seconds) <= 60 or not 1 <= float(fps) <= 30:
            raise ValueError("seconds must be 0.05..60 and fps must be 1..30")
        if not 1 <= int(montage_frames) <= 24:
            raise ValueError("montage_frames must be 1..24")
        if foreground:
            raise ValueError("foreground capture requires explicit native force_focus")
        if capture_mode not in {"auto", "screen", "window", "printwindow", "wgc"}:
            raise ValueError("capture_mode must be auto, screen, window, printwindow, or wgc")
        if region_space not in {"surface", "screen", "image"}:
            raise ValueError("region_space must be surface, screen, or image")
        target, region, region_space, binding = self._resolve_capture_args(
            target, region, region_space, scope
        )
        if capture_mode == "auto":
            is_window = (
                target != "desktop"
                and not target.startswith("display:")
                and target
                not in {
                    "primary",
                    "left",
                    "right",
                    "region",
                }
            )
            capture_mode = "window" if is_window else "screen"
        self._checkpoint(cancel_event, deadline)
        group_token = None
        returned_metadata = None
        adapter, owned = None, False
        started = time.monotonic()
        timestamps: list[float] = []
        images: list[Any] = []
        count = min(int(seconds * fps) + 1, 1801)
        total_bytes = 0
        try:
            base = None
            crop = None
            if capture_mode == "screen":
                adapter, owned = self._make_grabber(grabber)
                rect = self._target_rect(target, adapter)
                base = {
                    **rect[4],
                    "capture_backend": "mss" if owned else "injected",
                    "capture_surface": "screen_composite",
                    "occlusion_free": False,
                }
                if binding:
                    from .coordinates import validate_window

                    validate_window(binding, base)
            for index in range(count):
                self._checkpoint(cancel_event, deadline)
                if index and stop_event is not None and stop_event.is_set():
                    break
                due = started + index / fps
                wait = due - time.monotonic()
                if wait > 0:
                    # Preserve the legacy cancellable wait while also observing
                    # script completion and the inherited operation deadline.
                    while wait > 0:
                        if stop_event is not None and stop_event.is_set():
                            break
                        duration = (
                            min(wait, 0.025)
                            if stop_event is not None or deadline is not None
                            else wait
                        )
                        if cancel_event is None:
                            time.sleep(duration)
                        else:
                            cancel_event.wait(duration)
                        self._checkpoint(cancel_event, deadline)
                        wait = due - time.monotonic()
                if index and stop_event is not None and stop_event.is_set():
                    break
                if time.monotonic() - started > seconds + 0.2 and index:
                    break
                if capture_mode == "screen":
                    if "hwnd" in base:
                        self._revalidate(base)
                    image = self._grab(adapter, rect[:4])
                    if "hwnd" in base:
                        self._revalidate(base)
                    current_crop = self._crop_region(region, region_space, image, base)
                    if crop is not None and crop != current_crop:
                        raise CaptureTargetChanged("recording crop geometry changed")
                    crop = current_crop
                    image = image.crop(tuple(crop))
                else:
                    shot = self.screenshot(
                        target,
                        max_dim=0,
                        format="png",
                        region=region,
                        region_space=region_space,
                        capture_mode=capture_mode,
                        grabber=grabber,
                        scope=scope,
                        remember=False,
                        cancel_event=cancel_event,
                        deadline=deadline,
                    )
                    if base is None:
                        base = shot.metadata
                        target = f"hwnd:{base['hwnd']}"
                        if binding:
                            from .coordinates import validate_window

                            validate_window(binding, base)
                    else:
                        from .coordinates import validate_window

                        validate_window(base, shot.metadata, strong=False)
                    image = _image_from_grab(shot.image_bytes)
                    crop = shot.metadata["region"]
                if images and image.size != images[0].size:
                    raise CaptureTargetChanged("recording frame dimensions changed")
                total_bytes += image.width * image.height * 3
                if total_bytes > 512 * 1024 * 1024:
                    raise ValueError("recording frame memory exceeds 512 MiB")
                images.append(image)
                timestamps.append(time.monotonic() - started)
            if not images:
                raise RuntimeError("capture produced no frames")
            sampled_at = time.monotonic()
            montage_images = list(images)
            all_timestamps = list(timestamps)
            # Internal DSL shots are explicit extra montage samples, not timed
            # encoder frames and never a replacement for the clickable context.
            for extra in list(extra_frames or []):
                self._checkpoint(cancel_event, deadline)
                if isinstance(extra, CaptureFrame):
                    extra_image = _image_from_grab(extra.image_bytes)
                    stamp = extra.metadata.get("timestamp_monotonic", started) - started
                elif isinstance(extra, dict) and isinstance(extra.get("frame"), CaptureFrame):
                    extra_image = _image_from_grab(extra["frame"].image_bytes)
                    stamp = extra.get(
                        "timestamp",
                        extra["frame"].metadata.get("timestamp_monotonic", started) - started,
                    )
                else:
                    raise ValueError(
                        "extra_frames must contain CaptureFrame objects or frame/timestamp objects"
                    )
                total_bytes += extra_image.width * extra_image.height * 3
                if total_bytes > 512 * 1024 * 1024:
                    raise ValueError("recording frame memory exceeds 512 MiB")
                montage_images.append(extra_image)
                all_timestamps.append(max(0.0, float(stamp)))
            ordered = sorted(
                zip(all_timestamps, montage_images, strict=True), key=lambda item: item[0]
            )
            all_timestamps, montage_images = map(list, zip(*ordered, strict=True))
            from PIL import Image, ImageDraw

            selected_count = min(montage_frames, len(montage_images))
            chosen_indices = [
                round(i * (len(montage_images) - 1) / max(1, selected_count - 1))
                for i in range(selected_count)
            ]
            chosen = [montage_images[index] for index in chosen_indices]
            montage_timestamps = [all_timestamps[index] for index in chosen_indices]
            thumb_w = min(512, max(image.width for image in chosen))
            thumbs = []
            for image in chosen:
                ratio = thumb_w / image.width
                thumbs.append(image.resize((thumb_w, max(1, round(image.height * ratio)))))
            columns = min(3, len(thumbs))
            rows = (len(thumbs) + columns - 1) // columns
            cell_h = max(image.height for image in thumbs) + 24
            montage = Image.new("RGB", (thumb_w * columns, cell_h * rows), "black")
            draw = ImageDraw.Draw(montage)
            for i, image in enumerate(thumbs):
                x, y = (i % columns) * thumb_w, (i // columns) * cell_h
                montage.paste(image, (x, y))
                draw.text(
                    (x + 4, y + image.height + 2), f"{montage_timestamps[i]:.3f}s", fill="white"
                )
            montage_original_size = list(montage.size)
            ratio = 1.0 if max_dim == 0 else min(1.0, max_dim / max(montage.size))
            if ratio < 1:
                montage = montage.resize(
                    (max(1, round(montage.width * ratio)), max(1, round(montage.height * ratio)))
                )
            montage_scale = [
                montage.width / montage_original_size[0],
                montage.height / montage_original_size[1],
            ]
            encoded, mime = _encode(montage, format, quality=effective_quality)
            self._checkpoint(cancel_event, deadline)
            self._ensure_store()
            if not self._artifact_store.group_open:
                group_token = self._artifact_store.begin_group()
            montage_path = self._allocate(".jpg" if format == "jpeg" else f".{format}")
            try:
                self._write_artifact(montage_path, encoded)
                self._register(montage_path)
            except BaseException:
                self._discard_artifact(montage_path)
                raise
            warning = None
            self._last_encoder_diagnostics = {}
            video_path = self._write_video(
                images, fps, cancel_event=cancel_event, deadline=deadline
            )
            if video_path is None:
                diagnostic = self._last_encoder_diagnostics
                warning = (
                    f"{diagnostic.get('backend', 'pyav')} video encoding failed "
                    f"({diagnostic.get('code', 'VIDEO_ENCODING_FAILED')}): "
                    f"{diagnostic.get('message', 'no MP4 was created')}"
                )
            frame_paths = []
            index_path = None
            if keep_frames:
                for index, (image, stamp) in enumerate(
                    zip(montage_images, all_timestamps, strict=True)
                ):
                    self._checkpoint(cancel_event, deadline)
                    frame_path = self._save_artifact(".png", _encode(image, "png")[0])
                    frame_paths.append(
                        {"index": index, "timestamp": stamp, "path": str(frame_path)}
                    )
                index_path = self._save_artifact(
                    ".json", json.dumps({"frames": frame_paths}, separators=(",", ":")).encode()
                )
            elapsed = max(sampled_at - started, 1e-9)
            achieved_fps = (
                (len(images) - 1) / (timestamps[-1] - timestamps[0])
                if len(images) > 1 and timestamps[-1] > timestamps[0]
                else len(images) / elapsed
            )
            static = len(images) > 1 and all(
                image.tobytes() == images[0].tobytes() for image in images[1:]
            )
            metadata = {
                **base,
                "timestamps": timestamps,
                "montage_timestamps": montage_timestamps,
                "frames": len(images),
                "fps": fps,
                "seconds": seconds,
                "achieved_fps": achieved_fps,
                "elapsed_seconds": elapsed,
                "static_frames": static,
                "logical_size": list(images[0].size),
                "encoded_size": [v + v % 2 for v in images[0].size] if video_path else None,
                "physical_transform": None,
                "coordinate_space": "montage",
                "output_size": list(montage.size),
                "width": montage.width,
                "height": montage.height,
                "original_size": montage_original_size,
                "montage_original_size": montage_original_size,
                "scale": montage_scale,
                "format": format,
                "quality": effective_quality,
                "quality_applied": format != "png" and effective_quality is not None,
                "image_bytes": len(encoded),
                "mime_type": mime,
                "region": crop,
                "region_space": region_space,
                "extra_frames": len(montage_images) - len(images),
                "frame_paths": [entry["path"] for entry in frame_paths],
                "frame_index_path": str(index_path) if index_path else None,
                "montage_path": str(montage_path),
                "mp4_path": str(video_path) if video_path else None,
                "encoder_backend": self._last_encoder_diagnostics.get("backend", "pyav"),
                "encoder_diagnostics": dict(self._last_encoder_diagnostics),
                "artifact_retention": self._artifact_store.prune(),
            }
            if warning:
                metadata["warning"] = warning
                metadata["encoder_error"] = dict(self._last_encoder_diagnostics)
            if static:
                metadata.setdefault("warnings", []).append(
                    {
                        "code": "STATIC_FRAMES",
                        "detail": "All sampled frames are identical; capture may be static",
                    }
                )
            self._checkpoint(cancel_event, deadline)
            returned_metadata = metadata
            return CaptureFrame(encoded, mime, metadata)
        finally:
            if group_token is not None:
                try:
                    self._artifact_store.end_group(group_token)
                except Exception as exc:
                    if returned_metadata is not None:
                        returned_metadata.setdefault("warnings", []).append(
                            {
                                "code": "ARTIFACT_RETENTION_FAILED",
                                "detail": type(exc).__name__,
                            }
                        )
            if owned and hasattr(adapter, "close"):
                adapter.close()

    def _write_video(
        self,
        images: list[Any],
        fps: float,
        stamp: str = "",
        *,
        cancel_event: Any = None,
        deadline: float | None = None,
    ) -> Path | None:
        from .video_encoder import VideoEncoder, VideoEncodingError

        self._checkpoint(cancel_event, deadline)
        self._last_encoder_diagnostics = {}
        if not images or any(image.size != images[0].size for image in images):
            raise ValueError("encoder requires nonempty identical frame dimensions")
        self._ensure_store()
        path = self._allocate(".mp4")
        registered = False
        try:
            with self._lock:
                if self._closed:
                    raise RuntimeError("capture service is closed")
                if self._encoder is None:
                    self._encoder = VideoEncoder()
                encoder = self._encoder
            encoded = encoder.encode(
                images,
                fps,
                cancel_event=cancel_event,
                deadline=deadline,
                max_bytes=min(self._artifact_store.max_bytes, 256 * 1024**2),
            )
            self._checkpoint(cancel_event, deadline)
            if len(encoded.data) > self._artifact_store.max_bytes:
                raise VideoEncodingError(
                    "VIDEO_OUTPUT_LIMIT", "encoded video exceeds artifact store byte limit"
                )
            if not encoded.data:
                raise VideoEncodingError("VIDEO_EMPTY_OUTPUT", "encoder returned no video bytes")
            self._write_artifact(path, encoded.data)
            self._register(path)
            registered = True
            self._last_encoder_diagnostics = dict(encoded.metadata)
            return path
        except TimeoutError:
            raise
        except (VideoEncodingError, OSError, ValueError) as exc:
            self._last_encoder_diagnostics = {
                "backend": "pyav",
                "code": getattr(exc, "code", "VIDEO_ENCODING_FAILED"),
                "message": "".join(
                    c for c in str(getattr(exc, "message", exc)) if c.isprintable() or c in "\n\t"
                )[:4096],
            }
            return None
        finally:
            if not registered:
                self._discard_artifact(path)

    def _save_artifact(self, suffix: str, data: bytes) -> Path:
        path = self._allocate(suffix)
        try:
            self._write_artifact(path, data)
            self._register(path)
        except BaseException:
            self._discard_artifact(path)
            raise
        return path

    def close(self) -> None:
        with self._lock:
            self._closed = True
            encoder = self._encoder
            self._encoder = None
        if encoder is not None:
            encoder.close()
        if self._owns_artifact_store and self._artifact_store is not None:
            self._artifact_store.close()
