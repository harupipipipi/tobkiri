from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parent.parent
SCAN_PATH = ROOT / "scripts" / "quality" / "scan_defaultspack_boundaries.py"


def _load_boundary_scan_module():
    spec = importlib.util.spec_from_file_location("scan_defaultspack_boundaries_test", SCAN_PATH)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_defaultspack_boundary_scan_passes():
    result = subprocess.run(
        [
            sys.executable,
            "scripts/quality/scan_defaultspack_boundaries.py",
        ],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert "passed" in result.stdout


def test_defaultspack_boundary_scan_detects_relative_domain_imports():
    scanner = _load_boundary_scan_module()

    imports = scanner._iter_domain_imports(
        ROOT / "ecosystem" / "defaultspack" / "domain" / "prompt" / "effective.py"
    )

    assert "capability" in imports


def _scan_fixture(tmp_path: Path, source: str) -> list[str]:
    scanner = _load_boundary_scan_module()
    pack_root = tmp_path / "defaultspack"
    tool_root = pack_root / "domain" / "tool"
    chat_root = pack_root / "domain" / "chat"
    tool_root.mkdir(parents=True)
    chat_root.mkdir(parents=True)
    (tool_root / "consumer.py").write_text(source, encoding="utf-8")
    (chat_root / "ir_blocks.py").write_text("PUBLIC = True\n", encoding="utf-8")
    (chat_root / "store.py").write_text("PRIVATE = True\n", encoding="utf-8")
    policy = pack_root / "domain_boundaries.yaml"
    policy.write_text(
        """domains:
  chat:
    may_import: []
    path: domain/chat
  tool:
    may_import: []
    path: domain/tool
    public_imports:
    - domain/chat/ir_blocks
""",
        encoding="utf-8",
    )
    return scanner.scan_boundaries(pack_root, policy)


@pytest.mark.parametrize(
    "source",
    [
        "from domain.chat.ir_blocks import PUBLIC\n",
        "from ecosystem.defaultspack.domain.chat.ir_blocks import PUBLIC\n",
        "from ..chat.ir_blocks import PUBLIC\n",
        "import domain.chat.ir_blocks\n",
    ],
)
def test_public_contract_import_passes_in_temporary_tree(tmp_path: Path, source: str):
    errors = _scan_fixture(tmp_path, source)

    assert errors == []


def test_unapproved_internal_import_fails_in_temporary_tree(tmp_path: Path):
    errors = _scan_fixture(tmp_path, "from domain.chat.store import PRIVATE\n")

    assert any("bypasses public contract: tool -> domain/chat/store" in error for error in errors)


@pytest.mark.parametrize(
    "source",
    [
        "from domain import chat\n",
        "from domain.chat import store\n",
        "from .. import chat\n",
    ],
)
def test_package_import_forms_cannot_bypass_public_contract(tmp_path: Path, source: str):
    errors = _scan_fixture(tmp_path, source)

    assert any("bypasses public contract: tool -> domain/chat" in error for error in errors)


def test_qualified_relative_import_above_domain_root_cannot_bypass_policy(tmp_path: Path):
    errors = _scan_fixture(
        tmp_path, "from ...domain.chat.store import PRIVATE as private_value\n"
    )

    assert any("bypasses public contract: tool -> domain/chat/store" in error for error in errors)


def test_mismatched_domain_path_cannot_disable_scanning(tmp_path: Path):
    scanner = _load_boundary_scan_module()
    pack_root = tmp_path / "defaultspack"
    (pack_root / "domain" / "tool").mkdir(parents=True)
    (pack_root / "domain" / "chat").mkdir()
    (pack_root / "domain" / "tool" / "consumer.py").write_text(
        "from domain.chat.store import PRIVATE\n", encoding="utf-8"
    )
    policy = pack_root / "domain_boundaries.yaml"
    policy.write_text(
        """domains:
  chat:
    may_import: []
    path: domain/chat
  tool:
    may_import: []
    path: domain/chat
""",
        encoding="utf-8",
    )

    errors = scanner.scan_boundaries(pack_root, policy)

    assert any("domain path must be domain/tool" in error for error in errors)


def test_public_import_package_is_rejected_as_ambiguous(tmp_path: Path):
    scanner = _load_boundary_scan_module()
    pack_root = tmp_path / "defaultspack"
    (pack_root / "domain" / "tool").mkdir(parents=True)
    public_package = pack_root / "domain" / "chat" / "contracts"
    public_package.mkdir(parents=True)
    (public_package / "__init__.py").write_text("PUBLIC = True\n", encoding="utf-8")
    policy = pack_root / "domain_boundaries.yaml"
    policy.write_text(
        """domains:
  chat:
    may_import: []
    path: domain/chat
  tool:
    may_import: []
    path: domain/tool
    public_imports:
    - domain/chat/contracts
""",
        encoding="utf-8",
    )

    errors = scanner.scan_boundaries(pack_root, policy)

    assert any(
        "public import must name a concrete .py module for tool: domain/chat/contracts"
        in error
        for error in errors
    )


@pytest.mark.parametrize(
    ("policy_text", "message"),
    [
        ("domains: []\n", "domains must be a mapping"),
        ("domains: [\n", "invalid YAML"),
        (
            """domains:
  chat:
    may_import: domain/tool
    path: domain/chat
  tool:
    may_import: []
    path: domain/tool
exceptions: not-a-list
""",
            "domains.chat.may_import must be a list",
        ),
    ],
)
def test_invalid_policy_is_reported(tmp_path: Path, policy_text: str, message: str):
    scanner = _load_boundary_scan_module()
    pack_root = tmp_path / "defaultspack"
    (pack_root / "domain" / "tool").mkdir(parents=True)
    (pack_root / "domain" / "chat").mkdir()
    policy = pack_root / "domain_boundaries.yaml"
    policy.write_text(policy_text, encoding="utf-8")

    errors = scanner.scan_boundaries(pack_root, policy)

    assert any(message in error for error in errors)


def test_python_parse_failure_is_reported_without_traceback(tmp_path: Path):
    scanner = _load_boundary_scan_module()
    pack_root = tmp_path / "defaultspack"
    chat_root = pack_root / "domain" / "chat"
    chat_root.mkdir(parents=True)
    (chat_root / "broken.py").write_text("def broken(:\n", encoding="utf-8")
    policy = pack_root / "domain_boundaries.yaml"
    policy.write_text(
        """domains:
  chat:
    may_import: []
    path: domain/chat
""",
        encoding="utf-8",
    )

    errors = scanner.scan_boundaries(pack_root, policy)

    assert any("cannot parse domain/chat/broken.py: SyntaxError" in error for error in errors)


def test_exact_exception_does_not_allow_other_modules(tmp_path: Path):
    scanner = _load_boundary_scan_module()
    pack_root = tmp_path / "defaultspack"
    tool_root = pack_root / "domain" / "tool"
    chat_root = pack_root / "domain" / "chat"
    tool_root.mkdir(parents=True)
    chat_root.mkdir(parents=True)
    (tool_root / "consumer.py").write_text(
        "from domain.chat.ir_blocks import PUBLIC\n"
        "from domain.chat.store import PRIVATE\n",
        encoding="utf-8",
    )
    (chat_root / "store.py").write_text("PRIVATE = True\n", encoding="utf-8")
    (chat_root / "ir_blocks.py").write_text("PUBLIC = True\n", encoding="utf-8")
    policy = pack_root / "domain_boundaries.yaml"
    policy.write_text(
        """domains:
  chat:
    may_import: []
    path: domain/chat
  tool:
    may_import: []
    path: domain/tool
exceptions:
- file: domain/tool/consumer.py
  from: tool
  import: domain/chat/ir_blocks
  reason: exact temporary contract
""",
        encoding="utf-8",
    )

    errors = scanner.scan_boundaries(pack_root, policy)

    assert sum("not allowlisted: tool -> chat" in error for error in errors) == 1


def test_top_level_domain_module_is_scanned(tmp_path: Path):
    scanner = _load_boundary_scan_module()
    pack_root = tmp_path / "defaultspack"
    domain_root = pack_root / "domain"
    (domain_root / "chat").mkdir(parents=True)
    (domain_root / "legacy.py").write_text(
        "from domain.chat.store import PRIVATE\n", encoding="utf-8"
    )
    policy = pack_root / "domain_boundaries.yaml"
    policy.write_text(
        """domains:
  chat:
    may_import: []
    path: domain/chat
  legacy:
    may_import: []
    path: domain/legacy.py
""",
        encoding="utf-8",
    )

    errors = scanner.scan_boundaries(pack_root, policy)

    assert any("not allowlisted: legacy -> chat" in error for error in errors)


def test_key_edges_use_public_contracts_in_repository_policy():
    scanner = _load_boundary_scan_module()
    policy = scanner._read_yaml(ROOT / "ecosystem" / "defaultspack" / "domain_boundaries.yaml")[
        "domains"
    ]

    assert "domain/chat" not in policy["ai_client"]["may_import"]
    assert set(policy["ai_client"]["public_imports"]) == {
        "domain/chat/ir",
        "domain/chat/ir_blocks",
        "domain/chat/ir_legacy_adapter",
        "domain/chat/store",
        "domain/frontend_settings_store",
    }
    assert "domain/chat" not in policy["tool"]["may_import"]
    assert set(policy["tool"]["public_imports"]) == {
        "domain/chat/ir_blocks",
        "domain/chat/store",
        "domain/chat/tool_selection_schema",
    }
    assert "domain/capability" not in policy["chat"]["may_import"]
    assert set(policy["chat"]["public_imports"]) == {
        "domain/capability/activity_registry",
        "domain/capability/catalog",
        "domain/capability/models",
        "domain/capability/orchestrator",
        "domain/capability/repository",
        "domain/capability/settings",
        "domain/mention",
        "domain/tool_policy/internal_context",
    }
    assert policy["mention"] == {
        "may_import": [],
        "path": "domain/mention.py",
    }
    assert "domain/mention" not in policy["company"]["may_import"]
    assert set(policy["company"]["public_imports"]) == {
        "domain/agent/placement_catalog",
        "domain/mention",
    }
    assert "domain/mention" not in policy["subagent_team"]["may_import"]
    assert set(policy["subagent_team"]["public_imports"]) == {"domain/mention"}
    assert policy["share"]["may_import"] == []
    assert set(policy["share"]["public_imports"]) == {
        "domain/chat/store",
        "domain/safety/audit",
    }
    assert "domain/capability" not in policy["frontend"]["may_import"]
    assert set(policy["frontend"]["public_imports"]) == {
        "domain/capability/catalog",
        "domain/frontend_settings_store",
    }
