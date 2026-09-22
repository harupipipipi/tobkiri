from __future__ import annotations

import importlib.util
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

DEFAULTS_ROOT = ROOT / "ecosystem" / "defaults"
DEFAULTSPACK_ROOT = ROOT / "ecosystem" / "defaultspack"


def _load_module(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_rumi_widgets_pack_modules_are_core_runtime_shims():
    modules = ["__init__.py", "base.py", "controls.py", "custom.py", "display.py", "layout.py", "stream.py"]
    for module in modules:
        assert not (DEFAULTS_ROOT / "lib" / "rumi_widgets" / module).exists()
        source = _read(DEFAULTSPACK_ROOT / "lib" / "rumi_widgets" / module)
        assert "core_runtime.rumi_widgets" in source
        assert "class Widget" not in source
        assert "class Button" not in source

    from tests.legacy_authority_contracts import assert_profile_resolver_requires_authority_snapshot

    assert_profile_resolver_requires_authority_snapshot()


def test_prompt_builder_and_session_manager_are_core_runtime_shims():
    for relative_path, marker in (
        ("domain/prompt/builder.py", "core_runtime.prompt_builder"),
        ("domain/chat/session_manager.py", "core_runtime.chat_session_manager"),
    ):
        assert not (DEFAULTS_ROOT / relative_path).exists()
        source = _read(DEFAULTSPACK_ROOT / relative_path)
        assert marker in source

    builder_source = _read(DEFAULTSPACK_ROOT / "domain" / "prompt" / "builder.py")
    assert "class PromptBuilder" not in builder_source
    assert "def evaluate_condition" not in builder_source
    session_source = _read(DEFAULTSPACK_ROOT / "domain" / "chat" / "session_manager.py")
    assert "def create_session" not in session_source
    assert "def add_conversation" not in session_source

    from tests.legacy_authority_contracts import assert_profile_resolver_requires_authority_snapshot

    assert_profile_resolver_requires_authority_snapshot()


def test_unified_template_modules_are_safe_core_runtime_shims():
    prompt = {
        "id": "reply_style",
        "name": "reply_style",
        "description": "Reply style",
        "body": "Use a {{tone}} tone.",
        "variables": [{"name": "tone", "type": "string", "required": True}],
        "metadata": {"handler_code": "raise RuntimeError('should stay inert')"},
    }

    assert not (DEFAULTS_ROOT / "domain" / "template" / "unified.py").exists()
    source = _read(DEFAULTSPACK_ROOT / "domain" / "template" / "unified.py")
    assert "core_runtime.template_unified" in source
    assert '"type": "dynamic"' not in source
    assert '"type": "prompt"' not in source

    module = _load_module(
        DEFAULTSPACK_ROOT / "domain" / "template" / "unified.py",
        "defaultspack_template_unified_shim",
    )
    tool = module.convert_prompt_to_tool(prompt)

    assert tool["execution"]["type"] == "rumi_function"
    assert tool["execution"]["qualified_name"] == "defaultspack:prompt_render"
    assert "handler_code" not in tool
    assert tool["metadata"]["template_facade_preview"] is True
    assert tool["metadata"]["template_body"] == prompt["body"]
    assert tool["metadata"].get("legacy_handler_code") is None

    from tests.legacy_authority_contracts import assert_profile_resolver_requires_authority_snapshot

    assert_profile_resolver_requires_authority_snapshot()
