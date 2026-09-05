import test from 'node:test';
import assert from 'node:assert/strict';

import { transformPack } from './transforms';

test('transformPack preserves approval state from panel packs API', () => {
  const pack = transformPack({
    pack_id: 'pack_a',
    name: 'Pack A',
    version: '1.0.0',
    description: 'Demo pack',
    is_core: false,
    required: true,
    installed: true,
    enabled: true,
    artifact_digest: 'sha256:artifact-a',
    profile_id: 'profile-a',
    workspace_id: 'workspace-a',
    profile_revision: 'sha256:profile-a',
    plan_digest: 'sha256:plan-a',
    catalog_revision: 'catalog-a',
    approval_status: 'modified',
    approval_reason: 'hash_mismatch',
    approved: false,
    hash_valid: false,
    critical_changed: true,
    approval_issues: ['hash_mismatch', 'critical_changed'],
    capabilities: [
      {name: 'file.inspect', description: 'Inspect files in the selected workspace.'},
    ],
    operations: [{
      operation_id: 'rumi_file_inspect_pack.file-inspect',
      contract_id: 'tobkiri.service.file.inspect.v1',
      provider_id: 'rumi_file_inspect_pack.file-inspect.service',
      capabilities: ['file.inspect'],
      input_schema: {type: 'object', required: ['name', 'path']},
      invokable: true,
    }],
    dependencies: ['workspace.base'],
  });

  assert.equal(pack.id, 'pack_a');
  assert.equal(pack.installed, true);
  assert.equal(pack.required, true);
  assert.equal(pack.artifactDigest, 'sha256:artifact-a');
  assert.equal(pack.profileId, 'profile-a');
  assert.equal(pack.workspaceId, 'workspace-a');
  assert.equal(pack.profileRevision, 'sha256:profile-a');
  assert.equal(pack.planDigest, 'sha256:plan-a');
  assert.equal(pack.catalogRevision, 'catalog-a');
  assert.equal(pack.approvalStatus, 'modified');
  assert.equal(pack.approvalReason, 'hash_mismatch');
  assert.equal(pack.approved, false);
  assert.equal(pack.hashValid, false);
  assert.equal(pack.criticalChanged, true);
  assert.deepEqual(pack.approvalIssues, ['hash_mismatch', 'critical_changed']);
  assert.deepEqual(pack.capabilities, [
    {name: 'file.inspect', description: 'Inspect files in the selected workspace.'},
  ]);
  assert.deepEqual(pack.operations, [{
    operationId: 'rumi_file_inspect_pack.file-inspect',
    contractId: 'tobkiri.service.file.inspect.v1',
    providerId: 'rumi_file_inspect_pack.file-inspect.service',
    capabilities: ['file.inspect'],
    inputSchema: {type: 'object', required: ['name', 'path']},
    invokable: true,
  }]);
  assert.deepEqual(pack.flows, ['rumi_file_inspect_pack.file-inspect']);
  assert.deepEqual(pack.dependencies, ['workspace.base']);
});

test('transformPack normalizes the v4 declared and invokable operation projections', () => {
  const declaredOperation = {
    operation_id: 'pack.status',
    contract_id: 'tobkiri.pack.status.v1',
    provider_id: 'pack.status.provider',
    function_id: 'pack.status.provider',
    required_capabilities: ['pack.status'],
  };
  const pack = transformPack({
    pack_id: 'pack_v4',
    name: 'Pack v4',
    version: '1.0.0',
    description: 'v4 projection',
    is_core: false,
    installed: true,
    enabled: true,
    artifact_digest: 'sha256:artifact-v4',
    profile_id: 'profile-v4',
    workspace_id: 'workspace-v4',
    profile_revision: 'sha256:profile-v4',
    plan_digest: 'sha256:plan-v4',
    catalog_revision: 'catalog-v4',
    declared_operations: [declaredOperation],
    invokable_operations: [declaredOperation],
  });

  assert.deepEqual(pack.operations, [{
    operationId: 'pack.status',
    contractId: 'tobkiri.pack.status.v1',
    providerId: 'pack.status.provider',
    capabilities: ['pack.status'],
    inputSchema: {},
    invokable: true,
  }]);
  assert.deepEqual(pack.flows, ['pack.status']);
});
