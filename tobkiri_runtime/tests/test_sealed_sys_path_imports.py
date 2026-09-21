"""Sealed-runtime import safety for the defaultspack AI client surface.

The packaged runtime replaces ``sys.path`` with a frozen guard after sealed
attestation (``tobkiri_sealed.bootstrap._SealedSysPath``).  Any
``sys.path.insert``/``append`` executed while a lazily imported pack module
loads then raises ``SealedBootstrapError`` and surfaces to the caller as
``ProviderExecutionError`` (HTTP 503) — the model-search defect this test
regresses.  Modules must therefore import cleanly without mutating
``sys.path``; every root they need is already attested into the sealed
environment (``app`` and ``app/ecosystem/defaultspack``).
"""

from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
DEFAULTSPACK_ROOT = ROOT / "ecosystem" / "defaultspack"
for _root in (str(ROOT), str(DEFAULTSPACK_ROOT)):
    if _root not in sys.path:
        sys.path.insert(0, _root)


class SealedSysPathMutation(AssertionError):
    """Raised by the test guard when a module mutates the frozen sys.path."""


class _FrozenSysPath(list):
    """Mirror of the sealed bootstrap's post-attestation ``sys.path`` guard."""

    @staticmethod
    def _blocked(*_args, **_kwargs):
        raise SealedSysPathMutation("sealed sys.path changed after attestation")

    insert = _blocked
    append = _blocked
    extend = _blocked
    pop = _blocked
    remove = _blocked
    clear = _blocked
    sort = _blocked
    reverse = _blocked
    __setitem__ = _blocked
    __delitem__ = _blocked
    __iadd__ = _blocked
    __imul__ = _blocked


@pytest.fixture()
def frozen_sys_path():
    """Install the frozen sys.path guard for one test, then restore it."""
    original = sys.path
    sys.path = _FrozenSysPath(original)
    try:
        yield sys.path
    finally:
        sys.path = original


def _purge(prefixes: tuple[str, ...]) -> None:
    """Drop cached modules so their top-level code re-executes on import."""
    for name in [name for name in sys.modules if name.startswith(prefixes)]:
        sys.modules.pop(name, None)


# Modules reachable from the model-search / provider-dispatch import chain in
# the sealed defaultspack role.  Each is exercised under the frozen guard.
_SEALED_IMPORTABLE_MODULES = (
    "ecosystem.defaultspack.domain.ai_client.model_search",
    "ecosystem.defaultspack.backend.ai_client.provider_catalog",
    "ecosystem.defaultspack.domain.ai_client.providers",
    "ecosystem.defaultspack.domain.ai_client.providers.openai_provider",
    "ecosystem.defaultspack.domain.ai_client.providers.anthropic_provider",
    "ecosystem.defaultspack.domain.ai_client.providers.genspark_provider",
    "ecosystem.defaultspack.domain.ai_client.providers.rumi_provider",
    "ecosystem.defaultspack.domain.ai_client.providers.stub_provider",
    "ecosystem.defaultspack.domain.ai_client.client",
    "ecosystem.defaultspack.domain.ai_client.pipeline",
    "ecosystem.defaultspack.domain.ai_client.router",
    "ecosystem.defaultspack.domain.ai_client.parallel",
    "ecosystem.defaultspack.domain.ai_client.evaluator",
    "ecosystem.defaultspack.domain.ai_client.model_router",
    "ecosystem.defaultspack.domain.ai_client.model_profiles",
    "ecosystem.defaultspack.domain.ai_client.task_analyzer",
    "ecosystem.defaultspack.blocks.chat._prompt_helpers",
)


@pytest.mark.parametrize("module_name", _SEALED_IMPORTABLE_MODULES)
def test_pack_module_imports_without_sys_path_mutation(
    frozen_sys_path, module_name: str
) -> None:
    """Each module must import under the sealed frozen sys.path guard."""
    _purge(
        (
            "ecosystem.defaultspack.domain.ai_client",
            "ecosystem.defaultspack.backend.ai_client",
            "ecosystem.defaultspack.blocks.chat._prompt_helpers",
            "domain.ai_client",
            "blocks.chat._prompt_helpers",
        )
    )
    importlib.import_module(module_name)


def test_model_search_dispatch_chain_imports_under_frozen_path(
    frozen_sys_path,
) -> None:
    """The exact sealed crash chain: provider_catalog → registry → providers."""
    _purge(
        (
            "ecosystem.defaultspack.domain.ai_client",
            "ecosystem.defaultspack.backend.ai_client",
            "domain.ai_client",
        )
    )
    # Mirrors model_search.py's lazy import in the sealed traceback.
    from ecosystem.defaultspack.backend.ai_client.provider_catalog import (
        get_provider_catalog,
    )

    assert callable(get_provider_catalog)


def test_openai_provider_public_surface(frozen_sys_path) -> None:
    """The provider module keeps its contract after the sys.path removal."""
    _purge(("ecosystem.defaultspack.domain.ai_client", "domain.ai_client"))
    from ecosystem.defaultspack.domain.ai_client.base_provider import (
        BaseProvider,
    )
    from ecosystem.defaultspack.domain.ai_client.providers.openai_provider import (
        OpenAIProvider,
    )

    assert issubclass(OpenAIProvider, BaseProvider)
    provider = OpenAIProvider(api_key="")
    # No key -> no network: the live inventory surface returns empty.
    assert provider.list_models() == []
    request = provider.build_request([{"role": "user", "content": "hi"}])
    assert request == [{"role": "user", "content": "hi"}]
    parsed = provider.parse_response(
        {
            "choices": [
                {"message": {"content": "ok"}, "finish_reason": "stop"}
            ],
            "usage": {
                "prompt_tokens": 1,
                "completion_tokens": 2,
                "total_tokens": 3,
            },
        }
    )
    assert parsed["content"] == [{"type": "text", "text": "ok"}]
    assert parsed["usage"]["total_tokens"] == 3


def test_stub_provider_remains_invocable(frozen_sys_path) -> None:
    """The local-first stub provider still answers without credentials."""
    _purge(("ecosystem.defaultspack.domain.ai_client", "domain.ai_client"))
    from ecosystem.defaultspack.domain.ai_client.providers.stub_provider import (
        StubProvider,
    )

    response = StubProvider().complete(
        "default", [{"role": "user", "content": "ping"}], [], {}
    )
    assert response["content"][0]["text"]
    stream = list(
        StubProvider().stream(
            "default", [{"role": "user", "content": "ping"}], [], {}
        )
    )
    assert stream[-1]["type"] == "stream_end"
