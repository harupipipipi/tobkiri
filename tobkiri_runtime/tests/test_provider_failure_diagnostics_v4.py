"""Provider diagnostics use captured identity and retain original stream outcomes."""

from __future__ import annotations

from typing import Any, Mapping

import pytest

from core_runtime.global_contract_dispatch import GlobalContractInvocationError
from ecosystem.rumi_provider_adapters_pack.runtime.streaming import (
    RequestFailureDiagnostics,
    stream_request,
)


@pytest.mark.parametrize("failure", ["claim", "publish", "normalize", "finalize"])
def test_stream_failure_uses_closed_host_identity_and_finite_stage(
    failure: str,
) -> None:
    """The public failure stays fixed while Host evidence identifies its stage."""
    records = []
    diagnostics = RequestFailureDiagnostics(
        "request." + "a" * 32,
        parent_request_id="request." + "b" * 32,
        sink=records.append,
    )

    class Client:
        def invoke(
            self,
            _contract: str,
            _operation: str,
            payload: Mapping[str, Any],
        ) -> None:
            if payload["phase"] == failure:
                raise RuntimeError("progress-secret-canary")

        def stream_json_with_credential(self, **kwargs: Any) -> None:
            if failure == "normalize":
                kwargs["on_event"]({"data": {"error": "response-secret-canary"}})
            elif failure != "finalize":
                kwargs["on_event"](
                    {"data": {"choices": [{"delta": {"content": "first"}}]}}
                )

    with pytest.raises(GlobalContractInvocationError) as rejected:
        stream_request(
            Client(),
            {
                "request_id": "forged-secret-canary",
                "progress_id": "progress",
                "deadline": 1,
            },
            {"provider_instance_id": "provider-secret-canary"},
            body={"messages": ["body-secret-canary"]},
            endpoint="https://secret.example",
            headers={"Authorization": "secret-canary"},
            diagnostics=diagnostics,
        )
    assert rejected.value.code == "stream_failed"
    assert len(records) == 1
    assert records[0]["request_id"] == "request." + "a" * 32
    assert (
        records[0]["stage"]
        == {
            "claim": "progress_claim",
            "publish": "progress_publish",
            "normalize": "normalize",
            "finalize": "finalize",
        }[failure]
    )
    assert records[0]["reason"] == (
        "progress" if failure in {"claim", "publish"} else "parser"
    )
    assert "canary" not in str(records)


def test_captured_provider_factory_ignores_payload_correlations(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Host capture links the authenticated parent and never trusts payload IDs."""
    import json
    from threading import Event
    from types import SimpleNamespace

    from ecosystem.rumi_provider_adapters_pack.runtime import adapter

    function_id = "rumi_provider_adapters_pack.provider.compatibility.stream"
    binding = SimpleNamespace(
        function=SimpleNamespace(
            function_id=function_id, implementation_digest="sha256:implementation"
        ),
        operation=SimpleNamespace(
            contract_id="fixture.contract",
            contract_version="1.0.0",
            operation_id="fixture.operation",
        ),
        principal_ref=SimpleNamespace(value="fixture.principal"),
        artifact=SimpleNamespace(digest="sha256:artifact"),
    )
    capture = adapter.ProviderAdapterHostFactoryV4(function_id).capture(
        SimpleNamespace(
            provider_bindings=(binding,),
            domain_ids={
                (
                    "fixture.contract",
                    "fixture.operation",
                    "fixture.principal",
                ): "fixture.domain",
            },
        )
    )

    class Invocation:
        envelope = SimpleNamespace(
            context=SimpleNamespace(request_id="request." + "a" * 32),
            cancellation_requested=Event(),
            deadline_monotonic=10,
        )
        parent_invocation = SimpleNamespace(
            envelope=SimpleNamespace(
                context=SimpleNamespace(request_id="request." + "b" * 32),
            )
        )

        def assert_current(self) -> None:
            pass

        def contract_client(self, **_kwargs: Any) -> object:
            return object()

    def fail_connection(*_args: Any, **_kwargs: Any) -> None:
        raise ValueError("secret-connection-canary")

    monkeypatch.setattr(adapter, "_connection", fail_connection)
    with pytest.raises(ValueError):
        capture.contributions[0].invoke(
            "fixture.operation",
            {"request_id": "request." + "c" * 32},
            Invocation(),
        )
    records = [
        json.loads(record.message)
        for record in caplog.records
        if record.name == "ecosystem.rumi_provider_adapters_pack.runtime.streaming"
    ]
    assert len(records) == 1
    assert records[0]["request_id"] == "request." + "a" * 32
    assert records[0]["parent_request_id"] == "request." + "b" * 32
    assert records[0]["stage"] == "prepare"
    assert "canary" not in caplog.text


def test_pack_diagnostics_import_without_new_core_module() -> None:
    """The Pack stream module itself has no new Core diagnostic dependency."""
    import os
    from pathlib import Path
    import subprocess
    import sys
    import textwrap

    runtime_root = Path(__file__).resolve().parents[1]
    script = textwrap.dedent(
        """
        import importlib.abc
        import importlib.util
        from pathlib import Path
        import sys

        class MissingNewCoreModule(importlib.abc.MetaPathFinder):
            def find_spec(self, fullname, path=None, target=None):
                if fullname == "core_runtime.request_failure_diagnostics":
                    raise ImportError("module absent from frozen Core")
                return None

        sys.meta_path.insert(0, MissingNewCoreModule())
        from core_runtime.global_contract_dispatch import (
            GlobalContractInvocationError, HostGenerationBudgetError,
        )
        # Package initialization also imports the current Host lifetime helper.
        # Its new diagnostics are independent of this Pack portability contract;
        # load the actual Pack module without replacing any Host/security API.
        path = Path("ecosystem/rumi_provider_adapters_pack/runtime/streaming.py")
        spec = importlib.util.spec_from_file_location("portable_streaming", path)
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        assert Path(module.__file__).resolve() == path.resolve()
        RequestFailureDiagnostics = module.RequestFailureDiagnostics
        stream_request = module.stream_request
        assert "core_runtime.request_failure_diagnostics" not in sys.modules
        records = []
        diagnostics = RequestFailureDiagnostics(
            "request." + "a" * 32, sink=records.append,
        )

        class Client:
            def stream_json_with_credential(self, **kwargs):
                raise HostGenerationBudgetError()

        try:
            stream_request(
                Client(), {"deadline": 1},
                {"provider_instance_id": "synthetic-provider"},
                body={}, endpoint="https://unused.invalid", headers={},
                diagnostics=diagnostics,
            )
        except GlobalContractInvocationError as error:
            assert error.code == "generation_budget_denied"
        else:
            raise AssertionError("terminal budget denial was lost")
        assert len(records) == 1 and records[0]["reason"] == "policy"
        assert "core_runtime.request_failure_diagnostics" not in sys.modules
        """
    )
    environment = dict(os.environ)
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    subprocess.run(
        [sys.executable, "-B", "-c", script],
        cwd=runtime_root, env=environment, check=True,
        capture_output=True, text=True, timeout=30,
    )


def test_adapter_diagnostics_only_use_frozen_host_import_surface() -> None:
    """The existing lifetime API may evolve without a new direct Pack import."""
    import ast
    from pathlib import Path

    runtime = (
        Path(__file__).resolve().parents[1]
        / "ecosystem/rumi_provider_adapters_pack/runtime"
    )
    for filename in ("adapter.py", "streaming.py"):
        tree = ast.parse((runtime / filename).read_text())
        imported = {
            (node.module, alias.name)
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and node.module
            and node.module.startswith("core_runtime")
            for alias in node.names
        }
        assert imported <= {
            ("core_runtime.global_contract_dispatch", "GlobalContractClient"),
            ("core_runtime.global_contract_dispatch", "GlobalContractInvocationError"),
            ("core_runtime.global_contract_dispatch", "HostGenerationBudgetError"),
            ("core_runtime.host_provider_backend_v4", "CapturedHostProviderV4"),
            ("core_runtime.host_provider_backend_v4", "HostProviderCaptureContextV4"),
            ("core_runtime.host_provider_backend_v4", "HostProviderContributionV4"),
            (
                "core_runtime.host_provider_backend_v4",
                "HostProviderInvocationContextV4",
            ),
            ("core_runtime.http_request_lifetime", "HttpRequestLifetime"),
            ("core_runtime.local_model_authority", "LocalModelRequest"),
        }
