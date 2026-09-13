from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parent.parent
PACKS = ("defaultspack", "defaults")

SAFE_BUILTINS = {
    "None": None,
    "True": True,
    "False": False,
    "bool": bool,
    "dict": dict,
    "float": float,
    "int": int,
    "isinstance": isinstance,
    "list": list,
    "str": str,
}


def _assert_removed_defaults_module(pack: str, relative_path: str) -> bool:
    """Migrate the removed ``defaults`` copy to the Pack v4 boundary contract."""
    if pack != "defaults":
        return False

    from tempfile import TemporaryDirectory

    from tests.legacy_authority_contracts import assert_profile_resolver_requires_authority_snapshot
    from tests.v4_batch_support import assert_payload_mutations_denied, harness

    assert not (ROOT / "ecosystem" / pack / relative_path).exists()
    assert_profile_resolver_requires_authority_snapshot()
    with TemporaryDirectory() as root:
        assert_payload_mutations_denied(harness(Path(root)))
    return True


def _load_pack_module(pack: str, relative_path: str):
    path = ROOT / "ecosystem" / pack / relative_path
    module_name = "_rumi_{}_{}".format(
        pack,
        relative_path.replace("/", "_").replace(".", "_"),
    )
    spec = importlib.util.spec_from_file_location(module_name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _handler_from_code(code: str):
    namespace = {"__builtins__": dict(SAFE_BUILTINS)}
    exec(code, namespace)
    return namespace["handler"]


def _generate_ai_code(monkeypatch, pack: str, content: str):
    builder = _load_pack_module(pack, "domain/tool/builder.py")

    class StubAIClient:
        def list_providers(self):
            return [{"id": "local"}]

        def list_models(self, provider):
            return [{"id": "local/model"}]

        def complete(self, model, messages):
            return {"content": content}

    monkeypatch.setattr(builder, "AIClient", StubAIClient)
    return builder.generate_handler_code_with_ai(
        "generated_tool",
        "Generated tool",
        {"type": "object", "properties": {}, "required": []},
    )


def _assert_safe_template(code: str):
    assert "# TODO: implement" not in code
    handler = _handler_from_code(code)
    result = handler({}, {})
    assert result["is_error"] is True
    assert result["widget"]["code"] == "NOT_IMPLEMENTED"


@pytest.mark.parametrize("pack", PACKS)
def test_builder_fallback_template_validates_schema_then_fails_closed(pack):
    if _assert_removed_defaults_module(pack, "domain/tool/builder.py"):
        return
    builder = _load_pack_module(pack, "domain/tool/builder.py")
    schema = {
        "type": "object",
        "properties": {
            "path": {"type": "string"},
            "count": {"type": "integer"},
            "mode": {"type": "string", "enum": ["fast", "safe"]},
        },
        "required": ["path"],
    }

    code = builder.generate_skeleton("demo_tool", "Demo tool", schema)

    assert "# TODO: implement" not in code
    assert "executed successfully" not in code
    handler = _handler_from_code(code)

    missing = handler({}, {})
    assert missing["is_error"] is True
    assert missing["widget"]["code"] == "INVALID_ARGUMENTS"
    assert "missing required argument 'path'" in missing["result"]

    bad_type = handler({"path": "README.md", "count": True}, {})
    assert bad_type["is_error"] is True
    assert bad_type["widget"]["code"] == "INVALID_ARGUMENTS"
    assert "count" in bad_type["result"]

    unimplemented = handler({"path": "README.md", "count": 1, "mode": "safe"}, {})
    assert unimplemented["is_error"] is True
    assert unimplemented["widget"]["code"] == "NOT_IMPLEMENTED"
    assert unimplemented["widget"]["details"]["schema_validated"] is True


@pytest.mark.parametrize("pack", PACKS)
def test_builder_ai_unavailable_returns_safe_template(monkeypatch, pack):
    if _assert_removed_defaults_module(pack, "domain/tool/builder.py"):
        return
    builder = _load_pack_module(pack, "domain/tool/builder.py")

    class StubOnlyAIClient:
        def list_providers(self):
            return [{"id": "stub"}]

    monkeypatch.setattr(builder, "AIClient", StubOnlyAIClient)

    code = builder.generate_handler_code_with_ai(
        "offline_tool",
        "Offline tool",
        {"type": "object", "properties": {}, "required": []},
    )

    assert "# TODO: implement" not in code
    handler = _handler_from_code(code)
    result = handler({}, {})
    assert result["is_error"] is True
    assert result["widget"]["code"] == "NOT_IMPLEMENTED"


@pytest.mark.parametrize("pack", PACKS)
def test_builder_invalid_ai_output_returns_safe_template(monkeypatch, pack):
    if _assert_removed_defaults_module(pack, "domain/tool/builder.py"):
        return
    builder = _load_pack_module(pack, "domain/tool/builder.py")

    class InvalidAIClient:
        def list_providers(self):
            return [{"id": "local"}]

        def list_models(self, provider):
            return [{"id": "local/model"}]

        def complete(self, model, messages):
            return {"content": "```python\ndef handler(arguments, context):\n    return {\n```"}

    monkeypatch.setattr(builder, "AIClient", InvalidAIClient)

    code = builder.generate_handler_code_with_ai(
        "invalid_ai_tool",
        "Invalid AI tool",
        {"type": "object", "properties": {}, "required": []},
    )

    assert "# TODO: implement" not in code
    handler = _handler_from_code(code)
    result = handler({}, {})
    assert result["is_error"] is True
    assert result["widget"]["code"] == "NOT_IMPLEMENTED"


@pytest.mark.parametrize("pack", PACKS)
def test_builder_valid_fenced_ai_output_is_used(monkeypatch, pack):
    if _assert_removed_defaults_module(pack, "domain/tool/builder.py"):
        return
    builder = _load_pack_module(pack, "domain/tool/builder.py")
    handler_code = (
        "def handler(arguments, context):\n"
        "    return {\"result\": \"ok\", \"is_error\": False, \"widget\": None}\n"
    )

    class ValidAIClient:
        def list_providers(self):
            return [{"id": "local"}]

        def list_models(self, provider):
            return [{"id": "local/model"}]

        def complete(self, model, messages):
            return {"content": "Here is the handler:\n```python\n{}```".format(handler_code)}

    monkeypatch.setattr(builder, "AIClient", ValidAIClient)

    code = builder.generate_handler_code_with_ai(
        "valid_ai_tool",
        "Valid AI tool",
        {"type": "object", "properties": {}, "required": []},
    )

    assert code == handler_code.strip()
    handler = _handler_from_code(code)
    assert handler({}, {}) == {"result": "ok", "is_error": False, "widget": None}


@pytest.mark.parametrize(
    "content",
    [
        "# def handler(arguments, context):\nvalue = 1\n",
        "def handler_wrong(arguments, context):\n    return {\"result\": \"ok\", \"is_error\": False, \"widget\": None}\n",
        "def handler(arguments):\n    return {\"result\": \"ok\", \"is_error\": False, \"widget\": None}\n",
        "def handler(arguments, context, extra):\n    return {\"result\": \"ok\", \"is_error\": False, \"widget\": None}\n",
        "def handler(*args):\n    return {\"result\": \"ok\", \"is_error\": False, \"widget\": None}\n",
    ],
)
@pytest.mark.parametrize("pack", PACKS)
def test_builder_rejects_ai_output_without_exact_handler(monkeypatch, pack, content):
    if _assert_removed_defaults_module(pack, "domain/tool/builder.py"):
        return
    code = _generate_ai_code(monkeypatch, pack, content)

    assert code != content.strip()
    _assert_safe_template(code)


@pytest.mark.parametrize("pack", PACKS)
def test_builder_rejects_ai_output_with_top_level_side_effects(monkeypatch, pack):
    if _assert_removed_defaults_module(pack, "domain/tool/builder.py"):
        return
    content = (
        "raise RuntimeError('top-level code must not run')\n"
        "def handler(arguments, context):\n"
        "    return {\"result\": \"ok\", \"is_error\": False, \"widget\": None}\n"
    )

    code = _generate_ai_code(monkeypatch, pack, content)

    assert "top-level code must not run" not in code
    _assert_safe_template(code)


@pytest.mark.parametrize(
    "content",
    [
        (
            "def handler(arguments: (1 / 0), context):\n"
            "    return {\"result\": \"ok\", \"is_error\": False, \"widget\": None}\n"
        ),
        (
            "def handler(arguments, context) -> (1 / 0):\n"
            "    return {\"result\": \"ok\", \"is_error\": False, \"widget\": None}\n"
        ),
        (
            "def handler(arguments: dict, context: dict):\n"
            "    return {\"result\": \"ok\", \"is_error\": False, \"widget\": None}\n"
        ),
        (
            "def handler(arguments, context):  # type: (dict, dict) -> dict\n"
            "    return {\"result\": \"ok\", \"is_error\": False, \"widget\": None}\n"
        ),
    ],
)
@pytest.mark.parametrize("pack", PACKS)
def test_builder_rejects_ai_output_with_annotations(monkeypatch, pack, content):
    if _assert_removed_defaults_module(pack, "domain/tool/builder.py"):
        return
    code = _generate_ai_code(monkeypatch, pack, content)

    assert code != content.strip()
    _assert_safe_template(code)


@pytest.mark.parametrize(
    "content",
    [
        "def handler(arguments, context):\n    continue\n",
        "def handler(arguments, context):\n    await something()\n",
        "def handler(arguments, context):\n    nonlocal missing_outer_binding\n",
    ],
)
@pytest.mark.parametrize("pack", PACKS)
def test_builder_rejects_ai_output_with_compile_invalid_body(monkeypatch, pack, content):
    if _assert_removed_defaults_module(pack, "domain/tool/builder.py"):
        return
    code = _generate_ai_code(monkeypatch, pack, content)

    assert code != content.strip()
    _assert_safe_template(code)


@pytest.mark.parametrize("pack", PACKS)
def test_container_create_fails_closed_when_docker_unavailable(monkeypatch, pack):
    if _assert_removed_defaults_module(pack, "domain/tool/container_manager.py"):
        return
    manager = _load_pack_module(pack, "domain/tool/container_manager.py")
    manager._containers.clear()
    monkeypatch.setattr(manager, "_docker_available", False)
    monkeypatch.setattr(manager, "_docker_cli_available", False)

    with pytest.raises(manager.DockerUnavailableError, match="local host execution fallback"):
        manager.create_container("demo", "ubuntu:22.04", {})

    assert manager._containers == {}


@pytest.mark.parametrize("pack", PACKS)
def test_container_legacy_local_stub_never_executes_on_host(monkeypatch, pack):
    if _assert_removed_defaults_module(pack, "domain/tool/container_manager.py"):
        return
    manager = _load_pack_module(pack, "domain/tool/container_manager.py")
    manager._containers.clear()
    info = manager.ContainerInfo("legacy-local", "demo", "ubuntu:22.04", "running-local", {})
    manager._containers[info.container_id] = info

    def fail_host_exec(*_args, **_kwargs):
        raise AssertionError("host command execution must not be used")

    monkeypatch.setattr(manager, "_run_cmd", fail_host_exec)

    with pytest.raises(manager.DockerUnavailableError, match="legacy local host execution fallback"):
        manager.exec_in_container(info.container_id, "echo should-not-run")
