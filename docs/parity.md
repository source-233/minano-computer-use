# sshh12 capability coverage — 0.3 implementation contract

0.3.1 video follow-up supersedes the external FFmpeg implementation described
below: mandatory PyAV dependency, isolated Python encoder process, no external
ffmpeg/ffprobe executable. Geometry, cancellation, output budgets and artifact
ownership requirements are unchanged; video decoding checks also use PyAV.

User requested full installed sshh12 capability coverage on 2026-11-12. Checkpoint
`5ba60d6` contains verified 0.2 (261 tests); 0.3 parity work was committed only
after the user's separate authorization and the final 659-test gate.
Coverage means equivalent useful capability, not reproducing unsafe old bugs.
The installed reference is pinned to `8d367a868f49150c9dcc8ea4d70b6efe8ad8a892`.

User refinement on image cost: screenshot and returned record/play montage
default max_dim=800, format='jpeg', quality=75; max_dim=0 requests unscaled.
The montage cap applies to the entire image, not each cell. Full-resolution
video/source-frame exports and explicit save=True PNG remain independent.
Response metadata gives actual output_size/format/quality/scale. PNG is explicit
when lossless small-text imagery is required; bytes alone are not a token metric.

## Permissions and common context

Seven public tools remain. Existing argument spellings remain valid; the native
permission, compact-image and auto-feedback defaults change as explicitly
approved above. All effects
remain serialized by Runtime and share its operation cancellation Event. Each
service takes `cancel_event=None, deadline=None` (absolute monotonic deadline).
Never reset deadline in a nested step; cancellation independently stops cleanup.
UIA never implicitly falls back to mouse input. Aliases cannot enable permissions.
User refinement: act/play `native=false` remains strict UIA, `native=true` alone
selects native input for that call. No startup flag or reconnect is required.
Retain --allow-native only as a deprecated compatibility no-op if necessary;
Runtime creates enabled backend for explicit native calls, low-level NativeInput
still has an enabled guard for direct internal use. Status reports explicit_per_call.

New startup flags `--allow-shell` and `--allow-process-control` default false;
each corresponding tool call must ALSO pass `allow_shell=true` or
`allow_process_control=true`. External window.close requires process-control
dual opt-in; owned worker/process-tree cleanup does not. Native input, including
message delivery, requires outer native=true only; shell/process gates unchanged.

Explicit clipboard get/set uses the system tool (no automatic clipboard writes).
Native paste uses that same service, preserves only text, verifies write, and
restores prior text only while its clipboard sequence is unchanged. No clipboard
contents in trace or error diagnostics. Full clipboard-format preservation is
not promised. Probes require shell gates and use the same bounded process runner.

## Ownership

A: native.py, input_plan.py, playscript.py; test_native.py and new test_input_plan.py,
test_playscript.py. No runtime or services modifications.
B: capture.py, coordinates.py; test_capture.py/new test_coordinates.py.
No window_capture.py or trace.py edits without parent.
C: automation_services.py; new test_services.py and service-specific helper modules
as needed (jobs.py, ocr.py); no runtime/native/capture edits.
D: runtime.py, server.py; test_runtime.py,test_protocol.py,new test_parity_protocol.py.
Parent: contracts.py, pyproject/lock, docs, final integration scripts and tests.

## A — native plan + DSL

`input_plan.normalize_native_action(action:dict)->dict` is pure and rejects
conflicting aliases/unknown fields. Existing action shape remains accepted.
Canonical names key/hold/text/paste/move/look/click/mouse_down/mouse_up/drag/wheel/
key_down/key_up/focus/force_focus. Map old mouse_move, mouse_move_relative,
left/right/middle/double/triple_click, left_mouse_down/up, scroll, hold_key,type.
Action `type` alias for action key allowed only if not conflicting. Old coordinate
array accepted; no coordinate clamping. `button`=left/right/middle/x1/x2;
click supports clicks1..3,modifiers; drag x/y,to:{x,y},path,seconds/duration is
TOTAL duration bounded10sec, press_dwell_ms bounded; reject conflicting endpoint/path.
wheel amount/delta raw delta retained; legacy scroll_amount notches*120 and
scroll_direction vertical/horizontal encoded correctly. Add full nav/extended/
punctuation key aliases, no multi-target message broadcast.

`NativeInput(enabled=False,adapter=None,services=None)` keeps set_cancel_event.
`.execute(action,target,key_method='scancode')`; key_method=auto|scancode|message.
auto selects browser message target ONLY for known browser process identities;
otherwise scancode. Message mode must resolve exactly one qualifying render
child or exact nonbrowser HWND; refuse ambiguous browser children. Check
foreground HWND/PID before each message/injection. PostMessage checks BOOL;
key lParam includes repeat/scancode/extended/up bits; modifiers best-effort
delivery, never claim application consumption. Existing scan path unchanged.
services is C's AutomationServices for native paste, using `.clipboard_session(text)`
context manager -> token; token sequence guards restore, no recursive runtime calls.
Mouse held tracking per button, release all owner-thread on cancel/error.

`playscript.parse_script(script:str)->list[dict]`: effect-free, max100 commands/
64KiB, validates before any effects, commands hold/tap/down/up/look/move/lmb/rmb/
mmb/scroll/type/paste/wait/probe/until/shot. Generate actions with old semantics
except unsupported/unsafe constructs refuse. Dynamic commands probe/until/shot
are internal Runtime actions, not native sends.
`playscript.evaluate_condition(expr:str,sample:dict)->bool`: safe bounded AST
interpreter, no eval/exec/calls/attributes; allow constants/names/dict subscripts/
boolean/comparisons/limited arithmetic, bound nodes/depth/string/numeric sizes.
until checks current sample once (probe only if none), true stops script;
no automatic repeated shell probes. Unknown command fails whole parse.

## B — coordinates and capture

`coordinates.CoordinateManager()` keeps caller-scoped latest frames and viewports.
Runtime uses a per-client context key passed by server Context session metadata
(fallback default for direct single-runtime tests, not process-global across clients).
`.remember(metadata,scope='default')->str`: frame/capture_id returned.
`.map_actions(actions,coordinate_space='screen',capture_id=None,target=None,scope='default')->list`:
physical screen default preserves existing native API; image requires explicit
capture_id or valid latest in same scope; validate target binding/geometry/DPI
against fresh window before effects; reject unknown physical transform or stale
capture, out of image points, target mismatch. Translate action x/y,to,path via
recorded immutable origin/scales. Relative look deltas remain relative input.
`.define_viewport(name,metadata,region=None,scope='default')`, `.list_viewports`,
`.clear_viewport(name,scope='default')`; window ratio crop tracks same identity
on move/resize, PID/create identity verified; no title rediscovery.
`.resolve(target,scope='default')->dict` for viewport target at current geometry;
returns concrete target + region/capture args; public regions documented not guessed.

`CaptureService(...,coordinate_manager=None)` keep previous interfaces, add
scope='default',region_space='surface',save=False,define_viewport=None,
foreground=False (reject true: require explicit native force_focus, never auto).
Targets desktop,display:N|primary|left|right,window:<title|process|hwnd>,
foreground,region,viewport:name; display indices zero-based. screen region object
{x,y,w,h} supported with region_space=screen; current [l,t,r,b] surface remains.
max_dim=0 full size, explicit save records original image through ArtifactStore
safe writer. screenshot remembers coordinate context, montage never does.

record adds region,region_space,keep_frames=False,capture_mode='screen',scope,
foreground=False. Viewport/ROI samples keep identity/geometry validity. Export
frames and JSON timestamp index only when requested, under existing group/quota.
Return actual achieved_fps/static warning and real encoder error code/limited
stderr diagnostics (no secrets). Fix odd width/height libx264 yuv420p padding,
expose encoded_size separately from logical_size. Missing/broken ffmpeg gives
montage plus honest warning. Internal record may use optional stop_event and
extra_frames list for DSL shot/end-of-script capture, no leaked thread.

## C — automation services

`AutomationServices(allow_shell=False,allow_process_control=False,artifact_store=None,adapter=None)`
`.process(action='list',argv=None,*,exe=None,args=None,cwd=None,shell=False,
command=None,timeout=10,pid=None,name=None,target=None,all=False,dir=None,glob=None,
query=None,ready='exists',shell_kind='powershell',allow_shell=False,
allow_process_control=False,cancel_event=None,deadline=None)->dict`
list true process inventory; launch supports argv or exe+args, explicit cwd,
shell=True means OS ShellExecute documents/URI (not command interpreter);
`shell` action uses separate shell gates. `kill` handle-bound PID/create check,
ambiguous name refuses unless all=true; no PID0 or server/worker self kill.
wait preserves access denied/WAIT_FAILED not treated exit. wait_for_file detects
new matching files after initial baseline and stable size; wait_for_window
unique query, visible/input_idle readiness returns actual readiness or refusal.
Shell runner bounded stdout/stderr, private safe artifacts for large output,
Windows Job Object containment BEFORE child runs/spawns; cancel/timeout kill
owned tree, no global taskkill matching. Use suspended process+job/resume or
equivalent safe launch, fail closed if unavailable.

`.system(action,text=None,...cancel_event=None,deadline=None)->dict`: cursor,
get_clipboard,set_clipboard with exact readback; displays includes DPI/scale/primary/index.
`.window(action,target=None,selector=None,*,allow_process_control=False,
cancel_event=None,deadline=None,**kwargs)->dict`: close posts WM_CLOSE to exact
validated HWND under process-control gates; OCR fixed known script, no user
interpolation arbitrary shell. `ocr(target,...)` helper returns text or explicit
unavailable/timeout; Runtime get_text alias requests UIA then OCR if empty.
`.clipboard_session(text,cancel_event=None,deadline=None)` context manager token
gets exact previous unicode text, writes+readbacks, sequence guard restoration.
All service functions import-safe on non-Windows and injectable for tests.

## D — integration surface

Extend existing Runtime/CLI and seven schemas additively. Runtime uses A
normalization rather than duplicate native validator. Capture manager per Runtime.
act adds coordinate_space='screen',capture_id=None,screenshot='auto'|false|true|'text',
settle_ms=80,focus=None,key_method='scancode',scope='default'. focus argument
requires native=true and explicitly queues force_focus (not UIA fallback).
normalize entire plan before effects. After-actions screenshot/text optional;
use same owner operation/store group, no nested controller deadlock. Auto is the
default: native batches return an 800px JPEG observation, strict UIA batches return
bounded compact text/state without an image. Explicit false disables observation,
true forces image, text forces text. Observation failure never replays input or
overwrites the primary action outcome; cancellation skips automatic observation.
window aliases get_text/ui_tree/click_element; query for discovery, window=target,
role/nth explicit selection. click_element uses strict Invoke only; no auto
Toggle or coordinate fallback. window focus requires native flag or use act;
close process-control gates; get_text auto OCR only on successful empty UIA
result, not on provider timeout or errors. window.list query/limit/details
metadata compact and still preserve current default list API.

play actions optional or script (mutually exclusive), accepts montage_frames,
probe,coordinate_space,capture_id,key_method,scope,allow_shell. Parsed DSL
executes through same native/UIA policy. Probe JSON bounded; until safe AST;
shot uses capture without updating click context; script capture stops when
script finishes (no hard-real-time/action-frame promise).
system extends C plus viewports/clear_viewport; cancel/status still direct
lock-independent no executor fast path. process delegates C with constructor
flags and tool per-call gates. TRACE never records commands/clipboard/text.

## Verification and truthful coverage

Owners run focused tests only. Parent full suite after reconcile, real MCP
schemas/permission refusal/cancel, owned Win32/clipboard fixtures, synthetic odd
1927x1148 real FFmpeg and decode. No live game control in this pass unless user
asks. Include capability matrix test: public missing action is a defect, but
environment-dependent OCR/WGC reports unsupported with evidence, not fake success.
No auto commit of parity work, global MCP untouched.

## Capability coverage and intentional differences

| Installed sshh12 capability | Minano equivalent | Intentional boundary |
|---|---|---|
| window list/focus/close/get_text/ui_tree/click_element | Same aliases plus find/inspect/wait_for/token | Unique target; focus is explicit native; close permission required; no physical fallback |
| Absolute/relative pointer, left/right/middle/X click, double/triple, down/up/drag | Canonical input plan plus legacy spellings | Physical pixels or validated image capture; total drag duration; release on exit |
| Key scan codes/chords/holds/type/paste | Same intent, expanded names, explicit delivery method | No automatic fallback; paste restores text only if clipboard sequence still owned |
| Browser message keyboard | Explicit message/auto under native=true | Exactly one eligible render child; no broadcast; invoked not consumed |
| screenshot desktop/display/window/region/viewport/save | Scoped CoordinateManager + CaptureService | No automatic focus or silent fallback; latest context scoped to caller |
| record MP4/montage/region/keep_frames | Encoded video + compact montage + exported PNG/index | Old keep_frames was a no-op; new version implements exports; no exact frame pairing |
| play line DSL, probe/until/shot | Same commands through validated native plan | No eval; probe uses gated shell; until is single latest-sample check |
| process launch/open/kill/wait/shell/file/window waits | AutomationServices + suspended contained JobRunner | Shell and external control separately default-disabled; ambiguous matches refuse |
| system displays/cursor/clipboard/viewports | Same actions and metadata | No implicit clipboard mutation; no caller-chosen cross-session capture scope |

Wire compatibility is additive where unambiguous, not a promise that every unsafe
historical invocation works unchanged. Existing minano screen-coordinate API stays
the default; old image coordinates require explicit coordinate_space='image'.
Desktop/display/screen-viewport images supply a coordinate surface, not a keyboard
or mouse recipient. Native image input must separately select a concrete live
window; it never infers foreground from the captured surface. HWND-bound images
still require that same window identity/geometry. Region recapture validation
remains separate from selecting an input recipient.
Explicit screenshot/record foreground=true refuses and directs callers to the
equivalent native force_focus action rather than stealing focus during a read.
UIA checkbox/select actions use explicit toggle/select instead of guessing a
different pattern when Invoke is unsupported. Unknown commands/fields/aliases
refuse before effects. These are policy improvements, not missing capabilities.
Paste text containing NUL is rejected while validating the entire plan, before
any preceding action runs. A truncated shell probe is not complete JSON evidence:
reject it without running until or replaying the probe, retaining mutation
uncertainty because the shell has already executed.

Evidence is separated: fake adapters prove ordering/refusal/cleanup, owned Windows
fixtures prove COM/OCR/process behavior, synthetic real FFmpeg proves encoding and
decode, and docs/game-input.md records limited live game evidence of 0.2. No new
claim of universal game compatibility or a fresh real-game 0.3 test is made.
