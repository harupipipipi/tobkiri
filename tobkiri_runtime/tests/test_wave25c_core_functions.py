"""
test_wave25c_core_functions.py - W25-C: core_pack functions/ テスト

functions/ ディレクトリ内の 5 つの Docker function (run, exec, stop, logs, list)
の manifest.json と main.py を検証する。
"""

import json
import py_compile
import re
import sys
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# 定数
# ---------------------------------------------------------------------------

FUNC_BASE = (
    Path(__file__).resolve().parent.parent
    / "core_runtime"
    / "core_pack"
    / "core_docker_capability"
    / "functions"
)

FUNCTION_NAMES = ["run", "exec", "stop", "logs", "list"]

FUNCTION_ID_PATTERN = re.compile(r"^[a-z][a-z0-9_]*$")


# ---------------------------------------------------------------------------
# ヘルパー
# ---------------------------------------------------------------------------

def _load_manifest(func_name: str) -> dict:
    path = FUNC_BASE / func_name / "manifest.json"
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


# ---------------------------------------------------------------------------
# Test 1: functions/ ディレクトリが存在する
# ---------------------------------------------------------------------------

def test_functions_directory_exists():
    assert FUNC_BASE.is_dir(), "functions/ directory does not exist: " + str(FUNC_BASE)


# ---------------------------------------------------------------------------
# Test 2: 5 つ全ての function ディレクトリが存在する
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("func_name", FUNCTION_NAMES)
def test_function_directory_exists(func_name):
    d = FUNC_BASE / func_name
    assert d.is_dir(), "Function directory missing: " + str(d)


# ---------------------------------------------------------------------------
# Test 3: 各 manifest.json が有効な JSON である
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("func_name", FUNCTION_NAMES)
def test_manifest_is_valid_json(func_name):
    path = FUNC_BASE / func_name / "manifest.json"
    assert path.is_file(), "manifest.json not found: " + str(path)
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    assert isinstance(data, dict)


# ---------------------------------------------------------------------------
# Test 4: function_id がディレクトリ名と一致する
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("func_name", FUNCTION_NAMES)
def test_function_id_matches_dirname(func_name):
    manifest = _load_manifest(func_name)
    fid = manifest.get("function_id")
    assert fid == func_name, (
        "function_id mismatch: manifest says "
        + repr(fid)
        + " but directory is "
        + repr(func_name)
    )


# ---------------------------------------------------------------------------
# Test 5: function_id が ^[a-z][a-z0-9_]*$ パターンに一致する
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("func_name", FUNCTION_NAMES)
def test_function_id_pattern(func_name):
    manifest = _load_manifest(func_name)
    fid = manifest["function_id"]
    assert FUNCTION_ID_PATTERN.match(fid), (
        "function_id " + repr(fid) + " does not match pattern ^[a-z][a-z0-9_]*$"
    )


# ---------------------------------------------------------------------------
# Test 6: requires フィールドが存在し list 型
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("func_name", FUNCTION_NAMES)
def test_requires_is_list(func_name):
    manifest = _load_manifest(func_name)
    requires = manifest.get("requires")
    assert requires is not None, "requires field is missing"
    assert isinstance(requires, list), "requires is not a list: " + str(type(requires))
    for item in requires:
        assert isinstance(item, str), "requires item is not a string: " + repr(item)


# ---------------------------------------------------------------------------
# Test 7: grant_config フィールドが存在する（run, exec は非空）
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("func_name", FUNCTION_NAMES)
def test_grant_config_exists(func_name):
    manifest = _load_manifest(func_name)
    gc = manifest.get("grant_config")
    assert gc is not None, "grant_config is missing for " + func_name
    assert isinstance(gc, dict), "grant_config is not a dict: " + str(type(gc))
    if func_name in ("run", "exec"):
        assert len(gc) > 0, "grant_config must be non-empty for " + func_name


# ---------------------------------------------------------------------------
# Test 8: 各 main.py が存在する
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("func_name", FUNCTION_NAMES)
def test_main_py_exists(func_name):
    path = FUNC_BASE / func_name / "main.py"
    assert path.is_file(), "main.py not found: " + str(path)


# ---------------------------------------------------------------------------
# Test 9: 各 main.py が構文的に正しい
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("func_name", FUNCTION_NAMES)
def test_main_py_compiles(func_name):
    path = FUNC_BASE / func_name / "main.py"
    try:
        py_compile.compile(str(path), doraise=True)
    except py_compile.PyCompileError as exc:
        pytest.fail("main.py has syntax error: " + str(exc))


# ---------------------------------------------------------------------------
# Test 10: run の input_schema に image と command が required
# ---------------------------------------------------------------------------

def test_run_input_schema_required_fields():
    manifest = _load_manifest("run")
    schema = manifest.get("input_schema", {})
    required = schema.get("required", [])
    assert "image" in required, "image not in run input_schema.required"
    assert "command" in required, "command not in run input_schema.required"


# ---------------------------------------------------------------------------
# Test 11: exec の input_schema に container_id と command が required
# ---------------------------------------------------------------------------

def test_exec_input_schema_required_fields():
    manifest = _load_manifest("exec")
    schema = manifest.get("input_schema", {})
    required = schema.get("required", [])
    assert "container_id" in required, "container_id not in exec input_schema.required"
    assert "command" in required, "command not in exec input_schema.required"


# ---------------------------------------------------------------------------
# Test 12: 全 function の host_execution が False
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("func_name", FUNCTION_NAMES)
def test_host_execution_is_false(func_name):
    manifest = _load_manifest(func_name)
    assert manifest.get("host_execution") is False, (
        "host_execution should be False for core function " + func_name
    )


# ---------------------------------------------------------------------------
# Test 13: 全 function に tags が設定されている
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("func_name", FUNCTION_NAMES)
def test_tags_are_set(func_name):
    manifest = _load_manifest(func_name)
    tags = manifest.get("tags")
    assert isinstance(tags, list) and len(tags) > 0, (
        "tags should be a non-empty list for " + func_name
    )


# ---------------------------------------------------------------------------
# Test 14: FunctionRegistry への登録テスト（モック）
# ---------------------------------------------------------------------------

def test_function_registry_mock_registration():
    """The v4 catalog, not a process-global FunctionRegistry, owns functions."""
    from ecosystem.defaultspack.domain.runtime_v4 import BundledCatalog
    from tests.legacy_authority_contracts import assert_retired_module_absent

    catalog = BundledCatalog.load(
        Path(__file__).resolve().parents[1] / "ecosystem" / "defaultspack" / "v4"
    )
    assert catalog.packs
    assert all(
        manifest["functions"]
        for manifest in catalog.packs.values()
        if manifest["pack"]["kind"] not in {"base", "shell"}
    )
    assert_retired_module_absent("core_runtime.function_registry")


# ---------------------------------------------------------------------------
# Test 15: pack_validator で validation error が出ない
# ---------------------------------------------------------------------------

def test_pack_validator_no_errors():
    """
    pack_validator の _validate_functions() を直接呼んで
    errors が空であることを確認する。
    """
    project_root = Path(__file__).resolve().parent.parent
    if str(project_root) not in sys.path:
        sys.path.insert(0, str(project_root))

    pack_subdir = (
        Path(__file__).resolve().parent.parent
        / "core_runtime"
        / "core_pack"
        / "core_docker_capability"
    )

    from core_runtime.pack_validator import _validate_functions

    warnings, errors = _validate_functions(pack_subdir, "core_docker_capability")
    assert errors == [], "pack_validator reported errors: " + str(errors)


# ---------------------------------------------------------------------------
# Test 16: caller_requires は全 function で list 型
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("func_name", FUNCTION_NAMES)
def test_caller_requires_is_list(func_name):
    manifest = _load_manifest(func_name)
    cr = manifest.get("caller_requires")
    assert isinstance(cr, list), "caller_requires should be a list for " + func_name


# ---------------------------------------------------------------------------
# Test 17: run の grant_config に必須キーが含まれる
# ---------------------------------------------------------------------------

def test_run_grant_config_keys():
    manifest = _load_manifest("run")
    gc = manifest["grant_config"]
    expected_keys = [
        "allowed_images", "max_memory", "max_cpus",
        "max_pids", "network_allowed", "max_containers", "max_execution_time",
    ]
    for key in expected_keys:
        assert key in gc, "grant_config missing key: " + key
