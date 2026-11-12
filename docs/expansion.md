# Capability expansion contract (0.2)

Video implementation note: the 0.3.1 backend uses PyAV in an isolated Python
worker. External FFmpeg references below describe historical implementation;
the output, cancellation and safe-artifact contracts continue to apply.

User authorized all candidates in computer-use-comparison.md, not a new full Agent.
Keep seven tools, strict UIA default, no global MCP install, no automatic commit.

## Shared API / ownership freeze

Parent owns contracts.py, output.py, trace.py, pyproject/lock, documentation/scripts.
Lane A owns uia_backend.py, win32.py, tests/test_uia_backend.py,test_win32.py, new test_uia_queries.py/test_tokens.py.
Lane B owns runtime.py,server.py,broker.py,worker.py,test_runtime.py,test_protocol.py,test_broker.py,new test_cancel_protocol.py.
Lane C owns native.py,control.py,test_native.py,new test_control.py.
Lane D owns capture.py,window_capture.py,test_capture.py,new test_window_capture.py.
No cross-lane edits without parent.

### Results / output
Existing statuses unchanged. Cancellation uses refused before any effect or unknown if may
have acted, code CANCELLED, cancelled=true. Always stop batch on cancellation independent
of stop_on_error. system.cancel only means request accepted, not successful cleanup.
New metadata: delivery=accessibility|native|read_only, fallbacks=[], target_binding when known.

`output.bound_result(value:dict,max_output_chars=24000,max_output_bytes=65536)->dict`.
Budget JSON envelope including UTF8 escaping; truncate data/observations payload only, preserve
status/operation/error code/may_have_acted/cancelled, identity/token as whole string or omit,
no dangling indexes (when output omitted, keep minimal summary not partial broken tree).
`output.validate_budget(chars,bytes)` returns pair or raises ValueError; min2048,max1MiB.
Call bound_result in UIA before IPC and server before serialization (ignore image bytes; cap images
separately). Requests carry max_output_chars/bytes,compact; don't equate output truncation with
traversal completeness or mutation uniqueness. Aggregate batch output also bounded.

### UIA reads/tokens/wait (A integrates with output.py directly)
New read actions: find,wait_for. `window` new args query:str|None,match='contains'|'exact',
limit=20,compact=False,include_tokens=False,token:str|None,condition:dict|None,
poll_interval=0.05,timeout=5,max_output_chars=24000,max_output_bytes=65536.
find requires query or selector, returns matched metadata + ancestry/path + selector suggestions;
matched_total_known false on traversal truncation; return-limit != completeness. No regex in MVP.
Tokens issue on find/inspect or include_tokens: cache <=256 per UIABackend, random unguessable
token, monotonic TTL60s; live root/element ref only in owning child; bind PID/HWND + process
creation if available + root/element RuntimeId and critical metadata. Every use validate owner,
TTL and bindings; new worker cache rejects old token; no auto fallback/re-search on token failure.
token and selector mutually exclusive; target still explicit and must match token binding.
Do not claim UUID/PID alone proves all possible window lifetime changes; document residuals.

wait_for condition exactly one of {exists:bool},{value:str},{toggle_state:0|1|2},
{selected:bool},{expand_state:int}; selector or token identifies element. Exists=false requires
complete successful search, not provider failure/truncation. Broker sends `_deadline_monotonic`
absolute deadline with response reserve; UIA polls read-only until deadline, never replay mutation.
Return verified on match, error code CONDITION_TIMEOUT plus last_observation on expiry;
backend handles missing/ambiguous/stale safely. `post_action_wait` is not default random sleep;
use explicit wait_for after a single write and keep invoked/unknown honest.

### Cancellation (C contract, B integration)
`control.OperationCancelled(Exception)` code CANCELLED.
`control.OperationController.operation()` context manager -> Operation with `.event` threading.Event,
`.call_id` UUID, `.checkpoint()`, `.wait(seconds)` raises when cancelled.
`.cancel()` -> JSON-friendly {requested:bool,call_id:str|None}; only small state lock, never
runtime/broker locks, injection or disk IO. `.snapshot()` -> state, `.close()` signals current
and closes admissions. Runtime holds serialization lock BEFORE entering operation context;
queued callers must not register/reset active event. Active stays until owner cleanup/capture join.
NativeInput `.set_cancel_event(event_or_None)` and checkpoints consult it; releases bypass
cancel/foreground as emergency cleanup, attempted on owner thread. Failed releases fault-gate
next native execute (CLEANUP_REQUIRED) until release_all succeeds; no user holds guessed/released.

B server uses async tools with to_thread for blocking execution; system.cancel fast path directly
signals (no executor), so saturation doesn't starve it. Request cancellation/disconnect signals
controller and shields/joins owner future before cleanup and releasing active registration.
Runtime.system('cancel') bypasses serialization; status reports active state without queueing.
Ctrl+C cleanup not a crash guarantee, no default global keyboard hook.
UIABroker.call(...,cancel_event=None) includes startup/IO in deadline and polls cancel, kills/reaps
child before next request, may_have_acted after mutation dispatch, no retries. Reserve at least
50ms response handling so wait_for can return last observation.

### Capture contract (D, B kwargs integration)
screenshot(...,region=None,capture_mode='screen',allow_fallback=False,timeout=5,cancel_event=None)
region=[left,top,right,bottom] relative to selected surface pixels, integer and contained; crop
original before resize (max_dim may upscale only with explicit zoom? No implicit upscaling).
capture_mode screen|window|printwindow|wgc; default screen preserves behavior. window tries
PrintWindow then WGC; screen-region fallback ONLY allow_fallback=True; report all attempts.
Metadata: frame_id, wall/monotonic timestamps, original_size/surface_size/output_size, region,
coordinate_space, origin if reliable, scale double-axis, capture_backend,capture_surface,
occluded bool|null, fallbacks with reasons. WGC item pixels origin is not GetWindowRect guarantee;
set physical transform unknown if not established, don't invent coordinates.
No activation/focus. Reject minimized/invalid/hidden; validate HWND/PID before+after capture.
PrintWindow and WGC isolated child with hard total timeout/cancel; bounded <=128MiB bitmap
before allocation; full GDI and subprocess cleanup; legitimate black frames not auto failures.
Optional windows-capture==2.0.1 extra wgc (numpy/opencv deps), default doesn't import it.
WGC WindowsCapture(window_hwnd=int,cursor_capture=False,draw_border=None,secondary_window=False),
one-frame callback copies BGRA and stops; child process protects blocking stop/join.
record(...cancel_event=None): cancellation in sampling waits and FFmpeg lifecycle; bounded cleanup;
play doesn't leave capture thread running; return truthful partial metadata when stopped.
`CaptureService(...,artifact_store=None)` accepts parent's store; no implicit arbitrary directory deletion.

### Trace + artifact retention (parent, B integration)
`trace.ArtifactStore(root:Path,max_bytes=256MiB,max_files=200)` .allocate(suffix:str)->Path,
.register(path:Path)->None, .prune()->dict; writes owner manifest only, cleanup only listed owned
regular files inside root, reject symlink/reparse traversal, protect active/unregistered files.
Capture uses allocate+register montage/video, trace uses registered segments; default store
enabled for generated capture artifacts even when trace disabled. Never sweep arbitrary files.
Gate refinement: ArtifactStore.write_bytes/write_text safely commits via exclusive temporary
file and atomic replacement; Windows pins directory components against replacement, POSIX uses
anchored dirfd. FFmpeg uses anonymous temporary output, never a writable reservation path.
begin_group/end_group surrounds capture plus trace, retaining result group until next group.
Pinned group may report over_budget; do not silently evict paths before owner returns.
`trace.TraceRecorder(store,enabled=False)` .record(operation,request:dict,result:dict)->None;
allowlist only call/batch/index,monotonic/wall times,method,status,error CODE, cancellation/cleanup,
PID/HWND, artifacts when safe; no titles,values,selectors,text,error messages by default.
Failed logging not change action outcome. .status(),.close(); per-call IDs attached by runtime.
Runtime(...trace_enabled=False,artifact_max_bytes=...,artifact_max_files=...) and CLI --trace,
--artifact-max-mb,--artifact-max-files. Optional metadata traces no automatic replay.
MCP ToolAnnotations readOnlyHint for screenshot/system (system cancel mutation => conservative!),
mixed window/act/play/process conservative hints; annotations never approval.

## Verification budget
Each owner focused unit/regression tests; mocks no real native injection. Parent upgrades owned
Win32 fixture for delayed state and tokens plus window-capture tests. Final full suite once
after reconcile, actual concurrent stdio cancel while blocked+queued requests, worker kill/recovery,
real fixture UIA and optional installed WGC on owned fixture, synthetic FFmpeg, wheel build.
One final oracle gate for concurrency/privacy/identity risks; fix blockers only.

Discovered Windows bootstrap issue: a replacement multiprocessing child can hang before Python
user code when OS stdin shares the MCP input pipe. Worker-level redirect is too late.
Child launch must explicitly isolate OS stdin/stdout at CreateProcess time using a scoped local
launcher, not global _winapi monkeypatch or process-wide temporary SetStdHandle. Keep the real
stdio cancellation/recovery regression; do not remove it to make the suite pass.
