# Verification evidence and limits

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
