# Rumi Remote Mobile TODO

## Done in this PR

- [x] Add a dedicated Flutter app under `tobkiri_mobile/`.
- [x] Add a bearer-auth Kernel Pack API client for defaultspack remote access.
- [x] Add secure storage for server URL, token, and refresh preferences.
- [x] Add health, module list/detail, migration, and pack request views.
- [x] Add module actions for enable, disable, reload, and rollback.
- [x] Add Android LAN HTTP and internet permissions.
- [x] Add iOS local network and HTTP transport permissions.
- [x] Add Flutter CI for formatting, analysis, and unit tests.
- [x] Add unit coverage for API envelope handling, auth headers, URL
      normalization, module parsing, and action routes.
- [x] Add QR-code pairing from the PC viewer so users do not type the URL and
      token manually.
- [x] Add authenticated PC chat transport on the mobile contract.
- [x] Add PC chat controls for model display, model selection, composer **+**
      actions, and PC slash commands discovered from the PC command
      manifest/capabilities response.
- [x] Add local/mobile model selection for on-device chat.

## Next Hardening

- [ ] Add optional HTTPS certificate pinning for reverse-proxy deployments.
- [ ] Add read-only mode that hides mutation buttons unless explicitly enabled.
- [ ] Add push/local notifications for defaultspack migration and pack request
      changes.
- [ ] Add screenshot-based golden tests after Flutter SDK is available locally.
