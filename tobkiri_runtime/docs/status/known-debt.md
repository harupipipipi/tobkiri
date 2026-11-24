# Known Debt

Last updated: 2026-08-10

## P0

- complete exact packaged Launcher UI evidence on the final PR artifact
- run a real isolated PackVM guest operation when host capacity meets the typed
  provisioning plan; never weaken the capacity guard or use Host fallback

## P1

- keep PackVM base-image pins and CVE response policy current through reviewed
  digest updates
- expand installer signing/notarization from the current deterministic ad-hoc
  verification when release credentials and policy are available
- continue narrowing compatibility vocabulary where doing so does not break
  stable storage, audit, or offline migration contracts

## P2

- continue measuring PackVM boot, disk growth, and cleanup bounds on supported hosts
- keep update/apply flows moving toward explicit capability ownership
- trim residual transport/runtime compatibility shims once replacement paths are stable
- continue splitting `AmbientTriggerPanel.tsx` state/effects into smaller ambient hooks after the hand-tracker, routing, storage, and bridge seams are stable
- keep the PR #347 macOS ambient smoke checklist as a manual release gate until it has automated coverage: first mic/camera grant, denial and re-grant, window close/reopen, camera disconnect, finger recording dispatch, and approval gesture audit-failure stop
