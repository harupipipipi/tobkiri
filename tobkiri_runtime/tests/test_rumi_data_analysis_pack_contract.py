from __future__ import annotations



import json
import re
import sys
from pathlib import Path

import yaml
import pytest

pytestmark = pytest.mark.contract


ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from ecosystem.setup_pack.pack_selector import PackSelector  # noqa: E402


PACK_ID = "rumi_data_analysis_pack"
PACK_DIR = ROOT / "ecosystem" / PACK_ID
SETUP_PACK_JSON = ROOT / "ecosystem" / "setup_pack" / PACK_ID / "pack.json"


def _json_files() -> list[Path]:
    return sorted(PACK_DIR.glob("**/*.json")) + [SETUP_PACK_JSON]


def _yaml_files() -> list[Path]:
    return sorted(PACK_DIR.glob("**/*.yaml"))


def test_data_analysis_pack_required_docs_and_assets_exist():
    required = [
        PACK_DIR / "ecosystem.json",
        PACK_DIR / "README.md",
        PACK_DIR / "docs" / "README.md",
        PACK_DIR / "docs" / "architecture.md",
        PACK_DIR / "docs" / "interfaces.md",
        PACK_DIR / "docs" / "operations.md",
        PACK_DIR / "catalog" / "capabilities.yaml",
        PACK_DIR / "catalog" / "chart_kinds.json",
        PACK_DIR / "specs" / "chart_spec.schema.yaml",
        PACK_DIR / "specs" / "audit_trail.schema.yaml",
    ]

    missing = [str(path.relative_to(ROOT)) for path in required if not path.is_file()]

    assert missing == []
    assert list((PACK_DIR / "profiles").glob("*.profile.yaml"))
    assert list((PACK_DIR / "prompts").glob("*.system.md"))
    assert list((PACK_DIR / "presets").glob("*.preset.yaml"))
    assert list((PACK_DIR / "examples").glob("*.example.yaml"))
    assert list((PACK_DIR / "recipes").glob("*.yaml"))


def test_data_analysis_pack_json_and_yaml_parse():
    for path in _json_files():
        data = json.loads(path.read_text(encoding="utf-8"))
        assert isinstance(data, dict), path

    for path in _yaml_files():
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        assert isinstance(data, dict), path
        assert any(key.endswith("_id") or key in {"id", "preset_id", "profile_id", "spec_id", "recipe_id"} for key in data), path


def test_data_analysis_pack_setup_metadata_and_selector_discoverability():
    setup = json.loads(SETUP_PACK_JSON.read_text(encoding="utf-8"))
    ecosystem = json.loads((PACK_DIR / "ecosystem.json").read_text(encoding="utf-8"))

    assert setup["pack_id"] == PACK_ID
    assert setup["target_pack_id"] == PACK_ID
    assert setup["recommended"] is False
    assert setup["risk_level"] == "low"
    assert setup["conflicts_with"] == []
    assert setup["overlap_policy"]["workspace_pack"] == "complement_not_replace"
    assert setup["overlap_policy"]["defaultspack_core"] == "do_not_override"
    assert setup["base_pack_promotion"]["eligible"] is False
    assert ecosystem["runtime"]["type"] == "declarative_pack"

    dependencies = {item["pack_id"]: item.get("version") for item in setup["depends_on"]}
    assert dependencies == {
        "defaultspack": ">=2.0.0",
        "rumi_default_tools_pack": ">=1.0.0",
        "rumi_local_agent_pack": ">=1.0.0",
    }

    selector = PackSelector(ROOT / "ecosystem" / "setup_pack")
    candidates = {candidate.pack_id: candidate for candidate in selector.scan_candidates()}
    candidate = candidates[PACK_ID]

    assert candidate.pack_identity == f"rumi:ecosystem/{PACK_ID}"
    assert candidate.overlap_policy["workspace_pack"] == "complement_not_replace"
    assert candidate.base_pack_promotion["eligible"] is False
    assert candidate.marketplace["id"] == "rumi.data_analysis_pack"
    assert candidate.signing["mode"] == "repository_reviewed"

    issues = selector.validate_candidates(
        installed_packs={
            "defaultspack": {"version": "2.0.0"},
            "rumi_default_tools_pack": {"version": "1.0.0"},
            "rumi_local_agent_pack": {"version": "1.0.0"},
        },
        platform_name="macos",
        python_version="3.13.0",
    )
    assert [issue for issue in issues if issue["pack_id"] == PACK_ID] == []


def test_data_analysis_profiles_are_local_first_and_network_none():
    for path in sorted((PACK_DIR / "profiles").glob("*.profile.yaml")):
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        policy = data.get("policy", {})

        assert data["profile_id"].startswith("rumi_data_analysis.")
        assert policy.get("local_first") is True
        assert policy.get("network_default") == "none"

    for path in sorted((PACK_DIR / "presets").glob("*.preset.yaml")):
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        assert data["policy"]["network_default"] == "none"


def test_data_analysis_pack_contains_no_executable_code_or_secret_like_literals():
    forbidden_suffixes = {".py", ".sh", ".js", ".ts", ".tsx", ".ipynb", ".sql"}
    executable_files = [
        str(path.relative_to(ROOT))
        for path in PACK_DIR.glob("**/*")
        if path.is_file() and path.suffix in forbidden_suffixes
    ]
    assert executable_files == []

    secret_patterns = [
        re.compile(pattern, re.IGNORECASE)
        for pattern in [
            r"api[_-]?key\s*[:=]\s*['\"]?[A-Za-z0-9_-]{12,}",
            r"bearer\s+[A-Za-z0-9._-]{16,}",
            r"sk-[A-Za-z0-9]{16,}",
            r"password\s*[:=]\s*['\"][^'\"]+['\"]",
            r"oauth[_-]?client[_-]?secret\s*[:=]",
            r"postgres(ql)?://[^\\s]+:[^\\s]+@",
            r"mysql://[^\\s]+:[^\\s]+@",
        ]
    ]

    checked_files = [
        path
        for path in PACK_DIR.glob("**/*")
        if path.is_file() and path.suffix in {".json", ".yaml", ".md"}
    ]
    checked_files.append(SETUP_PACK_JSON)

    offenders: list[str] = []
    for path in checked_files:
        text = path.read_text(encoding="utf-8")
        if any(pattern.search(text) for pattern in secret_patterns):
            offenders.append(str(path.relative_to(ROOT)))

    assert offenders == []
