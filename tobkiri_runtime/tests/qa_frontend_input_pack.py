"""Prepare the QA declarative-route Pack for Launcher folder-picker testing.

Run from ``tobkiri_runtime``::

    python -m tests.qa_frontend_input_pack /tmp/qa.frontend.input

The command writes a signed Pack folder and a sibling ``.public.pem`` file.
It never writes the short-lived Ed25519 private key to disk.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shutil

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from core_runtime.pack_signature import (
    build_signed_manifest,
    sign_manifest,
    verify_signed_pack,
)
from tobkiri_host.artifact_compiler import compile_pack_root


PACK_ID = "qa.frontend.input"
PUBLISHER_ID = "publisher.qa.frontend"
VERSION = "0.1.0"
FIXTURE = Path(__file__).resolve().parent / "fixtures" / "qa_frontend_input_pack"
STATIC_SIGNED_PACK = (
    Path(__file__).resolve().parent / "fixtures" / "qa_frontend_input_signed" / PACK_ID
)
STATIC_PUBLIC_KEY = STATIC_SIGNED_PACK.parent / f"{PACK_ID}.public.pem"


def build_signed_qa_pack(pack_root: Path, public_key_path: Path) -> dict:
    """Copy, sign, and verify the committed inert Pack without retaining a key."""

    pack_root = Path(pack_root).resolve()
    public_key_path = Path(public_key_path).resolve()
    source = FIXTURE.resolve(strict=True)
    if pack_root == source or source in pack_root.parents:
        raise ValueError("output Pack must be outside its source fixture")
    if public_key_path == pack_root or pack_root in public_key_path.parents:
        raise ValueError("public key must be outside the signed Pack")
    if pack_root.exists() or public_key_path.exists():
        raise FileExistsError("Pack folder and public key outputs must be new")
    compiled = compile_pack_root(source)
    if compiled.artifact.pack_id != PACK_ID:
        raise ValueError("QA Pack identity changed")

    pack_root.parent.mkdir(parents=True, exist_ok=True)
    public_key_path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(source, pack_root)
    signed_path = pack_root / ".tobkiri" / "signed-pack.json"
    signed_path.parent.mkdir()
    for path in (pack_root, *pack_root.rglob("*")):
        if path.is_dir():
            path.chmod(0o555 if os.name == "nt" else 0o755)
        elif path.is_file():
            path.chmod(0o444 if os.name == "nt" else 0o644)
    private_key = Ed25519PrivateKey.generate()
    manifest = build_signed_manifest(
        pack_root,
        pack_id=PACK_ID,
        version=VERSION,
        publisher_id=PUBLISHER_ID,
        core_compatibility=">=0",
        contract_versions={},
        requested_capabilities=[],
    )
    signed = sign_manifest(manifest, private_key)
    signed_path.write_text(
        json.dumps(signed, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    signed_path.chmod(0o444 if os.name == "nt" else 0o644)
    public_key = private_key.public_key()
    pem = public_key.public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    public_key_path.write_bytes(pem)
    verify_signed_pack(
        pack_root,
        signed,
        public_key,
        expected_publisher_id=PUBLISHER_ID,
        expected_pack_id=PACK_ID,
        expected_version=VERSION,
        expected_contract_versions={},
        expected_capabilities=[],
    )
    return signed


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("pack_root", type=Path, help="new output Pack folder")
    args = parser.parse_args()
    public_key_path = Path(str(args.pack_root) + ".public.pem")
    signed = build_signed_qa_pack(args.pack_root, public_key_path)
    print(f"Signed Pack folder: {args.pack_root.resolve()}")
    print(f"Publisher public key: {public_key_path.resolve()}")
    print(f"Publisher: {signed['publisher_id']}")


if __name__ == "__main__":
    main()
