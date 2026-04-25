This branch contains a patch export of the local WIP branch `codex/vm-sync-live-bot` because direct `git push` from the Codex runtime was blocked by network restrictions.

Local source commit:
- `f33152443cb47ffb75daf457008cceae76117a18`

Patch files:
- `vm-sync-live-bot.patch.part01`
- `vm-sync-live-bot.patch.part02`
- `vm-sync-live-bot.patch.part03`

On the VM, after cloning this repository and checking out `codex/vm-sync-live-bot`, reconstruct and apply the patch:

```powershell
Get-Content artifacts/vm-sync-live-bot/vm-sync-live-bot.patch.part01,artifacts/vm-sync-live-bot/vm-sync-live-bot.patch.part02,artifacts/vm-sync-live-bot/vm-sync-live-bot.patch.part03 -Raw | Set-Content vm-sync-live-bot.patch

git checkout -b vm-work

git apply --index vm-sync-live-bot.patch

git commit -m "Apply Codex VM sync patch"
```

If the VM has access to the host workspace directly, you can instead import the local bundle file `artifacts/vm-sync-live-bot.bundle` from the host and fetch from it.