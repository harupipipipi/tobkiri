from __future__ import annotations

import json
import hashlib
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULTSPACK_ROOT = ROOT / "ecosystem" / "defaultspack"

sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(DEFAULTSPACK_ROOT))

from domain.function_runtime.manifest_factory import FUNCTION_SPECS_BY_ID, manifest_for  # noqa: E402
from domain.function_runtime.registry import TOOL_FUNCTION_ACTIONS  # noqa: E402
from domain.function_runtime.security import HIGH_RISK_CALLER_REQUIREMENT  # noqa: E402


AMBIENT_CUSTOM_WRAPPER_FUNCTIONS = frozenset({"ambient_monitor_start"})
FACTORY_OWNED_MANIFEST_KEYS = {
    "function_id",
    "description",
    "tags",
    "risk",
    "requires",
    "caller_requires",
    "host_execution",
    "calling_convention",
    "entrypoint",
    "extensions",
}


def _manifest(function_id: str) -> dict:
    path = DEFAULTSPACK_ROOT / "functions" / function_id / "manifest.json"
    return json.loads(path.read_text(encoding="utf-8"))


def test_generated_defaultspack_function_manifests_are_valid():
    aliases: dict[str, str] = {}
    for function_id in FUNCTION_SPECS_BY_ID:
        function_dir = DEFAULTSPACK_ROOT / "functions" / function_id
        manifest_path = function_dir / "manifest.json"
        main_path = function_dir / "main.py"

        assert manifest_path.is_file(), function_id
        assert main_path.is_file(), function_id

        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        assert manifest["function_id"] == function_id
        assert re.match(r"^[a-z][a-z0-9_]*$", function_id)
        assert manifest["calling_convention"] == "subprocess"
        assert manifest["entrypoint"] == "main.py:run"

        vocab_aliases = manifest.get("vocab_aliases") or []
        assert any(alias.startswith("defaultspack.") for alias in vocab_aliases), function_id
        for alias in vocab_aliases:
            assert alias not in aliases, alias
            aliases[alias] = function_id


def _generated_main_template(function_id: str) -> str:
    return f'''from __future__ import annotations

import sys
from pathlib import Path

PACK_ROOT = Path(__file__).resolve().parents[2]
RUMI_ROOT = PACK_ROOT.parents[1]
for path in (str(PACK_ROOT), str(RUMI_ROOT)):
    if path not in sys.path:
        sys.path.insert(0, path)

from domain.function_runtime.dispatcher import run_defaultspack_function


def run(context, args):
    return run_defaultspack_function("{function_id}", args, context)
'''


def test_ambient_function_manifests_and_wrappers_follow_factory_specs():
    for function_id, spec in FUNCTION_SPECS_BY_ID.items():
        if not function_id.startswith("ambient_"):
            continue
        committed = _manifest(function_id)
        generated = manifest_for(spec)
        for key in FACTORY_OWNED_MANIFEST_KEYS:
            assert committed.get(key) == generated.get(key), f"{function_id}:{key}"
        main_path = DEFAULTSPACK_ROOT / "functions" / function_id / "main.py"
        if function_id in AMBIENT_CUSTOM_WRAPPER_FUNCTIONS:
            assert main_path.read_text(encoding="utf-8") != _generated_main_template(function_id)
        else:
            assert main_path.read_text(encoding="utf-8") == _generated_main_template(function_id)


def test_adaptive_function_manifests_and_wrappers_follow_factory_specs():
    for function_id, spec in FUNCTION_SPECS_BY_ID.items():
        if not function_id.startswith("adaptive_"):
            continue
        committed = _manifest(function_id)
        generated = manifest_for(spec)
        for key in FACTORY_OWNED_MANIFEST_KEYS | {"input_schema", "output_schema"}:
            assert committed.get(key) == generated.get(key), f"{function_id}:{key}"
        main_path = DEFAULTSPACK_ROOT / "functions" / function_id / "main.py"
        assert main_path.read_text(encoding="utf-8") == _generated_main_template(function_id)


def test_high_risk_functions_declare_caller_requirements():
    high_risk = [
        manifest
        for manifest in (_manifest(function_id) for function_id in FUNCTION_SPECS_BY_ID)
        if manifest.get("risk") == "high"
    ]
    assert high_risk
    for manifest in high_risk:
        assert manifest.get("requires"), manifest["function_id"]
        assert HIGH_RISK_CALLER_REQUIREMENT in manifest.get("caller_requires", []), manifest["function_id"]


def test_v4_defaultspack_catalog_pins_the_real_conversation_implementation():
    pack = json.loads((DEFAULTSPACK_ROOT / "pack.v4.json").read_text(encoding="utf-8"))
    executable = json.loads(
        (DEFAULTSPACK_ROOT / "executables.v4.json").read_text(encoding="utf-8")
    )
    implementation = DEFAULTSPACK_ROOT / "runtime" / "conversation.py"
    expected_digest = "sha256:" + hashlib.sha256(implementation.read_bytes()).hexdigest()

    assert pack["pack"]["id"] == "defaultspack"
    assert [item["id"] for item in pack["functions"]] == [
        "defaultspack.conversation"
    ]
    assert executable["variants"][0]["function_id"] == "defaultspack.conversation"
    assert executable["variants"][0]["implementation_path"] == "runtime/conversation.py"
    assert executable["variants"][0]["implementation_digest"] == expected_digest


def test_browser_screenshot_alias_uses_implemented_screenshot_action():
    assert TOOL_FUNCTION_ACTIONS["browser_screenshot"] == (
        "browser_computer",
        {"action": "computer.screenshot"},
    )
