"""Build source declarations; no install, approval, or activation occurs."""
from __future__ import annotations

import hashlib
import json
import shutil
import tempfile
from pathlib import Path

from core_runtime.pack_authoring import PythonPackFunction, build_python_pack
from tobkiri_protocol.canonical import canonical_digest
from tobkiri_host.artifact_compiler import compile_pack_root

ROOT = Path(__file__).parent
PACK = ROOT / 'acceptance.cli.presentation'


def canonical(value: object) -> str:
    """Use the public protocol canonical digest rule."""
    return canonical_digest(value)


def write(path: Path, value: object) -> None:
    """Write readable, reproducible authoring JSON."""
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + '\n')


def build(
    pack_id: str = 'acceptance.cli.presentation',
    contract_id: str = 'acceptance.presentation.transcript.v1',
    function_id: str = 'acceptance.cli.presentation.render',
    operation_id: str = 'acceptance.presentation.render',
    destination: Path = PACK,
) -> dict:
    """Render a closed pure presentation contract and unresolved profile intent."""
    if destination.is_symlink():
        raise ValueError('output symlink denied')
    destination.resolve().relative_to(ROOT.resolve())
    pid, cid, fid, op = pack_id, contract_id, function_id, operation_id
    source = 'sha256:' + hashlib.sha256((ROOT / 'source/render.py').read_bytes()).hexdigest()
    provenance = {'schema': 'io.tobkiri.provenance.v1', 'source_kind': 'repository',
                  'source_path': 'source/render.py', 'source_digest': source,
                  'repository_commit': 'working-tree', 'repository_tree': source[7:],
                  'generator': 'acceptance.cli.presentation.authoring',
                  'generator_version': '1.0.0', 'normative': False, 'evidence': []}
    item = {'type': 'object', 'additionalProperties': False,
            'required': ['role', 'text'], 'properties': {
                'role': {'enum': ['user', 'assistant', 'system', 'tool']},
                'text': {'type': 'string', 'maxLength': 8192}}}
    inp = {'type': 'object', 'additionalProperties': False,
           'required': ['messages', 'columns', 'output_limit'], 'properties': {
               'messages': {'type': 'array', 'maxItems': 128, 'items': item},
               'columns': {'type': 'integer', 'minimum': 20, 'maximum': 200},
               'output_limit': {'type': 'integer', 'minimum': 1, 'maximum': 1048576}}}
    out = {'type': 'object', 'additionalProperties': False,
           'required': ['stdout', 'stderr', 'exit_status', 'stream'], 'properties': {
               'stdout': {'type': 'string'}, 'stderr': {'const': ''},
               'exit_status': {'const': 0}, 'stream': {'const': 'complete'}}}
    err = {'type': 'object', 'additionalProperties': False, 'required': ['error'],
           'properties': {'error': {'enum': ['invalid_input', 'output_limit']}}}
    schemas = {canonical(s): s for s in (inp, out, err)}
    operation = {'operation_id': op, 'input_schema_digest': canonical(inp),
                 'output_schema_digest': canonical(out),
                 'error_schema_digest': canonical(err), 'effect_ceiling': ['pure'],
                 'scope_semantics': 'declarative', 'idempotency': {'mode': 'replayable'},
                 'timeout_default_ms': 1000, 'timeout_hard_max_ms': 5000}
    contract = {'contract_api_version': 'io.tobkiri.contract.v4', 'contract_id': cid,
                'version': '1.0.0', 'owner': pid, 'status': 'draft',
                'operations': [operation], 'schema_catalog': schemas,
                'provider_semantics': {'provider_id': pid, 'cardinality': 'one',
                    'security': 'public', 'failure': 'fail_closed',
                    'isolation': 'sandbox', 'required_capabilities': [],
                    'lifecycle': {}}, 'provenance': provenance}
    contract['revision_digest'] = canonical({
        key: value for key, value in contract.items() if key != 'provenance'
    })
    contracts = {'catalog_api_version': 'io.tobkiri.pack-contract-catalog.v4',
                 'pack_id': pid, 'source_identity': source, 'contracts': [contract]}
    write(ROOT / 'contracts.draft.v4.json', contracts)
    write(ROOT / 'function.draft.v4.json', {
        'id': fid, 'implementation_digest': source,
        'contract_revision_digest': contract['revision_digest'],
        'operations': [op], 'role': 'pure', 'isolation': 'pack_vm'})
    intent = {'intent_api_version': 'io.tobkiri.profile-intent.v1',
        'profile_id': 'acceptance.cli', 'display_name': 'Tobkiri CLI',
        'state': 'needs_resolution', 'mode': 'interactive', 'catalog_revision': None,
        'base': {'pack_id': 'acceptance.base', 'artifact_digest': None,
                 'definition_revision': None,
                 'resolution': 'verified_exact_artifact_required'},
        'shell': {'provider_id': 'runtime.cli.shell', 'pack_id': 'runtime.cli.shell',
                  'artifact_digest': None, 'executable_artifact_digest': None,
                  'definition_revision': None, 'contract_id': 'app.shell.v1',
                  'platform': 'macos', 'architecture': 'arm64'},
        'packs': [{'pack_id': pid, 'artifact_digest': None, 'role': 'provider'},
                  {'pack_id': 'acceptance.cli.application',
                   'artifact_digest': None, 'role': 'application'}],
        'requested_edges': [{'caller_function_id': 'acceptance.cli.application.present',
                             'target_provider_id': pid, 'contract_id': cid,
                             'operation_id': op, 'requested_scope_template': {}}],
        'authority_references': [], 'profile_authority_snapshot_digest': None}
    write(ROOT / 'cli.profile.intent.v1.json', intent)
    source_bytes = (ROOT / 'source/render.py').read_bytes().replace(
        b"'acceptance.presentation.render'", repr(op).encode())
    # The producer refuses existing destinations: always render to a fresh path.
    with tempfile.TemporaryDirectory(dir=ROOT, prefix='build-') as temporary:
        fresh = Path(temporary) / pid
        build_python_pack(
            fresh, pack_id=pid, version='1.0.0',
            display_name='Tobkiri CLI transcript presentation',
            contracts=[contract], functions=[PythonPackFunction(
                function_id=fid, contract_id=cid, operation_ids=(op,),
                implementation_path='runtime/render.py', source=source_bytes)],
        )
        compiled = compile_pack_root(fresh)
        route = compiled.routes[(cid, op)]
        assert route['runtime_abi'] == 'python3.13'
        if destination.exists():
            shutil.rmtree(destination)
        shutil.copytree(fresh, destination)
    return {'pack_id': pid, 'contract_id': cid, 'operation_id': op,
            'runtime_abi': route['runtime_abi']}



if __name__ == '__main__':
    build()
