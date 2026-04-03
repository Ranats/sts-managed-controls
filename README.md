# STS Autoplayer Lab

Local research and tooling project for Slay the Spire 2.

The repo currently has two practical tracks:

- live automation and state observation
- `STS Managed Controls`, a local desktop control panel for in-process card, power, relic, block, and energy manipulation

If this project is published publicly, the cleanest positioning is not "cheat pack" but:

- single-player modding and debugging toolkit
- local sandbox for testing interactions, balance, and content mods
- research environment for state probing and UI automation

That framing is materially better for GitHub, Nexus Mods, Discord, and Reddit than a trainer-first pitch.

## Managed Controls

The fastest way to try the current local runtime controls is:

```powershell
python -m sts_bot.cli managed-control-ui --profile profiles\windows.example.json
```

The current UI supports:

- probe current managed state
- set or maintain block
- set or maintain energy
- create or apply powers through the bridge mod
- search cards, powers, and relics from `sts2.dll`
- add cards to hand or deck
- replace the master deck
- obtain relics

Detailed operator notes are in `docs/workstreams/managed_runtime_controls.md`.

## Public Release Direction

If this repo is prepared for a public release, the strongest channel mix is:

- GitHub for source, issues, and releases
- Nexus Mods for discovery and binary distribution
- Discord / Reddit for announcements and support

The most realistic monetization path is:

- free core release
- optional support links such as GitHub Sponsors or Ko-fi
- later, only if justified, a separate "pro" workflow layer for mod authors

Trying to force an underground paid-cheat positioning is possible, but the audience is smaller and the reputation cost is much higher.

Release planning notes and listing copy are in:

- `docs/release/public_release_checklist.md`
- `docs/release/listing_copy.md`

## Architecture

The bot is now split into:

- decision/state code
  - policy, analysis, reward evaluation, battle helpers
- window/runtime code
  - `WindowLocator`
  - `TargetWindow`
  - `CoordinateTransform`
- capture backends
  - `WgcCaptureBackend`
  - `DxgiDuplicationCaptureBackend`
  - `Win32WindowCaptureBackend`
  - `VisibleRegionCaptureBackend`
  - `LegacyForegroundCaptureBackend`
- input backends
  - `WindowMessageInputBackend`
  - `LegacyForegroundInputBackend`

The main adapter keeps using the existing screen recognition and action selection flow. It now consumes injected capture/input backends instead of directly calling global desktop capture or global input APIs.

## Default Behavior

Default runtime selection:

- capture backend: `auto`
- input backend: `auto`
- `auto` capture order:
  - `wgc`
  - `dxgi`
  - `win32_window`
  - `visible_region`
  - `legacy` only if `allow_foreground_fallback=true`
- `auto` input order:
  - `window_messages`
  - `legacy` only if `allow_foreground_fallback=true`

Current repo build status:

- `wgc`: implemented through the optional `windows-capture` package; otherwise reports unsupported
- `dxgi`: declared but not bundled here, reports unsupported
- `win32_window`: implemented, background-capable for visible windows
- `visible_region`: implemented, captures only the target client region via screen `BitBlt` while the window remains visible
- `window_messages`: implemented, background-capable transport
- `legacy`: retained only for explicit opt-in comparisons

Current optional capture add-on:

- If the Python package `windows-capture` is installed, `wgc` becomes available and `auto` may select it first.
- This path uses Windows Graphics Capture through that package and is intended for non-foreground / occluded windows.
- Minimized or fully hidden behavior is still not guaranteed here and should be treated as unverified.

No silent fallback is performed from message-based or background capture paths to foreground/global input.

## Background Capability Model

Supported by design:

- capture while another window stays in front
- message-based input without moving the global cursor
- client-coordinate transforms that track window size changes

Not guaranteed:

- minimized windows
- fully occluded or non-presenting windows
- games that ignore `WM_*` input even though the backend can deliver it

The repo reports these as capability/diagnostic facts instead of silently switching to global input.

## Legacy Paths

The following are legacy/debug-only and are not part of the normal path:

- `pyautogui`
- `pydirectinput`
- `SendInput`
- `mouse_event`
- `keybd_event`
- `ImageGrab`
- foreground activation helpers in `windows_api.py`

They remain available only through `LegacyForegroundCaptureBackend` / `LegacyForegroundInputBackend`, both of which are:

- `foreground_only=true`
- disabled by default
- intended only for explicit comparison/debugging

## Setup

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -e .
pip install pytesseract
```

Install Tesseract OCR as well. The code auto-detects:

- `C:\Program Files\Tesseract-OCR\tesseract.exe`
- `C:\Program Files (x86)\Tesseract-OCR\tesseract.exe`

## Profile Settings

`profiles/windows.example.json` now includes backend/runtime controls:

- `capture_backend`
  - `auto | wgc | dxgi | win32 | visible_region | legacy`
- `input_backend_name`
  - `auto | window_messages | legacy`
- `legacy_input_backend`
  - existing legacy transport selector such as `combined`, `sendinput_scan`, `pyautogui`
- `startup_sequence_labels`
  - ordered startup actions used by `startup-sequence-live` and `hybrid-run-live`
- `scene_input_backends`
  - per-screen backend overrides such as `menu=legacy`, `battle=window_messages`
- `target_process_name`
- `target_title_regex`
- `target_class_name`
- `dry_run`
- `verbose_diagnostics`
- `allow_foreground_fallback`

Environment overrides:

- `STS_BOT_CAPTURE_BACKEND`
- `STS_BOT_INPUT_BACKEND`
- `STS_BOT_TARGET_PROCESS`
- `STS_BOT_TARGET_TITLE_REGEX`
- `STS_BOT_TARGET_CLASS`
- `STS_BOT_DRY_RUN`
- `STS_BOT_ALLOW_FOREGROUND_FALLBACK`

## Diagnostics

Capability report:

```powershell
python -m sts_bot.cli capability-report --profile profiles\windows.example.json
```

Background capture smoke:

```powershell
python -m sts_bot.cli bg-capture-smoke --profile profiles\windows.example.json --frames 120 --timeout-ms 250
```

This reports:

- target HWND/title/class/pid
- selected capture/input backends
- background support flags
- sample capture success and blank/non-blank status
- frame count and average capture interval
- whether the foreground title changed
- whether the cursor moved

Background input smoke against a local Win32 test window:

```powershell
python -m sts_bot.cli bg-input-smoke --timeout-ms 6000
```

This verifies message-based click/key delivery without changing the foreground window or moving the cursor.

Inspect the real game window and GUI-thread state:

```powershell
python -m sts_bot.cli inspect-window --profile profiles\windows.example.json
```

Probe one background input against the live game:

```powershell
python -m sts_bot.cli bg-game-input-probe --profile profiles\windows.example.json --key enter
python -m sts_bot.cli bg-game-input-probe --profile profiles\windows.example.json --label "Single Play"
python -m sts_bot.cli bg-game-input-probe --profile profiles\windows.example.json --label "Single Play" --window-message-delivery post
```

These commands report the chosen backend, foreground/cursor stability, message target HWNDs, and whether a meaningful visual scene change was observed.

Run a background input matrix across delivery/activation combinations:

```powershell
python -m sts_bot.cli bg-game-input-matrix --profile profiles\windows.example.json --key enter
python -m sts_bot.cli bg-game-input-matrix --profile profiles\windows.example.json --label "Single Play"
```

This is useful when a scene accepts background capture but the game may still reject `WM_*` input. The command prints a compact table of `delivery`, `activation`, `frame_diff`, and `observed_effect`.

Run an explicit foreground bootstrap for startup scenes:

```powershell
python -m sts_bot.cli startup-sequence-live --profile profiles\windows.example.json --allow-foreground-fallback --focus-window
python -m sts_bot.cli bootstrap-live --profile profiles\windows.example.json --allow-foreground-fallback --focus-window --stop-screen battle
```

These commands are intentionally opt-in. They use the legacy foreground path to get through scenes that currently reject background input, then you can switch back to the default background-capable path.

Run the mixed-mode path end-to-end:

```powershell
python -m sts_bot.cli hybrid-run-live --profile profiles\windows.example.json --allow-foreground-fallback --focus-window --max-steps 120
```

This command uses `startup_sequence_labels` plus `scene_input_backends` from the profile. In the current example profile, startup scenes and battle are `gamepad`-first, while `neow_dialog`, `continue`, map confirm, and `reward_gold_only` confirm use `window_messages`.

Dry-run against the real game:

```powershell
python -m sts_bot.cli game-dry-run --profile profiles\windows.example.json
```

This resolves the target window and selected action, then prints the intended backend/target without sending game input.

## Live Read-Only Flow

Write a starter profile:

```powershell
python -m sts_bot.cli write-example-profile
```

Probe the current live state:

```powershell
python -m sts_bot.cli probe-live --profile profiles\windows.example.json --show-anchor-scores --save-screenshot captures\probe.png
```

Probe a saved screenshot offline:

```powershell
python -m sts_bot.cli probe-image --profile profiles\windows.example.json --input observations\live_session\frames\0016_unknown.png --show-anchor-scores
```

Passively watch a live manual session:

```powershell
python -m sts_bot.cli watch-live --profile profiles\windows.example.json --seconds 120 --interval 1.0 --jsonl-out observations\live.jsonl --capture-dir observations\frames --only-on-change
```

Summarize a saved observation log:

```powershell
python -m sts_bot.cli summarize-observations --input observations\live.jsonl
```

Capture the current game window:

```powershell
python -m sts_bot.cli capture-live --profile profiles\windows.example.json --output captures\screen.png
```

Annotate a screenshot:

```powershell
python -m sts_bot.cli annotate-live --profile profiles\windows.example.json --output captures\annotated.png
```

Crop a template from a screenshot:

```powershell
python -m sts_bot.cli crop-template --input captures\screen.png --rect 1244,756,174,92 --output profiles\templates\battle_anchor.png
```

## Live Input Flow

Run the live adapter:

```powershell
python -m sts_bot.cli run-live --profile profiles\windows.example.json --episodes 1 --max-steps 300
```

One-step action:

```powershell
python -m sts_bot.cli step-live --profile profiles\windows.example.json
```

Raw key/click injection:

```powershell
python -m sts_bot.cli inject-live --profile profiles\windows.example.json --key enter
```

Battle helpers:

```powershell
python -m sts_bot.cli play-card-live --profile profiles\windows.example.json --slot 1
python -m sts_bot.cli play-turn-live --profile profiles\windows.example.json
python -m sts_bot.cli inject-gamepad-live --profile profiles\windows.example.json --buttons dpad_down,a
```

Reward scenes are now split into `reward_menu`, `reward_cards`, and `reward_gold_only`. The current default flow is:

1. Take gold if a menu row exists.
2. Open the card reward panel.
3. On lone-gold follow-up screens, use `dpad_down` to create focus, then `Enter` via `window_messages`.

Decision traces now persist a compact state snapshot plus a human-readable reasoning summary for each logged step. You can inspect the latest run with:

```powershell
python -m sts_bot.cli trace-run --db data\runs.sqlite3 --json-out reports\latest_trace.json
```

This is intended to answer "why did the bot pick this card / path / reward?" without opening SQLite manually.

Legacy foreground backends are only used if you explicitly opt in:

```powershell
python -m sts_bot.cli capability-report --profile profiles\windows.example.json --allow-foreground-fallback
python -m sts_bot.cli inject-live --profile profiles\windows.example.json --backend legacy --allow-foreground-fallback --key enter
```

Known mixed-mode workflow in current testing:

1. Use `startup-sequence-live` with `--allow-foreground-fallback --focus-window` to move through `menu -> mode_select -> character_select -> neow`.
2. Once the game reaches `battle`, switch back to default `window_messages`.
3. Use `play-turn-live --backend window_messages` or `step-live --backend window_messages`.
4. Or let `hybrid-run-live` pick those backends automatically from the profile.

## Known Limitations

- Background capture now prefers WGC when available. `visible_region` remains a fallback.
- `WindowMessageInputBackend` can deliver input in the background, but the game may ignore some or all `WM_*` messages.
- Current title-screen testing shows background capture working with `visible_region`, while message-based `Enter` and `Single Play` probes still produce `observed_effect=false`.
- Current title-screen matrix testing also shows `send/post x none/key/click/all` all remaining below the visual-change threshold.
- In contrast, current battle-scene testing shows background `window_messages` accepted: `bg-game-input-matrix --key 1` and `--key e` both produced `observed_effect=true`, and `play-turn-live --backend window_messages` executed combat actions.
- Reward handling is now explicitly split into `reward_menu`, `reward_cards`, and `reward_gold_only`; saved-frame probes confirm those classifications.
- Battle is controller-first now, but some live `play-turn-live` runs can still hang longer than intended in complex combat states.
- `wgc` now has a concrete implementation path when `windows-capture` is installed, but fully occluded / minimized behavior is not yet fully characterized in this repo.
- `Win32WindowCaptureBackend` is background-capable for visible windows in current testing. Minimized/fully hidden behavior is not guaranteed here.
- `VisibleRegionCaptureBackend` works only while the target client region stays visible and unobscured.
- In current testing, `auto` often selects `visible_region` on title/menu scenes because `PrintWindow` can return black frames there.
- OCR quality for HUD values is still incomplete.
- Some screen types remain calibrated with one-off anchors.
- `game_dry_run` can still report a blank-frame failure if the capture backend cannot retrieve a valid client image at that moment.

## Unsupported / Fallback Rules

The runtime returns unsupported instead of silently degrading when:

- `capture_backend=wgc` but no WGC helper is available
- `capture_backend=dxgi` but no DXGI helper is available
- `capture_backend=legacy` while `allow_foreground_fallback=false`
- `input_backend_name=legacy` while `allow_foreground_fallback=false`
- an unknown backend name is requested

Legacy foreground fallback is available only when explicitly enabled. It is not the default path.

## Calibration Model

Template image guidance is in `profiles/templates/README.md`.
