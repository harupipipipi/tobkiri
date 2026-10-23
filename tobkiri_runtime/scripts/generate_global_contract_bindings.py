"""Generate language bindings from the global contract type schema."""

from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).parents[1]
SCHEMA = ROOT / "schemas" / "global_contract_types.schema.json"
OUT = ROOT / "generated" / "global_contracts"


def generate() -> None:
    """Generate deterministic TypeScript and Dart contract envelopes."""
    schema = json.loads(SCHEMA.read_text(encoding="utf-8"))
    statuses = schema["properties"]["status"]["enum"]
    union = " | ".join(f"'{status}'" for status in statuses)
    python_values = ",\n    ".join(repr(status) for status in statuses)
    dart_values = ",\n  ".join(_dart_enum(status) for status in statuses)
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "global_contract_types.py").write_text(
        '"""Generated from schemas/global_contract_types.schema.json."""\n\n'
        "from __future__ import annotations\n\n"
        "from typing import Generic, Literal, TypedDict, TypeVar\n"
        "from typing_extensions import NotRequired\n\n"
        f"ContractStatus = Literal[\n    {python_values},\n]\n\n"
        "T = TypeVar(\"T\")\n\n\n"
        "class ContractResult(TypedDict, Generic[T]):\n"
        "    \"\"\"Generated cross-language contract result shape.\"\"\"\n\n"
        "    status: ContractStatus\n"
        "    contract_id: str\n"
        "    version: str\n"
        "    provider_instance_id: str\n"
        "    diagnostics: NotRequired[list[str]]\n"
        "    value: NotRequired[T]\n",
        encoding="utf-8",
    )
    (OUT / "global_contract_types.ts").write_text(
        "// Generated from schemas/global_contract_types.schema.json.\n"
        f"export type ContractStatus = {union};\n\n"
        "export interface ContractResult<T = unknown> {\n"
        "  status: ContractStatus;\n"
        "  contract_id: string;\n"
        "  version: string;\n"
        "  provider_instance_id: string;\n"
        "  diagnostics?: string[];\n"
        "  value?: T;\n"
        "}\n",
        encoding="utf-8",
    )
    (OUT / "global_contract_types.dart").write_text(
        "// Generated from schemas/global_contract_types.schema.json.\n"
        f"enum ContractStatus {{\n  {dart_values}\n}}\n\n"
        "class ContractResult<T> {\n"
        "  const ContractResult({\n"
        "    required this.status,\n"
        "    required this.contractId,\n"
        "    required this.version,\n"
        "    required this.providerInstanceId,\n"
        "    this.diagnostics = const [],\n"
        "    this.value,\n"
        "  });\n\n"
        "  final ContractStatus status;\n"
        "  final String contractId;\n"
        "  final String version;\n"
        "  final String providerInstanceId;\n"
        "  final List<String> diagnostics;\n"
        "  final T? value;\n"
        "}\n",
        encoding="utf-8",
    )


def _dart_enum(value: str) -> str:
    """Convert a snake-case schema value into a Dart enum member."""
    head, *tail = value.split("_")
    return head + "".join(item.capitalize() for item in tail)


if __name__ == "__main__":
    generate()
