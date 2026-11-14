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
from typing import Annotated

import anyio
from mcp.server.fastmcp import Context, FastMCP, Image
from mcp.types import CallToolResult, TextContent, ToolAnnotations

from .contracts import STOP_STATUSES
from .output import bound_result
from .runtime import Runtime
from .tool_schema import SERVER_INSTRUCTIONS, parameter
from .wire import public_result


def _response(value, operation, max_output_chars=24000, max_output_bytes=65536):
    frame = None
    if not isinstance(value, dict):
        frame = value
        value = {"status": "ok", "operation": operation, "data": frame.metadata}
    elif "frame" in value:
        value = dict(value)
        frame = value.pop("frame")
    value = bound_result(public_result(value), max_output_chars, max_output_bytes)
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

    app = FastMCP(
        "minano-computer-use",
        instructions=SERVER_INSTRUCTIONS,
        lifespan=lifespan,
        log_level="WARNING",
    )
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
        action: Annotated[str, parameter("window", "action")] = "list",
        target: Annotated[str | None, parameter("window", "target")] = None,
        selector: Annotated[str | dict | None, parameter("window", "selector")] = None,
        value: Annotated[str | None, parameter("window", "value")] = None,
        depth: Annotated[int, parameter("window", "depth")] = 8,
        max_nodes: Annotated[int, parameter("window", "max_nodes")] = 500,
        timeout: Annotated[float, parameter("window", "timeout")] = 5.0,
        direction: Annotated[str | None, parameter("window", "direction")] = None,
        amount: Annotated[str | None, parameter("window", "amount")] = None,
        query: Annotated[str | None, parameter("window", "query")] = None,
        match: Annotated[str, parameter("window", "match")] = "contains",
        limit: Annotated[int | None, parameter("window", "limit")] = None,
        compact: Annotated[bool, parameter("window", "compact")] = False,
        include_tokens: Annotated[bool, parameter("window", "include_tokens")] = False,
        token: Annotated[str | None, parameter("window", "token")] = None,
        condition: Annotated[dict | None, parameter("window", "condition")] = None,
        poll_interval: Annotated[float, parameter("window", "poll_interval")] = 0.05,
        max_output_chars: Annotated[int, parameter("window", "max_output_chars")] = 24000,
        max_output_bytes: Annotated[int, parameter("window", "max_output_bytes")] = 65536,
        window: Annotated[str | None, parameter("window", "window")] = None,
        role: Annotated[str | None, parameter("window", "role")] = None,
        nth: Annotated[int | None, parameter("window", "nth")] = None,
        native: Annotated[bool, parameter("window", "native")] = False,
        allow_process_control: Annotated[
            bool, parameter("window", "allow_process_control")
        ] = False,
        details: Annotated[bool, parameter("window", "details")] = False,
        rich: Annotated[bool | None, parameter("window", "rich")] = None,
        text_limit: Annotated[int | None, parameter("window", "text_limit")] = None,
        include_ax: Annotated[bool, parameter("window", "include_ax")] = True,
        include_screenshot: Annotated[bool, parameter("window", "include_screenshot")] = True,
        max_dim: Annotated[int, parameter("window", "max_dim")] = 800,
        format: Annotated[str, parameter("window", "format")] = "jpeg",
        quality: Annotated[int, parameter("window", "quality")] = 75,
        capture_mode: Annotated[str, parameter("window", "capture_mode")] = "screen",
        allow_fallback: Annotated[bool, parameter("window", "allow_fallback")] = False,
        ctx: Context = None,
    ) -> CallToolResult:
        """Discover windows/controls, read state, or perform one semantic UIA action.

        list finds unknown windows; state combines UIA+image; find/inspect narrows reads.
        With include_tokens=true, use eligible nodes' tokens without another find.
        Example: {"action":"state","target":"hwnd:0x123","include_tokens":true}.
        Prefer wait_for for known UIA conditions; act for batches, screenshot for pixels.
        Truncated output is not proof of absence; narrow/extend reads. Rediscover stale
        references; indexes are not tokens. Writes modify the app; set_value writes once
        and confirms the same element <=600ms within timeout, without replay.
        close needs both process-control permissions and may lose work. UIA never falls
        back to real input. Only explicit native focus/force_focus activates, though
        providers may change focus. Unsupported/ambiguous targets refuse.
        Returns bounded JSON plus optional image; state components have independent
        timing/errors and are non-atomic.
        """
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
        if action in {"state", "tree", "text", "find", "inspect", "get_text", "ui_tree"}:
            kwargs["rich"] = action == "state" if rich is None else rich
            if text_limit is not None or action == "state":
                kwargs["text_limit"] = 500 if text_limit is None else text_limit
        if action == "state":
            kwargs.update(
                include_ax=include_ax,
                include_screenshot=include_screenshot,
                max_dim=max_dim,
                format=format,
                quality=quality,
                capture_mode=capture_mode,
                allow_fallback=allow_fallback,
            )
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
            await invoke(owned.window, action, target, selector, scope=caller_scope(ctx), **kwargs),
            "window",
            max_output_chars,
            max_output_bytes,
        )

    @app.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True))
    async def act(
        actions: Annotated[list[dict], parameter("act", "actions")],
        target: Annotated[str | None, parameter("act", "target")] = None,
        native: Annotated[bool, parameter("act", "native")] = False,
        stop_on_error: Annotated[bool, parameter("act", "stop_on_error")] = True,
        timeout: Annotated[float, parameter("act", "timeout")] = 5.0,
        max_output_chars: Annotated[int, parameter("act", "max_output_chars")] = 24000,
        max_output_bytes: Annotated[int, parameter("act", "max_output_bytes")] = 65536,
        coordinate_space: Annotated[str, parameter("act", "coordinate_space")] = "screen",
        capture_id: Annotated[str | None, parameter("act", "capture_id")] = None,
        screenshot: Annotated[bool | str, parameter("act", "screenshot")] = "auto",
        settle_ms: Annotated[float, parameter("act", "settle_ms")] = 80,
        focus: Annotated[bool | str | None, parameter("act", "focus")] = None,
        key_method: Annotated[str, parameter("act", "key_method")] = "scancode",
        input_mode: Annotated[str | None, parameter("act", "input_mode")] = None,
        feedback: Annotated[str, parameter("act", "feedback")] = "compact",
        ctx: Context = None,
    ) -> CallToolResult:
        """Execute 1..100 ordered actions with post-action observation in the SAME call.

        Reuse the returned image/data.after; do not routinely call screenshot next.
        Default screenshot="auto": UIA=text, native=image, message=state+image when
        available. Use screenshot=true for a UIA image, false for none. Cancellation,
        deadline or budgets may omit feedback. Read again for missing/failed feedback
        or new observation requirements, not after every action.
        Batch predetermined steps; inspect when the next step depends on results.
        Prefer wait_for for UIA readiness; play for timed recording.
        Examples (replace HWNDs with observed values):
        UIA: {"target":"hwnd:0x123","screenshot":true,"actions":[
        {"action":"invoke","selector":{"automation_id":"ok"}}]}.
        Native: {"target":"hwnd:0x123","native":true,"actions":[
        {"action":"hold","key":"W","seconds":0.2}]}.
        Message: {"target":"hwnd:0x123","input_mode":"message","actions":[
        {"action":"set_value","recipient":"hwnd:0x124","value":"hello"}]}.

        UIA never falls back; native needs foreground and explicit focus/force_focus.
        Background input_mode="message" is not key_method="message".
        Whole-plan validation precedes effects; stop on errors by default, no rollback.
        Returns results/counts/receipts/cleanup separately from data.after. An image
        is not action proof; observation failure is not action failure. Accepted input
        is not consumption. Inspect uncertain effects before deciding; never replay
        automatically. timeout is not a universal whole-batch deadline; see its scope.
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
                input_mode=input_mode,
                feedback=feedback,
                scope=caller_scope(ctx),
            ),
            "act",
            max_output_chars,
            max_output_bytes,
        )

    @app.tool(annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False))
    async def screenshot(
        target: Annotated[str, parameter("screenshot", "target")] = "desktop",
        max_dim: Annotated[int, parameter("screenshot", "max_dim")] = 800,
        format: Annotated[str, parameter("screenshot", "format")] = "jpeg",
        region: Annotated[list[int] | dict | None, parameter("screenshot", "region")] = None,
        capture_mode: Annotated[str, parameter("screenshot", "capture_mode")] = "screen",
        allow_fallback: Annotated[bool, parameter("screenshot", "allow_fallback")] = False,
        timeout: Annotated[float, parameter("screenshot", "timeout")] = 5.0,
        max_output_chars: Annotated[int, parameter("screenshot", "max_output_chars")] = 24000,
        max_output_bytes: Annotated[int, parameter("screenshot", "max_output_bytes")] = 65536,
        region_space: Annotated[str, parameter("screenshot", "region_space")] = "surface",
        save: Annotated[bool, parameter("screenshot", "save")] = False,
        define_viewport: Annotated[str | None, parameter("screenshot", "define_viewport")] = None,
        foreground: Annotated[bool, parameter("screenshot", "foreground")] = False,
        quality: Annotated[int, parameter("screenshot", "quality")] = 75,
        ctx: Context = None,
    ) -> CallToolResult:
        """Capture one desktop/display/window/crop image; no input or activation.

        Do NOT call this routinely after act: reuse its returned image/data.after.
        UIA post-action image: set screenshot=true on act. Read separately for missing/
        failed feedback or a new time, target, crop, resolution or backend. Use window
        state for UIA+image, record for a time series, play for actions+recording.
        Screen capture may include occluders; window backends need allow_fallback=true
        to fall back to screen. WGC is optional. Images may contain sensitive pixels
        despite UIA redaction. save persists the original; define_viewport names a crop.
        For image clicks, set coordinate_space="image", pass capture_id and an explicit
        window target. Never use resized image pixels as screen pixels or remove stale
        bindings; reobserve instead. No guessed/clamped points.
        Returns MCP image plus JSON backend/geometry/transform/capture_id/times/path.
        Example: {"target":"hwnd:0x123","capture_mode":"printwindow","save":true}.
        """
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
        target: Annotated[str, parameter("record", "target")] = "desktop",
        seconds: Annotated[float, parameter("record", "seconds")] = 3.0,
        fps: Annotated[float, parameter("record", "fps")] = 10,
        montage_frames: Annotated[int, parameter("record", "montage_frames")] = 6,
        max_output_chars: Annotated[int, parameter("record", "max_output_chars")] = 24000,
        max_output_bytes: Annotated[int, parameter("record", "max_output_bytes")] = 65536,
        region: Annotated[list[int] | dict | None, parameter("record", "region")] = None,
        region_space: Annotated[str, parameter("record", "region_space")] = "surface",
        keep_frames: Annotated[bool, parameter("record", "keep_frames")] = False,
        capture_mode: Annotated[str, parameter("record", "capture_mode")] = "screen",
        foreground: Annotated[bool, parameter("record", "foreground")] = False,
        max_dim: Annotated[int, parameter("record", "max_dim")] = 800,
        format: Annotated[str, parameter("record", "format")] = "jpeg",
        quality: Annotated[int, parameter("record", "quality")] = 75,
        ctx: Context = None,
    ) -> CallToolResult:
        """Observe 0.05..60 seconds without input or activation; use play to include actions.

        screenshot is for one frame. Screen mode may record occluders. Example:
        {"target":"desktop","seconds":3,"fps":5,"montage_frames":4}.
        Returns MCP montage plus JSON sample times/achieved_fps/backend, H.264 MP4
        path or encoding warning, and optional PNG/index paths. Check recording and
        encoding evidence separately. Sensitive artifacts obey memory/storage quotas;
        montage resizing does not resize originals. Montages are not click maps or
        exact action/frame pairs. Cancel stops sampling/encoding with cleanup, not
        rollback of saved artifacts.
        """
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
        actions: Annotated[list[dict] | None, parameter("play", "actions")] = None,
        target: Annotated[str | None, parameter("play", "target")] = None,
        native: Annotated[bool, parameter("play", "native")] = False,
        seconds: Annotated[float, parameter("play", "seconds")] = 3.0,
        fps: Annotated[float, parameter("play", "fps")] = 10,
        stop_on_error: Annotated[bool, parameter("play", "stop_on_error")] = True,
        timeout: Annotated[float, parameter("play", "timeout")] = 5.0,
        max_output_chars: Annotated[int, parameter("play", "max_output_chars")] = 24000,
        max_output_bytes: Annotated[int, parameter("play", "max_output_bytes")] = 65536,
        script: Annotated[str | None, parameter("play", "script")] = None,
        montage_frames: Annotated[int, parameter("play", "montage_frames")] = 6,
        probe: Annotated[str | dict | None, parameter("play", "probe")] = None,
        coordinate_space: Annotated[str, parameter("play", "coordinate_space")] = "screen",
        capture_id: Annotated[str | None, parameter("play", "capture_id")] = None,
        key_method: Annotated[str, parameter("play", "key_method")] = "scancode",
        allow_shell: Annotated[bool, parameter("play", "allow_shell")] = False,
        max_dim: Annotated[int, parameter("play", "max_dim")] = 800,
        format: Annotated[str, parameter("play", "format")] = "jpeg",
        quality: Annotated[int, parameter("play", "quality")] = 75,
        input_mode: Annotated[str | None, parameter("play", "input_mode")] = None,
        feedback: Annotated[str, parameter("play", "feedback")] = "compact",
        ctx: Context = None,
    ) -> CallToolResult:
        """Run timed actions or sequential DSL with recording; use act for ordinary batches.

        Reuse the returned montage for this interval, not a ritual screenshot.
        Montages are not click maps; record observes without input.
        Exactly one of actions/script; max100 expanded actions. seconds=0.1..30;
        at is relative, nondecreasing and <=seconds, not a global timeline.
        Sequential execution; late actions run immediately. Key/mouse DSL needs
        native/message; UIA uses actions. Timing scopes are in timeout's description.
        Example: {"target":"hwnd:0x123","native":true,"seconds":2,"actions":[
        {"at":0,"action":"hold","key":"W","seconds":0.2}]}.
        Default UIA has no fallback. Native requires foreground; explicit focus only,
        loss stops by default, never auto-refocus. Message is background, not a game
        bypass. probe/until shell needs --allow-shell AND allow_shell=true.
        Whole-plan validation, no rollback/replay of partial effects.
        Returns data.actions.data results/counts/cleanup separately from data.capture
        and optional montage/MP4. Capture failure is not action failure; accepted
        input is not consumption. No exact action/frame pairs. Final cancellation
        return follows owned-input release attempts and capture/encoder cleanup.
        """
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
                input_mode=input_mode,
                feedback=feedback,
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
        action: Annotated[str, parameter("process", "action")] = "list",
        argv: Annotated[list[str] | None, parameter("process", "argv")] = None,
        exe: Annotated[str | None, parameter("process", "exe")] = None,
        args: Annotated[list[str] | None, parameter("process", "args")] = None,
        cwd: Annotated[str | None, parameter("process", "cwd")] = None,
        shell: Annotated[bool, parameter("process", "shell")] = False,
        command: Annotated[str | None, parameter("process", "command")] = None,
        timeout: Annotated[float, parameter("process", "timeout")] = 10,
        pid: Annotated[int | None, parameter("process", "pid")] = None,
        name: Annotated[str | None, parameter("process", "name")] = None,
        target: Annotated[str | None, parameter("process", "target")] = None,
        all: Annotated[bool, parameter("process", "all")] = False,
        dir: Annotated[str | None, parameter("process", "dir")] = None,
        glob: Annotated[str | None, parameter("process", "glob")] = None,
        query: Annotated[str | None, parameter("process", "query")] = None,
        ready: Annotated[str, parameter("process", "ready")] = "exists",
        shell_kind: Annotated[str, parameter("process", "shell_kind")] = "powershell",
        allow_shell: Annotated[bool, parameter("process", "allow_shell")] = False,
        allow_process_control: Annotated[
            bool, parameter("process", "allow_process_control")
        ] = False,
        max_output_chars: Annotated[int, parameter("process", "max_output_chars")] = 24000,
        max_output_bytes: Annotated[int, parameter("process", "max_output_bytes")] = 65536,
    ) -> CallToolResult:
        """List/launch programs, wait for readiness/exit, or explicitly run shell/kill.

        Use window for UI, system for MCP status. launch does not guarantee focus or
        readiness: {"action":"launch","argv":["notepad.exe"]}; then wait_for_window
        if needed. wait binds process handles and reports exit codes; a missing window
        is not process-exit evidence. wait_for_file requires a NEW file stable >=200ms.
        shell runs with user rights: --allow-shell AND allow_shell=true. kill may lose
        work: --allow-process-control AND allow_process_control=true. Protected workers/
        server refuse; ambiguous matches need explicit all=true. Never replay mutations
        automatically after timeout/cancel.
        Returns bounded inventory/identity/readiness/exit JSON or shell exit_code/
        stdout/stderr (sensitive, possibly truncated).
        """
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
        action: Annotated[str, parameter("system", "action")] = "status",
        text: Annotated[str | None, parameter("system", "text")] = None,
        name: Annotated[str | None, parameter("system", "name")] = None,
        ctx: Context = None,
    ) -> CallToolResult:
        """Read MCP/desktop/clipboard state, cancel work, or manage named crops.

        status identifies the RUNNING server/version/operation, not app processes.
        Example: {"action":"status"}. cancel bypasses the operation lock but does
        NOT mean input was released or effects rolled back; await the original call's
        final cleanup evidence, never replay pending actions.
        displays/cursor only observe. Clipboard reads are sensitive; set_clipboard
        replaces text and may discard non-text formats. viewports/clear_viewport
        manage client crop bindings, not OS windows.
        Returns bounded JSON, no image or physical input; unsupported actions refuse.
        """
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
