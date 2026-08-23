# Component import identity compatibility

The canonical Python package for Defaults Profile component discovery is
`ecosystem.defaultspack.domain.components`. Runtime code must use that package
or package-relative imports. `domain.components` is a compatibility name only.

Both names are intentionally bound to the same package and submodule objects in
`sys.modules`. The compatibility path must never execute the source files a
second time: registry singletons, validation classes, caches, and registrations
must retain one identity regardless of import order. A fresh-interpreter test
covers both orders, and an AST guard rejects new runtime imports of the legacy
name.

Component entrypoint selection is separate from Python import compatibility.
The component registry resolves a selected manifest's canonical category and
component ID, and `resolve_component_entrypoint` resolves only the named
`entrypoints` contract inside that component directory. It does not infer a
module from filesystem layout, scan for alternatives, import executable code,
or provide host/PackVM fallback execution. Invalid, missing, escaping, or
symlinked entrypoints fail closed with path-free diagnostics containing the
resolver module, component, contract, manifest revision, and stable reason.

## Compatibility lifecycle

- Owner: Defaults Profile runtime maintainers.
- Supported caller: managed Defaults Profile bundles created before this
  canonical import contract shipped.
- Removal wave: the Pack ABI v5 migration wave, after telemetry and repository
  scans show no supported bundle still imports `domain.components`.
- Deadline: before Pack ABI v5 general availability. Removal must be a dedicated
  compatibility migration; it must not be folded into unrelated Pack v4 work.

Until that removal gate is met, compatibility is source-only. It does not add a
second component authority, implicit lookup, fallback provider, or execution
path.
