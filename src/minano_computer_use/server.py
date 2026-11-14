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
from .tool_schema import parameter
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
        """Discover Windows windows/controls, read state, or perform one semantic UIA action.

        Start with list, then state/find/inspect for a concrete target and selector/token.
        Example: {"action":"state","target":"hwnd:0x123","include_tokens":true}.
        Use act for ordered batches, screenshot for an image only, process for lifecycle.
        invoke/set_value/toggle/select/expand/collapse/scroll modify the application;
        close may lose unsaved work and needs both process-control permissions.
        UIA never falls back to real input; only explicit native focus/force_focus
        activates. Providers can independently change focus. Unsupported/ambiguous
        controls refuse. set_value writes once, confirms the same reference <=600ms
        within timeout, and never re-searches or replays a write.
        Returns bounded JSON status/operation/data/error plus optional image. State
        includes independently timed UIA/image components and partial errors, not an
        atomic frame. Tokens expire; node indexes are not safe action references.
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
        """Execute 1..100 ordered Windows control/input actions with optional final observation.

        Use window to discover exact selectors/tokens first; use play for at-timestamp
        scheduling with recording, record for observation without actions.
        UIA example: actions=[{"action":"invoke","selector":{"automation_id":"ok"}}].
        Native example: native=true, actions=[{"action":"hold","key":"W","seconds":0.2}].
        Default strict UIA never falls back. native=true or input_mode=native selects
        real input; message explicitly selects background HWND messages. native=true
        conflicts with explicit uia/message. Native requires foreground; force_focus
        or focus must be explicit (focus argument is a force_focus shortcut).
        Validate the whole plan first; no rollback of earlier effects. Stop on errors
        by default and attempt release of tool-owned held inputs. Loss/timeout may
        have acted: inspect indexed results, counts, receipts and cleanup; never replay.
        Returns JSON plus auto-observation: UIA text/native image/message state.
        screenshot=false disables observation; feedback controls native metadata only.
        Observation failure cannot change primary results; system acceptance does not
        verify game consumption. Unsupported UIA controls need explicit channel choice.
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
        """Capture one desktop/display/window/crop image without injecting input or activating.

        Use this for pixels only, window(action=state) for UIA plus an image, record
        for a time series, or play for actions plus recording. Default desktop screen
        capture includes visible occluders; explicit window backends may fail and do
        not fall back to screen unless allow_fallback=true. WGC is optional.
        Returns an MCP image plus separate bounded JSON metadata: backend, source and
        output geometry, transform, capture_id, timestamps, and optional artifact path.
        Example: {"target":"hwnd:0x123","capture_mode":"printwindow","save":true}.
        save persists the original locally; define_viewport changes only the client
        crop registry. Images can contain sensitive pixels despite UIA text redaction.
        For subsequent image-coordinate input, pass capture_id and an explicit window;
        invalid identity/transform refuses, never guesses or clamps a click.
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
        """Observe 0.05..60 seconds of screen/window activity without executing actions.

        Use screenshot for one frame, play for actions with capture. No focus/input
        changes; screen mode may record occluders. Example: {"target":"desktop",
        "seconds":3,"fps":5,"montage_frames":4}. Limits include duration, memory and
        managed artifact quota; unsupported backend/identity failure is reported.
        Returns timestamped MCP montage plus JSON sample times, achieved_fps, backend,
        local H.264 MP4 path or encoding warning, and optional PNG-frame/index paths.
        Encoding uses an isolated PyAV worker; no external ffmpeg executable. Artifacts
        may contain sensitive pixels and are quota-managed; montage resize never
        downscales video/original frames. Montages do not establish click coordinates
        or exact game-frame/action pairing. Cancellation requests stop sampling and
        encoding with cleanup, not rollback of already-saved artifacts.
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
        """Run a bounded Windows action sequence while recording independent visual samples.

        Choose play for at-timed actions or sequential DSL with recording; use act
        for ordinary batches/final feedback, record for no-input observation.
        Exactly one of actions/script is required; max100 expanded actions. seconds
        is 0.1..30, at is relative to this call, nondecreasing, and <=seconds (not an
        absolute/global timeline). Actions run sequentially; late ones run immediately.
        The key/mouse DSL needs explicit native or message mode; UIA uses actions.
        Example: {"target":"hwnd:0x123","native":true,"seconds":2,"actions":[
        {"at":0,"action":"hold","key":"W","seconds":0.2}]}.
        Defaults to strict UIA. Native touches real input and requires foreground;
        focus/force_focus must be explicit, loss stops by default, never auto-refocus.
        Message mode is explicit background delivery, not a game-input bypass.
        probe/until executes a shell command only with --allow-shell AND allow_shell.
        Validate the whole plan first; partial effects are not rolled back/replayed.
        Returns JSON data.actions.data results/counts/cleanup separately from data.capture
        errors/metadata and optional montage/MP4 paths. Capture failure does not prove
        action failure, a missing window does not prove process exit, and accepted
        events do not prove game consumption. No strict frame/action pairing.
        Cancellation signals first; final return follows tool-owned input release
        attempts and capture/encoder cleanup. feedback controls native metadata only.
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
        """List/start Windows programs, wait for lifecycle/readiness, or explicitly run/kill.

        Use window for controls, act/play for input, system for the MCP's own status.
        Default list is read-only; launch starts a program without promising focus or
        readiness. Example: {"action":"launch","argv":["notepad.exe"]}; then explicitly
        wait_for_window if needed. wait binds process handles and reports exit codes;
        window lookup failure alone is not process-exit evidence. wait_for_file ignores
        files existing at call start and requires a new file stable for >=200ms.
        shell executes arbitrary commands with current user rights: needs --allow-shell
        AND allow_shell=true. kill irreversibly terminates processes and may lose work:
        needs --allow-process-control AND allow_process_control=true. Protected server/
        worker processes refuse; ambiguity refuses unless explicitly all=true.
        Returns bounded JSON inventory, launched identity, readiness/exit evidence, or
        shell exit_code/stdout/stderr (potentially sensitive/truncated). Timeout/cancel
        may follow effects; never automatically rerun a launch/command/kill.
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
        """Read MCP/desktop/clipboard state, cancel active work, or manage named crops.

        Use status to identify the RUNNING server/version and active operation; process
        inventories applications, not this service's execution state. Example:
        {"action":"status"}, {"action":"get_clipboard"}, {"action":"clear_viewport","name":"panel"}.
        cancel signals the current operation without waiting behind its execution lock;
        accepted cancellation does NOT mean input was released or work rolled back.
        Await the original call's final cleanup evidence; don't replay pending actions.
        displays/cursor only observe. get_clipboard reads sensitive Unicode text;
        set_clipboard replaces system clipboard text and may discard non-text formats.
        viewports lists this client's crop bindings; clear_viewport removes one, not
        an OS window. Returns bounded JSON status/data/error, never screenshots or
        physical input; unsupported actions refuse rather than executing commands.
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
