"""Oracle for authored authority loss through scaffold regeneration.

Every mutation is in pytest's tmp_path; no probe is imported or executed.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

RUNTIME = Path(__file__).resolve().parents[1]
from core_runtime.pack_sdk import refresh_scaffold_artifacts
from scripts.generate_packvm_sandbox_qa_pack import PACK_ID, build_documents
from tobkiri_host.artifact_compiler import compile_pack_root
from tobkiri_protocol.canonical import canonical_digest
from tobkiri_protocol.validation import validate_document


def _write(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, sort_keys=True, indent=2) + '\n', encoding='utf-8')


def _digest(path: Path) -> str:
    return 'sha256:' + hashlib.sha256(path.read_bytes()).hexdigest()


def _snapshot(root: Path) -> dict[str, bytes]:
    return {p.relative_to(root).as_posix(): p.read_bytes() for p in sorted(root.rglob('*')) if p.is_file()}


def _cli(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run([sys.executable, '-B', str(RUNTIME / 'scripts/tobkiri_pack.py'), *args],
        cwd=RUNTIME, env={**os.environ, 'PYTHONDONTWRITEBYTECODE': '1'},
        text=True, capture_output=True, check=False)


def _populated_scaffold(tmp_path: Path) -> Path:
    root = tmp_path / PACK_ID
    result = _cli('init', str(root), '--pack-id', PACK_ID, '--display-name', 'Preservation fixture', '--profile', 'minimal')
    assert result.returncode == 0, result.stderr
    (root / 'runtime').mkdir()
    shutil.copyfile(RUNTIME / 'acceptance/packvm_sandbox_qa_pack/runtime/probe.py', root / 'runtime/probe.py')
    source_identity = canonical_digest(json.loads((root / 'scaffold-source.v1.json').read_text()))
    docs = build_documents(root)
    for name, doc in docs.items():
        if name == 'pack.v4.json':
            doc['integrity']['source_identity'] = source_identity
        else:
            doc['source_identity'] = source_identity
        if 'provenance' in doc:
            doc['provenance']['source_digest'] = source_identity
        for contract in doc.get('contracts', []):
            if 'provenance' in contract:
                contract['provenance']['source_digest'] = source_identity
    executable = docs['executables.v4.json']
    executable['catalog_digest'] = canonical_digest({k: v for k, v in executable.items() if k != 'catalog_digest'})
    _write(root / 'executables.v4.json', executable)
    _write(root / 'contracts.v4.json', docs['contracts.v4.json'])
    manifest = docs['pack.v4.json']
    manifest['artifacts'][0]['digest'] = _digest(root / 'executables.v4.json')
    for relative in ('README.md', 'scaffold-source.v1.json'):
        manifest['artifacts'].append({'path': relative, 'digest': _digest(root / relative),
            'kind': 'sidecar' if relative.endswith('.json') else 'asset'})
    manifest['integrity']['contract_catalog_digest'] = _digest(root / 'contracts.v4.json')
    artifact_digest = canonical_digest(manifest['artifacts'])
    manifest['pack']['artifact_digest'] = artifact_digest
    manifest['integrity']['artifact_set_digest'] = artifact_digest
    _write(root / 'pack.v4.json', manifest)
    index = docs['artifact-index.v4.json']
    index['artifact_set_digest'] = artifact_digest
    for entry in index['artifacts']:
        entry['digest'] = _digest(root / entry['path'])
    for relative in ('README.md', 'scaffold-source.v1.json'):
        index['artifacts'].append({'path': relative, 'digest': _digest(root / relative), 'role': 'sidecar'})
    index['integrity_seal']['signed_digest'] = canonical_digest({k: v for k, v in index.items() if k != 'integrity_seal'})
    _write(root / 'artifact-index.v4.json', index)
    for name, kind in [('pack.v4.json', 'pack'), ('contracts.v4.json', 'pack_contract_catalog'),
        ('executables.v4.json', 'executable_catalog'), ('artifact-index.v4.json', 'pack_artifact_index')]:
        validate_document((root / name).read_bytes(), kind)
    compiled = compile_pack_root(root)
    assert (len(compiled.artifact.functions), len(compiled.artifact.variants), len(compiled.routes)) == (1, 1, 7)
    return root


def _assert_preserved_or_refused(root: Path, before: dict[str, bytes], failed: bool) -> None:
    after = _snapshot(root)
    if failed:
        assert after == before, 'Refusal must precede component and authority writes'
        return
    old_pack, new_pack = json.loads(before['pack.v4.json']), json.loads(after['pack.v4.json'])
    for key in ('functions', 'contracts', 'operation_catalog', 'provider_catalog', 'requirements', 'provenance', 'migration'):
        assert new_pack[key] == old_pack[key], f'Authored manifest field erased: {key}'
    assert json.loads(after['contracts.v4.json']) == json.loads(before['contracts.v4.json'])
    assert json.loads(after['executables.v4.json']) == json.loads(before['executables.v4.json'])
    compiled = compile_pack_root(root)
    assert (len(compiled.artifact.functions), len(compiled.artifact.variants), len(compiled.routes)) == (1, 1, 7)
    assert after['runtime/probe.py'] == before['runtime/probe.py']


@pytest.mark.parametrize('kind', ['activity', 'skill', 'tool'])
def test_cli_add_preserves_authored_pack_or_refuses_before_writing(tmp_path: Path, kind: str) -> None:
    root = _populated_scaffold(tmp_path)
    before = _snapshot(root)
    result = _cli('add', str(root), kind, '--id', PACK_ID + '.review', '--display-name', 'Review', '--description', 'Review bounded fixture input')
    _assert_preserved_or_refused(root, before, result.returncode != 0)


def test_direct_refresh_preserves_authored_pack_or_refuses_before_writing(tmp_path: Path) -> None:
    root = _populated_scaffold(tmp_path)
    before = _snapshot(root)
    try:
        refresh_scaffold_artifacts(root)
    except ValueError:
        failed = True
    else:
        failed = False
    _assert_preserved_or_refused(root, before, failed)
