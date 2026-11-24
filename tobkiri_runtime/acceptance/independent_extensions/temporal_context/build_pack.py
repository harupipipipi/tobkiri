"""Build an offline executable Pack through the documented public producer."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from author_contract import render
from author_workflow import workflow_template
from core_runtime.pack_authoring import PythonPackFunction, build_python_pack

ROOT = Path(__file__).resolve().parent


def build(output: Path, pack_id: str = "acceptance.temporal.context",
          function_id: str | None = None) -> Path:
    """Produce exact executable metadata; grant no runtime authority."""
    contract = render(pack_id)
    assets = {"content/" + str(path.relative_to(
        ROOT / "profile_projections/temporal")): path.read_bytes()
              for path in (ROOT / "profile_projections/temporal").rglob("*")
              if path.is_file()}
    selected_function = function_id or f"{pack_id}.reduce"
    assets = {name: body.decode().replace("acceptance.temporal.context.reduce",
                                         selected_function).replace(
        "acceptance.temporal.context", pack_id).encode()
              for name, body in assets.items()}
    assets.pop("content/workflows/acceptance.temporal.context.workflow.intent.v1.json", None)
    assets["content/contexts/acceptance.temporal.rules.conversation-context.v1.json"] = (
        json.dumps({"context_binding_api_version":
                    "io.tobkiri.conversation-context-binding.v1",
                    "kind": "timing.task_gap", "workflow_id": pack_id,
                    "output_step_id": "context"}, sort_keys=True, indent=2) + "\n"
    ).encode()
    assets[f"content/workflows/{pack_id}.workflow.intent.v1.json"] = (
        json.dumps(workflow_template(pack_id=pack_id, function_id=selected_function), sort_keys=True, indent=2)
        + "\n").encode()
    return build_python_pack(
        output, pack_id=pack_id, version="0.1.0",
        display_name="Tobkiri temporal context", contracts=[contract],
        functions=[PythonPackFunction(
            function_id=selected_function,
            contract_id=contract["contract_id"], operation_ids=("temporal.reduce", "timing.project.owner"),
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
