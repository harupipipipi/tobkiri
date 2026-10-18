from __future__ import annotations

import ast
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
CORE_RUNTIME_ROOT = ROOT / "core_runtime"
DEFAULTSPACK_ROOT = ROOT / "ecosystem" / "defaultspack"


def test_application_domain_modules_are_absent_from_core_runtime() -> None:
    retired = {
        "chat_session_manager.py",
        "defaultspack_host_contract_adapter.py",
        "frontend_host.py",
        "prompt_builder.py",
        "supervisor_dashboard.py",
    }
    core_names = {path.name for path in CORE_RUNTIME_ROOT.glob("*.py")}

    assert retired.isdisjoint(core_names)
    assert not any(name.startswith("ai_input_") for name in core_names)


def test_core_runtime_python_files_do_not_directly_import_pack_domains() -> None:
    for module_path in sorted(CORE_RUNTIME_ROOT.rglob("*.py")):
        tree = ast.parse(module_path.read_text(encoding="utf-8"))
        forbidden: list[str] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                forbidden.extend(
                    alias.name
                    for alias in node.names
                    if alias.name == "domain"
                    or alias.name.startswith("domain.")
                    or alias.name == "ecosystem.defaultspack"
                    or alias.name.startswith("ecosystem.defaultspack.")
                )
            elif isinstance(node, ast.ImportFrom):
                module = node.module or ""
                if (
                    module == "domain"
                    or module.startswith("domain.")
                    or module == "ecosystem.defaultspack"
                    or module.startswith("ecosystem.defaultspack.")
                ):
                    forbidden.append(module)

        assert forbidden == [], str(module_path.relative_to(ROOT))


def test_prompt_and_chat_implementations_are_pack_owned() -> None:
    prompt = (DEFAULTSPACK_ROOT / "domain" / "prompt" / "builder.py").read_text(
        encoding="utf-8"
    )
    chat = (DEFAULTSPACK_ROOT / "domain" / "chat" / "session_manager.py").read_text(
        encoding="utf-8"
    )

    assert "class PromptBuilder" in prompt
    assert "def evaluate_condition" in prompt
    assert "core_runtime.prompt_builder" not in prompt
    assert "class SessionManager" in chat
    assert "def create_session" in chat
    assert "core_runtime.chat_session_manager" not in chat
