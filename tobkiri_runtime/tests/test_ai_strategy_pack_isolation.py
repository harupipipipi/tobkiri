"""Guard the ownership boundary for optional AI strategy Packs.

The Host and Gateway may know the generic strategy contract, but an installed
strategy's product name and algorithm belong to that Pack alone.  This check
keeps a future strategy from quietly acquiring a Gateway or defaultspack
special case.
"""

from pathlib import Path

from ecosystem.defaultspack.domain.runtime_v4 import (
    BundledCatalog,
    dynamic_profile_edges,
)


RUNTIME = Path(__file__).resolve().parents[1]
PRODUCTION_ROOTS = (
    RUNTIME / "core_runtime",
    RUNTIME / "tobkiri_host",
    RUNTIME / "tobkiri_protocol",
    RUNTIME / "ecosystem",
)
SOURCE_SUFFIXES = {".py", ".ts", ".tsx", ".js", ".json", ".yaml", ".yml"}
GENERATED_NAMES = {
    "pack.v4.json",
    "contracts.v4.json",
    "executables.v4.json",
    "artifact-index.v4.json",
}


def test_optional_strategy_name_is_owned_by_its_pack() -> None:
    """No production implementation outside the Pack may special-case it."""

    violations: list[str] = []
    for root in PRODUCTION_ROOTS:
        for path in root.rglob("*"):
            if not path.is_file() or path.suffix not in SOURCE_SUFFIXES:
                continue
            relative = path.relative_to(RUNTIME)
            if "rumi_deepthink_pack" in relative.parts:
                continue
            if path.name.startswith("test_") or ".test." in path.name:
                continue
            if any(
                part
                in {
                    "__pycache__",
                    "__tests__",
                    "node_modules",
                    "dist",
                    "build",
                    "generated",
                    "v4",
                }
                for part in relative.parts
            ):
                continue
            if path.name in GENERATED_NAMES:
                continue
            source = path.read_text(encoding="utf-8").casefold()
            if any(name in source for name in ("deepthink", "tobkirithink")):
                violations.append(relative.as_posix())
    assert not violations, "Pack-specific strategy code leaked into: " + ", ".join(
        sorted(violations)
    )


def test_installed_strategy_manifest_derives_runtime_and_gateway_edges() -> None:
    """A real optional Pack gains only its signed generic Plan operations."""

    catalog = BundledCatalog.load(RUNTIME / "ecosystem/defaultspack/v4")
    edges = dynamic_profile_edges(catalog, "defaults", ("rumi_deepthink_pack",))
    actual = {
        (
            edge["caller_function_id"],
            edge["target_provider_id"],
            edge["contract_id"],
            edge["operation_id"],
        )
        for edge in edges
    }
    execute = "rumi_deepthink_pack.deepthink.execute"
    assert actual == {
        (
            "rumi_ai_strategy_runtime_pack.ai-strategy.dispatch",
            execute,
            "tobkiri.service.ai.strategy.execute.v1",
            execute,
        ),
        (
            "rumi_ai_strategy_runtime_pack.ai-strategy.catalog",
            execute,
            "tobkiri.service.ai.strategy.execute.v1",
            execute,
        ),
        (
            execute,
            "rumi_ai_gateway_pack.ai-gateway.route-quote",
            "tobkiri.resource.ai.route.quote.v1",
            "rumi_ai_gateway_pack.ai-gateway.route-quote",
        ),
        (
            execute,
            "rumi_ai_gateway_pack.ai-gateway.generate",
            "tobkiri.service.ai.generate.v1",
            "rumi_ai_gateway_pack.ai-gateway.generate",
        ),
    }
