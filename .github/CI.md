# CI tiers

## Baseline checks

Ordinary pull requests run the `Desktop contracts (baseline)` job on
Ubuntu. It exercises contracts without opening a real desktop window or sending
physical input:

- Viewer launch routing through the Tauri bridge and the scoped desktop
  capability path.
- Desktop and browser computer-use delegation through `ComputerSeatService`.
- Approval and dry-run behavior covered by those focused contract files.

Run the same checks locally from the repository root:

```bash
cd tobkiri_runtime
python -m pytest tests/test_defaultspack_desktop_launch_flow.py -v
python -m pytest tests/test_computer_desktop_action_delegation.py tests/test_browser_computer_seat_delegation.py -v

cd ../tobkiri_launcher/frontend
npm ci
node --import tsx --test src/lib/api.test.ts
```

When one of these checks fails, the workflow uploads the pytest and Node test
logs in the `desktop-contract-smoke-*` artifact.

## Full CI checks

Add the `full-ci` label to a ready-for-review pull request to run the expensive
desktop jobs. Pushes to `master` also run these jobs.

- `tobkiri-launcher-macos` builds the launcher frontend and runs the complete Rust
  viewer test suite.
- `mac-computer-driver-smoke` exercises native driver and fallback contracts.
- `mac-computer-use-visuals` compiles the EdgeHaze helper and runs the macOS
  visual/control tests.
- `Desktop Installers` builds the macOS, Windows, and Linux installer matrix.
