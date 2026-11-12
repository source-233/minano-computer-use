# Windows game input evidence

This document distinguishes delivered input from visible game response. Tests are
limited to the running Spider-Man 2 profile-selection menu and ReShade tabs.
No save was loaded/created/deleted; no F10 capture was triggered.

## Observed baseline — 2026-11-12

- New `force_focus`: exact foreground HWND verified for the game.
- New native `move`: profile hover highlight changed, so pointer movement was
  visibly consumed. New immediate down/up `click` had no demonstrated effect.
- Installed sshh12 MCP: scan-code hold Down for 150 ms moved selection from
  New Game 2 to New Game 3; hold Up for 150 ms returned to New Game 2.
- Installed sshh12 MCP: hold Home for 100 ms opened ReShade; a real left click
  switched the Add-ons tab to About. This is stronger evidence than SendInput
  acceptance alone. No addon parameters were changed.

## Narrow compatibility change

The old click helper waits 20 ms after cursor positioning and holds the mouse
button down for 30 ms. Minano previously released immediately. Native click now
uses the same timing: move -> cancellable/focus-checked 20 ms settle -> revalidate
hit target -> down -> cancellable/focus-checked 30 ms hold -> up.

The reason is a hypothesis, not a proved engine implementation detail: a
frame-polled input consumer may miss a button state that is released too soon.
Waiting after an already-completed click cannot restore a missed down state.

No automatic focus, window-message fallback, driver, process injection or game
hook is introduced. Explicit native opt-in and target checks remain. Cancellation
or foreground loss during a held click attempts release on the operation owner
thread, even if the target is no longer foreground.

## Minano live retest after the timing fix

The project-level MCP was restarted with native input temporarily enabled so
the running server used the edited code. The same game HWND/PID was revalidated.

- `force_focus`: verified the exact foreground target (and used no-op when already front).
- Native click at the current screen-transformed Add-ons-tab location changed
  ReShade from About back to Add-ons. Before/after screen captures show the page
  content changed, not just a hover highlight. Call ID:
  `f62e7c50-137c-454f-bca8-fc09359f5002`.
- Native held Home (VK36) for 100ms closed ReShade.
- Native held Down 150ms changed the profile highlight from New Game 2 to 3;
  native held Up 150ms restored New Game 2. Calls:
  `40f6447d-0510-4448-8888-4bc30d96d7e2` and
  `e1591b7f-dbdf-480a-82c0-342124aef34a`.
- Every native batch reported completed release cleanup. No save was loaded or
  altered. The Add-ons tab and closed-panel state were restored. The project
  config was restored byte-for-byte, global config hash unchanged, and the live
  reconnected MCP reported `allow_native=false`.

Final software checks after this change: Ruff check/format passed (45 files),
`pytest` **261 passed in 23.78s**, actual stdio smoke passed, and 0.2.0 build passed.
The live observations establish menu keyboard and ReShade click control for this
running build; they do not establish free-roam movement or camera rotation.

## Free-roam follow-up — 2026-11-12

At the user's request, the existing profile was selected and Continue was used.
Entering gameplay can trigger the game's autosave; this was disclosed, and no
manual save creation/deletion or settings changes were requested or performed.

Initial short W/D attempts while crouched on a lamp-post support produced no
demonstrated movement. A W+Space attempt was interrupted; the user reported
that the input method was invoked and movement failed. This is unresolved and
must not be reported as a successful jump or proof that combinations are safe.

The user then moved the character onto a flat roof. A fresh foreground-verified
screen capture established the new baseline; no old position assumptions were
reused. Two short Minano native `play` calls then produced visible responses:

| Test | Observation | Call/artifact |
|---|---|---|
| Hold W 300ms, without Space/modifiers | Walking animation, camera/nearby geometry change and character displacement after the input | `4e91ec65-93c7-4514-a366-e4f482efa7ef`; `artifact-9f272b2209da490599a877bd6ddae852.png` |
| Relative look +100 then -100, separated in time | Scene shifted horizontally and returned toward the prior heading while character stayed in place | `c16cc564-8ed3-4351-9dc2-b3c2f195d158`; `artifact-fe098e6c70eb4c3684016dba902a9fba.png` |

Both calls returned completed release cleanup, with no held inputs remaining.
The artifacts are under `.artifacts/minano-artifacts/` and subject to retention,
not permanent test fixtures. They show independently timestamped observations,
not strict game-frame/action pairing.

Limitations observed during the same run:
- Other windows occluded one earlier screenshot; it was discarded as evidence.
- `play` returned real montage images but `mp4_path=null` with an encoding/backend
  warning. Real-game MP4 recording is not verified, even though synthetic-video
  tests previously passed. No fake MP4 success was claimed.
- Input-method interference with the interrupted W+Space attempt is not fixed.
- Short W/relative-look success is not a guarantee of every control, movement
  state, background input, game build or remote-session environment.

After the test, the original project config bytes were restored, the global
config hash was unchanged, and the temporary native launch flag was removed.

## Verification boundary

Fake-clock tests establish minimum settle/hold ordering and cleanup on cancellation,
foreground loss and failed SendInput. Real game validation must separately establish
visible effects. Menu/overlay success does not prove in-game movement, camera rotation,
all key bindings, exact action/frame pairing, or compatibility with other games.

Use the screenshots' reported physical coordinate transform for screen-mode
input. Do not pass resized-image coordinates directly to native `x/y`. PrintWindow
and WGC may report an unknown physical transform and must not be used to guess clicks.
