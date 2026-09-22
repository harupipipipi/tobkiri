# Interfaces

## Inputs

- Local user-supplied artifacts or records emitted by adjacent owner packs.
- Schema IDs listed in `ecosystem.json`.
- Evidence IDs, review state, and handoff owner labels.

## Outputs

- Draft packets.
- Review checklist packets.
- Handoff packets for owner packs.
- UI contract templates for host surfaces to render.
- Export/share package manifests with version pins and checksums, but no created files, zips, links, uploads, or tokens.

## Optional Integrations

- `rumi_frontend_design_pack`: Owns frontend design generation and UI review workflows for artifact runtime contracts.
- `defaultspack`: Owns artifact persistence, sandbox execution, MCP and API execution, share-link creation, and media/runtime delivery.
- `rumi_default_tools_pack`: Owns browser automation and operator-side inspection tools used around artifact apps.

## Required Secrets

None.

## Does Not Provide

- frontend design generation
- file persistence
- sandbox isolation runtime
- MCP execution
- API execution
- media transforms
- browser automation

## Defaultspack Boundary

This pack may reference defaultspack artifact indexes, chat workspaces, share package shapes, approval prompt fields, and tool or MCP execution names. It must not mutate defaultspack stores, create share links, invoke tools, approve itself, or trust client supplied `approved` flags.
