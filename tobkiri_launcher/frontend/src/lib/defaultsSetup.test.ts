import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import test from 'node:test';
import {
  parseDefaultsActivationResponse,
  parseDefaultsSetupState,
  type DefaultsConfirmation,
} from './defaultsSetup';

const canonicalFixture = JSON.parse(readFileSync(new URL(
  '../../../../tobkiri_runtime/tobkiri_protocol/fixtures/defaults_setup_v4.canonical.json',
  import.meta.url,
), 'utf8'));
const preFixBindingFixture = JSON.parse(readFileSync(new URL(
  '../../../../tobkiri_runtime/tobkiri_protocol/fixtures/defaults_setup_v4.pre_fix_binding_shape.json',
  import.meta.url,
), 'utf8'));

function state() {
  return structuredClone(canonicalFixture);
}

function realActivationFixture(): {
  confirmation: DefaultsConfirmation;
  response: Record<string, unknown>;
} {
  const confirmation = {
    ...state().recommended_default_profile.confirmation,
    profile_revision: 'sha256:03eee62aa3851951d7bfe8ecfc4e64defc848e97cfe1ba96ace8e1a78d2cb164',
    plan_digest: 'sha256:8c02ac80815e6189b96d780ae969cb9c2b12af7cc156334de45c4767f9ce78d6',
    authority_snapshot_digest: 'sha256:a13c3777a0c41098a4d4a1b787c315756c5776c359719b369723ee1f96d10e22',
  } as DefaultsConfirmation;
  return {
    confirmation,
    response: {
      activation_id: 'activation:defaults-8c02ac80815e6189',
      audit_receipt: {
        activation_id: 'activation:defaults-8c02ac80815e6189',
        fencing_token: 1,
        reservation_id: 'activation-reservation:oJfXu2HtwTfNe-aRjwbgL19agiZWQHuk',
        state: 'committed',
      },
      authority_snapshot_digest: confirmation.authority_snapshot_digest,
      fencing_token: 1,
      plan_digest: confirmation.plan_digest,
      profile_id: 'defaults',
      profile_revision: confirmation.profile_revision,
      restart_required: false,
      security_epoch: 1,
      setup_api_version: 'io.tobkiri.setup-state.v4',
      state: 'active',
    },
  };
}

test('packaged authenticated setup payload shape accepts the v4 binding fields', () => {
  const parsed = parseDefaultsSetupState(state());
  assert.equal(parsed.state, 'review_required');
  assert.equal(parsed.denial_diagnostic, null);
  assert.deepEqual(parsed.required_transaction, [
    'catalog.verify',
    'profile.resolve',
    'authority.snapshot',
    'activation.prepare',
    'activation.commit',
    'runtime.capture',
  ]);
  assert.equal(
    parsed.recommended_default_profile.confirmation.shell.executable_artifact_digest,
    `sha256:${'e'.repeat(64)}`,
  );
  assert.equal(
    parsed.recommended_default_profile.confirmation.bindings[0].authority_reference,
    `authority-ref:${'f'.repeat(64)}`,
  );
  assert.equal(
    parsed.recommended_default_profile.confirmation.bindings[0].executable_catalog_digest,
    `sha256:${'e'.repeat(64)}`,
  );
  assert.equal(
    parsed.recommended_default_profile.confirmation.bindings[0].execution_kind,
    'pack_vm',
  );
});

test('the pre-fix frontend binding fixture reproduces the packaged GUI rejection', () => {
  const invalid = state();
  const canonicalBinding = invalid.recommended_default_profile.confirmation.bindings[0];
  assert.deepEqual(
    preFixBindingFixture.missing_canonical_fields,
    Object.keys(canonicalBinding).filter(
      (key) => !Object.hasOwn(preFixBindingFixture.binding, key),
    ),
  );
  invalid.recommended_default_profile.confirmation.bindings[0] =
    structuredClone(preFixBindingFixture.binding);
  assert.throws(
    () => parseDefaultsSetupState(invalid),
    /Defaults binding has unknown or missing fields/,
  );
});

test('typed setup contract requires a valid executable artifact digest', () => {
  const missing = state();
  delete (missing.recommended_default_profile.confirmation.shell as Record<string, unknown>)
    .executable_artifact_digest;
  assert.throws(
    () => parseDefaultsSetupState(missing),
    /Confirmed Shell has unknown or missing fields/,
  );

  const invalid = state();
  (invalid.recommended_default_profile.confirmation.shell as Record<string, unknown>)
    .executable_artifact_digest = 'sha256:not-a-digest';
  assert.throws(
    () => parseDefaultsSetupState(invalid),
    /Confirmed Shell executable artifact digest is invalid/,
  );
});

test('typed setup contract rejects extra confirmation shell fields', () => {
  const extra = state();
  (extra.recommended_default_profile.confirmation.shell as Record<string, unknown>)
    .untrusted_digest = `sha256:${'f'.repeat(64)}`;
  assert.throws(
    () => parseDefaultsSetupState(extra),
    /Confirmed Shell has unknown or missing fields/,
  );
});

test('packaged setup parser rejects binding field drift and projection tampering', () => {
  const extra = state();
  (extra.recommended_default_profile.confirmation.bindings[0] as Record<string, unknown>)
    .provider_authority_digest = `sha256:${'f'.repeat(64)}`;
  assert.throws(
    () => parseDefaultsSetupState(extra),
    /Defaults binding has unknown or missing fields/,
  );

  const missing = state();
  delete (missing.recommended_default_profile.confirmation.bindings[0] as Record<string, unknown>)
    .requested_scope_digest;
  assert.throws(
    () => parseDefaultsSetupState(missing),
    /Defaults binding has unknown or missing fields/,
  );

  const invalidAuthority = state();
  invalidAuthority.recommended_default_profile.confirmation.bindings[0].authority_reference = 'authority-ref:test';
  assert.throws(
    () => parseDefaultsSetupState(invalidAuthority),
    /Defaults binding authority reference is invalid/,
  );

  const projection = state();
  projection.packs[0].display_name = 'Tampered Pack';
  assert.throws(
    () => parseDefaultsSetupState(projection),
    /Defaults setup Pack projection does not match the Profile/,
  );
});

test('typed setup contract fails closed on provider mismatch or duplication', () => {
  const missing = state();
  missing.recommended_default_profile.confirmation.bindings = [];
  assert.throws(() => parseDefaultsSetupState(missing), /exactly one conversation provider/);

  const duplicate = state();
  duplicate.recommended_default_profile.confirmation.bindings.push(
    {...duplicate.recommended_default_profile.confirmation.bindings[0]},
  );
  assert.throws(() => parseDefaultsSetupState(duplicate), /duplicate identity/);
});

test('typed setup contract rejects stale, malformed, and wrong-type binding evidence', () => {
  const staleRevision = state();
  staleRevision.recommended_default_profile.confirmation.catalog_revision = 'stale';
  assert.throws(() => parseDefaultsSetupState(staleRevision), /catalog_revision is invalid/);

  const wrongEpochType = state();
  wrongEpochType.recommended_default_profile.confirmation.security_epoch = '1';
  assert.throws(() => parseDefaultsSetupState(wrongEpochType), /SecurityEpoch is invalid/);

  const badExecutableCatalog = state();
  badExecutableCatalog.recommended_default_profile.confirmation.bindings[0]
    .executable_catalog_digest = `sha256:${'g'.repeat(64)}`;
  assert.throws(
    () => parseDefaultsSetupState(badExecutableCatalog),
    /executable catalog digest is invalid/,
  );

  const wrongVariantType = state();
  wrongVariantType.recommended_default_profile.confirmation.bindings[0].variant_id = 7;
  assert.throws(() => parseDefaultsSetupState(wrongVariantType), /variant_id is invalid/);

  const duplicateAdapter = state();
  duplicateAdapter.recommended_default_profile.confirmation.bindings[0].adapter_digests = [
    `sha256:${'0'.repeat(64)}`,
    `sha256:${'0'.repeat(64)}`,
  ];
  assert.throws(() => parseDefaultsSetupState(duplicateAdapter), /adapter digests are invalid/);
});

test('activation denial remains typed and fail-closed', () => {
  const denied = state();
  denied.state = 'activation_denied';
  denied.denial_diagnostic = 'Profile revision is stale';
  assert.equal(parseDefaultsSetupState(denied).state, 'activation_denied');

  denied.denial_diagnostic = null;
  assert.throws(() => parseDefaultsSetupState(denied), /requires a diagnostic/);
});

test('typed setup contract rejects legacy and unknown state shapes', () => {
  assert.throws(
    () => parseDefaultsSetupState({state: 'legacy_setup_retired'}),
    /unknown or missing fields/,
  );
});

test('preserved packaged activation success is bound to the submitted confirmation', () => {
  const fixture = realActivationFixture();
  const parsed = parseDefaultsActivationResponse(fixture.response, fixture.confirmation);
  assert.equal(parsed.activation_id, 'activation:defaults-8c02ac80815e6189');
  assert.equal(parsed.audit_receipt.reservation_id, 'activation-reservation:oJfXu2HtwTfNe-aRjwbgL19agiZWQHuk');
  assert.equal(parsed.security_epoch, fixture.confirmation.security_epoch);
});

test('activation evidence rejects every digest, epoch, token, and identity tamper', () => {
  const tamperCases: Array<[string, (response: Record<string, unknown>) => void]> = [
    ['profile revision', (response) => { response.profile_revision = `sha256:${'f'.repeat(64)}`; }],
    ['plan digest', (response) => { response.plan_digest = `sha256:${'f'.repeat(64)}`; }],
    ['authority snapshot', (response) => {
      response.authority_snapshot_digest = `sha256:${'f'.repeat(64)}`;
    }],
    ['profile revision malformed', (response) => { response.profile_revision = 'sha256:not-a-digest'; }],
    ['security epoch equality', (response) => { response.security_epoch = 2; }],
    ['security epoch string', (response) => { response.security_epoch = '1'; }],
    ['security epoch negative', (response) => { response.security_epoch = -1; }],
    ['fencing token zero', (response) => { response.fencing_token = 0; }],
    ['fencing token string', (response) => { response.fencing_token = '1'; }],
    ['fencing token negative', (response) => { response.fencing_token = -1; }],
    ['activation identity empty', (response) => { response.activation_id = ''; }],
    ['activation identity noncanonical', (response) => { response.activation_id = 'activation:test'; }],
    ['reservation identity empty', (response) => {
      (response.audit_receipt as Record<string, unknown>).reservation_id = '';
    }],
    ['reservation identity noncanonical', (response) => {
      (response.audit_receipt as Record<string, unknown>).reservation_id = 'reservation:test';
    }],
    ['audit activation binding', (response) => {
      (response.audit_receipt as Record<string, unknown>).activation_id = 'activation:defaults-aaaaaaaa';
    }],
    ['audit fencing binding', (response) => {
      (response.audit_receipt as Record<string, unknown>).fencing_token = 2;
    }],
  ];

  for (const [label, mutate] of tamperCases) {
    const fixture = realActivationFixture();
    mutate(fixture.response);
    assert.throws(
      () => parseDefaultsActivationResponse(fixture.response, fixture.confirmation),
      undefined,
      label,
    );
  }
});

test('activation evidence rejects a tampered submitted confirmation', () => {
  const fields = ['profile_revision', 'plan_digest', 'authority_snapshot_digest'] as const;
  for (const field of fields) {
    const fixture = realActivationFixture();
    const confirmation = {
      ...fixture.confirmation,
      [field]: `sha256:${'f'.repeat(64)}`,
    } as DefaultsConfirmation;
    assert.throws(
      () => parseDefaultsActivationResponse(fixture.response, confirmation),
      /does not match the submitted confirmation/,
      field,
    );
  }

  const epochFixture = realActivationFixture();
  assert.throws(
    () => parseDefaultsActivationResponse(
      epochFixture.response,
      {...epochFixture.confirmation, security_epoch: 2},
    ),
    /does not match the submitted confirmation/,
  );
});
