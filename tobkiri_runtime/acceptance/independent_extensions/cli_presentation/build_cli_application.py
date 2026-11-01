"""Build a distinct source Application, finite map, and non-native CLI archive."""
from __future__ import annotations

import hashlib
import argparse
import importlib.util
import json
from pathlib import Path
import shutil
import tempfile
import zipfile

from core_runtime.pack_authoring import PythonPackFunction, build_python_pack
from core_runtime.profile_authoring import build_named_profile
from tobkiri_protocol.canonical import canonical_digest
from tobkiri_host.artifact_compiler import compile_pack_root

ROOT = Path(__file__).parent
RUNTIME = ROOT.parents[2]
APPLICATION = ROOT / 'acceptance.cli.application'


def dumps(value: object) -> bytes:
    """Produce deterministic UTF-8 source resource bytes."""
    return (json.dumps(value, sort_keys=True, indent=2) + '\n').encode()


def build_application(destination: Path = APPLICATION) -> dict:
    """Publish only compiler-validated Application source, never native authority."""
    destination.resolve().relative_to(ROOT.resolve())
    if destination.is_symlink():
        raise ValueError('source output symlink denied')
    renderer = json.loads((ROOT / 'acceptance.cli.presentation/contracts.v4.json').read_text())['contracts'][0]
    operation = renderer['operations'][0]
    input_schema = renderer['schema_catalog'][operation['input_schema_digest']]
    app_id = 'acceptance.cli.application'
    logical_path = '/api/transcript/render'
    commands = {'transcript.render': {'namespace': app_id, 'path': logical_path,
                                      'input_schema': input_schema}}
    target = {'contribution_id': 'acceptance.cli.transcript',
              'contract_id': renderer['contract_id'],
              'operation_id': operation['operation_id'],
              'provider_id': 'acceptance.cli.presentation.render',
              'function_id': 'acceptance.cli.presentation.render',
              'allowed_payload_keys': sorted(input_schema['properties'])}
    frontend_map = {'schema': 'io.tobkiri.frontend-contract-map.v4', 'pack_id': app_id,
                    'routes': [{'method': 'POST', 'path': logical_path,
                                'presentation': 'broker_result', 'targets': [target]}]}
    source = (ROOT / 'source/application.py').read_bytes()
    inp = {'type': 'object', 'additionalProperties': False, 'properties': {}}
    out = {'type': 'object', 'additionalProperties': False,
           'required': ['presentation', 'command_id'], 'properties': {
               'presentation': {'const': 'terminal_stdio'},
               'command_id': {'const': 'transcript.render'}}}
    error = {'type': 'object', 'additionalProperties': False,
             'required': ['error'], 'properties': {'error': {'type': 'string'}}}
    contract = {'contract_api_version': 'io.tobkiri.contract.v4',
                'contract_id': 'acceptance.cli.application.v1', 'version': '1.0.0',
                'owner': app_id, 'status': 'draft',
                'operations': [{'operation_id': 'launch',
                    'input_schema_digest': canonical_digest(inp),
                    'output_schema_digest': canonical_digest(out),
                    'error_schema_digest': canonical_digest(error),
                    'effect_ceiling': ['pure'], 'scope_semantics': 'declarative',
                    'idempotency': {'mode': 'replayable'}}],
                'schema_catalog': {canonical_digest(s): s for s in (inp, out, error)},
                'provider_semantics': {'provider_id': app_id, 'cardinality': 'one',
                    'security': 'public', 'failure': 'fail_closed', 'isolation': 'sandbox',
                    'required_capabilities': [], 'lifecycle': {}},
                'provenance': {'schema': 'io.tobkiri.provenance.v1',
                    'source_kind': 'repository', 'source_path': 'source/application.py',
                    'source_digest': 'sha256:' + hashlib.sha256(source).hexdigest(),
                    'repository_commit': 'working-tree',
                    'repository_tree': hashlib.sha256(source).hexdigest(),
                    'generator': 'acceptance.cli.application.author',
                    'generator_version': '1.0.0', 'normative': False, 'evidence': []}}
    contract['revision_digest'] = canonical_digest({
        k: v for k, v in contract.items() if k != 'provenance'})
    assets = {'frontend_contract_map.v4.json': dumps(frontend_map),
              'frontend/cli_frontend/commands.json': dumps(commands),
              'frontend/__main__.py': b'from cli_frontend.frontend import main\nraise SystemExit(main())\n'}
    for name in ('__init__.py', 'frontend.py'):
        assets['frontend/cli_frontend/' + name] = (ROOT / 'source/cli_frontend' / name).read_bytes()
    with tempfile.TemporaryDirectory(dir=ROOT, prefix='application-build-') as temporary:
        fresh = Path(temporary) / app_id
        build_python_pack(fresh, pack_id=app_id, version='1.0.0',
                          display_name='Tobkiri independent CLI Application source',
                          contracts=[contract], functions=[PythonPackFunction(
                              function_id=app_id, contract_id=contract['contract_id'],
                              operation_ids=('launch',), implementation_path='runtime/application.py',
                              source=source)], assets=assets, application=True)
        compile_pack_root(fresh)
        if destination.exists():
            shutil.rmtree(destination)
        shutil.copytree(fresh, destination)
    return {'application_id': app_id, 'commands': commands, 'frontend_map': frontend_map}


def build_source_archive(output: Path) -> str:
    """Archive captured frontend assets deterministically; no interpreter is bundled."""
    output.resolve().relative_to(ROOT.resolve())
    with zipfile.ZipFile(output, 'w', compression=zipfile.ZIP_STORED) as archive:
        for file in sorted((APPLICATION / 'frontend').rglob('*')):
            if file.is_file():
                info = zipfile.ZipInfo(str(file.relative_to(APPLICATION / 'frontend')),
                                       date_time=(1980, 1, 1, 0, 0, 0))
                info.external_attr = 0o100644 << 16
                archive.writestr(info, file.read_bytes())
    return 'sha256:' + hashlib.sha256(output.read_bytes()).hexdigest()


def compile_cli_profile(destination: Path) -> Path:
    """Select the source CLI Application and renderer with the public compiler."""
    destination.resolve().relative_to(ROOT.resolve())
    spec = importlib.util.spec_from_file_location('own_profile_author', ROOT / 'profile_authoring.py')
    author = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(author)
    bundle = RUNTIME / 'ecosystem/defaultspack/v4'
    intent = author.make_profile_intent(
        profile_id='independent.cli.transcript', display_name='Tobkiri independent CLI transcript',
        base_definition=json.loads((bundle / 'defaults-basepack.base.v1.json').read_text()),
        shell_definition=json.loads((bundle / 'shell.cli.default.shell.v1.json').read_text()),
        application_manifest=json.loads((APPLICATION / 'pack.v4.json').read_text()),
        selected_pack_manifests=[json.loads((ROOT / 'acceptance.cli.presentation/pack.v4.json').read_text())])
    intent['requested_edges'] = [{'caller_function_id': 'shell.cli.default',
        'target_provider_id': 'acceptance.cli.presentation.render',
        'contract_id': 'acceptance.presentation.transcript.v1',
        'operation_id': 'acceptance.presentation.render', 'requested_scope_template': {}}]
    (ROOT / 'independent.cli.transcript.profile.intent.v1.json').write_bytes(dumps(intent))
    return build_named_profile(destination, template_bundle=bundle, intent=intent,
                               additional_pack_roots=(APPLICATION, ROOT / 'acceptance.cli.presentation'))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--compile-profile', type=Path,
                        help='New source release directory inside owned namespace')
    arguments = parser.parse_args()
    build_application()
    print(build_source_archive(ROOT / 'cli-frontend-source.pyz'))
    if arguments.compile_profile is not None:
        compile_cli_profile(arguments.compile_profile)

