import assert from 'node:assert/strict';
import test from 'node:test';

import {
  createProfileCeremonyClient,
  type ProfileCeremonyTransport,
} from './profileCeremony';
import {
  RUNTIME_SURFACE_API_VERSION,
  RuntimeSurfaceError,
  type RuntimeSurfaceEnvelope,
} from './runtimeSurface';

const digest = (character: string): string => `sha256:${character.repeat(64)}`;

function catalogBindingFields() {
  return {
    profile_definition_digest: digest('6'),
    profile_catalog_digest: digest('7'),
    bundle_lock_digest: digest('8'),
  };
}

function profileSnapshot(): RuntimeSurfaceEnvelope<unknown> {
  return {
    runtime_surface_api_version: RUNTIME_SURFACE_API_VERSION,
    surface: 'profile',
    state: 'ready',
    profile_id: 'defaults',
    profile_revision: digest('a'),
    plan_digest: digest('b'),
    catalog_revision: digest('c'),
    records: {
      profile_lock: {digest: digest('d'), source_ref: 'profile-lock-v4://defaults/lock'},
      resolved_plan: {digest: digest('b'), source_ref: 'resolved-plan-v1://defaults/plan'},
      activation_record: {digest: digest('1'), source_ref: 'activation-record-v1://defaults/activation'},
      authority_snapshot: {digest: digest('e'), source_ref: 'authority-snapshot-v4://defaults/snapshot'},
    },
    data: {
      profile: {
        profile_id: 'defaults',
        display_name: 'Defaults',
        profile_revision: digest('a'),
        catalog_revision: digest('c'),
      },
      profile_lock: {
        lock_digest: digest('d'),
        plan_digest: digest('b'),
        bundle_digest: digest('8'),
        closure_digest: digest('9'),
        profile_authority_snapshot_digest: digest('e'),
        security_epoch: 4,
      },
      resolved_plan: {
        plan_digest: digest('b'),
        bundle_digest: digest('8'),
        closure_digest: digest('9'),
        security_epoch: 4,
      },
      activation_record: {
        activation_api_version: 'io.tobkiri.activation-record.v2',
        profile_id: 'defaults',
        profile_revision: digest('a'),
        activation_id: 'activation:defaults-one',
        state: 'active',
        state_generation: 1,
        catalog_revision: digest('c'),
        bundle_digest: digest('8'),
        lock_digest: digest('d'),
        plan_digest: digest('b'),
        closure_digest: digest('9'),
        profile_authority_snapshot_digest: digest('e'),
        security_epoch: 4,
        fencing_token: 7,
        created_at: '2026-08-10T00:00:00Z',
        committed_at: '2026-08-10T00:00:01Z',
      },
      authority_snapshot: {
        profile_authority_snapshot_digest: digest('e'),
        security_epoch: 4,
        fencing_token: 7,
      },
      profile_document: {packs: [{pack_id: 'provider-pack', role: 'provider', artifact_digest: digest('1')}]},
      resolved_wiring: {requested_edges: [], bindings: []},
    },
  };
}

function queuedTransport(responses: unknown[], calls: Array<{target: string; payload: Record<string, unknown>}>): ProfileCeremonyTransport {
  return {
    write: async <T>(target, payload): Promise<T> => {
      calls.push({target: target.operation_id, payload});
      const response = responses.shift();
      if (response === undefined) throw new Error('missing fixture response');
      return response as T;
    },
  };
}

test('Profile ceremony sends exact staged payloads and requires the authoritative activate snapshot', async () => {
  const snapshot = profileSnapshot();
  const candidateDigest = digest('2');
  const approvalDigest = digest('3');
  const calls: Array<{target: string; payload: Record<string, unknown>}> = [];
  const client = createProfileCeremonyClient(undefined, queuedTransport([
    {
      runtime_surface_api_version: RUNTIME_SURFACE_API_VERSION,
      state: 'resolved',
      candidate_id: 'candidate-one',
      candidate_digest: candidateDigest,
      expires_in: 60,
      review: {profile: {}, profile_lock: {lock_digest: digest('d')}, resolved_plan: {plan_digest: digest('b')}, predecessor: {}},
      next_action: 'review',
      write_set: [],
    },
    {
      runtime_surface_api_version: RUNTIME_SURFACE_API_VERSION,
      state: 'reviewed',
      candidate_id: 'candidate-one',
      candidate_digest: candidateDigest,
      next_action: 'approval',
      write_set: [],
    },
    {
      runtime_surface_api_version: RUNTIME_SURFACE_API_VERSION,
      state: 'approved',
      approval_id: 'approval-one',
      approval_digest: approvalDigest,
      expires_in: 30,
      next_action: 'activation',
      write_set: [],
      authority_approval: {
        approval_id: 'approval-one',
        approval_digest: approvalDigest,
        decision: 'approved',
        security_epoch: 4,
      },
    },
    {
      runtime_surface_api_version: RUNTIME_SURFACE_API_VERSION,
      state: 'active',
      profile_id: 'defaults',
      activation_id: 'activation:defaults-one',
      plan_digest: digest('b'),
      security_epoch: 4,
      fencing_token: 7,
      authoritative_snapshot: snapshot,
    },
  ], calls));

  const result = await client.resolve({
    profile_id: 'defaults',
    expected_profile_revision: digest('a'),
    expected_plan_digest: digest('b'),
    desired_pack_ids: ['provider-pack'],
    ...catalogBindingFields(),
  });
  await client.review({candidate_id: 'candidate-one', candidate_digest: candidateDigest});
  await client.approve({candidate_id: 'candidate-one', candidate_digest: candidateDigest});
  const activated = await client.activate({approval_id: 'approval-one', approval_digest: approvalDigest});

  assert.equal(activated.authoritative_snapshot.profile_revision, digest('a'));
  assert.deepEqual(calls.map((call) => call.target), [
    'profile.change.resolve',
    'profile.change.review',
    'profile.change.approve',
    'profile.change.activate',
  ]);
  assert.deepEqual(calls[0].payload, {
    profile_id: 'defaults',
    expected_profile_revision: digest('a'),
    expected_plan_digest: digest('b'),
    desired_pack_ids: ['provider-pack'],
    ...catalogBindingFields(),
  });
  assert.equal(Object.prototype.hasOwnProperty.call(calls[0].payload, 'approved'), false);
  assert.deepEqual(calls[3].payload, {
    approval_id: 'approval-one',
    approval_digest: approvalDigest,
  });
});

test('Profile review rejects a response substituted for the requested candidate', async () => {
  const candidateDigest = digest('2');
  const client = createProfileCeremonyClient(undefined, queuedTransport([{
    runtime_surface_api_version: RUNTIME_SURFACE_API_VERSION,
    state: 'reviewed',
    candidate_id: 'candidate-b',
    candidate_digest: digest('3'),
    next_action: 'approval',
    write_set: [],
  }], []));

  await assert.rejects(
    client.review({candidate_id: 'candidate-a', candidate_digest: candidateDigest}),
    (error: unknown) => error instanceof RuntimeSurfaceError && error.code === 'DIGEST_MISMATCH',
  );
});

test('named Profile resolve binds exact definition, catalog, and bundle-lock digests', async () => {
  const calls: Array<{target: string; payload: Record<string, unknown>}> = [];
  const definitionDigest = digest('6');
  const catalogDigest = digest('7');
  const bundleDigest = digest('8');
  const client = createProfileCeremonyClient(undefined, queuedTransport([{
    runtime_surface_api_version: RUNTIME_SURFACE_API_VERSION,
    state: 'resolved',
    candidate_id: 'candidate-named',
    candidate_digest: digest('9'),
    expires_in: 60,
    review: {
      profile: {profile_id: 'alternate'},
      profile_lock: {},
      resolved_plan: {},
      predecessor: {},
      catalog_binding: {
        profile_definition_digest: definitionDigest,
        profile_catalog_digest: catalogDigest,
        bundle_lock_digest: bundleDigest,
      },
    },
    next_action: 'review',
    write_set: [],
  }], calls));

  const result = await client.resolve({
    profile_id: 'alternate',
    expected_profile_revision: digest('a'),
    expected_plan_digest: digest('b'),
    desired_pack_ids: ['provider-pack'],
    profile_definition_digest: definitionDigest,
    profile_catalog_digest: catalogDigest,
    bundle_lock_digest: bundleDigest,
  });

  assert.deepEqual(Object.keys(calls[0].payload).sort(), [
    'bundle_lock_digest',
    'desired_pack_ids',
    'expected_plan_digest',
    'expected_profile_revision',
    'profile_catalog_digest',
    'profile_definition_digest',
    'profile_id',
  ]);
  assert.deepEqual(calls[0].payload, {
    profile_id: 'alternate',
    expected_profile_revision: digest('a'),
    expected_plan_digest: digest('b'),
    desired_pack_ids: ['provider-pack'],
    profile_definition_digest: definitionDigest,
    profile_catalog_digest: catalogDigest,
    bundle_lock_digest: bundleDigest,
  });
  assert.equal(result.review.catalog_binding?.profile_catalog_digest, catalogDigest);
});

test('named Profile selection rejects partial catalog bindings and legacy non-default payloads', () => {
  const client = createProfileCeremonyClient(undefined, queuedTransport([], []));
  assert.throws(
    () => client.resolve({
      profile_id: 'alternate',
      expected_profile_revision: digest('a'),
      expected_plan_digest: digest('b'),
      desired_pack_ids: ['provider-pack'],
      profile_definition_digest: digest('6'),
    } as never),
    (error: unknown) => error instanceof RuntimeSurfaceError && error.code === 'INVALID',
  );
  assert.throws(
    () => client.resolve({
      profile_id: 'alternate',
      expected_profile_revision: digest('a'),
      expected_plan_digest: digest('b'),
      desired_pack_ids: ['provider-pack'],
    }),
    (error: unknown) => error instanceof RuntimeSurfaceError && error.code === 'INVALID',
  );
});

test('Profile ceremony maps stale, digest mismatch, timeout, and denial errors fail-closed', async () => {
  const errorCases = [
    ['STALE_REVISION', 'STALE'],
    ['DIGEST_MISMATCH', 'DIGEST_MISMATCH'],
    ['TIMEOUT', 'TIMEOUT'],
    ['UNAPPROVED', 'APPROVAL_DENIED'],
  ] as const;
  for (const [code, expectedCode] of errorCases) {
    const client = createProfileCeremonyClient(undefined, queuedTransport([{
      runtime_surface_api_version: RUNTIME_SURFACE_API_VERSION,
      state: 'error',
      code,
      message: code,
      retryable: code === 'TIMEOUT',
      write_set: [],
    }], []));
    await assert.rejects(
      client.resolve({
        profile_id: 'defaults',
        expected_profile_revision: digest('a'),
        expected_plan_digest: digest('b'),
        desired_pack_ids: ['provider-pack'],
        ...catalogBindingFields(),
      }),
      (error: unknown) => error instanceof RuntimeSurfaceError && error.code === expectedCode,
    );
  }
});

test('Profile approval denial and missing activate snapshot are not accepted as success', async () => {
  const deniedClient = createProfileCeremonyClient(undefined, queuedTransport([{
    runtime_surface_api_version: RUNTIME_SURFACE_API_VERSION,
    state: 'approved',
    approval_id: 'approval-one',
    approval_digest: digest('3'),
    expires_in: 30,
    next_action: 'activation',
    write_set: [],
    authority_approval: {
      approval_id: 'approval-one',
      approval_digest: digest('3'),
      decision: 'denied',
      security_epoch: 4,
    },
  }], []));
  await assert.rejects(
    deniedClient.approve({candidate_id: 'candidate-one', candidate_digest: digest('2')}),
    (error: unknown) => error instanceof RuntimeSurfaceError && error.code === 'APPROVAL_DENIED',
  );

  const missingSnapshotClient = createProfileCeremonyClient(undefined, queuedTransport([{
    runtime_surface_api_version: RUNTIME_SURFACE_API_VERSION,
    state: 'active',
    profile_id: 'defaults',
    activation_id: 'activation-two',
    plan_digest: digest('b'),
    security_epoch: 4,
    fencing_token: 8,
  }], []));
  await assert.rejects(
    missingSnapshotClient.activate({approval_id: 'approval-one', approval_digest: digest('3')}),
    (error: unknown) => error instanceof RuntimeSurfaceError && error.code === 'INVALID',
  );
});

test('Profile approval and activate snapshots remain digest-bound to the Kernel records', async () => {
  const mismatchedApprovalClient = createProfileCeremonyClient(undefined, queuedTransport([{
    runtime_surface_api_version: RUNTIME_SURFACE_API_VERSION,
    state: 'approved',
    approval_id: 'approval-one',
    approval_digest: digest('3'),
    expires_in: 30,
    next_action: 'activation',
    write_set: [],
    authority_approval: {
      approval_id: 'approval-two',
      approval_digest: digest('4'),
      decision: 'approved',
      security_epoch: 4,
    },
  }], []));
  await assert.rejects(
    mismatchedApprovalClient.approve({candidate_id: 'candidate-one', candidate_digest: digest('2')}),
    (error: unknown) => error instanceof RuntimeSurfaceError && error.code === 'DIGEST_MISMATCH',
  );

  const mismatchedSnapshot = profileSnapshot();
  mismatchedSnapshot.plan_digest = digest('f');
  mismatchedSnapshot.records.resolved_plan = {digest: digest('f'), source_ref: 'resolved-plan-v1://defaults/plan-two'};
  const mismatchedData = mismatchedSnapshot.data as Record<string, unknown>;
  mismatchedData.resolved_plan = {
    ...(mismatchedData.resolved_plan as Record<string, unknown>),
    plan_digest: digest('f'),
  };
  mismatchedData.profile_lock = {
    ...(mismatchedData.profile_lock as Record<string, unknown>),
    plan_digest: digest('f'),
  };
  const mismatchedActivation = mismatchedData.activation_record as Record<string, unknown>;
  mismatchedActivation.plan_digest = digest('f');
  const mismatchedSnapshotClient = createProfileCeremonyClient(undefined, queuedTransport([{
    runtime_surface_api_version: RUNTIME_SURFACE_API_VERSION,
    state: 'active',
    profile_id: 'defaults',
    activation_id: 'activation-two',
    plan_digest: digest('b'),
    security_epoch: 4,
    fencing_token: 8,
    authoritative_snapshot: mismatchedSnapshot,
  }], []));
  await assert.rejects(
    mismatchedSnapshotClient.activate({approval_id: 'approval-one', approval_digest: digest('3')}),
    (error: unknown) => error instanceof RuntimeSurfaceError && error.code === 'DIGEST_MISMATCH',
  );
});

test('Profile activation rejects top-level metadata that disagrees with the authoritative activation record', async () => {
  const mismatches = [
    ['activation_id', 'activation:defaults-two'],
    ['security_epoch', 5],
    ['fencing_token', 8],
  ] as const;

  for (const [field, value] of mismatches) {
    const client = createProfileCeremonyClient(undefined, queuedTransport([{
      runtime_surface_api_version: RUNTIME_SURFACE_API_VERSION,
      state: 'active',
      profile_id: 'defaults',
      activation_id: field === 'activation_id' ? value : 'activation:defaults-one',
      plan_digest: digest('b'),
      security_epoch: field === 'security_epoch' ? value : 4,
      fencing_token: field === 'fencing_token' ? value : 7,
      authoritative_snapshot: profileSnapshot(),
    }], []));

    await assert.rejects(
      client.activate({approval_id: 'approval-one', approval_digest: digest('3')}),
      (error: unknown) => error instanceof RuntimeSurfaceError && error.code === 'DIGEST_MISMATCH',
      `expected ${field} mismatch to fail closed`,
    );
  }
});

test('Profile ceremony rejects client approval flags and non-digest guards before transport', async () => {
  const calls: Array<{target: string; payload: Record<string, unknown>}> = [];
  const client = createProfileCeremonyClient(undefined, queuedTransport([], calls));
  assert.throws(
    () => client.review({candidate_id: 'candidate-one', candidate_digest: digest('2'), approved: true} as never),
    (error: unknown) => error instanceof RuntimeSurfaceError && error.code === 'INVALID',
  );
  assert.throws(
    () => client.resolve({
      profile_id: 'defaults',
      expected_profile_revision: 'revision-one',
      expected_plan_digest: digest('b'),
      desired_pack_ids: ['provider-pack'],
    }),
    (error: unknown) => error instanceof RuntimeSurfaceError && error.code === 'INVALID',
  );
  assert.equal(calls.length, 0);
});
