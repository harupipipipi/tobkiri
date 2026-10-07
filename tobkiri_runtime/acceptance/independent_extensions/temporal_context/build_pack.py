"""Build an offline executable Pack through the documented public producer."""
from __future__ import annotations

import argparse
from pathlib import Path

from author_contract import render
from core_runtime.pack_authoring import PythonPackFunction, build_python_pack

ROOT = Path(__file__).resolve().parent


def build(output: Path, pack_id: str = "acceptance.temporal.context",
          function_id: str | None = None) -> Path:
    """Produce exact executable metadata; grant no runtime authority."""
    contract = render(pack_id)
    assets = {str(path.relative_to(ROOT / "profile_projections")): path.read_bytes()
              for path in (ROOT / "profile_projections").rglob("*")
              if path.is_file()}
    return build_python_pack(
        output, pack_id=pack_id, version="0.1.0",
        display_name="Tobkiri temporal context", contracts=[contract],
        functions=[PythonPackFunction(
            function_id=function_id or f"{pack_id}.reduce",
            contract_id=contract["contract_id"], operation_ids=("temporal.reduce",),
            implementation_path="runtime/temporal.py",
            source=(ROOT / "source/temporal.py").read_bytes(),
        )], assets=assets,
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path, help="new nonexistent output directory")
    parser.add_argument("--pack-id", default="acceptance.temporal.context")
    parser.add_argument("--function-id")
    args = parser.parse_args()
    print(build(args.output, args.pack_id, args.function_id))
