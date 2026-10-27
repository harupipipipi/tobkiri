"""Fetch only the four recorded producer inputs; never execute their content.

Run on the remote build worker. The destination must be fresh beneath a trusted
parent. Source staging independently rechecks every archive before extraction.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import tempfile
from typing import BinaryIO
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

from stage_sources import ARCHIVES
from verify_source import MAX_ARCHIVE


def capture(stream: BinaryIO, output: BinaryIO, expected: str) -> int:
    """Copy a bounded stream and require its complete recorded digest."""
    digest = hashlib.sha256()
    count = 0
    while chunk := stream.read(min(65536, MAX_ARCHIVE - count + 1)):
        count += len(chunk)
        if count > MAX_ARCHIVE:
            raise ValueError("source archive exceeds its original size bound")
        digest.update(chunk)
        output.write(chunk)
    if not count or digest.hexdigest() != expected:
        raise ValueError("source archive differs from its recorded digest")
    return count


def fetch(destination: Path) -> dict:
    """Fetch verified archives into a fresh private directory, without unpacking."""
    if not destination.is_absolute() or not destination.parent.is_dir():
        raise ValueError("absolute destination beneath a trusted parent required")
    # mkdir is exclusive, including dangling symlinks. Never remove or replace
    # an existing destination; failed downloads are private diagnostics only.
    destination.mkdir(mode=0o700)
    records = []
    for identity in ARCHIVES.values():
        request = Request(identity["url"], headers={"User-Agent": "Tobkiri-source-replay"})
        with urlopen(request, timeout=60) as response:
            if urlsplit(response.geturl()).scheme != "https":
                raise ValueError("producer source redirect must remain HTTPS")
            with tempfile.TemporaryFile(dir=destination) as temporary:
                size = capture(response, temporary, identity["sha256"])
                temporary.seek(0)
                # Only verified complete bytes receive a public archive name.
                with (destination / identity["filename"]).open("xb") as output:
                    while chunk := temporary.read(65536):
                        output.write(chunk)
        records.append({"filename": identity["filename"], "bytes": size,
                        "sha256": identity["sha256"], "url": identity["url"]})
    receipt = {"format": "tobkiri.python313-source-download/1", "archives": records,
               "upstream_execution": False, "componentization": False,
               "guest_execution": False, "production_enabled": False}
    with (destination / "download.json").open("x", encoding="utf-8") as output:
        json.dump(receipt, output, sort_keys=True, indent=2)
        output.write("\n")
    return receipt


def main() -> None:
    """Acquire only the archive identities already recorded in stage_sources."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--destination", type=Path, required=True)
    args = parser.parse_args()
    receipt = fetch(args.destination)
    print(json.dumps({"verified_archives": len(receipt["archives"]),
                      "upstream_execution": False}))


if __name__ == "__main__":
    main()
