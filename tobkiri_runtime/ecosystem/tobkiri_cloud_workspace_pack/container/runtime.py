"""Nonroot portable container entrypoint; no remote ingress or action execution."""

from __future__ import annotations

import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import sys
from typing import Any
from urllib.request import urlopen

from capsule import MAX_ARCHIVE_BYTES, canonical, import_archive
from recipe import RECIPE_DIGEST


def restore(capsule: Path, workspace: Path) -> dict[str, Any]:
    """Verify input first and publish only regular files into an empty mount."""
    if capsule.is_symlink() or not capsule.is_file():
        raise ValueError("workspace capsule must be a regular input file")
    with capsule.open("rb") as stream:
        data = stream.read(MAX_ARCHIVE_BYTES + 1)
    manifest, blobs = import_archive(data)
    if manifest["recipe_digest"] != RECIPE_DIGEST:
        raise ValueError("workspace capsule recipe differs from this runtime")
    if workspace.is_symlink() or not workspace.is_dir() or any(workspace.iterdir()):
        raise ValueError("container workspace must be an empty nonroot writable mount")
    # An empty mount is a provisioning precondition. Partial extraction is
    # never advertised healthy; the Host must discard a failed mount.
    for entry in manifest["files"]:
        path = workspace / entry["path"]
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with path.open("xb") as stream:
            stream.write(blobs[entry["digest"]])
        path.chmod(0o600)
    (workspace / "capsule-manifest.json").write_bytes(canonical(manifest))
    return manifest


def serve(workspace: Path, manifest: dict[str, Any], listen: str, port: int) -> None:
    """Serve health and checkpoint metadata; external access belongs to deployment."""

    class Handler(BaseHTTPRequestHandler):
        """Expose finite read-only metadata without work contents or credentials."""

        def do_GET(self) -> None:
            """Respond to the fixed health and workspace descriptor routes."""
            if self.path == "/health":
                result = {
                    "status": "ready",
                    "checkpoint_digest": manifest["manifest_digest"],
                }
            elif self.path == "/workspace":
                result = {
                    "version": "tobkiri.workspace-runtime.v1",
                    "workspace_id": manifest["workspace_id"],
                    "checkpoint_digest": manifest["manifest_digest"],
                    "file_count": len(manifest["files"]),
                    "execution": "metadata_service_ready",
                    "workload_execution": "not_implemented",
                    "remote_ingress": "unconfigured",
                }
            else:
                self.send_error(404)
                return
            data = canonical(result)
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, format: str, *args: Any) -> None:
            """Avoid logging arbitrary request data or workspace paths."""

    ThreadingHTTPServer((listen, port), Handler).serve_forever()


def main() -> int:
    """Start a validated nonroot workspace or run its finite health probe."""
    parser = argparse.ArgumentParser(description="Tobkiri local workspace runtime")
    parser.add_argument("--healthcheck", action="store_true")
    parser.add_argument("--capsule", type=Path, default=Path("/input/workspace.zip"))
    parser.add_argument("--workspace", type=Path, default=Path("/workspace"))
    parser.add_argument(
        "--listen", choices=["127.0.0.1", "0.0.0.0"], default="127.0.0.1"
    )
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error("port is invalid")
    if args.healthcheck:
        with urlopen(f"http://127.0.0.1:{args.port}/health", timeout=2) as response:
            return 0 if json.load(response).get("status") == "ready" else 1
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        raise PermissionError("workspace runtime must run as nonroot")
    manifest = restore(args.capsule, args.workspace)
    serve(args.workspace, manifest, args.listen, args.port)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (ValueError, PermissionError, OSError):
        print("Tobkiri workspace runtime could not start", file=sys.stderr)
        sys.exit(1)
