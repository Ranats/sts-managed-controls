# Listing Copy

## Short Description

Local control panel and runtime toolkit for Slay the Spire 2. Probe live state, set gold/block/energy, grant powers, add upgraded cards, and test mod interactions through a bridge-based in-process workflow.

## GitHub Description

Single-player Slay the Spire 2 runtime toolkit for live state probing, bridge-based power/card/relic operations, and local modding/debug workflows.

## Nexus Summary

`STS Managed Controls` is a local desktop control panel for Slay the Spire 2 that lets you inspect live managed state and trigger selected in-process actions through a lightweight bridge mod.

It is aimed at:

- mod authors
- testers
- single-player sandbox users

Current capabilities include:

- live managed probe
- gold, block, and energy control
- power application through the bridge mod
- searchable card / power / relic catalogs
- adding upgraded cards to the current hand
- experimental deck, relic, map-jump, and tuning operations

## First Paragraph

This project started as a local Slay the Spire 2 automation lab and now includes `STS Managed Controls`, a desktop UI for probing live game state and applying selected runtime changes in a single-player environment. The focus is debugging, sandboxing, and mod-development workflows rather than competitive or multiplayer use.

## Feature Bullets

- Probe floor, ascension, HP, block, gold, and energy without OCR.
- Search live-discovered cards, powers, and relics from `sts2.dll`.
- Apply powers through an in-process bridge path.
- Add upgraded cards to the current hand.
- Set gold directly from the desktop UI or CLI.
- Configure combat-start auto powers for fast testing loops.
- Maintain block or energy automatically for testing scenarios.
- Run from a local GUI instead of repeatedly typing commands.

## Experimental Feature Bullets

- Add cards to the master deck.
- Replace the entire master deck.
- Obtain relics through the bridge mod.
- Jump to a specific map coordinate.
- Tune selected card or relic dynamic vars.

## Support Copy

The public build can ship with a 30-minute managed-controls trial and a local unlock key for unlimited use. Optional support links can still help fund compatibility updates, maintenance, and UI improvements. Be explicit that the current unlock flow is a soft local gate rather than hardened DRM.

## Media Notes

Suggested media order on GitHub or Nexus:

- lead screenshot: `docs/release/gui.png`
- follow-up video: `docs/release/Slay the Spire 2 - 2026-04-05 18-24-09.mp4`
- secondary video: `docs/release/Slay the Spire 2 - 2026-04-03 21-21-07.mp4`

## Safety Note

Use this on local single-player runs. Back up saves if you care about them. Modded save files and experimental bridge actions can break normal progression or require cleanup after testing.
