"""Command-line Pack SDK for scaffold, generation, validation, and signing."""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core_runtime.pack_sdk import (  # noqa: E402
    PackSdkGenerator,
    refresh_scaffold_artifacts,
    scaffold_pack,
    validate_pack_manifest,
)
from scripts.offline_legacy_projection import (  # noqa: E402
    generate_legacy_ecosystem_projection,
)
from core_runtime.pack_templates import (  # noqa: E402
    COMPONENT_KINDS,
    PROFILES,
    scaffold_component,
    validate_template_components,
)
from core_runtime.pack_signature import (  # noqa: E402
    SIGNED_MANIFEST_RELATIVE,
    build_signed_manifest,
    sign_manifest,
    verify_signed_pack,
)

DEFAULT_SCHEMAS = [
    ROOT / "tobkiri_protocol" / "schemas" / "pack_manifest_v4.schema.json",
    ROOT / "schemas" / "global_contract_types.schema.json",
    ROOT
    / "ecosystem"
    / "defaultspack"
    / "schemas"
    / "command-protocol-v1.schema.json",
]


def main(argv: list[str] | None = None) -> int:
    """Run the Tobkiri Pack SDK command."""

    parser = _parser()
    args = parser.parse_args(argv)
    result = args.handler(args)
    if result is not None:
        print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="tobkiri-pack")
    subcommands = parser.add_subparsers(required=True)

    init = subcommands.add_parser("init")
    init.add_argument("target", type=Path)
    init.add_argument("--pack-id", required=True)
    init.add_argument("--display-name", required=True)
    init.add_argument("--profile", choices=PROFILES, default="complete")
    init.add_argument(
        "--intent",
        default="",
        help="Used by --profile auto to choose codex, hermes, or complete.",
    )
    init.set_defaults(handler=_init)

    add = subcommands.add_parser("add")
    add.add_argument("pack_root", type=Path)
    add.add_argument("kind", choices=COMPONENT_KINDS)
    add.add_argument("--id", required=True)
    add.add_argument("--display-name", required=True)
    add.add_argument("--description", required=True)
    add.set_defaults(handler=_add)

    generate = subcommands.add_parser("generate")
    generate.add_argument("output", type=Path)
    generate.add_argument("--check", action="store_true")
    generate.add_argument("--schema", action="append", type=Path)
    generate.set_defaults(handler=_generate)

    validate = subcommands.add_parser("validate")
    validate.add_argument("manifest", type=Path)
    validate.add_argument(
        "--schema",
        type=Path,
        default=ROOT / "tobkiri_protocol" / "schemas" / "pack_manifest_v4.schema.json",
    )
    validate.set_defaults(handler=_validate)

    project_legacy = subcommands.add_parser("project-legacy")
    project_legacy.add_argument("manifest", type=Path)
    project_legacy.add_argument("output", type=Path)
    project_legacy.add_argument("--check", action="store_true")
    project_legacy.set_defaults(handler=_project_legacy)

    sign = subcommands.add_parser("sign")
    sign.add_argument("pack_root", type=Path)
    sign.add_argument("--private-key", type=Path, required=True)
    sign.add_argument("--pack-id", required=True)
    sign.add_argument("--version", required=True)
    sign.add_argument("--publisher-id", required=True)
    sign.add_argument("--core-compatibility", required=True)
    sign.add_argument(
        "--contract-version",
        action="append",
        default=[],
        metavar="CONTRACT=VERSION",
    )
    sign.add_argument("--requested-capability", action="append", default=[])
    sign.add_argument("--build-provenance", type=Path)
    sign.add_argument(
        "--output",
        type=Path,
        help=f"Defaults to PACK_ROOT/{SIGNED_MANIFEST_RELATIVE}",
    )
    sign.set_defaults(handler=_sign)

    verify = subcommands.add_parser("verify")
    verify.add_argument("pack_root", type=Path)
    verify.add_argument("--public-key", type=Path, required=True)
    verify.add_argument("--manifest", type=Path)
    verify.add_argument("--publisher-id")
    verify.add_argument("--revoked-key-id", action="append", default=[])
    verify.set_defaults(handler=_verify)

    inspect = subcommands.add_parser("inspect")
    inspect.add_argument("pack_root", type=Path)
    inspect.add_argument("--manifest", type=Path)
    inspect.set_defaults(handler=_inspect)
    return parser


def _init(args: argparse.Namespace) -> dict[str, object]:
    path = scaffold_pack(
        args.target,
        pack_id=args.pack_id,
        display_name=args.display_name,
        profile=args.profile,
        intent=args.intent,
    )
    validate_template_components(
        args.target,
        ROOT / "ecosystem" / "defaultspack" / "schemas",
    )
    contract_path = args.target / "template.contract.json"
    contract = (
        json.loads(contract_path.read_text(encoding="utf-8"))
        if contract_path.is_file()
        else {}
    )
    return {
        "created": str(path),
        "profile": contract.get("profile", args.profile),
        "authority": "none",
    }


def _add(args: argparse.Namespace) -> dict[str, object]:
    paths = scaffold_component(
        args.pack_root,
        kind=args.kind,
        component_id=args.id,
        display_name=args.display_name,
        description=args.description,
    )
    validate_template_components(
        args.pack_root,
        ROOT / "ecosystem" / "defaultspack" / "schemas",
        component_paths=paths,
    )
    refresh_scaffold_artifacts(args.pack_root)
    return {
        "created": [str(path) for path in paths],
        "kind": args.kind,
        "enabled": False if args.kind == "tool" else True,
    }


def _generate(args: argparse.Namespace) -> dict[str, object]:
    generator = PackSdkGenerator(args.schema or DEFAULT_SCHEMAS)
    digests = generator.generate(args.output, check=args.check)
    if args.output.resolve() == (ROOT / "generated" / "pack_sdk").resolve():
        _sync_client_bindings(args.output, check=args.check)
    return {"output": str(args.output), "digests": digests, "check": args.check}


def _sync_client_bindings(output: Path, *, check: bool) -> None:
    mirrors = {
        output / "commandProtocolModels.ts": (
            ROOT
            / "ecosystem"
            / "defaultspack"
            / "webapp"
            / "src"
            / "generated"
            / "commandProtocolModels.ts"
        ),
        output / "command_protocol_models.dart": (
            ROOT.parent
            / "tobkiri_mobile"
            / "lib"
            / "src"
            / "generated"
            / "command_protocol_models.dart"
        ),
    }
    for source, target in mirrors.items():
        content = source.read_text(encoding="utf-8")
        if check:
            if not target.is_file() or target.read_text(encoding="utf-8") != content:
                raise ValueError(f"generated client binding drift detected: {target}")
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary = tempfile.mkstemp(
            prefix=f".{target.name}.",
            dir=target.parent,
        )
        temporary_path = Path(temporary)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary_path, target)
        finally:
            temporary_path.unlink(missing_ok=True)


def _validate(args: argparse.Namespace) -> dict[str, object]:
    manifest = validate_pack_manifest(args.manifest, schema_path=args.schema)
    return {
        "valid": True,
        "pack_id": manifest["pack"]["id"],
        "version": manifest["pack"]["version"],
    }


def _project_legacy(args: argparse.Namespace) -> dict[str, object]:
    """Generate or verify the read-only ecosystem.json compatibility view."""
    source_identity = generate_legacy_ecosystem_projection(
        args.manifest,
        args.output,
        check=bool(args.check),
    )
    return {
        "output": str(args.output),
        "check": bool(args.check),
        "source_content_hash": source_identity,
    }


def _sign(args: argparse.Namespace) -> dict[str, object]:
    pack_root = args.pack_root.resolve()
    private_key_path = args.private_key.resolve()
    try:
        private_key_path.relative_to(pack_root)
    except ValueError:
        pass
    else:
        raise ValueError("private signing keys must be outside the Pack root")
    output = (args.output or pack_root / SIGNED_MANIFEST_RELATIVE).resolve()
    try:
        relative_output = output.relative_to(pack_root).as_posix()
    except ValueError:
        relative_output = ""
    if relative_output and relative_output != SIGNED_MANIFEST_RELATIVE:
        raise ValueError(
            "signed manifests written inside a Pack must use the reserved path"
        )
    key = serialization.load_pem_private_key(
        private_key_path.read_bytes(),
        password=None,
    )
    if not isinstance(key, Ed25519PrivateKey):
        raise ValueError("Pack signing requires an Ed25519 private key")
    manifest = build_signed_manifest(
        pack_root,
        pack_id=args.pack_id,
        version=args.version,
        publisher_id=args.publisher_id,
        core_compatibility=args.core_compatibility,
        contract_versions=_key_value_pairs(args.contract_version),
        requested_capabilities=args.requested_capability,
        build_provenance=(
            json.loads(args.build_provenance.read_text(encoding="utf-8"))
            if args.build_provenance
            else None
        ),
    )
    signed = sign_manifest(manifest, key)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(signed, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return {
        "signed": True,
        "manifest": str(output),
        "key_id": signed["signature"]["key_id"],
    }


def _key_value_pairs(values: list[str]) -> dict[str, str]:
    result: dict[str, str] = {}
    for value in values:
        key, separator, item = str(value).partition("=")
        if not separator or not key.strip() or not item.strip():
            raise ValueError("contract versions must use CONTRACT=VERSION")
        result[key.strip()] = item.strip()
    return result


def _verify(args: argparse.Namespace) -> dict[str, object]:
    from rumi_ai import __version__ as core_version

    key = serialization.load_pem_public_key(args.public_key.read_bytes())
    manifest_path = args.manifest or args.pack_root / SIGNED_MANIFEST_RELATIVE
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    return verify_signed_pack(
        args.pack_root,
        manifest,
        key,
        expected_publisher_id=args.publisher_id,
        revoked_key_ids=set(args.revoked_key_id),
        core_version=str(core_version),
    )


def _inspect(args: argparse.Namespace) -> dict[str, object]:
    manifest_path = args.manifest or args.pack_root / SIGNED_MANIFEST_RELATIVE
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    return {
        "pack_id": manifest.get("pack_id"),
        "version": manifest.get("version"),
        "publisher_id": manifest.get("publisher_id"),
        "core_compatibility": manifest.get("core_compatibility"),
        "file_count": len(manifest.get("files") or []),
        "requested_capabilities": manifest.get("requested_capabilities") or [],
        "authority_granted": False,
        "signature": {
            "algorithm": (manifest.get("signature") or {}).get("algorithm"),
            "key_id": (manifest.get("signature") or {}).get("key_id"),
        },
    }


if __name__ == "__main__":
    raise SystemExit(main())
