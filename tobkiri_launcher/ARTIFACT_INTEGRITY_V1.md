# Presentation artifact integrity v1

The Shell presentation artifact digest and size use one cross-platform
contract in the release materializer, Python release verifier, Launcher build
script, and Launcher runtime.

- Reject the artifact root, every descendant symlink, and every non-regular
  filesystem entry.
- For a root file, use the empty relative name. For files below a directory,
  use the UTF-8 relative path with `/` separators. Directories contribute no
  hash or size bytes.
- Visit directory children in ascending portable filename order.
- For each file, append `relative-name UTF-8`, one NUL byte, and the exact file
  bytes to a SHA-256 stream. The published digest is `sha256:<lowercase hex>`.
- The published size is the checked sum of exact bytes read from every regular
  file, excluding directory and filesystem-allocation metadata.

The checked-in vectors at
`scripts/tests/fixtures/artifact_integrity_vectors.json` are consumed by both
the Python tests and the Rust integrity tests. In particular, the Linux
AppImage and Windows `.exe` vectors exercise the root-file case, while the
macOS `.app` vector protects the directory traversal case.
