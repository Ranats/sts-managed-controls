# Release Runbook

This is the shortest path from local repo to public release.

## What Can Be Automated

Already automatable locally:

- prepare a public release bundle
- create a release zip from public-safe paths only
- generate listing copy and release notes source material

Not automatable from this workspace alone:

- pushing to your GitHub account
- creating a GitHub repository under your account
- publishing on Nexus Mods
- posting on Reddit or Discord

Those steps require your account access and final approval.

## One-Pass Local Prep

From the repo root:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\prep_public_release.ps1
```

Optional explicit version:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\prep_public_release.ps1 -Version 0.1.0
```

This creates:

- `dist\sts-managed-controls-<version>\`
- `dist\sts-managed-controls-<version>.zip`

The zip intentionally includes only the public-safe subset of the repo.

## Recommended Publish Order

1. Review `docs/release/private_content_audit.md`
2. Run the release prep script
3. Inspect the generated zip contents
4. Create the GitHub repository or push to an existing one
5. Create a GitHub Release and upload the zip
6. Create a Nexus Mods page and upload the same zip
7. Post announcement links to Discord and Reddit

## GitHub

Use:

- `README.md` for the main project page
- `docs/release/listing_copy.md`
  - `GitHub Description`
  - `Short Description`

What to upload in the first GitHub Release:

- the generated zip
- the bundled UI screenshot: `docs/release/gui.png`
- one or both bundled demo videos:
  - `docs/release/Slay the Spire 2 - 2026-04-03 21-21-07.mp4`
  - `docs/release/Slay the Spire 2 - 2026-04-05 18-24-09.mp4`

If GitHub Release assets feel too heavy, upload the zip there and use the screenshot/video files in:

- the repository README
- the release description
- Nexus screenshots / media

## Nexus Mods

Use:

- `docs/release/listing_copy.md`
  - `Nexus Summary`
  - `First Paragraph`
  - `Feature Bullets`
  - `Experimental Feature Bullets`
  - `Safety Note`

Mark these as experimental on the page:

- add card to deck
- replace master deck
- obtain relic

## Suggested Public Scope

Ship confidently:

- managed control UI
- probe-managed
- bridge power apply
- bridge add card to hand
- block / energy controls
- searchable catalogs

Keep labeled experimental:

- add card to deck
- replace deck
- obtain relic

## Monetization

Recommended launch model:

- 30-minute managed-controls trial
- local unlock-key activation for unlimited use
- optional support links
- be explicit that the current unlock flow is a soft local gate, not hardened DRM

Good places for support links:

- GitHub Sponsors
- Ko-fi

## My Recommendation

Do not try to force full publication automation first.

The best split is:

- I automate packaging and release copy locally
- you do the account-bound publish clicks

That keeps the risky part small while still removing most of the tedious work.
