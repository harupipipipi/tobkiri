"""Authenticate the preserved custom component and materialize an inactive Pack."""
from __future__ import annotations

import argparse
import json
import lzma
from pathlib import Path

from materialize_ordered_tool_bridge import materialize, no_links, require_hash
from stage_ordered_tool_bridge import PACK, ROOT, bounded_read

FIXTURE = ROOT / "crates/tobkiri-e2e/fixtures/ordered-python"
COMPRESSED_SHA = "85b4d52f8ec9f2f6f530dafa4e1dda64b5fe8c63730509e3839560ce9c4f7165"
COMPRESSED_BYTES = 3938612
ARTIFACT_SHA = "3c2aa68e256fa125e2e2a4cb3098704220fdf4832d83c699559ead60617aceac"
ARTIFACT_BYTES = 17899248
RECEIPT_SHA = "480803d6ac72f53c9bc19d56eab187f9185c30ff2a9457e32c1fd9796c11d87d"


def decompress_verified(data: bytes, *, compressed_sha: str,
                        artifact_sha: str, artifact_bytes: int) -> bytes:
    """Accept one pinned XZ stream, with bounded dictionary and output memory."""
    if not 0 < artifact_bytes <= 128 * 1024 * 1024:
        raise ValueError("invalid decompression bound")
    require_hash(data, compressed_sha, "compressed component")
    decoder = lzma.LZMADecompressor(format=lzma.FORMAT_XZ,
                                   memlimit=128 * 1024 * 1024)
    binary = decoder.decompress(data, max_length=artifact_bytes + 1)
    if (len(binary) != artifact_bytes or not decoder.eof
            or decoder.unused_data):
        raise ValueError("component size, stream termination or trailing bytes differ")
    require_hash(binary, artifact_sha, "component")
    return binary


def stage_preserved(destination: Path) -> dict:
    """Stage exact preserved bytes; never execute a guest or approve a Profile.

    As for materialize(), the destination parent is trusted and must not be
    concurrently replaced. A failed fresh directory is retained for diagnosis.
    """
    destination = destination.absolute()
    no_links(destination)
    if destination.exists() or not destination.parent.is_dir():
        raise ValueError("destination must be fresh with an existing trusted parent")
    for name in ("component.wasm.xz", "component.build.json"):
        no_links(FIXTURE / name)
    compressed = bounded_read(FIXTURE / "component.wasm.xz", COMPRESSED_BYTES)
    if len(compressed) != COMPRESSED_BYTES:
        raise ValueError("compressed component size differs")
    receipt = bounded_read(FIXTURE / "component.build.json", 1024 * 1024)
    require_hash(receipt, RECEIPT_SHA, "build receipt")
    binary = decompress_verified(compressed, compressed_sha=COMPRESSED_SHA,
                                 artifact_sha=ARTIFACT_SHA,
                                 artifact_bytes=ARTIFACT_BYTES)
    destination.mkdir()
    artifact = destination / "component.wasm"
    artifact.write_bytes(binary)
    artifact.with_suffix(".build.json").write_bytes(receipt)
    result = materialize(artifact, PACK / "oracle.json", destination / "pack",
                         artifact_sha256=ARTIFACT_SHA, receipt_sha256=RECEIPT_SHA)
    result["compressed_sha256"] = COMPRESSED_SHA
    result["build_mode"] = "hash-bound-custom-python313-producer-trial"
    (destination / "materialization.json").write_text(
        json.dumps(result, sort_keys=True, indent=2) + "\n")
    return result


def main() -> None:
    """Materialize only the exact checked-in custom-producer test artifact."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(stage_preserved(args.output), sort_keys=True))


if __name__ == "__main__":
    main()
