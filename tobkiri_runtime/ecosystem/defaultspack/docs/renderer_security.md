# Renderer security and recovery

Defaultspack treats shell renderer metadata as a request, not as executable
authority. A manifest cannot make code trusted by declaring `trust: "local"`.

## Renderer classes

- **Built-in renderer**: a build-owned JavaScript artifact under the immutable
  `/static/renderers/` or `/static/assets/renderers/` paths. The backend must
  attach a verified built-in provenance decision, content hash, and build ID.
- **Approved extension UI**: declarative fields, panels, widgets, and bindings
  interpreted by built-in components. Approval does not grant application-origin
  JavaScript execution.
- **Untrusted content**: third-party HTML, SVG, URLs, and arbitrary modules. It
  must stay behind the artifact/placement isolation boundary and cannot become a
  shell renderer.

Writable user renderer directories, external origins, URL query/hash variants,
encoded paths, and self-declared provenance are never imported into the
application origin.

## Failure recovery

Each verified built-in renderer runs behind its own Suspense and error boundary.
An import, export, or render failure:

1. quarantines that module for the current browser session;
2. restores the known built-in renderer for only the affected region;
3. shows Retry, keep-disabled, and safe-mode actions; and
4. reports only bounded renderer ID, module filename, provenance source, build
   ID, and failure category. Raw exceptions, stack traces, URLs, credentials,
   user content, and the full content hash are not included.

Use `?safe_mode=1` when extension presentation is suspected. Safe mode ignores
custom shell layout and renderer selections, restores every standard region,
and resolves all regions to the checked-in built-in components.
