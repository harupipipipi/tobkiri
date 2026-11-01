from __future__ import annotations

import json
import importlib.util
from pathlib import Path

import pytest

from core_runtime.pack_sdk import (
    PackSdkError,
    PackSdkGenerator,
    scaffold_pack,
    validate_pack_manifest,
)
from core_runtime.pack_templates import (
    PackTemplateError,
    resolve_profile,
    scaffold_component,
    validate_template_components,
)

ROOT = Path(__file__).resolve().parent.parent
PACK_SCHEMA = ROOT / "tobkiri_protocol" / "schemas" / "pack_manifest_v4.schema.json"
CONTRACT_SCHEMA = ROOT / "schemas" / "global_contract_types.schema.json"


def test_sdk_generation_is_deterministic_and_detects_drift(tmp_path: Path) -> None:
    output = tmp_path / "generated"
    generator = PackSdkGenerator([PACK_SCHEMA, CONTRACT_SCHEMA])

    first = generator.generate(output)
    second = generator.generate(output, check=True)

    assert first == second
    index = json.loads((output / "contract_index.json").read_text(encoding="utf-8"))
    assert len(index["schemas"]) == 2
    assert all(record["sha256"] for record in index["schemas"])
    assert "packSchemaIds" in (output / "contract_ids.dart").read_text(
        encoding="utf-8"
    )
    spec = importlib.util.spec_from_file_location(
        "generated_command_models",
        output / "command_protocol_models.py",
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.CommandInvocationRequest

    (output / "contractIds.ts").write_text("// manually edited\n", encoding="utf-8")
    with pytest.raises(PackSdkError, match="drift"):
        generator.generate(output, check=True)

    generator.generate(output)
    (output / "stale_generated.ts").write_text("// stale\n", encoding="utf-8")
    with pytest.raises(PackSdkError, match="stale_generated"):
        generator.generate(output, check=True)


def test_scaffold_is_strictly_valid_and_untrusted(tmp_path: Path) -> None:
    manifest_path = scaffold_pack(
        tmp_path / "example",
        pack_id="example.echo",
        display_name="Echo",
    )

    manifest = validate_pack_manifest(manifest_path, schema_path=PACK_SCHEMA)

    assert manifest_path.name == "pack.v4.json"
    assert manifest["pack"]["id"] == "example.echo"
    assert manifest["provenance"]["source_kind"] == "generated"
    assert manifest["requirements"]["capabilities"] == []
    assert manifest["requirements"]["execution_boundary"] == "declarative_only"
    assert manifest["migration"]["compatibility"] == "none"
    assert (manifest_path.parent / "AGENTS.md").is_file()
    contract = json.loads(
        (manifest_path.parent / "template.contract.json").read_text(
            encoding="utf-8"
        )
    )
    assert contract["selection"]["owner"] == "ai"
    assert contract["selection"]["schema_policy"] == "progressive"
    assert contract["security"]["default_authority"] == "none"
    assert not (manifest_path.parent / "ecosystem.json").exists()
    assert not (manifest_path.parent / "rumi.pack.v3.json").exists()
    for name in ("contracts.v4.json", "artifact-index.v4.json", "executables.v4.json"):
        assert (manifest_path.parent / name).is_file()
    validate_template_components(
        manifest_path.parent,
        ROOT / "ecosystem" / "defaultspack" / "schemas",
    )


def test_manifest_validation_rejects_unknown_security_fields(
    tmp_path: Path,
) -> None:
    manifest_path = scaffold_pack(
        tmp_path / "example",
        pack_id="example.echo",
        display_name="Echo",
    )
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["pack"]["trusted"] = True
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(PackSdkError, match="Additional properties"):
        validate_pack_manifest(manifest_path, schema_path=PACK_SCHEMA)


@pytest.mark.parametrize(
    ("intent", "expected"),
    [
        ("implement a repository patch like Codex", "codex"),
        ("リポジトリを調査して実装とパッチを作る", "codex"),
        ("Hermes messaging gateway over SSH", "hermes"),
        ("SSHのリモートゲートウェイとメモリを使う", "hermes"),
        ("general agent", "complete"),
    ],
)
def test_auto_profile_selection_is_explainable(
    intent: str,
    expected: str,
) -> None:
    assert resolve_profile("auto", intent) == expected


def test_complete_scaffold_contains_codex_and_hermes_strengths(
    tmp_path: Path,
) -> None:
    root = tmp_path / "complete"
    scaffold_pack(
        root,
        pack_id="example.complete",
        display_name="Complete",
        profile="complete",
    )

    agents = (root / "AGENTS.md").read_text(encoding="utf-8")
    skill = (
        root / "extensions" / "skills" / "task_operator" / "SKILL.md"
    ).read_text(encoding="utf-8")
    compatibility = (
        root
        / "extensions"
        / "skills"
        / "task_operator"
        / "references"
        / "compatibility.md"
    ).read_text(encoding="utf-8")

    assert "nearest scoped AGENTS.md" in agents
    assert "narrowest configured toolset" in agents
    assert "Let the AI select" in agents
    assert "Capability Plan" in skill
    assert "feedback" in skill.casefold()
    assert "Codex-compatible" in compatibility
    assert "Hermes-compatible" in compatibility


def test_add_component_is_strict_and_never_overwrites(tmp_path: Path) -> None:
    root = tmp_path / "components"
    scaffold_pack(
        root,
        pack_id="example.components",
        display_name="Components",
        profile="minimal",
    )
    paths = scaffold_component(
        root,
        kind="tool",
        component_id="example.components.review",
        display_name="Review",
        description="Review a bounded input.",
    )

    manifest = json.loads(paths[0].read_text(encoding="utf-8"))
    assert manifest["schema_version"] == "tobkiri.tool/v3"
    assert manifest["enabled"] is False
    assert manifest["approval"]["minimum"] == "deny"
    validate_template_components(
        root,
        ROOT / "ecosystem" / "defaultspack" / "schemas",
        component_paths=paths,
    )

    with pytest.raises(PackTemplateError, match="overwrite"):
        scaffold_component(
            root,
            kind="tool",
            component_id="example.components.review",
            display_name="Review",
            description="Review a bounded input.",
        )


def test_add_component_requires_pack_root_and_preflights_every_file(
    tmp_path: Path,
) -> None:
    with pytest.raises(PackTemplateError, match="pack.v4.json"):
        scaffold_component(
            tmp_path / "not-a-pack",
            kind="activity",
            component_id="example.activity",
            display_name="Activity",
            description="Example.",
        )

    root = tmp_path / "atomic"
    scaffold_pack(
        root,
        pack_id="example.atomic",
        display_name="Atomic",
        profile="minimal",
    )
    skill_root = root / "extensions" / "skills" / "review"
    skill_root.mkdir(parents=True)
    (skill_root / "SKILL.md").write_text("owned\n", encoding="utf-8")

    with pytest.raises(PackTemplateError, match="overwrite"):
        scaffold_component(
            root,
            kind="skill",
            component_id="example.atomic.review",
            display_name="Review",
            description="Review an input.",
        )

    assert not (skill_root / "manifest.json").exists()
    assert (skill_root / "SKILL.md").read_text(encoding="utf-8") == "owned\n"


@pytest.mark.parametrize(
    ("profile", "intent", "expected"),
    [
        ("minimal", "", "minimal"),
        ("codex", "", "codex"),
        ("hermes", "", "hermes"),
        ("complete", "", "complete"),
        ("auto", "repository patch", "codex"),
        ("auto", "SSH remote memory", "hermes"),
        ("auto", "general assistant", "complete"),
    ],
)
def test_every_template_profile_scaffolds_and_validates(
    tmp_path: Path,
    profile: str,
    intent: str,
    expected: str,
) -> None:
    root = tmp_path / f"{profile}-{expected}"
    manifest_path = scaffold_pack(
        root,
        pack_id=f"example.{profile}_{expected}",
        display_name=f"{profile} {expected}",
        profile=profile,
        intent=intent,
    )

    validate_pack_manifest(manifest_path, schema_path=PACK_SCHEMA)
    if expected == "minimal":
        assert not (root / "template.contract.json").exists()
        return

    contract = json.loads(
        (root / "template.contract.json").read_text(encoding="utf-8")
    )
    assert contract["profile"] == expected
    validate_template_components(
        root,
        ROOT / "ecosystem" / "defaultspack" / "schemas",
    )


def test_skill_component_escapes_front_matter_and_normalizes_lines(
    tmp_path: Path,
) -> None:
    root = tmp_path / "safe-skill"
    scaffold_pack(
        root,
        pack_id="example.safe_skill",
        display_name="Safe skill",
        profile="minimal",
    )

    paths = scaffold_component(
        root,
        kind="skill",
        component_id="example.safe_skill.review",
        display_name='Review: "quoted"\nname',
        description="Review\nwithout front matter injection",
    )
    skill = next(path for path in paths if path.name == "SKILL.md").read_text(
        encoding="utf-8"
    )
    validate_template_components(
        root,
        ROOT / "ecosystem" / "defaultspack" / "schemas",
        component_paths=paths,
    )

    assert 'name: "Review: \\"quoted\\" name"' in skill
    assert "description: \"Review without front matter injection\"" in skill
    assert "\nname\n" not in skill
