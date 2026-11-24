from __future__ import annotations

import importlib.util
from pathlib import Path
import sys

import pytest


RUNTIME_ROOT = Path(__file__).resolve().parent.parent
REPO_ROOT = RUNTIME_ROOT.parent
SCANNER_PATH = REPO_ROOT / "scripts" / "quality" / "check_core_no_favoritism.py"


def _scanner():
    module_name = "check_core_no_favoritism_test"
    spec = importlib.util.spec_from_file_location(module_name, SCANNER_PATH)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def _scan_fixture(tmp_path: Path, name: str, source: str):
    core_root = tmp_path / "tobkiri_runtime" / "core_runtime"
    core_root.mkdir(parents=True)
    (core_root / name).write_text(source, encoding="utf-8")
    return _scanner().scan_core(tmp_path)


def _scan_json_fixture(tmp_path: Path, name: str, source: str):
    core_root = tmp_path / "tobkiri_runtime" / "core_runtime"
    core_root.mkdir(parents=True)
    (core_root / name).write_text(source, encoding="utf-8")
    return _scanner().scan_core(tmp_path)


def test_current_core_has_no_application_domain_ownership() -> None:
    assert _scanner().scan_core(REPO_ROOT) == []


def test_missing_core_root_fails_closed(tmp_path: Path) -> None:
    violations = _scanner().scan_core(tmp_path)

    assert [(item.path, item.rule) for item in violations] == [
        ("tobkiri_runtime/core_runtime", "core_root_missing")
    ]


def test_generic_contract_dispatch_is_allowed(tmp_path: Path) -> None:
    violations = _scan_fixture(
        tmp_path,
        "contract_dispatch.py",
        "from core_runtime.authority import AuthorityService\n"
        "CONTRACT_PREFIX = '/api/contracts/'\n",
    )
    assert violations == []


@pytest.mark.parametrize(
    "name",
    [
        "ai_input_compiler.py",
        "chat_session.py",
        "defaultspack_adapter.py",
        "frontend_routes.py",
        "prompt_builder.py",
        "runtime_surface.py",
        "supervisor_dashboard.py",
    ],
)
def test_application_filenames_are_rejected(tmp_path: Path, name: str) -> None:
    violations = _scan_fixture(tmp_path, name, "VALUE = 1\n")
    assert any(item.rule == "forbidden_core_filename" for item in violations)


@pytest.mark.parametrize(
    "source",
    [
        "from ecosystem.defaultspack.domain.chat import store\n",
        "import domain.prompt.builder\n",
        "import importlib\nimportlib.import_module('ecosystem.defaultspack.domain.tool')\n",
        (
            "from importlib import import_module as load_module\n"
            "load_module('ecosystem.defaultspack.domain.tool')\n"
        ),
        (
            "import importlib\n"
            "def _defaultspack_module():\n"
            "    return 'ecosystem.defaultspack.domain.chat'\n"
            "importlib.import_module(_defaultspack_module())\n"
        ),
        (
            "import sys\nfrom pathlib import Path\n"
            "_ROOT = Path('.') / 'ecosystem' / 'defaultspack' / 'domain'\n"
            "sys.path.insert(0, str(_ROOT))\n"
        ),
        (
            "import sys as runtime_sys\n"
            "_ROOT = 'ecosystem/defaultspack/domain'\n"
            "runtime_sys.path.extend([_ROOT])\n"
        ),
    ],
)
def test_static_dynamic_and_sys_path_pack_imports_are_rejected(tmp_path: Path, source: str) -> None:
    violations = _scan_fixture(tmp_path, "runtime.py", source)
    assert any(
        item.rule
        in {
            "forbidden_pack_import",
            "forbidden_dynamic_pack_import",
            "forbidden_pack_sys_path",
        }
        for item in violations
    )


@pytest.mark.parametrize(
    "literal",
    [
        "/api/chat/send",
        "/api/ui/capability/invoke",
        "defaultspack.conversation.v1",
        "io.tobkiri.launcher.runtime-surface.v4",
        "frontend_contract_map.v4.json",
        "defaults.activate",
        "install_defaults_profile",
    ],
)
def test_named_application_routes_and_contracts_are_rejected(tmp_path: Path, literal: str) -> None:
    violations = _scan_fixture(tmp_path, "runtime.py", f"VALUE = {literal!r}\n")
    assert any(item.rule == "forbidden_application_literal" for item in violations)


@pytest.mark.parametrize(
    "literal",
    [
        "defaultspack",
        "rumi_default_tools_pack",
        "ecosystem/defaultspack/ui",
        "RUMI_DEFAULTSPACK_LOCAL_TOKEN",
        "/chat",
    ],
)
def test_named_pack_literals_are_rejected_in_python(tmp_path: Path, literal: str) -> None:
    violations = _scan_fixture(tmp_path, "runtime.py", f"VALUE = {literal!r}\n")

    assert any(item.rule == "forbidden_application_literal" for item in violations)


def test_named_pack_literals_are_rejected_in_json(tmp_path: Path) -> None:
    violations = _scan_json_fixture(
        tmp_path,
        "runtime.json",
        '{"adapter": "defaultspack", "path": "/chat", '
        '"env": "RUMI_DEFAULTSPACK_LOCAL_TOKEN"}',
    )

    assert [item.rule for item in violations].count("forbidden_application_literal") == 3


def test_literal_allowlist_requires_an_explicit_rationale() -> None:
    scanner = _scanner()

    assert scanner.LITERAL_ALLOWLIST
    assert all(reason.strip() for reason in scanner.LITERAL_ALLOWLIST.values())
