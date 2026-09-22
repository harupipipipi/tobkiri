import assert from 'node:assert/strict';
import test from 'node:test';

import {
  GENERATED_FRONTEND_CONTRACT_MAP,
  PINNED_FRONTEND_CONTRACT_MAP_ARTIFACT_DIGEST,
  validateGeneratedFrontendContractMap,
} from './generatedFrontendContractMap';
import {
  CANONICAL_RUNTIME_SURFACES,
  RUNTIME_SURFACE_API_VERSION,
  RUNTIME_SURFACE_TARGETS,
  RuntimeSurfaceError,
  assertVerifiedRuntimeTarget,
  createRuntimeSurfaceClient,
  extractExactFlowDescriptors,
  extractExactOperationDescriptors,
  extractExactPackDescriptors,
  extractExactPlanBindings,
  extractExactProfileCatalog,
  extractExactProfileCatalogSelectablePackIds,
  extractExactProfileSelectablePackIds,
  extractExactRouteDescriptors,
  extractFiniteArtifactEntries,
  extractRuntimeProfileSettings,
  invokeRuntimeOperation,
  validateRuntimeSurfaceEnvelope,
  type RuntimeSurfaceEnvelope,
} from './runtimeSurface';

const digest = (character: string): string => `sha256:${character.repeat(64)}`;

function profileData() {
  return {
    profile: {
      profile_id: 'defaults',
      display_name: 'Defaults',
      profile_revision: digest('a'),
      catalog_revision: digest('c'),
    },
    profile_document: {
      packs: [
        {artifact_digest: digest('1'), pack_id: 'provider-pack', role: 'provider'},
        {artifact_digest: null, pack_id: 'application-pack', role: 'application'},
      ],
    },
    base: {pack_id: 'base-pack'},
    shell: {pack_id: 'shell-pack'},
    application: {pack_id: 'application-pack', role: 'application'},
    pack_closure: [{pack_id: 'provider-pack'}],
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
      authority_references: ['authority://one'],
      security_epoch: 4,
      fencing_token: 7,
    },
    resolved_wiring: {
      requested_edges: [],
      bindings: [{
        binding_id: 'binding-one',
        source_principal_id: 'principal-source',
        target_principal_id: 'principal-target',
        target_contract_id: 'contract.one.v1',
        operation_id: 'operation.one',
        owner_pack_id: 'provider-pack',
        edge_digest: digest('f'),
        authority_reference: 'authority://one',
      }],
    },
    artifact_entries: [{
      entry_id: 'artifact-one',
      owner_pack_id: 'provider-pack',
      path: 'artifacts/input.json',
      kind: 'manifest',
      artifact_digest: digest('a'),
    }],
  };
}

function settingsData() {
  return {
    user_settings: {
      scope: 'user',
      source: 'launcher_local',
      state: 'unavailable_from_runtime',
      mutable_via_profile_activation: false,
    },
    runtime_profile_settings: {
      scope: 'runtime_profile',
      mutable_via_profile_activation: true,
      profile_id: 'defaults',
      profile_revision: digest('a'),
      catalog_revision: digest('c'),
      plan_digest: digest('b'),
      lock_digest: digest('d'),
      security_epoch: 4,
    },
  };
}

function envelope<T>(surface: RuntimeSurfaceEnvelope<T>['surface'], data: T): RuntimeSurfaceEnvelope<T> {
  return {
    runtime_surface_api_version: RUNTIME_SURFACE_API_VERSION,
    surface,
    state: 'ready',
    profile_id: 'defaults',
    profile_revision: digest('a'),
    plan_digest: digest('b'),
    catalog_revision: digest('c'),
    records: {
      profile_lock: {digest: digest('d'), source_ref: 'profile-lock-v4://defaults/lock-one'},
      resolved_plan: {digest: digest('b'), source_ref: 'resolved-plan-v1://defaults/plan-one'},
      activation_record: {digest: digest('1'), source_ref: 'activation-record-v1://defaults/activation-one'},
      authority_snapshot: {digest: digest('e'), source_ref: 'authority-snapshot-v4://defaults/snapshot-one'},
    },
    data,
  };
}

test('frozen runtime surface targets cover only the seven canonical projections', () => {
  assert.deepEqual(CANONICAL_RUNTIME_SURFACES, ['profile', 'profiles', 'settings', 'packs', 'contracts', 'operations', 'principals']);
  assert.equal(RUNTIME_SURFACE_TARGETS.profile?.logical_target, '/api/runtime-surface/profile');
  assert.equal(RUNTIME_SURFACE_TARGETS.profiles?.logical_target, '/api/runtime-surface/profiles');
  assert.equal(RUNTIME_SURFACE_TARGETS.profiles?.operation_id, 'profile.catalog.read');
  assert.deepEqual(RUNTIME_SURFACE_TARGETS.profiles?.allowed_payload_keys, []);
  assert.equal(RUNTIME_SURFACE_TARGETS.settings?.read_guards, false);
  assert.equal(RUNTIME_SURFACE_TARGETS.operations?.logical_target, '/api/runtime-surface/topology/operations');
  assert.equal(RUNTIME_SURFACE_TARGETS.principals?.logical_target, '/api/runtime-surface/topology/principals');
});

test('runtime target and operation revisions fail closed on map or digest mismatches', async () => {
  const target = RUNTIME_SURFACE_TARGETS.operations;
  assert.ok(target);
  assert.throws(
    () => assertVerifiedRuntimeTarget({...target, map_artifact_digest: digest('f')}),
    (error: unknown) => error instanceof RuntimeSurfaceError && error.code === 'DIGEST_MISMATCH',
  );

  const operation = {
    action: 'contract_invoke' as const,
    operation_id: 'operation.one',
    contract_id: 'contract.one.v1',
    owner_pack_id: 'provider-pack',
    contribution_id: 'contribution-one',
    target_provider_id: 'provider.one',
    artifact_digest: digest('1'),
    invocation_contribution_id: 'invocation-contribution-one',
    invocation_owner_pack_id: 'provider-pack',
    invocation_catalog_hash: digest('c'),
    invocation_reason: null,
    invokable: true,
    catalog_digest: digest('c'),
    function_id: 'function-one',
    function_principal_id: 'principal.function-one',
    caller_function_id: 'caller.function-one',
    authority_reference: 'authority://one',
    route: {
      contract_id: 'contract.one.v1',
      operation_id: 'operation.one',
      function_id: 'function-one',
      provider_pack_id: 'provider-pack',
    },
    schema: {
      input_schema: {
        type: 'object',
        properties: {prompt: {type: 'string'}},
      },
    },
    input_schema: {
      type: 'object',
      properties: {prompt: {type: 'string'}},
    },
  };
  const envelopeValue = envelope('operations', {operations: [operation]});
  await assert.rejects(
    invokeRuntimeOperation({
      envelope: {...envelopeValue, catalog_revision: digest('d')},
      operation,
      payload: {prompt: 'hello'},
    }),
    (error: unknown) => error instanceof RuntimeSurfaceError && error.code === 'DIGEST_MISMATCH',
  );
  await assert.rejects(
    invokeRuntimeOperation({
      envelope: envelopeValue,
      operation: {...operation, artifact_digest: digest('f')},
      payload: {prompt: 'hello'},
    }),
    (error: unknown) => error instanceof RuntimeSurfaceError && error.code === 'DIGEST_MISMATCH',
  );
});

test('generated Contract Map is pinned to the canonical raw artifact and includes every map binding plus the operation status target', () => {
  assert.equal(
    GENERATED_FRONTEND_CONTRACT_MAP.artifact_digest,
    PINNED_FRONTEND_CONTRACT_MAP_ARTIFACT_DIGEST,
  );
  assert.equal(GENERATED_FRONTEND_CONTRACT_MAP.routes.length, 23);
  assert.doesNotThrow(() => validateGeneratedFrontendContractMap(GENERATED_FRONTEND_CONTRACT_MAP));

  const tampered = structuredClone(GENERATED_FRONTEND_CONTRACT_MAP);
  tampered.routes[0].targets[0].provider_id = 'untrusted.provider';
  assert.throws(
    () => validateGeneratedFrontendContractMap(tampered),
    /invalid|tampered/i,
  );

  const tamperedPresentation = structuredClone(GENERATED_FRONTEND_CONTRACT_MAP);
  tamperedPresentation.routes[0].presentation = 'untrusted.presentation';
  assert.throws(
    () => validateGeneratedFrontendContractMap(tamperedPresentation),
    /invalid|tampered/i,
  );

  const stale = structuredClone(GENERATED_FRONTEND_CONTRACT_MAP);
  stale.artifact_digest = 'sha256:' + '0'.repeat(64);
  assert.throws(
    () => validateGeneratedFrontendContractMap(stale),
    /stale|tampered/i,
  );
});

function profileCatalogData() {
  const closure = [
    {pack_id: 'base-pack', role: 'base', version: '1.0.0', artifact_digest: digest('1'), artifact_ref: `pack-v4://base-pack@${digest('1')}`},
    {pack_id: 'shell-pack', role: 'shell', version: '1.0.0', artifact_digest: digest('2'), artifact_ref: `pack-v4://shell-pack@${digest('2')}`},
    {pack_id: 'application-pack', role: 'application', version: '1.0.0', artifact_digest: digest('3'), artifact_ref: `pack-v4://application-pack@${digest('3')}`},
    {pack_id: 'provider-pack', role: 'provider', version: '1.0.0', artifact_digest: digest('4'), artifact_ref: `pack-v4://provider-pack@${digest('4')}`},
  ];
  const entry = (profileId: string, active: boolean) => ({
    profile_id: profileId,
    display_name: active ? 'Defaults' : 'Alternate',
    active,
    lifecycle_state: active ? 'active' as const : 'available' as const,
    available: true,
    diagnostics: [],
    definition: {
      digest: active ? digest('5') : digest('6'),
      ref: `profile-v4://${profileId}/${active ? digest('5') : digest('6')}`,
      catalog_revision: null,
      source_path: `ecosystem/defaultspack/v4/${profileId}.profile.v4.json`,
      provenance: {source_kind: 'repository'},
    },
    bindings: {
      base: {pack_id: 'base-pack', definition_revision: null, definition_digest: null, artifact_digest: digest('1')},
      shell: {provider_id: 'shell.provider', pack_id: 'shell-pack', definition_revision: null, definition_digest: null, artifact_digest: digest('2')},
      application: {pack_id: 'application-pack', artifact_digest: digest('3'), artifact_ref: `pack-v4://application-pack@${digest('3')}`},
    },
    pack_closure: closure,
    records: {
      profile_revision: active ? digest('a') : null,
      profile_lock_digest: active ? digest('d') : null,
      plan_digest: active ? digest('b') : null,
    },
    authority_snapshot: {
      state: active ? 'active' as const : 'captured_on_resolve' as const,
      digest: active ? digest('e') : null,
      ref: active ? `authority-snapshot-v4://${profileId}/${digest('e')}` : null,
      definition_references: [],
    },
    candidate: {state: 'not_staged', candidate_id: null, candidate_digest: null, expires_at: null},
  });
  return {
    catalog_api_version: 'io.tobkiri.profile-catalog-presentation.v4' as const,
    catalog_digest: digest('c'),
    bundle_lock_digest: digest('8'),
    catalog_ref: `profile-catalog-v4://bundle/${digest('c')}`,
    active_profile_id: 'defaults',
    count: 2,
    profiles: [entry('defaults', true), entry('alternate', false)],
  };
}

test('Profile catalog projection is exact, exposes the active marker, and derives only named provider Packs', () => {
  const accepted = validateRuntimeSurfaceEnvelope('profiles', envelope('profiles', profileCatalogData()));
  const projection = extractExactProfileCatalog(accepted.data);
  assert.ok(projection);
  assert.equal(projection.active_profile_id, 'defaults');
  assert.deepEqual(projection.profiles.map((entry) => entry.profile_id), ['defaults', 'alternate']);
  assert.deepEqual(extractExactProfileCatalogSelectablePackIds(projection.profiles[0]), ['provider-pack']);
  assert.deepEqual(extractExactProfileCatalogSelectablePackIds({...projection.profiles[0], pack_closure: []}), []);
});

test('Profile catalog tamper, unknown active marker, and extra fields fail closed', () => {
  const fixture = profileCatalogData();
  const tamperedDigest = structuredClone(fixture);
  tamperedDigest.profiles[0].definition.digest = digest('f');
  assert.equal(extractExactProfileCatalog(tamperedDigest), null);

  const unknownActive = structuredClone(fixture);
  unknownActive.active_profile_id = 'unknown-profile';
  assert.equal(extractExactProfileCatalog(unknownActive), null);

  const extraField = structuredClone(fixture);
  (extraField.profiles[0] as Record<string, unknown>).untrusted_pack_ids = ['injected-pack'];
  assert.equal(extractExactProfileCatalog(extraField), null);
});

test('real v4 read fixture accepts evidence refs and full Profile records only in profile data', () => {
  const accepted = validateRuntimeSurfaceEnvelope('profile', envelope('profile', profileData()));

  assert.deepEqual(accepted.records.profile_lock, {
    digest: digest('d'),
    source_ref: 'profile-lock-v4://defaults/lock-one',
  });
  assert.equal((accepted.data as ReturnType<typeof profileData>).profile_lock.lock_digest, digest('d'));
  assert.deepEqual(extractExactProfileSelectablePackIds(accepted.data), ['provider-pack']);
  assert.deepEqual(extractExactPlanBindings(accepted.data), profileData().resolved_wiring.bindings);
  assert.deepEqual(extractFiniteArtifactEntries(accepted.data), profileData().artifact_entries);
});

test('Profile activation record v2 is bound to every outer runtime digest', () => {
  const valid = envelope('profile', profileData());
  assert.doesNotThrow(() => validateRuntimeSurfaceEnvelope('profile', valid));

  for (const [field, replacement] of [
    ['profile_revision', digest('f')],
    ['catalog_revision', digest('f')],
    ['lock_digest', digest('f')],
    ['plan_digest', digest('f')],
    ['profile_authority_snapshot_digest', digest('f')],
    ['bundle_digest', digest('f')],
    ['closure_digest', digest('f')],
    ['security_epoch', 5],
    ['fencing_token', 8],
  ] as const) {
    const tampered = structuredClone(valid);
    (tampered.data.activation_record as Record<string, unknown>)[field] = replacement;
    assert.throws(
      () => validateRuntimeSurfaceEnvelope('profile', tampered),
      (error: unknown) => error instanceof RuntimeSurfaceError && error.code === 'INVALID',
    );
  }

  for (const tampered of [
    {...valid, plan_digest: digest('f')},
    {...valid, records: {...valid.records, resolved_plan: {
      ...valid.records.resolved_plan,
      digest: digest('f'),
    }}},
    {...valid, data: {...valid.data, resolved_plan: {
      ...valid.data.resolved_plan,
      bundle_digest: digest('f'),
    }}},
    {...valid, data: {...valid.data, profile_lock: {
      ...valid.data.profile_lock,
      closure_digest: digest('f'),
    }}},
    {...valid, data: {...valid.data, authority_snapshot: {
      ...valid.data.authority_snapshot,
      security_epoch: 5,
    }}},
  ]) {
    assert.throws(
      () => validateRuntimeSurfaceEnvelope('profile', tampered),
      (error: unknown) => error instanceof RuntimeSurfaceError && error.code === 'INVALID',
    );
  }

  const legacy = structuredClone(valid);
  legacy.data.activation_record.activation_api_version = 'io.tobkiri.activation-record.v1';
  assert.throws(
    () => validateRuntimeSurfaceEnvelope('profile', legacy),
    (error: unknown) => error instanceof RuntimeSurfaceError && error.code === 'INVALID',
  );
});

test('canonical read transport sends only target-selected guard keys', async () => {
  const calls: Array<{target: unknown; input: unknown}> = [];
  const client = createRuntimeSurfaceClient(
    {profile: RUNTIME_SURFACE_TARGETS.profile, settings: RUNTIME_SURFACE_TARGETS.settings},
    {
      read: async <T>(target, input): Promise<T> => {
        calls.push({target, input});
        const isSettings = target.logical_target.endsWith('/settings');
        return envelope(isSettings ? 'settings' : 'profile', isSettings ? settingsData() : profileData()) as unknown as T;
      },
    },
  );

  await client.read('profile', {
    expected_profile_revision: digest('a'),
    expected_plan_digest: digest('b'),
  });
  await client.read('settings', {
    expected_profile_revision: 'should-not-be-sent',
    expected_plan_digest: digest('x'),
  });

  assert.deepEqual(calls[0].input, {
    expected_profile_revision: digest('a'),
    expected_plan_digest: digest('b'),
  });
  assert.deepEqual(calls[1].input, {});
  assert.equal(Object.prototype.hasOwnProperty.call(calls[0].input as object, 'surface'), false);
});

test('Profile catalog transport uses the exact Broker route and never sends client guard or Pack payload keys', async () => {
  const calls: Array<{target: unknown; input: unknown}> = [];
  const client = createRuntimeSurfaceClient(
    {profiles: RUNTIME_SURFACE_TARGETS.profiles},
    {
      read: async <T>(target, input): Promise<T> => {
        calls.push({target, input});
        return envelope('profiles', profileCatalogData()) as unknown as T;
      },
    },
  );

  await client.read('profiles', {
    expected_profile_revision: digest('x'),
    expected_plan_digest: digest('y'),
  });

  assert.equal((calls[0].target as {logical_target: string}).logical_target, '/api/runtime-surface/profiles');
  assert.deepEqual(calls[0].input, {});
});

test('settings accepts only the runtime Profile scope and never treats user settings as runtime authority', () => {
  const settings = settingsData();
  const accepted = validateRuntimeSurfaceEnvelope('settings', envelope('settings', settings));
  assert.deepEqual(extractRuntimeProfileSettings(accepted.data), settings.runtime_profile_settings);
  assert.equal(extractRuntimeProfileSettings({user_settings: settings.user_settings}), null);
  assert.throws(
    () => validateRuntimeSurfaceEnvelope('settings', envelope('settings', {runtime_profile_settings: {}})),
    /invalid canonical/,
  );
});

test('HTTP-200 typed stale error is fail-closed and never accepted as a read projection', () => {
  assert.throws(
    () => validateRuntimeSurfaceEnvelope('profile', {
      runtime_surface_api_version: RUNTIME_SURFACE_API_VERSION,
      state: 'error',
      code: 'STALE_REVISION',
      message: 'stale',
      retryable: false,
      write_set: [],
    }),
    (error: unknown) => error instanceof RuntimeSurfaceError && error.code === 'STALE',
  );
  assert.throws(
    () => validateRuntimeSurfaceEnvelope('profile', {
      ...envelope('profile', profileData()),
      state: 'stale',
    }),
    (error: unknown) => error instanceof RuntimeSurfaceError && error.code === 'STALE',
  );
});

test('record refs reject extra authority fields and host sources', () => {
  const extra = envelope('profile', profileData());
  extra.records.authority_snapshot = {
    ...extra.records.authority_snapshot,
    security_epoch: 2,
  } as typeof extra.records.authority_snapshot;
  assert.throws(() => validateRuntimeSurfaceEnvelope('profile', extra), /invalid canonical/);

  const host = envelope('profile', profileData());
  host.records.profile_lock = {
    digest: digest('d'),
    source_ref: 'file:///tmp/profile-lock',
  };
  assert.throws(() => validateRuntimeSurfaceEnvelope('profile', host), /invalid canonical/);

  const extraEnvelope = {...envelope('profile', profileData()), extra_field: true};
  assert.throws(() => validateRuntimeSurfaceEnvelope('profile', extraEnvelope), /invalid canonical/);
});

test('synthetic links are rejected unless sourced from exact plan bindings', () => {
  assert.equal(extractExactPlanBindings({resolved_wiring: {bindings: [{id: 'row', contract_id: 'contract.one.v1'}]}}), null);
  assert.equal(extractExactPlanBindings(profileData())?.length, 1);
});

test('regex labels cannot classify a Pack as Flow or AI Input', () => {
  assert.deepEqual(extractExactFlowDescriptors({packs: [{name: 'flow-runner'}]}), []);
  assert.deepEqual(extractExactOperationDescriptors({operations: [{label: 'AI Input', operation_id: 'operation.one'}]}), []);
});

test('Pack projection rejects artifact-reference drift and duplicate Pack identities', () => {
  const pack = {
    pack_id: 'provider-pack',
    role: 'provider',
    kind: 'normal',
    version: '1.0.0',
    display_name: 'Provider Pack',
    artifact_digest: digest('a'),
    artifact_ref: `pack-v4://provider-pack@${digest('a')}`,
    installed: true,
    enabled: true,
    approved: true,
    required: false,
    invokable_operations: ['contract.one.v1::operation.one'],
  };
  assert.equal(extractExactPackDescriptors({packs: [pack]}).length, 1);
  assert.deepEqual(
    extractExactPackDescriptors({packs: [{...pack, artifact_ref: 'pack-v4://provider-pack@tampered'}]}),
    [],
  );
  assert.deepEqual(extractExactPackDescriptors({packs: [pack, pack]}), []);
});

test('operation extraction normalizes the formal contract_invoke action and rejects legacy write labels', () => {
  const raw = {
    operation_id: 'operation.one',
    contract_id: 'contract.one.v1',
    owner_pack_id: 'provider-pack',
    contribution_id: 'contribution-one',
    target_provider_id: 'provider.one',
    artifact_digest: digest('1'),
    invocation_contribution_id: null,
    invocation_owner_pack_id: null,
    invocation_catalog_hash: null,
    invocation_reason: 'not approved',
    invokable: false,
    catalog_digest: digest('c'),
    function_id: 'function-one',
    function_principal_id: 'principal.function-one',
    caller_function_id: 'caller.function-one',
    authority_reference: 'authority://one',
    route: {
      contract_id: 'contract.one.v1',
      operation_id: 'operation.one',
      function_id: 'function-one',
      provider_pack_id: 'provider-pack',
    },
    schema: {input_schema: {type: 'object', properties: {}}},
  };
  const [normalized] = extractExactOperationDescriptors({operations: [raw]});
  assert.equal(normalized?.action, 'contract_invoke');
  assert.deepEqual(extractExactOperationDescriptors({operations: [{...raw, action: 'write'}]}), []);
});

test('record digests are not exposed as profile files', () => {
  assert.equal(extractFiniteArtifactEntries({resolved_plan: {plan_digest: digest('b')}}), null);
});

test('stale or mismatched profile/plan/catalog blocks every surface action', async () => {
  assert.throws(
    () => validateRuntimeSurfaceEnvelope('profile', {...envelope('profile', profileData()), state: 'stale'}),
    (error: unknown) => error instanceof RuntimeSurfaceError && error.code === 'STALE',
  );
  const operation = {
    action: 'contract_invoke' as const,
    operation_id: 'operation.one',
    contract_id: 'contract.one.v1',
    owner_pack_id: 'provider-pack',
    contribution_id: 'catalog-only-contribution',
    target_provider_id: 'provider.one',
    artifact_digest: digest('1'),
    invocation_contribution_id: 'invocation-contribution-one',
    invocation_owner_pack_id: 'provider-pack',
    invocation_catalog_hash: digest('c'),
    invocation_reason: null,
    invokable: true,
    catalog_digest: digest('c'),
    function_id: 'function.one',
    function_principal_id: 'principal.function.one',
    caller_function_id: 'caller.function.one',
    authority_reference: 'authority://one',
    route: {
      contract_id: 'contract.one.v1',
      operation_id: 'operation.one',
      function_id: 'function.one',
      provider_pack_id: 'provider-pack',
    },
    schema: {
      input_schema: {
        type: 'object',
        properties: {prompt: {type: 'string'}},
      },
    },
    input_schema: {
      type: 'object',
      properties: {prompt: {type: 'string'}},
    },
  };
  await assert.rejects(
    invokeRuntimeOperation({
      envelope: envelope('operations', {operations: []}),
      operation: {...operation, catalog_digest: digest('d')},
      payload: {},
    }),
    (error: unknown) => error instanceof RuntimeSurfaceError && error.code === 'DIGEST_MISMATCH',
  );
});

test('finite artifact entries accept only normalized manifest-relative paths', () => {
  for (const path of ['/tmp/host', 'file:///tmp/host', 'C:/host', './artifact', '../artifact', 'a//b', 'a\\b']) {
    assert.equal(extractFiniteArtifactEntries({artifact_entries: [{
      entry_id: 'artifact-one', owner_pack_id: 'provider-pack', path, kind: 'manifest', artifact_digest: digest('a'),
    }]}), null, path);
  }
  assert.equal(extractFiniteArtifactEntries({artifact_entries: [{
    entry_id: 'artifact-one', owner_pack_id: 'provider-pack', path: 'artifacts/input.json', kind: 'manifest', artifact_digest: digest('a'),
  }]}).length, 1);
});

test('route projection consumes exact frontend map metadata without synthesizing routes', () => {
  const rows = extractExactRouteDescriptors({routes: [{
    route_id: 'route-one',
    method: 'GET',
    logical_target: '/api/runtime-surface/profile',
    contract_id: 'tobkiri.host.control-presentation.v4',
    operation_id: 'profile.read',
    contribution_id: 'defaults.runtime-surface.profile',
    presentation: 'profile',
    owner_pack_id: 'defaultspack',
    provider_id: 'tobkiri.host.control-presentation',
    function_id: 'profile-reader',
    function_principal_id: 'principal-one',
    manifest_digest: digest('2'),
    frontend_map_digest: digest('3'),
    allowed_payload_keys: ['expected_profile_revision', 'expected_plan_digest'],
    security: {
      transport: 'canonical_contract',
      panel_authentication_required: true,
      broker_authority_required: true,
      csrf_required: false,
      request_id_required: false,
      replay_protection_required: false,
    },
  }]});
  assert.equal(rows.length, 1);
  assert.equal(rows[0].frontend_map_digest, digest('3'));
  assert.equal(rows[0].security.transport, 'canonical_contract');
  assert.deepEqual(extractExactRouteDescriptors({operations: [{operation_id: 'profile.read'}]}), []);
});

test('operation invocation fails before Broker dispatch on stale catalog, denial, or authority payload', async () => {
  const operation = {
    action: 'contract_invoke' as const,
    operation_id: 'operation.one',
    contract_id: 'contract.one.v1',
    owner_pack_id: 'provider-pack',
    contribution_id: 'contribution-one',
    target_provider_id: 'provider.one',
    artifact_digest: digest('1'),
    invocation_contribution_id: 'invocation-contribution-one',
    invocation_owner_pack_id: 'provider-pack',
    invocation_catalog_hash: digest('c'),
    invocation_reason: null,
    invokable: true,
    catalog_digest: digest('4'),
    function_id: 'function-one',
    function_principal_id: 'principal.function-one',
    caller_function_id: 'caller.function-one',
    authority_reference: 'authority://one',
    route: {
      contract_id: 'contract.one.v1',
      operation_id: 'operation.one',
      function_id: 'function-one',
      provider_pack_id: 'provider-pack',
    },
    schema: {
      input_schema: {
        type: 'object',
        properties: {prompt: {type: 'string'}},
      },
    },
    input_schema: {
      type: 'object',
      properties: {prompt: {type: 'string'}},
    },
  };
  const validEnvelope = envelope('operations', {operations: [operation]});
  await assert.rejects(
    invokeRuntimeOperation({envelope: validEnvelope, operation, payload: {prompt: 'hello'}}),
    (error: unknown) => error instanceof RuntimeSurfaceError && error.code === 'DIGEST_MISMATCH',
  );
  await assert.rejects(
    invokeRuntimeOperation({
      envelope: envelope('operations', {
        operations: [{...operation, catalog_digest: digest('c'), invokable: false}],
      }),
      operation: {...operation, catalog_digest: digest('c'), invokable: false},
      payload: {},
    }),
    (error: unknown) => error instanceof RuntimeSurfaceError && error.code === 'APPROVAL_DENIED',
  );
  await assert.rejects(
    invokeRuntimeOperation({
      envelope: envelope('operations', {
        operations: [{...operation, catalog_digest: digest('c')}],
      }),
      operation: {...operation, catalog_digest: digest('c')},
      payload: {approved: true},
    }),
    (error: unknown) => error instanceof RuntimeSurfaceError && error.code === 'INVALID',
  );
  await assert.rejects(
    invokeRuntimeOperation({
      envelope: envelope('operations', {
        operations: [{...operation, catalog_digest: digest('c')}],
      }),
      operation: {...operation, catalog_digest: digest('c')},
      payload: {},
    }),
    (error: unknown) => error instanceof RuntimeSurfaceError && error.code === 'APPROVAL_DENIED',
  );
  await assert.rejects(
    invokeRuntimeOperation({
      envelope: {...validEnvelope, state: 'stale'},
      operation,
      payload: {prompt: 'hello'},
    }),
    (error: unknown) => error instanceof RuntimeSurfaceError && error.code === 'STALE',
  );
  await assert.rejects(
    invokeRuntimeOperation({
      envelope: {...validEnvelope, state: 'blocked'},
      operation,
      payload: {prompt: 'hello'},
    }),
    (error: unknown) => error instanceof RuntimeSurfaceError && error.code === 'APPROVAL_DENIED',
  );
});
