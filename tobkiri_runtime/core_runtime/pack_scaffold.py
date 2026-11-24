"""Compatibility CLI that emits only canonical Pack v4 scaffolds.

The former Wave 14 generator emitted ``ecosystem.json`` directly.  That file
is never created here: all entry points delegate to the Pack v4 SDK.
"""

from __future__ import annotations

import argparse
from pathlib import Path

from .pack_sdk import PackSdkError, scaffold_pack

VALID_TEMPLATES = ("minimal", "capability", "flow", "full")
_PROFILE_BY_TEMPLATE = {
    "minimal": "minimal",
    "capability": "codex",
    "flow": "hermes",
    "full": "complete",
}


class PackScaffold:
    """Generate one deterministic, authority-free Pack v4 directory."""

    def generate(
        self,
        pack_id: str,
        target_dir: Path,
        template: str = "minimal",
        force: bool = False,
    ) -> Path:
        """Create a Pack v4 scaffold without legacy manifests or dual writes."""

        if template not in VALID_TEMPLATES:
            raise ValueError(
                f"Unknown template: {template!r}. Valid templates: "
                + ", ".join(VALID_TEMPLATES)
            )
        target = Path(target_dir) / pack_id
        if force and target.exists():
            raise FileExistsError(
                "force overwrite was retired; choose an empty target to preserve provenance"
            )
        try:
            scaffold_pack(
                target,
                pack_id=pack_id,
                display_name=pack_id,
                profile=_PROFILE_BY_TEMPLATE[template],
            )
        except PackSdkError as exc:
            if "target directory must be empty" in str(exc):
                raise FileExistsError(str(exc)) from exc
            raise ValueError(str(exc)) from exc
        return target


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m core_runtime.pack_scaffold",
        description="Generate a canonical Tobkiri Pack v4 scaffold.",
    )
    parser.add_argument("pack_id")
    parser.add_argument(
        "--template", "-t", choices=VALID_TEMPLATES, default="minimal"
    )
    parser.add_argument("--output", "-o", default=".")
    parser.add_argument(
        "--force",
        "-f",
        action="store_true",
        help="Retired: v4 scaffolds never overwrite an existing Pack.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """Run the legacy module name with Pack v4-only behavior."""

    args = _build_parser().parse_args(argv)
    try:
        created = PackScaffold().generate(
            args.pack_id,
            Path(args.output),
            template=args.template,
            force=args.force,
        )
    except (ValueError, FileExistsError) as exc:
        print(f"Error: {exc}")
        return 1
    print(f"Pack v4 scaffold created: {created}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
