"""Assemble the local Registry Wasm Packs with the standard Pack authoring CLI."""

from __future__ import annotations

import argparse
import hashlib
from pathlib import Path
import shutil
import subprocess
import tempfile

from build import REPO, ROOT, sha256, write_json


# These names are source directories and the exact local Pack identities.
ORIGINAL_PACKS = ("provider-registry", "model-registry", "saved-conversation")
SETTLEMENT_PACK = "saved-turn-runtime"
ORCHESTRATION_PACK = "saved-orchestration"
PROVIDER_ADAPTER_PACK = "provider-adapter"
AI_ROUTING_PACK = "ai-routing"
SETTLEMENT_PACKS = (*ORIGINAL_PACKS, SETTLEMENT_PACK)
ORCHESTRATION_PACKS = (*SETTLEMENT_PACKS, ORCHESTRATION_PACK)
PROVIDER_ADAPTER_PACKS = (*ORCHESTRATION_PACKS, PROVIDER_ADAPTER_PACK)
PACKS = (*PROVIDER_ADAPTER_PACKS, AI_ROUTING_PACK)
PINNED_VERSIONS = {
    ORCHESTRATION_PACK: "0.1.0",
    PROVIDER_ADAPTER_PACK: "0.3.0",
    AI_ROUTING_PACK: "0.1.0",
}
CORE_WIT = REPO / "crates/tobkiri-wit/wit/core-v1-2/core.wit"


def stage_pack(
    tobkiri: Path, name: str, components: Path, work: Path, output: Path
) -> dict[str, str | int]:
    """Build one archive and return its receipt record."""
    pack_id = f"local.{name}"
    source = ROOT / f"{pack_id}-rust"
    stage = work / pack_id
    dependencies = stage / "wit/deps"
    dependencies.mkdir(parents=True)
    component_name = name.replace("-", "_")
    component = components / f"tobkiri_pack_{component_name}_rust.wasm"
    shutil.copyfile(component, stage / "component.wasm")
    shutil.copyfile(CORE_WIT, dependencies / "core.wit")
    shutil.copyfile(source / "wit/pack.wit", stage / "wit/pack.wit")
    for sidecar in sorted((source / "wit").glob("*.shape.json")):
        shutil.copyfile(sidecar, stage / "wit" / sidecar.name)
    if name == "model-registry":
        shutil.copyfile(
            ROOT / "local.provider-registry-rust/wit/pack.wit",
            dependencies / "provider-registry.wit",
        )

    manifest = stage / "pack.json"
    artifact = output / f"{pack_id}.tpk"
    subprocess.run(
        [
            tobkiri, "pack", "manifest", stage, "--template", source / "pack.draft.json",
            "--output", manifest,
        ],
        check=True,
    )
    subprocess.run([tobkiri, "pack", "build", stage, "--output", artifact], check=True)
    verified = subprocess.check_output([tobkiri, "pack", "verify", artifact], text=True)
    verified_id, version, digest = verified.split()
    if verified_id != pack_id or digest != sha256(manifest.read_bytes()):
        raise ValueError(f"verified Pack differs from the staged manifest for {pack_id}")
    if name in PINNED_VERSIONS and version != PINNED_VERSIONS[name]:
        raise ValueError(f"verified Pack version is invalid for {pack_id}")
    archive_digest = hashlib.sha256()
    with artifact.open("rb") as reader:
        while block := reader.read(64 * 1024):
            archive_digest.update(block)
    return {
        "id": pack_id,
        "version": version,
        "archive": artifact.name,
        "archive_bytes": artifact.stat().st_size,
        "archive_sha256": f"sha256:{archive_digest.hexdigest()}",
        "manifest_sha256": digest,
    }


def main() -> None:
    """Stage the selected archives and publish their receipt atomically."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tobkiri", required=True, type=Path)
    parser.add_argument("--components", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--pack", action="append", choices=PACKS)
    args = parser.parse_args()
    selected = set(args.pack or ORIGINAL_PACKS)
    receipt_version = 1
    if AI_ROUTING_PACK in selected:
        if selected != set(PACKS):
            parser.error("the ai-routing Pack requires the full Registry Pack set")
        receipt_version = 5
    elif PROVIDER_ADAPTER_PACK in selected:
        if selected != set(PROVIDER_ADAPTER_PACKS):
            parser.error("the provider-adapter Pack requires the full Registry Pack set")
        receipt_version = 4
    elif ORCHESTRATION_PACK in selected:
        if selected != set(ORCHESTRATION_PACKS):
            parser.error("the orchestration Pack requires the full Registry Pack set")
        receipt_version = 3
    elif SETTLEMENT_PACK in selected:
        if selected != set(SETTLEMENT_PACKS):
            parser.error("the settlement Pack requires the full Registry Pack set")
        receipt_version = 2
    output = args.output.absolute()
    if output.exists() or output.is_symlink():
        parser.error("output must be a new directory")

    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=output.parent, prefix=".registry-packs-") as scratch:
        work = Path(scratch)
        archives = work / "archives"
        archives.mkdir()
        records = [
            stage_pack(args.tobkiri.resolve(), name, args.components, work, archives)
            for name in PACKS
            if name in selected
        ]
        write_json(
            archives / "receipt.json",
            {"format": f"tobkiri.registry-packs/{receipt_version}", "packs": records},
        )
        archives.rename(output)


if __name__ == "__main__":
    main()
