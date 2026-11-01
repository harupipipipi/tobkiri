"""Authenticate the preserved custom component and materialize an inactive Pack."""
from __future__ import annotations

import argparse
import json
import lzma
from pathlib import Path

from materialize_ordered_tool_bridge import materialize, no_links, require_hash
from stage_ordered_tool_bridge import PACK, ROOT, bounded_read

FIXTURE = ROOT / "crates/tobkiri-e2e/fixtures/ordered-python"
COMPRESSED_SHA = "c34ba42e584ab8b6e2fdbfc94e102cef385bc4edc13083cd0d1fe54f7823ce04"
COMPRESSED_BYTES = 3952452
ARTIFACT_SHA = "9beee26b1ad8229c04571eca1f22d47f9d23e1a8475324919f8c1fc4c7ea1e94"
ARTIFACT_BYTES = 17921530
RECEIPT_SHA = "496d94b8a3ed9b53e97a58c8f58a17f42ea3d5d1837bb1466b039a82ae6d867a"


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
