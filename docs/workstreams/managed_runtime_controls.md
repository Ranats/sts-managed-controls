# Managed Runtime Controls

## Purpose

- This document is for manual local experimentation against a running `SlayTheSpire2.exe`.
- The supported entrypoint is:
  - `python -m sts_bot.cli ...`
- Prefer these commands over calling the temporary CLRMD helper directly.
- For true power creation or other authoritative effects, prefer the bridge mod path over raw `WriteProcessMemory`.

## Prerequisites

- The game is running.
- A valid live profile exists, for example:
  - `profiles\windows.example.json`
- `dotnet` is installed.
- The temporary CLRMD helper dependencies under `tmp\nuget` are present.

## Safe Starting Point

```powershell
python -m sts_bot.cli probe-managed --profile profiles\windows.example.json
```

Optional JSON export:

```powershell
python -m sts_bot.cli probe-managed --profile profiles\windows.example.json --json-out tmp\managed_probe.json
```

## Local UI

There is now a local control panel:

```powershell
python -m sts_bot.cli managed-control-ui --profile profiles\windows.example.json
```

The UI exposes:

- probe
- set gold
- set / maintain block
- set / maintain energy
- alias powers
- set power amount on an existing power
- install bridge mod
- apply power through the bridge mod
- searchable card / power / relic catalogs sourced from `sts2.dll`
- add upgraded cards to the master deck through the bridge mod
- add upgraded cards to the current hand through the bridge mod
- replace the entire master deck through the bridge mod
- obtain a relic through the bridge mod
- set or clear combat-start auto power rules
- jump to a map coordinate
- tune selected card or relic dynamic vars
- raw dev console command execution
- presets for `help`, `help power`, `help block`, `help energy`
- local license status and unlock-key activation

Notes:

- The UI is a separate local window, not an in-game overlay.
- UI actions also print log lines back to the launching console.
- `Apply Power (Bridge)` requires one game restart after `Install Bridge Mod`.
- `Set Existing Power` edits `_amount` only when that power already exists on the target.
- If a power is missing, the UI now falls back to `Apply Power (Bridge)` instead of only failing.
- Card / power / relic images are not wired yet. The current catalog is searchable text only.
- The UI itself can still open after the trial expires, but gated actions will ask for activation.

## Trial And Unlock

Managed-control features are now behind a soft local gate:

- first use starts a 30-minute local trial
- after expiry, managed writes and bridge commands are blocked
- activate unlimited mode with:

```powershell
python -m sts_bot.cli managed-controls-license-status
python -m sts_bot.cli activate-managed-controls --license-key <KEY>
```

Notes:

- local state is stored under `.managed_controls\license_state.json`
- this is a soft local paywall, not hardened DRM
- replace the validator before treating it as a serious commercial license system

## Bridge Mod Path

This is the current route toward true in-process power creation.

### `install-bridge-mod`

Builds a lightweight local mod and installs it into the game's `mods\CodexBridge` folder.

```powershell
python -m sts_bot.cli install-bridge-mod
python -m sts_bot.cli install-bridge-mod --game-dir "C:\Path\To\Slay the Spire 2"
```

After installing:

1. Restart the game once.
2. Let the game load the mod.
3. Use the UI or `bridge-apply-power` to request real in-process power application.

### `list-game-catalog`

Lists cards, powers, or relics discovered from `sts2.dll`.

```powershell
python -m sts_bot.cli list-game-catalog --kind cards --query whirlwind
python -m sts_bot.cli list-game-catalog --kind powers --query plating
python -m sts_bot.cli list-game-catalog --kind relics --query anchor
```

### `bridge-apply-power`

Sends a JSON request over a named pipe to the installed bridge mod, which then calls the game's own `PowerCmd.Apply(...)`.

```powershell
python -m sts_bot.cli bridge-apply-power --power-type StrengthPower --value 100 --target player
python -m sts_bot.cli bridge-apply-power --power-type PlatingPower --value 999 --target player
python -m sts_bot.cli bridge-apply-power --power-type StrengthPower --value 50 --target enemy --enemy-index 0
python -m sts_bot.cli bridge-apply-power --power-type BarricadePower --value 1 --target player
```

Supported power types right now:

- any constructible type under `MegaCrit.Sts2.Core.Models.Powers`
- use `list-game-catalog --kind powers` or the UI catalog to discover names

### `bridge-add-card`

Creates a fresh card in-process and adds it to either the player's master deck or current hand.

```powershell
python -m sts_bot.cli bridge-add-card --card-type Whirlwind --destination deck
python -m sts_bot.cli bridge-add-card --card-type Whirlwind --destination hand --count 2
python -m sts_bot.cli bridge-add-card --card-type Whirlwind --destination hand --count 1 --upgrade-count 2
python -m sts_bot.cli bridge-add-card --card-type Bash --destination hand
python -m sts_bot.cli bridge-add-card --card-type MegaCrit.Sts2.Core.Models.Cards.Whirlwind --destination deck
```

Supported card types right now:

- any discovered card model under `MegaCrit.Sts2.Core.Models.Cards`
- short names such as `Whirlwind`, `Bash`, `AdaptiveStrike`
- full names such as `MegaCrit.Sts2.Core.Models.Cards.Whirlwind`

### `bridge-replace-master-deck`

Removes the current master deck and replaces it with the requested card type. If `--count` is omitted, it reuses the current deck size.

```powershell
python -m sts_bot.cli bridge-replace-master-deck --card-type Whirlwind
python -m sts_bot.cli bridge-replace-master-deck --card-type Whirlwind --count 10
python -m sts_bot.cli bridge-replace-master-deck --card-type Whirlwind --count 10 --upgrade-count 2
python -m sts_bot.cli bridge-replace-master-deck --card-type Bash --count 10
```

### `bridge-obtain-relic`

Obtains a relic in-process through the bridge mod. This is currently routed as a run-state action, so use it outside combat.

```powershell
python -m sts_bot.cli bridge-obtain-relic --relic-type Anchor
python -m sts_bot.cli bridge-obtain-relic --relic-type Akabeko --count 2
```

Supported relic types right now:

- any constructible type under `MegaCrit.Sts2.Core.Models.Relics`
- use `list-game-catalog --kind relics` or the UI catalog to discover names

Notes:

- The bridge mod must already be installed and loaded by the game.
- This route is intended to create a fresh power in-process instead of editing an existing `_amount`.
- Hand add has been live-verified.
- Deck add, deck replace, and relic obtain still need more live validation across screen transitions.

### `bridge-set-auto-power`

Stores a rule that reapplies a power at combat start.

```powershell
python -m sts_bot.cli bridge-set-auto-power --power-type StrengthPower --value 100 --target player
python -m sts_bot.cli bridge-clear-auto-power --power-type StrengthPower --target player
```

### `bridge-jump-map`

Jumps directly to a specific map coordinate.

```powershell
python -m sts_bot.cli bridge-jump-map --col 2 --row 8
```

### `bridge-tune-card` and `bridge-tune-relic`

Adjusts selected dynamic vars on cards or owned relics.

```powershell
python -m sts_bot.cli bridge-tune-card --card-type Whirlwind --var-name Damage --value 99 --scope hand
python -m sts_bot.cli bridge-tune-relic --relic-type FestivePopper --var-name Damage --value 99
```

## Dev Console Path

### `enable-dev-console`

Sets `"full_console": true` in every discovered `settings.save` under the local Slay the Spire 2 app-data directory.

```powershell
python -m sts_bot.cli enable-dev-console
python -m sts_bot.cli enable-dev-console --settings-root "C:\Path\To\settings.save"
```

Arguments:

- `--settings-root`
  - Optional.
  - Root directory to search, or an explicit `settings.save` file.
- `--json-out`
  - Optional.

### `run-console-command`

Focuses the game, opens the dev console with backtick, types a command, and submits it.

```powershell
python -m sts_bot.cli run-console-command --profile profiles\windows.example.json --command-text "help"
python -m sts_bot.cli run-console-command --profile profiles\windows.example.json --command-text "help power" --leave-open
python -m sts_bot.cli run-console-command --profile profiles\windows.example.json --command-text "power ..." --leave-open
```

Arguments:

- `--profile`
  - Required.
- `--command-text`
  - Required.
  - The exact text to type into the game's dev console.
- `--backend`
  - Optional.
  - Default: `sendinput_scan`
- `--open-key`
  - Optional.
  - Default: `backtick`
- `--typing-interval`
  - Optional float seconds.
  - Default: `0.01`
- `--leave-open`
  - Optional.
  - Leaves the console open after submit.
- `--skip-enable-full-console`
  - Optional.
  - Skip automatic `settings.save` modification.
- `--settings-root`
  - Optional.
- `--json-out`
  - Optional.

Behavior:

- Uses the game's own in-process command execution instead of raw memory writes.
- In the current local build, the dev console is useful for diagnostics, but it does not expose a `power` command.
- Command syntax is game-version dependent, so start with:
  - `help`
  - `help power`
  - `help block`
  - `help energy`

## Managed Probe and Write Commands

### `probe-managed`

Reads current managed state without OCR.

```powershell
python -m sts_bot.cli probe-managed --profile profiles\windows.example.json
python -m sts_bot.cli probe-managed --profile profiles\windows.example.json --focus
python -m sts_bot.cli probe-managed --profile profiles\windows.example.json --json-out tmp\managed_probe.json
```

Current output includes:

- top-level numeric state
  - `floor`
  - `ascension`
  - `gold`
  - `hp`
  - `max_hp`
  - `block`
  - `energy`
  - `max_energy`
- managed object addresses
- player powers
- enemy list including powers

### `set-managed-block`

Writes the current player `Creature._block` value once.

```powershell
python -m sts_bot.cli set-managed-block --profile profiles\windows.example.json --value 25
python -m sts_bot.cli set-managed-block --profile profiles\windows.example.json --value 100 --json-out tmp\managed_block_write.json
```

### `set-managed-gold`

Writes the player's current gold once. This uses the managed probe path and updates both `Player._gold` and the current top-bar mirror when available.

```powershell
python -m sts_bot.cli set-managed-gold --profile profiles\windows.example.json --value 999
python -m sts_bot.cli set-managed-gold --profile profiles\windows.example.json --value 999 --json-out tmp\managed_gold_write.json
```

### `maintain-managed-block`

Periodically rewrites player block to keep it from falling off.

```powershell
python -m sts_bot.cli maintain-managed-block --profile profiles\windows.example.json --value 100
python -m sts_bot.cli maintain-managed-block --profile profiles\windows.example.json --value 100 --seconds 12 --interval 0.2
python -m sts_bot.cli maintain-managed-block --profile profiles\windows.example.json --value 100 --iterations 20 --interval 0.1 --json-out tmp\managed_block_maintain.json
```

### `set-managed-energy`

Writes the player's current energy and, optionally, max energy.

```powershell
python -m sts_bot.cli set-managed-energy --profile profiles\windows.example.json --value 10
python -m sts_bot.cli set-managed-energy --profile profiles\windows.example.json --value 100 --max-value 100
python -m sts_bot.cli set-managed-energy --profile profiles\windows.example.json --value 20 --max-value 20 --json-out tmp\managed_energy_write.json
```

Useful for `X` cost cards such as `Whirlwind`.

### `maintain-managed-energy`

Periodically rewrites current energy and optional max energy.

```powershell
python -m sts_bot.cli maintain-managed-energy --profile profiles\windows.example.json --value 100 --max-value 100 --seconds 10 --interval 0.15
python -m sts_bot.cli maintain-managed-energy --profile profiles\windows.example.json --value 100 --max-value 100 --iterations 20 --interval 0.1 --json-out tmp\managed_energy_maintain.json
```

This is the practical route for `X` cost cards when one-shot writes are quickly reverted.

### `set-managed-power`

Writes `_amount` on an already existing managed power object.

```powershell
python -m sts_bot.cli set-managed-power --profile profiles\windows.example.json --target enemy --power-type VulnerablePower --value 4
python -m sts_bot.cli set-managed-power --profile profiles\windows.example.json --target player --power-type StrengthPower --value 100
```

Current `power-type` values are whatever `probe-managed` currently prints for the selected target.

### `alias-managed-powers`

Points one target's `_powers` list at another target's existing power list.

```powershell
python -m sts_bot.cli alias-managed-powers --profile profiles\windows.example.json --source enemy --dest player
```

This is still experimental and not authoritative.

## Current Limits

- `set-managed-power` does not create a new managed power object.
- It only changes `_amount` on an already existing power object.
- `alias-managed-powers` is a hack for experimentation only.
- Shared lists mean both sides can change together.
- True new power creation should use the game's dev console path first.
- A dedicated mod/bootstrap path is still a follow-up if console commands become insufficient.
