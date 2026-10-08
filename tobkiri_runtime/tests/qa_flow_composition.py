"""Three independent authored Packs and a verified-byte test transport.

The transport is a subprocess fixture, not VM containment or Python 3.13 proof.
No real credentials or network are used.
"""
from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from core_runtime.native_pack_onboarding import preview_signed_pack, commit_signed_pack
from core_runtime.pack_authoring import build_authored_pack
from core_runtime.pack_signature import build_signed_manifest, sign_manifest
from tests.test_production_frontend_contract_http import _ShellPolicyPackVmBackend
from ecosystem.defaultspack.backend.sandbox.isolation.resources import packvm_guest_runner as runner
from tobkiri_host.effects import ProviderOutcome
from tobkiri_host.errors import BackendUnavailableError
from tobkiri_host.models import OpaqueAuthorityRef
from tobkiri_host.models import RuntimeEvidence

FIXTURES = Path(__file__).parent / "fixtures/flow_composition"
PACKS = {"qa.flow.source": "emit", "qa.flow.transform": "map", "qa.flow.sink": "collect"}


def admit_test_packs(root: Path) -> None:
    """Build each Pack independently and admit its exact signed artifact."""
    key = Ed25519PrivateKey.generate()
    public = root / "publisher.public.pem"
    public.write_bytes(key.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo,
    ))
    policy = root / "host-policy/publisher-trust.json"
    policy.parent.mkdir(mode=0o700)
    for pack_id in PACKS:
        output = root / "built" / pack_id
        build_authored_pack(FIXTURES / pack_id.rsplit(".", 1)[1], output)
        signed_dir = output / ".tobkiri"
        signed_dir.mkdir()
        manifest = build_signed_manifest(
            output, pack_id=pack_id, version="1.0.0",
            publisher_id="publisher.qa.flow", core_compatibility=">=0",
            contract_versions={pack_id + ".v1": "1.0.0"},
            requested_capabilities=[],
        )
        (signed_dir / "signed-pack.json").write_text(json.dumps(sign_manifest(manifest, key)))
        preview = preview_signed_pack(output, public)
        result = commit_signed_pack(
            output, public, expected_preview_digest=preview["preview_digest"],
            trust_store_path=policy,
        )
        assert result["state"] == "committed"


class CompositionChildBackend(_ShellPolicyPackVmBackend):
    """Execute only these three captured Pack artifacts in isolated children."""
    def __init__(self, root: Path):
        super().__init__()
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)
        self.targets = {}
        self.calls = []

    def supports(self, binding):
        pack = binding.artifact.pack_id
        return (pack in PACKS and binding.function.function_id == pack + ".provide"
                and binding.operation.contract_id == pack + ".v1"
                and binding.operation.operation_id == PACKS[pack])

    def materialize(self, binding, reservation_id):
        if not reservation_id or not self.supports(binding):
            raise BackendUnavailableError("QA binding unavailable")
        artifact = self._artifact_resolver(binding)
        assert artifact.artifact_digest == binding.artifact.digest
        assert artifact.implementation_digest == binding.function.implementation_digest
        contents = next(item.content for item in artifact.files if item.path == artifact.implementation_path)
        path = self.root / (binding.artifact.pack_id + ".py")
        if path.exists():
            assert path.read_bytes() == contents
        else:
            path.write_bytes(contents)
        domain = self._target_domain_resolver(binding)
        self.targets[(domain, binding.operation.contract_id, binding.operation.operation_id)] = path
        return RuntimeEvidence(
            domain_ref=OpaqueAuthorityRef(domain), executable_digest=binding.function.implementation_digest,
            backend_digest=self.status.backend_digest, authenticated_channel=True, nonce_fresh=True,
        )

    def invoke(self, request):
        key = (request.target_domain.value, request.contract_id, request.operation_id)
        path = self.targets.get(key)
        if path is None:
            raise BackendUnavailableError("QA invocation does not match materialization")
        child = subprocess.run(
            [sys.executable, "-I", "-S", runner.__file__, "--execute", str(path)],
            input=json.dumps({"contract_id": request.contract_id, "operation_id": request.operation_id,
                              "payload": dict(request.payload)}),
            text=True, capture_output=True, timeout=10, check=True,
        )
        terminal = json.loads(child.stdout)
        assert terminal["kind"] == runner.PACKVM_INVOKE_RESULT_KIND
        self.calls.append({"contract": request.contract_id, "input": dict(request.payload)})
        return ProviderOutcome(terminal["outcome"])
