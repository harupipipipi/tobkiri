> **旧仕様・移行用の参照資料**: この文書は Python runtime / Capability Graph / StartupProfile を前提にした旧アーキテクチャの記録です。現行の正の要件は [issue #1520](https://github.com/harupipipipi/tobkiri/issues/1520) です。新しい実装には [Rust 移行契約](../../docs/migration/1520/rust-contract.md) と [資源境界の修正](../../docs/migration/1520/resource-boundaries.md) を参照してください。この表示は移行の完了を意味しません。

# Port Standards

Port standards are string identifiers used to decide whether two ports can connect.

They are intentionally generic. Core compares strings and computes intersections. Ecosystem packs own domain meaning.

## Compatibility Rule

Phase 1 compatibility:

```text
source.direction == "output"
target.direction == "input"
source.standards intersect target.standards is not empty
```

## Examples

```text
rumi.flow.start
rumi.ai.client
rumi.ai.provider
rumi.tool.bundle
rumi.agent.runtime
rumi.memory.store
rumi.prompt.bundle
rumi.ui.surface
rumi.cli.surface
pack.github.repository.v1
company.internal.docs.v1
```

## Namespace Guidance

```text
rumi.*       reserved for rumiai standard names
<pack_id>.* pack-owned standards
company.*   organization-owned standards
org.*       organization-owned standards
```

Core must not treat a namespace as a permission boundary. Namespaces are compatibility labels only.

## Multiple Standards

A port may declare multiple standards.

```json
{
  "id": "tools",
  "direction": "input",
  "standards": [
    "rumi.tool.bundle",
    "defaultspack.tool.bundle.v1",
    "openai.function_tools.compat"
  ]
}
```

This allows one port to accept several compatible capability shapes without introducing domain-specific logic into core.

## Legacy Contract

`contract` is legacy input compatibility only.

```json
{
  "id": "tools",
  "direction": "input",
  "contract": "rumi.tool.bundle"
}
```

Loaders normalize it to:

```json
{
  "id": "tools",
  "direction": "input",
  "standards": ["rumi.tool.bundle"]
}
```

New files should use `standards`.

## Multiple And Required

Input port validation:

- `multiple: false` allows at most one incoming edge
- `multiple: true` allows multiple incoming edges
- `required: true` requires at least one incoming edge

Output-side `multiple` is not strictly enforced in Phase 1.

## Adapters

Adapters are deferred until after Phase 1. Initial validation uses exact standard intersection only.

Reserved future shape:

```json
{
  "from": "rumi.cli.surface",
  "to": "rumi.ui.surface",
  "adapter": "defaultspack.frontend.adapt_cli_surface"
}
```
