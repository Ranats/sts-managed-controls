# Private Content Audit

Use this list before publishing the repository or building a public zip.

## Keep Public

- `src/`
- `tests/`
- `profiles/templates/`
- `profiles/windows.example.json`
- `docs/release/`
- `docs/workstreams/managed_runtime_controls.md`
- bridge source under `tmp/codex_bridge_mod/`
- helper source that is still required by runtime features
  - for example `tmp/type_probe/` and `tmp/clrmd_probe/`

## Do Not Publish Raw

- local captures and observation logs
  - `captures/`
  - `observations/`
- local databases and traces
  - `data/*.sqlite3`
  - `reports/`
- crash dumps and large runtime artifacts
  - `tmp/*.dmp`
  - `tmp/*.dll`
  - `tmp/*.exe`
  - `tmp/*.runtimeconfig.json`
  - `tmp/*.deps.json`
- local install and probe outputs
  - `tmp/managed_*`
  - `tmp/bridge_install*.json`
  - `tmp/dev_console_*.json`
- local Python environments and caches
  - `.venv/`
  - `.pytest_cache/`
  - `pytest-cache-files-*`

## Review Before Publishing

- `docs/workstreams/`
  - contains internal prompts, research notes, and local-environment references
- `tmp/`
  - some subfolders are source and should stay public
  - many others are disposable diagnostics and should be excluded from release zips
- screenshots
  - keep only curated public demo assets in a separate folder such as `assets/`

## Known Sensitive Patterns

Search for these before a public push:

- absolute local paths
  - `C:\\Users\\`
  - `%APPDATA%`
  - Steam user IDs such as `765611...`
- local game install paths
  - `C:\\Program Files (x86)\\Steam\\steamapps\\common\\Slay the Spire 2`
- raw crash dumps or save backups
- screenshots that expose private overlays, account identifiers, or unrelated desktop windows

## Release Zip Guidance

Preferred public zip contents:

- package source
- example profile
- template assets
- release docs
- installation instructions

Do not build release zips from the full working tree.
