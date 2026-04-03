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
- one screenshot of the control UI
- one short GIF showing a live action such as hand add or power apply

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

- free release
- optional support links
- no paywall on the core tool

Good places for support links:

- GitHub Sponsors
- Ko-fi

## My Recommendation

Do not try to force full publication automation first.

The best split is:

- I automate packaging and release copy locally
- you do the account-bound publish clicks

That keeps the risky part small while still removing most of the tedious work.
