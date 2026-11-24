"""Run the PackVM sandbox matrix through a native authenticated adapter.

The adapter socket is intentionally not implemented by this script. It must be
owned by the running native Tobkiri process and must invoke the QA artifact via
the canonical Authority, Broker, admission, materialization, and PackVM path.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import socket
import stat
import sys
from typing import Any, Mapping

from acceptance.packvm_sandbox_acceptance import run_live_acceptance

_MAX_RESPONSE_BYTES = 1024 * 1024


class NativeSocketAcceptancePort:
    """Exchange finite acceptance requests with a native Broker adapter."""

    def __init__(self, path: Path, *, timeout_seconds: float = 300.0) -> None:
        self._path = path.resolve(strict=True)
        metadata = self._path.stat()
        if not stat.S_ISSOCK(metadata.st_mode):
            raise ValueError("PackVM acceptance adapter path is not a socket")
        if metadata.st_uid != os.getuid() or stat.S_IMODE(metadata.st_mode) & 0o077:
            raise PermissionError("PackVM acceptance adapter socket is not private")
        self._timeout_seconds = timeout_seconds

    def run_scenario(self, scenario: str, nonce: str) -> Mapping[str, Any]:
        request = json.dumps(
            {
                "kind": "tobkiri.packvm.sandbox-acceptance.request.v1",
                "scenario": scenario,
                "nonce": nonce,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
        if len(request) > 4096:
            raise ValueError("PackVM acceptance request is too large")
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(self._timeout_seconds)
            client.connect(str(self._path))
            client.sendall(len(request).to_bytes(4, "big") + request)
            size = int.from_bytes(_read_exact(client, 4), "big")
            if size <= 0 or size > _MAX_RESPONSE_BYTES:
                raise ValueError("PackVM acceptance response size is invalid")
            response = json.loads(_read_exact(client, size))
        if not isinstance(response, Mapping):
            raise ValueError("PackVM acceptance response is invalid")
        return response


def _read_exact(client: socket.socket, size: int) -> bytes:
    chunks = bytearray()
    while len(chunks) < size:
        chunk = client.recv(size - len(chunks))
        if not chunk:
            raise ConnectionError("PackVM acceptance adapter closed early")
        chunks.extend(chunk)
    return bytes(chunks)


def main(argv: list[str] | None = None) -> int:
    """Run live acceptance or fail without writing a success report."""

    parser = argparse.ArgumentParser()
    parser.add_argument("--adapter-socket", required=True, type=Path)
    parser.add_argument("--nonce-seed-file", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args(argv)
    seed = args.nonce_seed_file.read_bytes()
    report = run_live_acceptance(
        NativeSocketAcceptancePort(args.adapter_socket),
        nonce_seed=seed,
    )
    output = {
        "kind": "tobkiri.packvm.sandbox-acceptance-report.v1",
        "guest_artifact_identity": report.guest_artifact_identity,
        "attestation_digest": report.attestation_digest,
        "observations": list(report.observations),
        "report_digest": report.report_digest,
    }
    args.output.write_text(
        json.dumps(output, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
