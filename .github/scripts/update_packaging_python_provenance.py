#!/usr/bin/env python3
"""Generate or check reviewed macOS Python provenance from python.org APIs."""

from __future__ import annotations

import argparse
import hashlib
import json
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any


SCHEMA = "tobkiri.packaging-python-macos.v1"
PSF_TEAM_ID = "BMM5U3QVKW"
PSF_INSTALLER_SIGNER = "Developer ID Installer: Python Software Foundation (BMM5U3QVKW)"
PSF_CODE_IDENTIFIER = "org.python.python"
API_ROOT = "https://www.python.org/api/v2/downloads"
INSTALL_ROOT = "/Library/Frameworks/Python.framework/Versions"


def _read_json(url: str) -> Any:
    request = urllib.request.Request(
        url, headers={"User-Agent": "tobkiri-provenance/1"}
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        if response.geturl().split("?", 1)[0] != url.split("?", 1)[0]:
            raise ValueError("python.org metadata request was redirected")
        return json.load(response)


def generate(version: str, requirements: Path) -> dict[str, str]:
    """Generate deterministic provenance from one python.org release record."""
    if not version or any(
        part == "" or not part.isdigit() for part in version.split(".")
    ):
        raise ValueError("version must contain only numeric dot-separated components")
    if requirements.is_absolute() or any(part == ".." for part in requirements.parts):
        raise ValueError("requirements must be a repository-relative path")
    slug = f"python-{version.replace('.', '')}"
    release_query = urllib.parse.urlencode({"slug": slug})
    releases = _read_json(f"{API_ROOT}/release/?{release_query}")
    if not isinstance(releases, list) or len(releases) != 1:
        raise ValueError("python.org returned an ambiguous release")
    resource = releases[0].get("resource_uri", "")
    release_id = resource.rstrip("/").rsplit("/", 1)[-1]
    if not release_id.isdigit():
        raise ValueError("python.org release ID is invalid")
    files_query = urllib.parse.urlencode({"release": release_id})
    files = _read_json(f"{API_ROOT}/release_file/?{files_query}")
    expected_url = (
        f"https://www.python.org/ftp/python/{version}/python-{version}-macos11.pkg"
    )
    installers = [
        item
        for item in files
        if isinstance(item, dict) and item.get("url") == expected_url
    ]
    if len(installers) != 1:
        raise ValueError("python.org returned an ambiguous macOS installer")
    installer_sha256 = installers[0].get("sha256_sum")
    if not isinstance(installer_sha256, str) or len(installer_sha256) != 64:
        raise ValueError("python.org installer SHA-256 is unavailable")
    requirements_bytes = requirements.read_bytes()
    series = ".".join(version.split(".")[:2])
    return {
        "code_identifier": PSF_CODE_IDENTIFIER,
        "executable": f"tobkiri-packaging-venv/bin/python{series}",
        "install_root": f"{INSTALL_ROOT}/{series}",
        "installer_sha256": installer_sha256,
        "installer_signer": PSF_INSTALLER_SIGNER,
        "installer_team_id": PSF_TEAM_ID,
        "installer_url": expected_url,
        "release_page": f"https://www.python.org/downloads/release/{slug}/",
        "requirements_path": requirements.as_posix(),
        "requirements_sha256": hashlib.sha256(requirements_bytes).hexdigest(),
        "schema": SCHEMA,
        "version": version,
    }


def encoded(payload: dict[str, str]) -> bytes:
    """Return stable reviewable provenance bytes."""
    return (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--version", required=True)
    parser.add_argument("--requirements", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    payload = encoded(generate(args.version, args.requirements))
    if args.check:
        if args.output.read_bytes() != payload:
            parser.error("checked-in provenance differs from python.org metadata")
    else:
        args.output.write_bytes(payload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
