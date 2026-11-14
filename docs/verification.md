# Verification evidence and limits

## 0.5.3: hosted UIA, permission diagnostics and rectangle spaces

Issues #1/#2 and the remaining metadata suggestion from withdrawn #3 are addressed
in this release. This does not claim that the reporter's UWP environment has been
reproduced.

- Cross-process UIA descendants require a bounded live raw-view ancestry path to
  the pinned host root, a same-element-process native HWND on that path, Win32
  descendant/owner validation, and a separate readable process creation identity.
  State, token issue/use and semantic mutations share this proof. Tokens retain
  the original native anchor, ancestry and process identity, never silently rebind.
  Missing native anchors, root-PID mismatch, unavailable identity, detachment and
  reused processes remain refusals. Executable names grant no trust.
- Missing startup and/or call permissions produce `PERMISSION_REQUIRED` with only
  the missing layers named. Close, kill and shell/probe keep their original double
  gates; no operation is made available by changing an error code.
- Window rectangle values remain exclusive LTRB, with independently observed
  calling-thread DPI space. Capture labels window versus surface bounds, crop and
  response scaling. Known virtualized/unknown screen rectangles do not fabricate
  a physical transform; image-input identity validation also compares known spaces.

Final automated gate: Ruff check/format (103 files), **1349 tests**, real MCP stdio,
feedback round-trip, lock validation and wheel/source build all exit 0. New tests
cover hosted identity/lifecycle/token loss, before/after-write deadlines, permission
combinations through MCP, unchanged rectangle values and coordinate-space refusal.
The cancellation saturation fixture now signals entry into action execution before
requesting cancellation; operation admission alone did not establish that point.
No production cancellation behavior or timeout was changed to make that test pass.

Real evidence: `scripts/verify_windows.py` passes all 13 owned-window checks.
`scripts/verify_hosted_uia.py` creates a standard Edit in a second owned process,
obtains its state token under the host HWND, writes once, and independently checks
the value with native WM_GETTEXT. That functional sequence passed. Earlier complete
functional runs exited 0; the final strengthened run also passed all three functional
checks but detected physical cursor movement during the non-atomic interval and
therefore records **passed=false**. Both fixtures exited 0. It was not automatically
replayed, nor counted as a no-cursor-change pass. Current report:
`.artifacts/hosted-uia-smoke.json`. This proves no universal Calculator/UWP support;
the exact ApplicationFrameHost topology on the issue reporter's machine remains
unverified. No user app, game input or IME switch was used for these checks.

The verified 0.5.3 wheel was deployed to the global MCP with a 0.5.2 rollback wheel
and configuration backup. An isolated import from the installed uv tool environment
matched all 28 package modules to the verified source. Installed stdio/feedback
checks exited 0; the seven tool definitions match the source report. A separate
installed-executable check confirmed four permission refusals before effects and
rectangle format/space metadata on 26 enumerated windows. Reports are retained in
`.artifacts/installed-0.5.3/`. The reconnected live status reports 0.5.3, strict UIA
default, and shell/process-control gates disabled; configuration was restored
byte-for-byte. No hosted desktop experiment was replayed during deployment, and the
previous `passed=false` cursor-interference evidence remains unchanged.

## 0.5.2 tool-definition usability

The seven existing tools now describe all 122 public parameters, their valid values,
units, important defaults and interactions. `actions`, selector, wait condition,
region and probe objects have documented JSON Schema structure. Tool descriptions
state routing, prerequisites, side effects, return evidence and failure boundaries.
In particular, play documents its 30-second recording limit, relative/nondecreasing
`at <= seconds`, actions/script choice, foreground checks and independent action/
capture results. This does not extend play duration, permissions or backends.

Metadata uses `Annotated` plus Pydantic Field JSON Schema extras, preserving the
existing argument types, defaults and Runtime whole-plan validation. No action
model drops unknown fields, inserts input defaults or converts a refusal into a
successful no-op. Comparing the tool execution bodies as ASTs against `7a4bbcb`
confirmed all seven bodies are unchanged (excluding annotations/docstrings).

Evidence: 54 definition-specific tests validate all published schemas and parameter
defaults, canonical/legacy examples, bounded definitions, key constraints and
pre-effect refusals through the real MCP in-memory transport. The full regression
suite passes **1291 tests**; Ruff check/format, stdio, lock validation and wheel/source
build pass. The updated `.artifacts/stdio-smoke.json` includes the full definitions
and parameter-coverage count. No new third-party TDQS score is claimed; no paid or
authenticated scoring endpoint was called. No real input or new game/UIA-provider
compatibility claim was needed for metadata-only changes.

The 0.5.2 wheel was installed globally after preserving the prior wheel/config.
An isolated import from the uv tool environment matched all 27 package modules to
the verified source. Installed-Python stdio and feedback round-trips exited 0; all
seven installed `tools/list` definitions match the source report, with all 122
parameters described. The reconnected live `system.status` reports 0.5.2, strict UIA
default, and shell/process-control gates disabled. Global configuration was restored
byte-identically. Installed reports are in `.artifacts/installed-0.5.2/`.

## 0.5.1 UIA token and write confirmation

This release adds bounded actionable tokens to `state(include_tokens=true)` and
single-write `set_value` confirmation against the same pinned UIA reference. Tokens
are issued only when returned metadata and the cached live binding agree; failed
state publication rolls back token-cache and binding changes. SetValue polls at
50ms intervals for at most 600ms inside the call deadline. No selector re-search
or write replay occurs. If the deadline expires before a final privacy check, the
unknown result omits both before/after values rather than performing more UIA reads.

| Command | Result |
|---|---|
| `uv run --no-sync ruff check .` | exit 0 |
| `uv run --no-sync ruff format --check .` | exit 0; 95 files formatted |
| `uv run --no-sync pytest -q -o faulthandler_timeout=30` | exit 0; **1237 passed in 30.61s** |
| `uv run --no-sync python scripts/smoke_stdio.py` | exit 0; seven tools and running version 0.5.1 |
| `uv run --no-sync python scripts/verify_feedback.py` | exit 0; six transport/data-fidelity checks |
| `uv run --no-sync python scripts/verify_windows.py` | exit 0; 13 owned-window checks, fixture exit 0 |
| `uv lock --check --offline` | exit 0 |
| `uv build` | exit 0; 0.5.1 wheel and source distribution |

The owned-window check obtains Edit and Apply tokens from one screenshot-free state,
uses them directly for SetValue and Invoke, and verifies the resulting native text
and invocation count. Delayed readback, single-write counts, deadline crossings,
metadata changes, token expiration and rollback are established with deterministic
fakes; this does not claim every real UIA provider has bounded response latency.
Reports: `.artifacts/windows-smoke.json`, `.artifacts/stdio-smoke.json`, and
`.artifacts/feedback-smoke.json`. No user application, physical input, IME switch,
game recording or new WGC/background-input experiment was part of this gate.

The independent review closed the published-metadata/token mismatch, the
post-write deadline overrun, and the remaining pre-write discovery gap. The last
reproduction confirms that slow resolution crossing the deadline starts no
subsequent handle/COM/walk calls and performs no write; deadline expiry during
token validation does not delete a valid cached token.

The verified non-editable wheel was deployed to the global MCP. Using the tool's
installed Python with `PYTHONPATH`/`PYTHONHOME` unset and user-site imports disabled,
all 26 installed Python modules matched the verified source hashes. The installed
interpreter then ran `scripts/smoke_stdio.py`, `scripts/verify_feedback.py`, and
`scripts/verify_windows.py`: each exited 0, with all 13 owned-window checks passing
and fixture exit 0. Reports are retained in `.artifacts/installed-0.5.1/`.
The reconnected global `system.status` reports `package_version="0.5.1"`, strict
UIA by default, and shell/process-control gates disabled. Its configuration was
restored byte-for-byte. The source release is versioned separately from the global
wheel installation.

## 0.5.0 feedback and wire fidelity — historical pre-deployment evidence

### Final additional-review gate — 1112 tests, 2026-11-13

The additional review confirmed and fixed five issues: omitted component failure
evidence under tight budgets; unverified process-lifetime/thread identity in
input context; misleading requested-versus-actual message release evidence;
ambiguous GetAsyncKeyState zero reported as certainly released; and image-derived
actions re-resolving a reused window title after earlier batch effects.

The P1 image-binding fix retains an immutable validated capture/recipient pin
through native and message execution. Per-action and normal event checkpoints
revalidate HWND/PID/process creation, geometry and DPI. Cleanup remains exempt
from coordinate geometry but keeps its existing owner/identity requirements.
Injected backends that cannot explicitly enforce the pin refuse image actions.

An independent fake replay of the original P1 scenario now completes only the
first key action and refuses the later click with STALE_CAPTURE: zero cursor
moves and zero mouse events. This is bounded checkpoint validation, not an atomic
guarantee against OS changes between a check and a subsequent input API call.

| Command | Result |
|---|---|
| `uv run --no-sync ruff check .` | exit 0 |
| `uv run --no-sync ruff format --check .` | exit 0; 93 files formatted |
| `uv run --no-sync pytest -q -o faulthandler_timeout=30` | exit 0; **1112 passed in 40.87s** |
| `uv run --no-sync python scripts/smoke_stdio.py` | exit 0; seven tools and v0.5 schema |
| `uv run --no-sync python scripts/verify_feedback.py` | exit 0; six Python IPC/MCP/JS fidelity checks |
| `uv run --no-sync python scripts/verify_windows.py` | exit 0; ten owned UIA/PrintWindow/context checks |
| `uv run --no-sync python scripts/verify_playback.py` | exit 0; synthetic PyAV decode and frame exports |
| `uv lock --check --offline` | exit 0 |
| `uv build` | exit 0; 0.5.0 wheel and source distribution |

The image-binding/coordinates/runtime/native/parity/message focused suite passed
340 tests in4.27s before the final gate. The public budget tests retain sanitized
state component errors, actual recipient HWND/PID are separated from requested
target bindings in event details, and unavailable modifier samples remain null.

An attempted `scripts/verify_background.py` rerun exited1 before any test action:
the cursor moved during fixture launch. The fixture exited0 and all owned windows
were reclaimed, but the report correctly has passed=false. No cursor restoration
or automatic rerun was attempted. The previous 19-check pass predates these
changes and is historical evidence, not a fresh passing integration result.

All26 wheel Python modules matched final source byte-for-byte. Global MCP config
and installed-executable hashes were unchanged; 0.4 remains installed. Existing
index contents were preserved; this review did not stage, commit, push or install.
No game/native-input/IME experiment was added during this review. The separately
authorized chat-input comparison below does not prove the FH6 historical cause.

### Additional review findings (after the original 1036-test gate)

The subsequent review identified further cases absent from the first gate: minimal
state output could erase component failure codes; input context did not verify
the target's creation time/thread before certifying consistency; held message
keyup feedback could describe the request target/method rather than its pinned
actual recipient; GetAsyncKeyState zero was treated as certainly released even
though the API also returns zero on unavailable access. Narrow fixes and fake
regressions were added without desktop input. Modifiers now use bool/null, with
partial/unknown availability instead of falsely known all-up states.

A separate P1 pre-existing issue was reproduced with production native input and
fake windows: image coordinates were validated during whole-plan preparation,
but a later action could re-resolve a reused title and click another window. The
fix now carries the original capture/recipient pin through native and message
dispatch, and refuses changes to identity/geometry/DPI before action checkpoints.
Regression evidence appears in the final gate below; it does not establish
historical application consumption or eliminate the OS check-to-use interval.

The current contract is [v0.5.md](v0.5.md). The release adds bounded native
dispatch receipts, optional read-only input context, truthful UIA failure
predicates, exact public timestamp strings, runtime-instance facts and explicit
state capture selection. These do not establish the cause of the historical
Edge or FH6 failures. No new game input, IME switch, remote-software setting
change or global installation is part of this implementation run.

`scripts/verify_feedback.py` uses an isolated synthetic MCP server with a known
64-bit creation time, then reads the real stdio response in Python and passes
both public representations through JavaScript. It checks exact string/null
types and round-trip fidelity; it never queries a real window or sends input.
`scripts/verify_windows.py` additionally requests bound combined state through
the explicit PrintWindow backend on its owned fixture, without native input.
Implementation lanes were frozen and their artifacts reconciled before the
merged final gate below. Older-release results are historical and do not
substitute for the 0.5 evidence.

Completed scoped checks before the merged gate (not a substitute for it):
- UIA state/diagnostics: 71 tests; existing UIA/query regressions: 59 tests. The
  review also corrected zero thread-ID lookup to unavailable and root HWND zero
  to zero-or-unavailable, without weakening refusals or repeating queries.
- Runtime/protocol/output/trace/wire: 196 tests, including Bun round-trip; after
  the new static predicate was added, 52 relevant output/protocol tests passed.
- Real stdio schema/status smoke passed for version0.5.0, compact feedback,
  explicit state backend defaults and stable per-process instance facts.
- Synthetic stdio/broker/JavaScript verification passed six checks: exact
  internal Python IPC integer, exact external decimal string, nested identities,
  null, unchanged HWND/PID number types, and matching text/structured results.
- Owned Win32 fixture passed nine checks, including actual bound UIA plus
  explicit PrintWindow state. Fixture exit0, no native input. Report:
  `.artifacts/windows-smoke.json`.
- Existing synthetic PyAV playback passed: ten decoded video frames, three
  exported original PNGs and JPEG800x343 montage. No user game was recorded.
- Scoped budget reviews found that the 2048-character/byte fallback
  could omit a failed action's receipt and play's nested action counters. The
  repair retains core requested/accepted/unknown counts (including null accepted),
  the first failure, cleanup uncertainty and nested play counters. Actual MCP
  envelope regressions passed in 54 focused output/protocol tests; an independent
  in-memory act/play reproduction confirmed the repaired behavior. This is scoped
  evidence only; the merged native implementation and full-suite gate remain
  pending.

The first merged suite checkpoint reported 1027 passed / 1 failed. The failing
legacy UIA auto-readback fixture returned huge text even for invoke; it was
corrected to emit a realistic small invoke result and oversized text only for
the text read. The targeted regression then passed. This does not weaken the
new primary-evidence-before-auxiliary budget policy.

The owned-window script now also directly calls the read-only input-context
observer. Ten checks passed with fixture exit0 and no native input: target-thread
layout was observable and cross-process IME open/conversion remained explicitly
unknown. This is not a Chinese/English typing A/B test or evidence of the
historical game input state.

### Initial frozen-source gate, 2026-11-13

| Command | Exit / evidence |
|---|---|
| `uv lock --check --offline` | 0; lock and source/project versions agree on 0.5.0 |
| `uv run --no-sync ruff check .` | 0; all checks passed |
| `uv run --no-sync ruff format --check .` | 0; 90 files formatted |
| `uv run --no-sync pytest -q -o faulthandler_timeout=30` | 0; **1036 passed in 35.33s** |
| `uv run --no-sync python scripts/smoke_stdio.py` | 0; seven schemas, startup version/instance facts, default strict UIA |
| `uv run --no-sync python scripts/verify_feedback.py` | 0; six Python IPC/MCP/JavaScript fidelity checks |
| `uv run --no-sync python scripts/verify_windows.py` | 0; ten owned-window checks, PrintWindow and read-only input context |
| `uv run --no-sync python scripts/verify_playback.py` | 0; ten decoded video frames, three PNG exports, compact montage |
| `uv build` | 0; 0.5.0 wheel and source distribution |

This was the initial gate before the second review’s additional issues; the new
revalidation gate below supersedes it for current source.

The full merged run followed final native/output/trace repairs. The owned UIA,
PrintWindow and PyAV paths were not subsequently edited; their scoped evidence
was reused rather than repeating desktop work. Synthetic input uses only fake
adapters: the production native/runtime/wire integration does not invoke Win32
input. A separate real read-only context query established available layout
fields, not actual IME composition or application acceptance.

The final narrow review returned **PASS**, independently repeating the original
cancelled chord and message-cleanup cases using production NativeInput with
fake adapters. At 2048/4096/6144/8192 response budgets, action-local release
evidence survives independently of the final batch's zero-event cleanup; both
MCP representations match and fit. Finally-release methods reflect actual
PostMessage/SendInput calls; empty cleanup does not claim an input API call.
Trace schema 2 retains one aggregate action-local cleanup without text/events.

The built wheel's 26 Python modules were compared byte-for-byte with source.
Global MCP configuration and executable hashes remained unchanged. This run
does **not** install 0.5, commit, push, switch IME, alter remote-control settings,
send real game input, prove WGC, or diagnose the historical FH6/Edge root cause.

### Separately authorized chat-input observation

After the frozen-source gate, the user explicitly authorized input in this
session's composer. The same source NativeInput sent C then W as separate
0.2-second scan-code holds. Each returned two accepted events and zero unknown;
the observed scans were46/17, with no modifier keys down. Independent screenshots
showed Chinese IME composition `c`, then `c'w` with updated candidates. Cleanup
reported no remaining owned input. No Enter, Space, Unicode text, input-language
switch or game action was sent. Composition was left unsubmitted for the user.

This establishes real native-to-IME composition in that chat context only. It
does not establish FH6 acceptance, IME as the historical game's sole cause, or
English-layout A/B results. Receipts are `.artifacts/chat-ime-c-test.json` and
`.artifacts/chat-ime-w-test.json`; source and global installation were unchanged.

The user subsequently authorized a language-switch comparison. A separately
started real 0.5 MCP sent one Win+Space chord, then the target thread was checked
again before typing: language_id changed from0x0804 to0x0409. C/W scan-code holds
through the MCP produced plain `cw` in the same chat input, with two accepted
events per key and clean release. Independent screenshot showed no candidate
window. No Enter, Unicode text, clipboard or game input was used. The English
layout and unsent `cw` were left for the user; no automatic restore was issued.

The switch action's immediate context still sampled the old layout, while the
later check saw the new one. This reinforces non-atomic feedback semantics and
the need to observe a changed precondition before subsequent input. ImmIsIME
remained true under the English-labelled layout; it is not a composition/open
status. The comparison establishes chat-context behavior, not FH6 causality.

## 0.4 application state and background messages

The three new capabilities are documented in [v0.4.md](v0.4.md): explicit
background messages, combined application state and opt-in rich UIA metadata.
They do not relax the foreground requirement of real native input, add another
public tool, or enable shell/process-control permissions.

Pre-release merged gate, 2026-11-12:
| Command | Result |
|---|---|
| `uv run --extra wgc ruff check .` | exit0 |
| `uv run --extra wgc ruff format --check .` | exit0; 78 files formatted |
| `uv run --extra wgc pytest -q -o faulthandler_timeout=30` | exit0; **837 passed in 36.42s** |
| `uv run --extra wgc python scripts/smoke_stdio.py` | exit0; seven tools, input-mode/state schemas |
| `uv run --extra wgc python scripts/verify_windows.py` | exit0; existing own-control UIA/readback regressions |
| `uv run --extra wgc python scripts/verify_background.py` | exit0; 18 independent own-window checks |
| `uv run --extra wgc python scripts/verify_playback.py` | exit0; ten PyAV frames decoded, frame exports and compact montage |
| `uv build` | exit0; 0.4.0 wheel and source distribution |

The background script launches its own two NOACTIVATE windows, validates every
root/child PID, and independently reads Edit contents and fixture counters. It
proved exact message set_value, Unicode text, key delivery, a real standard-button
click using a no-activation fixture subclass, drag/client-coordinate translation,
both wheel axes/signs, unrelated same-PID root refusal, rich metadata and one root
binding across image/UIA. Physical cursor and foreground observations were stable,
with no real mouse/keyboard injection or focus command. Fixture exit0 and zero
remaining owned windows are checked. Report: `.artifacts/background-smoke.json`.

Actual UIA focused selected text was **unavailable** in that background fixture:
no SetFocus was issued because it may activate the parent. Positive scoped
selection is covered by fake-provider tests; independent native EM_GETSEL readback
does not establish that the UIA selection path worked in this desktop scenario.
Screenshots are screen composite and may contain occluders; no unoccluded-pixel
claim is made. Combined state is bound but non-atomic, with per-component timing
and partial-error reporting rather than a same-frame claim.

The generic cursor observation now uses GetPhysicalCursorPos, avoiding false
motion caused by a DPI-context change after UIA/MSS import. Password/unknown
password structured values and text are redacted; a redacted response is not an
empty response and cannot trigger OCR fallback. Screenshots themselves may still
contain anything the application paints; text redaction is not image masking.

Message tests include an actual isolated synthetic worker for cancellation,
timeout, kill/reap and uncertainty classification. Messages possibly queued before
a worker loss are not replayed; a new worker cannot confirm releases for old
possibly held messages. This is not a guarantee of background compatibility with
every application or game. Final source-boundary review is recorded after gate.

The first source review found five cases not covered by that pre-repair run:
explicit message image/text feedback could rediscover a reused caption; posted
text could be processed after a subsequent synchronous value replacement;
reparented controls could outlive traversal-based focus/selection proof;
aggregate ancestor values could bypass descendant password redaction; and output
pruning could leave focus/selection indices referring to omitted nodes. The
837-test result above is therefore a checkpoint, not a final post-repair claim.

Repairs bind every message feedback variant to the action's original root, use
bounded synchronous payload dispatch consistently, revalidate live UIA ancestry,
apply unsafe descendant scope to value reads, and invalidate omitted-node
references without remapping them. The owned-window script also checks mixed
text/value ordering. A later attempted desktop run was stopped by observed cursor
interference and is not counted as fresh passing evidence; guards and no-replay
behavior were retained. Final validation follows after all writers are frozen.

Post-repair frozen-source validation:
| Command | Result |
|---|---|
| `uv run --extra wgc ruff check .` | exit0 |
| `uv run --extra wgc ruff format --check .` | exit0; 78 files formatted |
| `uv run --extra wgc pytest -q -o faulthandler_timeout=30` | exit0; **899 passed in 34.35s** |
| `uv run --extra wgc python scripts/smoke_stdio.py` | exit0; seven tools and 0.4 schema |
| `uv run --extra wgc python scripts/verify_background.py` | exit0; **19 checks**, including mixed text/value ordering |
| `uv run --extra wgc python scripts/verify_playback.py` | exit0; ten decoded PyAV frames, three original PNG exports, JPEG800x343 |
| `uv build` | exit0; 0.4.0 wheel and source distribution |

The first post-repair suite exposed one stale integration-test adapter still
implementing `post_message`; it was changed to the production `dispatch_message`
interface before the passing run. No production fallback was added. The new live
ordering check sends `set_value("") -> text("A") -> set_value("B")` in one batch
and immediately reads exact `B` through an independent bounded WM_GETTEXT call;
it uses no sleep or pretend queue barrier. Physical cursor and foreground were
stable for the successful run, and all owned fixture windows were reclaimed.
The unavailable UIA selected-text boundary above is unchanged.

The same reviewer returned **PASS** on the five-finding narrow re-review, with
40 independent in-memory regression cases. The 0.4.0 wheel was installed into
the global uv tool environment; installed source hashes matched this source,
and the installed interpreter passed the stdio smoke and all 19 owned-window
checks. Global OpenCode configuration was restored byte-for-byte, without
enabling shell or process-control permissions. The live MCP advertises the
three input modes and retains strict UIA as the default.

After completion, the user separately authorized a new 0.4 commit and normal
push to main. Machine configuration and generated artifacts remain excluded.

## Current video backend — 0.3.1

PyAV 19.0.2 is a locked mandatory dependency. The production MP4 path no longer
invokes an external ffmpeg/ffprobe executable. H.264 encoding runs in a disposable
Python worker with duplicated anonymous-file handles, a hard output-byte limit,
and cancellation/deadline cleanup. Only the parent writes the final registered
artifact after a completed result. Each video is capped at the smaller of the
artifact byte budget and 256 MiB; raw input remains bounded at 512 MiB.

Three distinct evidence paths:
- `scripts/verify_pyav.py`: direct synthetic library proof, not worker cancellation
  proof. Even 64x48 and odd 1927x1148 sources encode ten frames and decode with
  checked colors, frame rate, PTS, duration and right/bottom padding.
- `scripts/verify_recording.py` / `verify_playback.py`: production CaptureService
  and isolated VideoEncoder produce MP4; PyAV reads it back, with original-frame
  exports and compact JPEG montage checked. No real desktop pixels or input.
- `tests/test_video_encoder.py`: real spawn encoding plus synthetic stuck/crashed/
  corrupt workers, hard quotas, deadline/cancel/concurrent close/recovery and
  child cleanup. Capture tests retain safe artifact and failure-reservation checks.

Executed after integration:
| Command | Result |
|---|---|
| `uv run --extra wgc ruff check .` | exit0 |
| `uv run --extra wgc ruff format --check .` | exit0; 67 files formatted |
| `uv run --extra wgc pytest -q -o faulthandler_timeout=30` | exit0; **708 passed in 38.00s** |
| `uv run --extra wgc python scripts/smoke_stdio.py` | exit0 |
| `uv run python scripts/verify_pyav.py` | exit0; `.artifacts/pyav-smoke.json` |
| `uv run python scripts/verify_recording.py` | exit0; even/odd H.264 decoded |
| `uv run python scripts/verify_playback.py` | exit0; ten video frames decoded and exports checked |
| `uv build` | exit0; 0.3.1 wheel and source distribution |

A separate temporary PyAV/Pillow-only experiment also encoded/decoded without
NumPy or external ffmpeg. The project verification script passed in an isolated
`uv run --no-project` environment with the project package installed.

The internal `CaptureService` injection parameter is now `encoder`, not `ffmpeg`;
the seven public MCP tools are unchanged. Historical external-FFmpeg evidence
below records earlier versions, not the current dependency requirement. This
does not prove fresh real-game MP4 capture or resolve prior IME interference.

Post-review hardening replaced unbounded child joins with terminate/join/kill/join
deadlines and an explicit cleanup-failure fault gate retaining unreaped ownership.
The same reviewer passed that narrow cleanup re-review. A pre-existing file-wait
test race was stabilized by creating its fixture only after baseline enumeration,
not by increasing the timeout or changing production file-wait semantics.

The built 0.3.1 wheel was installed into the isolated global uv tool environment.
The installed interpreter reran `scripts/verify_recording.py` successfully for
both source sizes, and installed module SHA-256 values matched the working source.
The OpenCode global config was temporarily disabled for installation and restored
byte-for-byte; no shell/process permissions were enabled. After verification,
the user separately authorized consolidating the complete project and PyAV
update into one commit and pushing that single-commit history to the public
repository. Machine configuration and generated artifacts are excluded.

## Claims and evidence owners

| Claim | Smallest evidence | Owner |
|---|---|---|
| Strict UIA never silently uses physical input | Backend unit tests for unsupported patterns/ambiguous and truncated matches | UIA lane |
| Values/states are verified, not merely called | Fake readback mismatch + owned native-control independent readback | UIA lane + reconciliation |
| A stuck provider does not permanently block future calls | Spawned synthetic hanging worker, deadline/reap/recovery, no mutation replay | Broker lane |
| Native input is explicitly gated and cleaned up | Disabled/foreground/SendInput short-write/finally tests with fake adapter | Native lane |
| Captures are correctly encoded and honestly labeled | Injected grabber tests, montage metadata, FFmpeg availability behavior | Capture lane |
| MCP protocol is clean and images are genuine content blocks | In-process SDK client + separate stdio child initialization/list/call | Protocol lane + reconciliation |
| Project builds without replacing installed tools | uv build, locked environment, untouched global configuration | Reconciliation |

Run the full suite after all writer lanes are reconciled. Focused lane results remain valid
only while their code/contracts are unchanged. Store concise final command exit statuses
and relevant reports in `.artifacts/verification.json` (ignored); keep user-facing boundaries
in README rather than asserting cross-application or game compatibility.

## Owned Windows fixture

`tests/windows_fixture.py` creates a standard Win32 window with an Edit control, Apply button,
native invocation counter, checkbox, and deliberately duplicate button names.
It uses NOACTIVATE + SW_SHOWNOACTIVATE. `scripts/verify_windows.py` launches only this fixture,
uses the production runtime/broker/UIA worker, and independently checks WM_GETTEXT,
BM_GETCHECK and the counter. It refuses to use native input and closes its child in finally.

This establishes a real UIA path for this fixture only. It is stronger than unit mocks but
does not establish calculator/browser/game compatibility, UIPI behavior across privilege
levels, full DPI/multimonitor coverage, or an absence of provider-triggered focus changes.

## Stdio smoke

`scripts/smoke_stdio.py` uses the official SDK client to launch the actual service, initialize,
list seven tools and call read-only `system.status`, then close the connection. JSON reports
and server stderr are separate. The in-process protocol tests cover image and error mapping;
stdio smoke additionally tests framing/entrypoint, not user-visible desktop effects.

## Initial Windows evidence — 2026-11-11

Final reconciled baseline commands (all exit **0**):

| Command | Evidence |
|---|---|
| `uv run ruff check .` | All checks passed |
| `uv run ruff format --check .` | 26 files already formatted |
| `uv run pytest -q` | 83 passed in 2.03s |
| `uv run python scripts/smoke_stdio.py` | Seven tools, strict/native-disabled default, native refusal isError, window listing without starting UIA |
| `uv run python scripts/verify_windows.py` | Real owned Win32 controls; five independent checks passed |
| `uv run python scripts/verify_recording.py` | Synthetic capture, H.264 MP4, ffprobe verified 64×48 and all 3 frames; montage timestamps match |
| `uv build` | Source tar.gz and Python wheel produced under dist/ |

Command logs: `.artifacts/final-check-01.log` through `final-check-07.log`;
machine-readable summary: `.artifacts/verification.json`. No real native injection,
user-app control or game interaction was performed by these checks.

The first owned-fixture smoke returned exit 0: Unicode SetValue matched independent
WM_GETTEXT, Invoke incremented the native counter once, Toggle matched BM_GETCHECK,
duplicate names and unsupported Invoke both refused. The fixture closed with exit 0 and
empty stderr. The real stdio client initialized, discovered all seven tools, and called
read-only status successfully. Raw reports are in `.artifacts/`.

Foreground HWND did not change during the recorded fixture actions. Cursor observations
did change while other desktop input was occurring; this run cannot establish a stationary
pointer or attribute those movements. No native input API was used by the smoke.
These observations are not a cross-application compatibility claim.

## 0.2 capability-expansion evidence — 2026-11-12

The expanded focused lanes provide: 89 UIA query/token tests, 29 native/control cancellation
tests, 19 capture/window-worker tests, and the output/trace budget and ownership tests.
The owned Windows fixture additionally passed the UIA tree, Unicode SetValue readback, Invoke,
Toggle, ambiguity refusal, token issue/use/refusal, delayed `wait_for`, and bounded-tree checks.
These are real standard Win32 controls, not a game or Calculator.

PrintWindow against that fixture produced a nonuniform 630×540 window image with clean worker
exit and no native input. Because the fixture/provider rendered a logical-size surface inside
a larger physical bitmap, the expanded implementation deliberately reports the physical
transform as unknown rather than inventing a click-coordinate mapping. This is a capture
honesty result, not a failure to produce bytes.

The optional `windows-capture==2.0.1` WGC path was installed in the isolated `wgc` extra and
tested against the same fixture. It fails at native `CreateForWindow` conversion with
`WGC_ITEM_CONVERSION_FAILED` / `Failed to convert item to GraphicsCaptureItem`; no screen
fallback or fabricated frame is reported. This environment therefore has **no verified WGC**.
Run `uv run --extra wgc python scripts/verify_window_capture.py --backend printwindow` for
the verified path; `--backend wgc` is an explicit capability check and is expected to fail on
this host until the Windows/WinRT capture-item issue is resolved. WGC support remains optional.

Final reconciled checks (all exit **0**, 2026-11-12):

| Command | Evidence |
|---|---|
| `uv sync --locked --python 3.13 --extra wgc` | CPython 3.13 environment and optional capture dependency resolved |
| `uv run --extra wgc ruff check .` | All checks passed |
| `uv run --extra wgc ruff format --check .` | 44 files formatted |
| `uv run --extra wgc pytest -q` | **261 passed in 23.78s** (including explicit force_focus and click-timing regressions) |
| `uv run --extra wgc python scripts/smoke_stdio.py` | Seven actual tools, read-only status/list, native refusal and clean framing |
| `uv run --extra wgc python scripts/verify_windows.py` | Eight owned-fixture checks including token, delayed condition and bounded output |
| `uv run --extra wgc python scripts/verify_window_capture.py --backend printwindow` | Real window pixels and original-surface ROI; physical transform unknown |
| `uv run --extra wgc python scripts/verify_recording.py` | Synthetic H.264 64×48, all 3 frames verified by ffprobe |
| `uv build` | 0.2.0 source tar.gz and wheel; upstream license/notice included |

Concurrency regressions exercise actual stdio with 40 queued requests, cancel fast path,
queued-request cancellation isolation, disconnect cleanup, and cancelled UIA child replacement.
The replacement assertion verifies a new worker PID, actual successful response, and no
remaining child/thread. The Windows launcher explicitly isolates OS standard handles before
Python bootstrap; redirecting stdout inside the worker is too late. This local adapter uses
CPython internals and the package therefore declares **3.13 only**.

Budget regressions preserve nested play/act first-failure error code and uncertainty under
2048-byte limits; identity strings are complete or omitted, never shortened into selectors.
Artifact regressions cover low-quota current-result pinning, symlink swaps before/after
validation, root replacement prevention, safe ledger persistence, and failed-write cleanup.
The current result group can exceed quota with explicit over_budget until the next artifact
group; unowned files are not deleted.

WGC `--backend wgc` remains a **failed optional capability test**, not part of the successful
core gate. No real SendInput/cursor/game interaction is part of these checks.

## Remaining boundaries

### Explicit force_focus follow-up — 2026-11-12

The native-only force_focus action adds minimized-window restoration, best-effort
AttachThreadInput/BringWindowToTop/ShowWindow/SetForegroundWindow and exact final
foreground HWND/PID verification. An already foreground, non-minimized target is
a no-op. Default UIA and conservative focus behavior remain unchanged.

Fake-adapter tests cover attachment/detachment, stale targets, foreground mismatch,
cancellation, duplicate/self/null threads and native double opt-in. Final full
suite: 245 passed; Ruff check/format, actual stdio smoke and build all exit 0.
This follow-up did not perform real activation or mouse/keyboard input; it does
not establish that Windows will permit activation in every environment.

### Game-input timing follow-up — 2026-11-12

Subsequent live testing did perform explicitly authorized native input in the
Spider-Man 2 profile menu/ReShade overlay. Installed sshh12 first verified
Down/Up 150ms selection changes and a real overlay tab click. Minano click was
then changed to 20ms cursor settle plus 30ms button dwell with cancellation,
foreground and release checks. The updated project MCP visibly switched About
to Add-ons, closed the overlay using held Home, and moved New Game 2 -> 3 -> 2
using held Down/Up. No save was loaded, created or deleted; no F10 was sent.

Final follow-up: 261 passed in 23.78s; 45 files formatted; Ruff, actual stdio
smoke and build all exit 0. Native test flag was removed, project config bytes
and global hash matched the pretest state, and live status confirmed native
disabled. These menu/overlay observations do not prove free-roam movement or
camera rotation. Exact observations and call IDs: [game-input.md](game-input.md).

- Timeout/worker termination does not prove a remote mutation was cancelled.
- A post-call snapshot or successful SendInput count does not prove an app consumed input.
- Screenshot timestamps do not prove exact action/frame pairing.
- Composite desktop crops are not occlusion-free window captures.
- A strict UIA policy cannot prevent a target provider from changing its own focus.
- No automatic retries of unknown write outcomes; no synthetic production mock fallback.

## 0.3 parity verification — 2026-11-12

Checkpoint `5ba60d6` was committed before parity work, after a fresh **261 passed**
run, Ruff check/format and actual stdio smoke. The later 0.3 changes are not
automatically committed. New interface/policy coverage: [parity.md](parity.md).

User-approved policy changes: native input is selected by `native=true` per call
without restart; screenshots and whole montages default to 800px JPEG quality75;
act auto feedback returns an image for native actions and compact text for UIA.
These defaults do not grant shell/process-control permissions.

Executed integration evidence (no user/game keyboard/mouse input):

| Command | Evidence |
|---|---|
| `uv run --no-sync --extra wgc python scripts/verify_windows.py` | Own Win32 controls: SetValue/Invoke/Toggle native readback, ambiguity refusal, no second invoke, token/wait/budget checks passed |
| `uv run --no-sync --extra wgc python scripts/verify_window_capture.py --backend printwindow` | Own fixture, actual PrintWindow JPEG and ROI passed |
| `uv run --no-sync --extra wgc python scripts/verify_recording.py` | 64x48 and 1927x1148 synthetic sources encoded H.264 and decoded; odd source padded to 1928x1148 |
| `uv run --no-sync --extra wgc python scripts/verify_playback.py` | Actual Runtime.play + fake input/synthetic pixels: 10 video frames decoded, >=3 distinct frame colors, montage800x343/10230bytes, 3 original PNG exports+index, DSL shot and click-context preservation passed |
| `uv build` | 0.3.0 wheel and source distribution built |

Reports are under `.artifacts/`: windows-smoke.json,
window-capture-printwindow-smoke.json, recording-smoke.json, playback-smoke.json.
Synthetic native adapters prove orchestration and frame output, not delivery into
a real game. No fresh 0.3 real-game MP4 claim is made. Earlier live observations
and unresolved IME interference are retained in [game-input.md](game-input.md).

Initial broad gate: Ruff check/format passed (63 files). Full pytest then stalled
in the old direct FastMCP call cancellation regression. Reproduction identified
the new Context-only caller-scope resolution refusing a direct in-process call;
the narrow no-request Context case now uses a per-server opaque local scope.
Real client/session scoping is unchanged. Admission tests now fail promptly
instead of waiting forever if a task exits before admission.

Revalidated after repair:
- `uv run --extra wgc ruff check .` — exit0.
- `uv run --extra wgc ruff format --check .` — exit0, 63 files formatted.
- `uv run --extra wgc pytest -q -o faulthandler_timeout=30` — exit0,
  **637 passed in 38.64s**.
- `uv run --extra wgc python scripts/smoke_stdio.py` — exit0, seven tools,
  explicit_per_call native policy, strict UIA refusal, clean stdio verified.

The suite includes real owned Windows Job Object containment/output/timeout/
cancel-and-grandchild cleanup and WinRT OCR on synthetic image data. Clipboard
read/write/restore-race regressions use fakes, not the user's actual clipboard.

Release-gate review subsequently found three gaps outside those 637 cases:
desktop/display image provenance was confused with the explicit input recipient;
NUL-containing paste was rejected too late; a truncated probe prefix could be
accepted as complete JSON. These require narrow repairs and new regressions.
The 637-pass evidence is a pre-repair checkpoint, not the final release claim.

The bounded repair pass separates screen-surface geometry from a concrete live
window recipient while preserving HWND-bound captures' identity and geometry
requirements. New cross-module regressions use real NativeInput with an injected
Win32 adapter, not a fake input backend that silently accepts desktop targets.
They cover desktop/display/screen-viewport -> explicit HWND image-click dispatch
and after-feedback without OS input. Paste NUL is now rejected by the pure plan
validator; truncated probe prefixes stop as unknown without evaluating until or
replaying shell execution.

### Final post-repair gate

The same reviewer returned PASS on the three material repairs. All writer lanes
were frozen before the final run; SHA-256 hashes of src/tests/scripts were checked
before and after the gate and were unchanged.

| Command | Final result |
|---|---|
| `uv run --extra wgc ruff check .` | exit0 |
| `uv run --extra wgc ruff format --check .` | exit0; 63 files formatted |
| `uv run --extra wgc pytest -q -o faulthandler_timeout=30` | exit0; **659 passed in 25.20s** |
| `uv run --extra wgc python scripts/smoke_stdio.py` | exit0; seven tools, clean protocol |
| `uv run --extra wgc python scripts/verify_playback.py` | exit0; 10 decoded video frames, JPEG800x343/10226bytes, three full-size PNG exports |
| `uv build` | exit0; 0.3.0 wheel and source distribution |

Earlier still-valid owned UIA/PrintWindow evidence remains separate from fake
native injection and synthetic video evidence. Packaged wheel inspection found
all new service/coordinate/script modules and the required MIT/PSF license files.
The live project MCP was reconnected to the 0.3 interface, with shell and external
process-control startup permissions still off. Global config was not modified.

Remaining limitations: optional WGC is not working on this machine; fresh real
game MP4 and the reported IME/combination interference are not reverified; no real
clipboard-mutation test was performed. Default image compression is a response
size policy, not a promise of a particular model token count. The user separately
authorized the 0.3 commit after this gate; local MCP config and generated artifacts
are excluded. Subsequent publication and history consolidation were separately
authorized by the user.
