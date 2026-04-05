# Public Release Checklist

## Positioning

Recommended public positioning:

- `Slay the Spire 2 local modding and debugging toolkit`
- `single-player runtime control panel`
- `state probe + bridge-based sandbox for cards, powers, relics, and energy`

Avoid leading with:

- `cheat`
- `hack`
- `paywalled trainer`

That framing reduces discoverability on GitHub and Nexus, and makes Reddit and Discord distribution harder.

## Distribution

Recommended channel order:

1. GitHub repository
2. GitHub Releases with zip assets
3. Nexus Mods page
4. Discord announcement
5. Reddit post in `r/slaythespire`

Steam Community can be used as a secondary pointer, but it should not be the primary distribution channel.

## Before Publishing

- Create a `.gitignore` before the first public push.
- Add a license file.
- Remove or sanitize local absolute paths from committed docs and examples.
- Keep public demo screenshots in a dedicated `assets/` folder instead of `captures/` or `observations/`.
- Confirm the default example profile does not depend on private local paths that break on another machine.
- Review `docs/release/private_content_audit.md`.
- Create a small release zip that includes:
  - `src/`
  - `profiles/windows.example.json`
  - `docs/workstreams/managed_runtime_controls.md`
  - bridge install instructions
- Add screenshots or a short GIF of:
  - the control UI
  - hand add working
  - power apply working
- Current bundled media:
  - `docs/release/gui.png`
  - `docs/release/Slay the Spire 2 - 2026-04-03 21-21-07.mp4`
  - `docs/release/Slay the Spire 2 - 2026-04-05 18-24-09.mp4`
- Mark unstable features explicitly.

## Release Readme Requirements

- One-sentence summary at the top.
- Who this is for:
  - single-player users
  - mod authors
  - testers
- What currently works.
- What is still experimental.
- Installation steps.
- Quick-start commands.
- Troubleshooting for:
  - bridge pipe unavailable
  - DLL locked during install
  - game restart required after bridge install
- Safety note:
  - use on local single-player runs
  - back up saves if needed
  - modded save files can break vanilla startup in some cases

## Monetization

Low-friction path:

- release the base tool for free
- add optional support links
- treat donations as support for maintenance and compatibility fixes

Reasonable support channels:

- GitHub Sponsors
- Ko-fi
- Patreon, only if updates become regular enough

If you ship the current trial flow, state it plainly:

- managed-control features start with a 30-minute local trial
- unlimited access requires an unlock key
- the current validator is a soft local gate and should be replaced before treating it as strong commercial licensing

## Suggested Public Scope For v0.1

Ship:

- managed control UI
- probe-managed
- block and energy controls
- bridge power apply
- bridge add card to hand
- searchable card/power/relic catalog

Mark experimental:

- add card to deck
- replace master deck
- obtain relic

Defer:

- image-based card browser
- in-game overlay
- broad save editing
- automation presets for complete runs

## Launch Sequence

1. Publish repository.
2. Create a `v0.1.0` GitHub Release with zip assets.
3. Publish Nexus page with screenshots and the same release zip.
4. Post one short announcement with:
  - one screenshot
  - one GIF
  - one paragraph on intended use
  - link to install docs

## Known Public-Facing Risks

- Game updates can break reflection-based bridge calls.
- Some runtime operations still need more live validation outside the current local environment.
- Card, power, and relic catalogs are dynamic, but interaction behavior may still vary by game version.
