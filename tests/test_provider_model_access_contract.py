"""Provider execution shares pure policy contracts, never a peer Pack runtime."""

import importlib
from pathlib import Path
import subprocess
import sys

import pytest


_RUNTIME = Path(__file__).resolve().parents[1] / "tobkiri_runtime"
_ISOLATED_ADAPTER = """
import importlib.abc
import sys
sys.path.insert(0, sys.argv[1])

class RejectRegistryRuntime(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname == 'ecosystem.rumi_provider_registry_pack' or fullname.startswith(
            'ecosystem.rumi_provider_registry_pack.'
        ):
            raise ImportError('Registry private runtime is unavailable')

sys.meta_path.insert(0, RejectRegistryRuntime())
from ecosystem.rumi_provider_adapters_pack.runtime import adapter
from core_runtime.global_contract_dispatch import GlobalContractInvocationError
from tobkiri_protocol.provider_compiler.model_access import VERSION
from tobkiri_protocol.provider_compiler.native_filters import CAPABILITY_REVISION

connection = {
    'provider_instance_id': 'saved.connection', 'enabled': True,
    'adapter_id': 'openai-compatible',
    'endpoint': 'https://openrouter.ai/api/v1',
    'credential_handle': 'opaque:test-only',
    'metadata': {'catalog_provider_id': 'openrouter'},
    'model_access': {
        'version': VERSION, 'mode': 'explicit', 'model_ids': ['vendor/allowed'],
    },
}
request = {'provider_connection_id': 'saved.connection',
           'model_id': 'vendor/allowed', 'messages': []}

class FakeHost:
    def __init__(self, record):
        self.record = record
        self.transports = []

    def invoke(self, contract, operation, payload):
        assert contract == adapter.REGISTRY_CONTRACT
        assert operation in {
            adapter.REGISTRY_GENERATE_OPERATION, adapter.REGISTRY_STREAM_OPERATION,
        }
        return {'providers': [self.record]}

    def post_json_with_credential(self, **kwargs):
        self.transports.append(kwargs)
        return {'choices': [{'message': {'content': 'ok'}}], 'usage': {}}

def expect_denied(record, payload, code='denied', factory='create_generate_operation',
                  operation='generate'):
    host = FakeHost(record)
    try:
        getattr(adapter, factory)(host)(operation, payload)
    except GlobalContractInvocationError as exc:
        assert exc.code == code, (exc.code, str(exc))
    else:
        raise AssertionError('unsafe request was accepted')
    assert not host.transports
"""


def _run_isolated(script: str) -> None:
    result = subprocess.run(
        [sys.executable, "-I", "-B", "-c", _ISOLATED_ADAPTER + script, str(_RUNTIME)],
        text=True,
        capture_output=True,
        check=False,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr


def test_registry_compatibility_exports_share_public_compiler(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.syspath_prepend(str(_RUNTIME))
    for public_name, legacy_name in [
        (
            "model_access",
            "ecosystem.rumi_provider_registry_pack.runtime.model_access",
        ),
        (
            "native_filters",
            "ecosystem.rumi_provider_registry_pack.domain.ai_client."
            "provider_compiler.native_filters",
        ),
    ]:
        public = importlib.import_module(
            f"tobkiri_protocol.provider_compiler.{public_name}"
        )
        legacy = importlib.import_module(legacy_name)
        for name in legacy.__all__:
            assert getattr(legacy, name) is getattr(public, name)
    adapter = importlib.import_module(
        "ecosystem.rumi_provider_adapters_pack.runtime.adapter"
    )
    model_access = importlib.import_module(
        "tobkiri_protocol.provider_compiler.model_access"
    )
    assert (
        adapter.compile_connection_parameters
        is model_access.compile_connection_parameters
    )
    assert adapter.local_openai_endpoint.__module__ == (
        "ecosystem.rumi_provider_adapters_pack.runtime.local_endpoint"
    )


@pytest.mark.parametrize(
    "factory,operation",
    [
        ("create_generate_operation", "generate"),
        ("create_stream_operation", "stream"),
        ("create_embedding_operation", "embed"),
        ("create_image_operation", "generate"),
        ("create_audio_transcribe_operation", "transcribe"),
        ("create_audio_speech_operation", "synthesize"),
    ],
)
def test_model_denial_precedes_credentials_without_registry_runtime(
    factory: str, operation: str,
) -> None:
    _run_isolated(f"""
# Even a missing credential must not mask the model-policy denial.
connection.pop('credential_handle')
expect_denied(connection, {{**request, 'model_id': 'vendor/forbidden'}},
              factory={factory!r}, operation={operation!r})
""")


def test_native_routing_and_security_guards_without_registry_runtime() -> None:
    _run_isolated("""
native = {
    'revision': CAPABILITY_REVISION,
    'discovery': {'output_modalities': ['text']},
    'routing': {'only': ['test-provider'], 'zdr': True},
}
connection['model_access']['native_filters'] = native
host = FakeHost(connection)
assert adapter.create_generate_operation(host)('generate', request)['output'] == 'ok'
assert len(host.transports) == 1
sent = host.transports[0]
body = sent.get('body', sent.get('payload'))
assert body['model'] == 'vendor/allowed'
assert body['provider'] == {
    'only': ['test-provider'], 'zdr': True, 'allow_fallbacks': False,
}
assert 'discovery' not in body
assert sent['credential_handle'] == 'opaque:test-only'

for key in ('model', 'models', 'provider', 'route', 'extra_body', 'fallbacks',
            'provider_id', 'provider_connection_id', 'endpoint', 'credential_handle'):
    expect_denied(connection, {**request, 'parameters': {key: 'override'}},
                  'invalid_request')
expect_denied(connection, {**request, 'model_id': 'invalid\\nmodel'}, 'invalid_request')
expect_denied(connection, {**request, 'credential_handle': 'opaque:override'})
expect_denied(connection, {**request, 'endpoint': 'https://untrusted.invalid/v1'})
expect_denied({**connection, 'endpoint': 'https://untrusted.invalid/v1'},
              request, 'invalid_request')
expect_denied(connection, request, 'incompatible',
              'create_embedding_operation', 'embed')

legacy = {key: value for key, value in connection.items() if key != 'model_access'}
expect_denied({**legacy, 'allowed_models': []}, request)
expect_denied({**legacy, 'metadata': {'allowed_models': []}}, request)
expect_denied({**legacy, 'credential_handle': 'plaintext-is-not-a-handle'}, request)
expect_denied({**legacy, 'endpoint': 'http://openrouter.ai/api/v1'}, request)
local = {**legacy, 'adapter_id': 'local-openai-compatible',
         'endpoint': 'http://127.0.0.1:18080/v1'}
expect_denied(local, request)
expect_denied({**local, 'credential_handle': None,
               'endpoint': 'http://example.invalid:18080/v1'}, request)

# Dynamic all-mode accepts future IDs without a catalog or a peer Pack import.
all_models = {**legacy, 'model_access': {'version': VERSION, 'mode': 'all'}}
host = FakeHost(all_models)
adapter.create_generate_operation(host)(
    'generate', {**request, 'model_id': 'vendor/future-model'},
)
assert len(host.transports) == 1
assert not any(name == 'ecosystem.rumi_provider_registry_pack' or name.startswith(
    'ecosystem.rumi_provider_registry_pack.'
) for name in sys.modules)
""")


def test_model_policy_presentation_without_registry_runtime() -> None:
    _run_isolated("""
import importlib.util
from pathlib import Path
source = (Path(sys.argv[1]) / 'ecosystem/defaultspack/defaultspack/'
          'model_access_presentation.py')
spec = importlib.util.spec_from_file_location('isolated_model_presentation', source)
presentation = importlib.util.module_from_spec(spec)
spec.loader.exec_module(presentation)
snapshot = {'profile_id': 'test-profile', 'revision': 1, 'providers': [connection]}
assert presentation.project_model_access(
    snapshot, 'saved.connection', native_capability_confirmed=True,
) == {
    'profile_id': 'test-profile', 'provider_instance_id': 'saved.connection',
    'registry_revision': 1, 'model_access': connection['model_access'],
    'native_capability': CAPABILITY_REVISION,
}
assert presentation.project_model_access_catalog(snapshot, 'saved.connection', {
    'models': [
        {'provider_id': 'openrouter', 'provider_model_id': 'vendor/allowed'},
        {'provider_id': 'other-provider', 'provider_model_id': 'vendor/other'},
    ],
}) == {
    'profile_id': 'test-profile', 'provider_instance_id': 'saved.connection',
    'models': [{'model_id': 'vendor/allowed', 'display_name': 'vendor/allowed'}],
    'status': 'unavailable',
}
""")


def test_model_catalog_uses_public_filters_without_registry_runtime() -> None:
    _run_isolated("""
from ecosystem.rumi_model_catalog_pack.runtime import catalog
from tobkiri_protocol.provider_compiler.native_filters import discovery_url
from pathlib import Path
assert Path(catalog.discovery_url.__code__.co_filename).read_bytes() == (
    Path(discovery_url.__code__.co_filename).read_bytes()
)
assert catalog.CAPABILITY_REVISION == CAPABILITY_REVISION
assert catalog.discovery_url({
    'revision': CAPABILITY_REVISION,
    'discovery': {'output_modalities': ['text'], 'category': 'programming'},
}) == ('https://openrouter.ai/api/v1/models?'
       'output_modalities=text&category=programming')
""")
