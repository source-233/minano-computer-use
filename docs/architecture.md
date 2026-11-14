# Architecture / implementation contract

0.5 implementation contract: [v0.5.md](v0.5.md). Native actions report bounded
dispatch receipts; optional detailed feedback is read-only and cannot delay
owner-thread cleanup or rewrite mutation results. UIA membership failure details
are captured at the failing predicate without provider text. Public
process_creation_time is a decimal string/null; Win32, runtime, tokens,
coordinates and worker IPC retain exact Python integers. Conversion occurs only
at the public MCP/trace boundary before response budgeting. State can explicitly
select an existing capture backend without changing foreground policy.

0.4 adds a separate background-message executor and bound, non-atomic application
state. See [v0.4.md](v0.4.md) for input selection, worker identity/cleanup, rich
UIA discovery, redaction and per-component failure semantics. Native foreground
checks remain unchanged; background mode is not a bypass inside NativeInput.

Current video backend (0.3.1): PyAV is a mandatory Python dependency. Production
H.264/MP4 encoding runs in an isolated, terminable Python worker with bounded
anonymous-file I/O. No external ffmpeg/ffprobe executable is required. Parent
capture owns artifact registration and only commits completed output. The
historical FFmpeg subprocess contracts below are superseded by this backend.

> The original 0.1 contract below is retained as a baseline. The 0.2 shared
> contract and new interfaces are documented in [expansion.md](expansion.md).
> 0.3 compatibility work and new authorization rules are in [parity.md](parity.md).
> Native input now uses the per-call `native` parameter only (default false);
> there is no native startup opt-in requirement. Shell/process control remain
> separately default-disabled with startup plus call authorization.
> Current results are in [verification.md](verification.md); research priorities
> are not an implemented-feature checklist.

## Scope

Fresh Python implementation; sshh12 is a product-interface reference, not a fork.
MCP SDK 1.x, Python 3.13+, Windows UIA via `uiautomation`, native Win32 via ctypes,
MSS/Pillow for screen-composite capture, optional local FFmpeg for MP4. No
telemetry, virtual gamepad, or globally installed MCP replacement. New shell
execution and external process control are separately opt-in; the 0.1 baseline
below is historical where superseded by parity.md.

## Modules and boundaries

- `contracts.py`: JSON result helper and operation sets; no Windows imports.
- `win32.py`: list/resolve HWND targets, foreground/cursor observations; no implicit focus.
- `uia_backend.py`: one `UIABackend` instance, created and used on the UIA child main thread.
- `worker.py`: JSON-only multiprocessing Pipe server, imports UIA only inside the child.
- `broker.py`: spawn process, send one request at a time, hard deadline, kill/join on timeout;
  never replay an interrupted action. Injectable worker function supports deterministic tests.
- `native.py`: opt-in Win32 inputs, foreground checks, SendInput count checks, held-input cleanup.
- `capture.py`: screen-composite screenshot, bounded recording, timestamped montage; no activation.
- `runtime.py`: serialization, validation, ordered batches, result aggregation, play orchestration.
- `server.py`: seven MCP tools `window`, `act`, `screenshot`, `record`, `play`, `process`, `system`;
  clean stdio, CLI. Transport does not implement backend policy.
- `tool_schema.py`: agent-facing parameter descriptions and JSON Schema structures for
  existing inputs; no Windows access, authorization, runtime defaults, or input execution.
  Schema metadata must not coerce/drop action fields or bypass whole-plan validation.

## Stable Python interfaces for parallel implementation

All arguments/results crossing processes must be JSON-serializable. No UIA/COM objects cross.

### Windows + UIA lane

`win32.list_windows() -> list[dict]`: each entry has `hwnd` (int), `pid`, `title`, `rect`
([left, top, right, bottom]), `visible`, `minimized`.

`win32.resolve_window(target: str) -> dict`: accepts `hwnd:0x...`, `pid:123`, exact title
(case-insensitive), then unique title substring. `foreground` is explicit. Reject ambiguity.
`win32.observations() -> dict`: `foreground_hwnd` and `cursor` [x,y].
`win32.TargetError(code: str, message: str)` carries `.code`.

`UIABackend.execute(request: dict) -> dict`:
`{action, target, selector, value, depth, max_nodes, timeout, direction, amount}`.
selector can be exact-name string or `{name, automation_id, control_type, index}`;
conditions AND, default exact case-sensitive UIA Name, optional explicit index zero-based;
ambiguous/missing matches refuse. Never implicit Invoke fallback to Toggle or physical input.
Reads: list/tree/text/inspect. Writes: invoke/set_value/toggle/select/expand/collapse/
scroll_into_view/scroll. Patterns unsupported => `refused`.
set_value exact readback; stateful operations readback => `verified` only on matching state;
Invoke and unverifiable scroll_into_view => `invoked`, not falsely verified.
No `SetFocus`, `SetCursorPos`, `SendInput` in UIA lane. Report before/after observations.
Traversal bounded by depth/node budget; selectors fail closed if truncated and uniqueness
cannot be established. Errors/COM exceptions become JSON with specific code.
`UIABackend.close()` uninitializes on same thread.

### Native + capture lane

`NativeInput(enabled: bool = False)`; `.execute(action: dict, target: str) -> dict`;
`.release_all() -> dict`; `.close()`.
Runtime only calls `.execute` when caller explicitly requested `native=True`.
Disabled backend always refuses; requires explicit target, verify foreground HWND before each
input; only explicit `focus` may activate a window. `look` is relative dx/dy; `move`/`click`
use physical screen x/y. key_down/up may only remain held within one batch; batch finally
releases all. Unicode text, key chords, hold all guarantee keyup attempts; SendInput short
write is not success. Safe cancellation/checkpoint between hold intervals. Use no mocks as
production fallback. Methods can accept an injected Win32 adapter for tests.

`CaptureService(output_dir: Path | None = None)`:
`.screenshot(target='desktop', max_dim=1568, format='png') -> CaptureFrame`;
CaptureFrame dataclass has `.image_bytes: bytes`, `.mime_type: str`, `.metadata: dict`.
`.record(target='desktop', seconds=3.0, fps=10, montage_frames=6) -> CaptureFrame`;
returns timestamped montage image and metadata (actual sample timestamps, MP4 path or warning,
capture_backend, capture_surface='screen_composite'). No focus, no claiming occlusion-free
window frames; reject minimized/offscreen invalid targets. Bound seconds/fps/frame memory.
`.close()` cleans active subprocesses/threads if any. Screen grabbing should be injectable.
FFmpeg invocation list arguments (never shell=True); absent FFmpeg still gives montage.
`play` captures alongside `act`, but image timestamps never prove strict action/frame pairing.

### Broker + runtime + MCP lane

`UIABroker(timeout=5.0, worker_target=None)`; `.call(request:dict, timeout:float|None=None)`;
`.close()`. Worker function signature `(connection)`; protocol uses UTF-8 JSON over
`send_bytes/recv_bytes`, never pickles COM objects. JSON object requests/results;
JSON `null` means close. On timeout: `unknown` + may_have_acted for writes,
`error` for reads; invalidate child before next call. No retries of mutations. Explicit lifecycle.

`Runtime(allow_native=False, broker=None, native=None, capture=None)`;
`.window(action='list', target=None, selector=None, **kwargs)`;
`.act(actions, target=None, native=False, stop_on_error=True, timeout=5.0)`;
`.screenshot/record` delegate under lock; `.play(actions, target, native=False, seconds=3,fps=10)`
uses timed action dicts with `at` in seconds; `.close()`.
Max100 actions, bounded waits/durations. Per-action target overrides require explicit values.
Batch preserves ordered per-action results and stops on refused/error/unknown when requested;
reports partial completion and always releases native holds even if input action fails.
`system` initially status/displays; clipboard access is deferred. `process` initially
list/launch (argv list),
no kill/shell. Reads may succeed independently of UIA worker. CLI supports --allow-native,
--output-dir. No auto global registration.

## Results

Shared `{status, operation, method?, data?, error?:{code,message}, ...}`.
Statuses: `ok` read/execution metadata; `invoked` no observed completion; `verified` observed
postcondition; `refused` policy/unsupported; `error` known failure; `unknown` may have acted;
`partial` batch stopped after prior actions. Observations report focus/pointer changes without
claiming the target provider will never activate itself. Never undo user focus automatically.

## Verification ownership and boundary

1. Backend owners: focused unit tests of selector ambiguity, unsupported patterns, zero physical
   fallback, truthful readback; native short-write/gating/finally cleanup; capture metadata/limits.
2. Broker/runtime owner: hanging synthetic worker killed/reaped; mutation not replayed, next
   worker recovers; ordered batches, strict/native gates, protocol images and clean stdio.
3. Parent reconciliation then final suite once. Add real Windows UIA smoke against a small
   owned standard-control fixture; no user Calculator/game manipulation or native injection.
4. Build wheel + isolated stdio initialize/list_tools/call read-only system.status. Evidence is
   not a guarantee of all apps/game input/strict frame pairing or focus-free third-party UIA.
