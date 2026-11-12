"""Seven-tool MCP 1.x stdio entry point. All logging belongs on stderr."""

from __future__ import annotations

import argparse
import asyncio
import json
import threading
import uuid
import weakref
from contextlib import asynccontextmanager
from importlib.metadata import version
from pathlib import Path

import anyio
from mcp.server.fastmcp import Context, FastMCP, Image
from mcp.types import CallToolResult, TextContent, ToolAnnotations

from .contracts import STOP_STATUSES
from .output import bound_result
from .runtime import Runtime


def _response(value, operation, max_output_chars=24000, max_output_bytes=65536):
    frame = None
    if not isinstance(value, dict):
        frame = value
        value = {"status": "ok", "operation": operation, "data": frame.metadata}
    elif "frame" in value:
        value = dict(value)
        frame = value.pop("frame")
    value = bound_result(value, max_output_chars, max_output_bytes)
    encoded = json.dumps(value, ensure_ascii=False, allow_nan=False)
    content = [TextContent(type="text", text=encoded)]
    if frame is not None:
        image = Image(data=frame.image_bytes, format=frame.mime_type.split("/", 1)[1])
        content.append(image.to_image_content())
    return CallToolResult(
        content=content, structuredContent=value, isError=value.get("status") in STOP_STATUSES
    )


def create_server(
    runtime=None,
    *,
    allow_native=False,
    output_dir=None,
    trace_enabled=False,
    artifact_max_bytes=256 * 1024 * 1024,
    artifact_max_files=200,
    allow_shell=False,
    allow_process_control=False,
):
    owned = (
        runtime
        if runtime is not None
        else Runtime(
            allow_native=allow_native,
            output_dir=output_dir,
            trace_enabled=trace_enabled,
            artifact_max_bytes=artifact_max_bytes,
            artifact_max_files=artifact_max_files,
            allow_shell=allow_shell,
            allow_process_control=allow_process_control,
        )
    )

    @asynccontextmanager
    async def lifespan(_):
        try:
            yield {"runtime": owned}
        finally:
            owned.controller.close()
            with anyio.CancelScope(shield=True):
                await asyncio.to_thread(owned.close)

    app = FastMCP("minano-computer-use", lifespan=lifespan, log_level="WARNING")
    session_scopes = weakref.WeakKeyDictionary()
    local_scope = uuid.uuid4().hex

    def caller_scope(ctx):
        # Context is injected by FastMCP, not a caller-controlled schema field.
        # Request IDs change on every call; the session object is stable per client.
        if ctx is None:
            return local_scope
        try:
            request_context = ctx.request_context
        except ValueError as exc:
            if str(exc) != "Context is not available outside of a request":
                raise
            # Direct FastMCP.call_tool has no transport request. This opaque
            # local scope is distinct from all real session/conversation scopes.
            return local_scope
        session = request_context.session
        if session not in session_scopes:
            session_scopes[session] = {}
        meta = request_context.meta
        metadata = meta.model_dump() if meta is not None else {}
        conversation = metadata.get("ai.opencode/sessionID")
        if not isinstance(conversation, str) or not conversation:
            conversation = None
        scopes = session_scopes[session]
        if conversation not in scopes:
            scopes[conversation] = uuid.uuid4().hex
        return scopes[conversation]

    async def invoke(method, *args, **kwargs):
        request_cancel = threading.Event()
        task = asyncio.create_task(
            asyncio.to_thread(method, *args, _request_cancel=request_cancel, **kwargs)
        )
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            owned.cancel_request(request_cancel)
            # Cleanup (native releases, capture joins, broker reap) completes before return.
            with anyio.CancelScope(shield=True):
                while not task.done():
                    try:
                        await asyncio.shield(task)
                    except asyncio.CancelledError:
                        continue
                task.result()
            raise

    @app.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True))
    async def window(
        action: str = "list",
        target: str | None = None,
        selector: str | dict | None = None,
        value: str | None = None,
        depth: int = 8,
        max_nodes: int = 500,
        timeout: float = 5.0,
        direction: str | None = None,
        amount: str | None = None,
        query: str | None = None,
        match: str = "contains",
        limit: int | None = None,
        compact: bool = False,
        include_tokens: bool = False,
        token: str | None = None,
        condition: dict | None = None,
        poll_interval: float = 0.05,
        max_output_chars: int = 24000,
        max_output_bytes: int = 65536,
        window: str | None = None,
        role: str | None = None,
        nth: int | None = None,
        native: bool = False,
        allow_process_control: bool = False,
        details: bool = False,
    ) -> CallToolResult:
        """List/read windows or use semantic UIA patterns; never physically falls back."""
        kwargs = dict(
            depth=depth,
            max_nodes=max_nodes,
            timeout=timeout,
            compact=compact,
            include_tokens=include_tokens,
            token=token,
            max_output_chars=max_output_chars,
            max_output_bytes=max_output_bytes,
            native=native,
            allow_process_control=allow_process_control,
        )
        for key, item in (("window", window), ("role", role), ("nth", nth)):
            if item is not None:
                kwargs[key] = item
        if action == "list":
            # Preserve the original default list API (no default 20-item truncation).
            if query is not None or details or limit is not None:
                kwargs.update(query=query, details=details)
                if limit is not None:
                    kwargs["limit"] = limit
        if action == "find":
            kwargs.update(query=query, match=match, limit=20 if limit is None else limit)
        if query is not None and action in {
            "get_text",
            "ui_tree",
            "focus",
            "force_focus",
            "close",
            "click_element",
        }:
            kwargs["query"] = query
        if action == "wait_for":
            kwargs.update(condition=condition, poll_interval=poll_interval)
        for key, item in (("value", value), ("direction", direction), ("amount", amount)):
            if item is not None:
                kwargs[key] = item
        kwargs = {key: value for key, value in kwargs.items() if value is not None}
        return _response(
            await invoke(owned.window, action, target, selector, **kwargs),
            "window",
            max_output_chars,
            max_output_bytes,
        )

    @app.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True))
    async def act(
        actions: list[dict],
        target: str | None = None,
        native: bool = False,
        stop_on_error: bool = True,
        timeout: float = 5.0,
        max_output_chars: int = 24000,
        max_output_bytes: int = 65536,
        coordinate_space: str = "screen",
        capture_id: str | None = None,
        screenshot: bool | str = "auto",
        settle_ms: float = 80,
        focus: bool | str | None = None,
        key_method: str = "scancode",
        ctx: Context = None,
    ) -> CallToolResult:
        """Run up to 100 ordered actions. Native requires explicit native=true per call.

        Explicit native force_focus restores/attaches input threads and verifies the exact
        foreground HWND; focus keeps conservative SetForegroundWindow behavior. Neither
        is automatically used before keyboard or mouse actions.
        Auto feedback returns an image for native calls, compact text for strict UIA;
        screenshot=false disables feedback. Observation does not verify game effects.
        """
        return _response(
            await invoke(
                owned.act,
                actions,
                target,
                native,
                stop_on_error,
                timeout,
                max_output_chars=max_output_chars,
                max_output_bytes=max_output_bytes,
                coordinate_space=coordinate_space,
                capture_id=capture_id,
                screenshot=screenshot,
                settle_ms=settle_ms,
                focus=focus,
                key_method=key_method,
                scope=caller_scope(ctx),
            ),
            "act",
            max_output_chars,
            max_output_bytes,
        )

    @app.tool(annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False))
    async def screenshot(
        target: str = "desktop",
        max_dim: int = 800,
        format: str = "jpeg",
        region: list[int] | dict | None = None,
        capture_mode: str = "screen",
        allow_fallback: bool = False,
        timeout: float = 5.0,
        max_output_chars: int = 24000,
        max_output_bytes: int = 65536,
        region_space: str = "surface",
        save: bool = False,
        define_viewport: str | None = None,
        foreground: bool = False,
        quality: int = 75,
        ctx: Context = None,
    ) -> CallToolResult:
        """Return a screen-composite MCP image, separately from JSON metadata."""
        return _response(
            await invoke(
                owned.screenshot,
                target,
                max_dim,
                format,
                region,
                capture_mode,
                allow_fallback,
                timeout,
                max_output_chars=max_output_chars,
                max_output_bytes=max_output_bytes,
                region_space=region_space,
                save=save,
                define_viewport=define_viewport,
                foreground=foreground,
                quality=quality,
                scope=caller_scope(ctx),
            ),
            "screenshot",
            max_output_chars,
            max_output_bytes,
        )

    @app.tool(annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False))
    async def record(
        target: str = "desktop",
        seconds: float = 3.0,
        fps: float = 10,
        montage_frames: int = 6,
        max_output_chars: int = 24000,
        max_output_bytes: int = 65536,
        region: list[int] | dict | None = None,
        region_space: str = "surface",
        keep_frames: bool = False,
        capture_mode: str = "screen",
        foreground: bool = False,
        max_dim: int = 800,
        format: str = "jpeg",
        quality: int = 75,
        ctx: Context = None,
    ) -> CallToolResult:
        """Bounded screen recording and timestamped montage; no focus activation."""
        return _response(
            await invoke(
                owned.record,
                target,
                seconds,
                fps,
                montage_frames,
                max_output_chars=max_output_chars,
                max_output_bytes=max_output_bytes,
                region=region,
                region_space=region_space,
                keep_frames=keep_frames,
                capture_mode=capture_mode,
                foreground=foreground,
                max_dim=max_dim,
                format=format,
                quality=quality,
                scope=caller_scope(ctx),
            ),
            "record",
            max_output_chars,
            max_output_bytes,
        )

    @app.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True))
    async def play(
        actions: list[dict] | None = None,
        target: str | None = None,
        native: bool = False,
        seconds: float = 3.0,
        fps: float = 10,
        stop_on_error: bool = True,
        timeout: float = 5.0,
        max_output_chars: int = 24000,
        max_output_bytes: int = 65536,
        script: str | None = None,
        montage_frames: int = 6,
        probe: str | dict | None = None,
        coordinate_space: str = "screen",
        capture_id: str | None = None,
        key_method: str = "scancode",
        allow_shell: bool = False,
        max_dim: int = 800,
        format: str = "jpeg",
        quality: int = 75,
        ctx: Context = None,
    ) -> CallToolResult:
        """Timed actions (at seconds) alongside capture; no strict frame/action pairing."""
        return _response(
            await invoke(
                owned.play,
                actions,
                target,
                native,
                seconds,
                fps,
                stop_on_error,
                timeout,
                max_output_chars=max_output_chars,
                max_output_bytes=max_output_bytes,
                script=script,
                montage_frames=montage_frames,
                probe=probe,
                coordinate_space=coordinate_space,
                capture_id=capture_id,
                key_method=key_method,
                allow_shell=allow_shell,
                max_dim=max_dim,
                format=format,
                quality=quality,
                scope=caller_scope(ctx),
            ),
            "play",
            max_output_chars,
            max_output_bytes,
        )

    @app.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True))
    async def process(
        action: str = "list",
        argv: list[str] | None = None,
        exe: str | None = None,
        args: list[str] | None = None,
        cwd: str | None = None,
        shell: bool = False,
        command: str | None = None,
        timeout: float = 10,
        pid: int | None = None,
        name: str | None = None,
        target: str | None = None,
        all: bool = False,
        dir: str | None = None,
        glob: str | None = None,
        query: str | None = None,
        ready: str = "exists",
        shell_kind: str = "powershell",
        allow_shell: bool = False,
        allow_process_control: bool = False,
        max_output_chars: int = 24000,
        max_output_bytes: int = 65536,
    ) -> CallToolResult:
        """Process inventory, launch and waits; shell/control require startup AND call opt-ins."""
        return _response(
            await invoke(
                owned.process,
                action,
                argv,
                exe=exe,
                args=args,
                cwd=cwd,
                shell=shell,
                command=command,
                timeout=timeout,
                pid=pid,
                name=name,
                target=target,
                all=all,
                dir=dir,
                glob=glob,
                query=query,
                ready=ready,
                shell_kind=shell_kind,
                allow_shell=allow_shell,
                allow_process_control=allow_process_control,
                max_output_chars=max_output_chars,
                max_output_bytes=max_output_bytes,
            ),
            "process",
            max_output_chars,
            max_output_bytes,
        )

    @app.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False))
    async def system(
        action: str = "status",
        text: str | None = None,
        name: str | None = None,
        ctx: Context = None,
    ) -> CallToolResult:
        """Read status/displays or request cancellation (acceptance is not cleanup)."""
        # This is deliberately a direct fast path: cancel must not wait for the executor.
        if action in {"cancel", "status"}:
            return _response(owned.system(action), "system")
        kwargs = {key: item for key, item in (("text", text), ("name", name)) if item is not None}
        return _response(
            await invoke(owned.system, action, scope=caller_scope(ctx), **kwargs), "system"
        )

    return app


def main():
    parser = argparse.ArgumentParser(description="Strict UIA computer-use MCP (stdio)")
    parser.add_argument(
        "--allow-native",
        action="store_true",
        help="Deprecated compatibility no-op; use native=true per call",
    )
    parser.add_argument("--allow-shell", action="store_true")
    parser.add_argument("--allow-process-control", action="store_true")
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--trace", action="store_true")
    parser.add_argument("--artifact-max-mb", type=int, default=256)
    parser.add_argument("--artifact-max-files", type=int, default=200)
    parser.add_argument("--version", action="version", version=version("minano-computer-use"))
    args = parser.parse_args()
    runtime = Runtime(
        allow_native=args.allow_native,
        allow_shell=args.allow_shell,
        allow_process_control=args.allow_process_control,
        output_dir=args.output_dir,
        trace_enabled=args.trace,
        artifact_max_bytes=args.artifact_max_mb * 1024 * 1024,
        artifact_max_files=args.artifact_max_files,
    )
    try:
        create_server(runtime).run(transport="stdio")
    finally:
        runtime.close()


if __name__ == "__main__":
    main()
