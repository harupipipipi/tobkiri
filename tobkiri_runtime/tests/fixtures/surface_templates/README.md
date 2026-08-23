# Surface Template conformance fixtures

The fixture set intentionally separates the three ownership roles:

- `logic.image.inspect` and `logic.search.choice` expose typed operation
  identities only.
- `surface.image.inspect` and `surface.search.choice` contain unchanged,
  renderer-neutral templates.
- `renderer.defaultspack.react` advertises semantic pattern capabilities only.

The same templates are consumed by the Python `RecordingSurface` and the
defaultspack React adapter. No fixture contains a component name, module path,
endpoint, callback, or local filesystem path.
