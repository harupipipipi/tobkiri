from __future__ import annotations

import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
DEFAULTSPACK_ROOT = ROOT / "ecosystem" / "defaultspack"
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(DEFAULTSPACK_ROOT))

from domain.components.registry import DomainComponentRegistry, build_domain_component_roots  # noqa: E402
from domain.prompt.manager import PromptManager  # noqa: E402
from domain.prompt.resolver import PromptResolver  # noqa: E402


def test_prompt_and_template_components_are_discoverable():
    registry = DomainComponentRegistry(build_domain_component_roots(DEFAULTSPACK_ROOT))

    assert registry.get("prompts", "default_chat").id == "default_chat"
    assert registry.get("prompts", "coding").id == "coding"
    assert registry.get("prompts", "response_policy").id == "response_policy"
    assert registry.get("templates", "plain_text_prompt").id == "plain_text_prompt"


def test_prompt_resolver_reads_component_backed_prompts():
    resolver = PromptResolver()

    assert "default chat assistant" in resolver.resolve_prompt_text("default_chat")
    assert "coding assistant" in resolver.resolve_prompt_text("coding")
    assert "store only" in resolver.resolve_prompt_text("response_policy")
    assert resolver.render("coding", {}) == resolver.resolve_prompt_text("coding")


def test_prompt_resolver_reads_pack_backed_prompt_when_source_pack_is_known(monkeypatch):
    selected = frozenset({"defaultspack", "rumi_operations_company_pack"})
    monkeypatch.setattr(
        "core_runtime.resolved_profile_scope.effective_pack_ids",
        lambda: selected,
    )
    monkeypatch.setattr(
        "domain.capability.catalog.effective_pack_ids",
        lambda: selected,
    )
    monkeypatch.setattr(
        "domain.prompt.resolver.prompt_pack_is_trusted",
        lambda pack_id: str(pack_id) in {"defaultspack", "rumi_operations_company_pack"},
    )
    resolver = PromptResolver()

    content = resolver.resolve_prompt_text(
        "mimo_coding_company",
        source_pack_id="rumi_operations_company_pack",
    )

    assert content is not None
    assert "MiMo Coding Company" in content


def test_prompt_manager_lists_component_prompts_and_preserves_owner_persistence(monkeypatch):
    import domain.prompt.manager as manager_module  # noqa: E402

    records = {}

    def fake_authored_prompts(profile_id):
        assert profile_id == "test-profile"
        return [dict(record) for record in records.values()]

    def fake_write_authored_prompt(profile_id, operation, payload):
        assert profile_id == "test-profile"
        assert operation == "save"
        prompt = {
            "prompt_id": payload["prompt_id"],
            "body": payload["body"],
            "description": payload.get("description", ""),
            "variables": payload.get("variables", []),
            "metadata": payload.get("metadata", {}),
            "body_hash": "sha256:test-body",
        }
        records[prompt["prompt_id"]] = prompt
        return {"prompt": prompt}

    monkeypatch.setattr(manager_module, "authored_prompts", fake_authored_prompts)
    monkeypatch.setattr(manager_module, "write_authored_prompt", fake_write_authored_prompt)
    monkeypatch.setattr("core_runtime.profile_paths.active_profile_id", lambda: "test-profile")

    manager = PromptManager()
    created = manager.create_prompt({"name": "custom_component_test", "content": "Hello {{ name }}"})

    prompt_ids = {prompt["id"] for prompt in manager.list_prompts()}
    reloaded = PromptManager().get_prompt(created["id"])

    assert {"default_chat", "coding", "response_policy"} <= prompt_ids
    assert reloaded["body"] == "Hello {{ name }}"


def test_prompt_layer_remains_provider_and_tool_independent():
    prompt_sources = [
        DEFAULTSPACK_ROOT / "domain" / "prompt" / "component_prompts.py",
        DEFAULTSPACK_ROOT / "domain" / "prompt" / "effective.py",
        DEFAULTSPACK_ROOT / "domain" / "prompt" / "resolver.py",
        DEFAULTSPACK_ROOT / "domain" / "prompt" / "template.py",
    ]
    source = "\n".join(path.read_text(encoding="utf-8") for path in prompt_sources)

    assert "domain.tool" not in source
    assert "ai_client" not in source


def test_component_prompt_entrypoints_are_confined_to_component_directory(tmp_path):
    from domain.prompt import component_prompts  # noqa: E402

    component_dir = tmp_path / "component"
    component_dir.mkdir()
    prompt_path = component_dir / "prompt.md"
    prompt_path.write_text("safe prompt", encoding="utf-8")
    outside_path = tmp_path / "secret.txt"
    outside_path.write_text("secret", encoding="utf-8")
    manifest = {
        "source_path": str(component_dir / "manifest.json"),
        "entrypoints": {"prompt": "prompt.md"},
    }

    assert component_prompts._component_file(manifest, "prompt") == prompt_path.resolve()

    manifest["entrypoints"]["prompt"] = str(outside_path)
    assert component_prompts._component_file(manifest, "prompt") is None

    manifest["entrypoints"]["prompt"] = "../secret.txt"
    assert component_prompts._component_file(manifest, "prompt") is None


def test_component_prompt_text_ignores_unsafe_or_unreadable_entrypoints(tmp_path, monkeypatch):
    from domain.prompt import component_prompts  # noqa: E402

    component_dir = tmp_path / "component"
    component_dir.mkdir()
    outside_path = tmp_path / "secret.txt"
    outside_path.write_text("secret", encoding="utf-8")
    invalid_path = component_dir / "invalid.md"
    invalid_path.write_bytes(b"\xff\xfe")

    manifest = {
        "source_path": str(component_dir / "manifest.json"),
        "entrypoints": {"prompt": "../secret.txt"},
    }
    monkeypatch.setattr(component_prompts, "component_prompt_manifests", lambda: {"leak": manifest})

    assert component_prompts.component_prompt_text("leak") is None

    manifest["entrypoints"]["prompt"] = "invalid.md"
    assert component_prompts.component_prompt_text("leak") is None
