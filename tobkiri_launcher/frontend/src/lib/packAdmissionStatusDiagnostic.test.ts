import assert from 'node:assert/strict';
import test from 'node:test';
import { packAdmissionStatusDiagnostic } from './packAdmissionStatusDiagnostic';

test('Pack status diagnostics distinguish native origin, Host transport and Host denial', () => {
  assert.equal(packAdmissionStatusDiagnostic('signed Pack trust status requires the current Launcher panel').code, 'PACK_STATUS_PANEL_MISMATCH');
  assert.equal(packAdmissionStatusDiagnostic(new Error('Local Host Pack trust status is unavailable')).code, 'PACK_STATUS_HOST_UNREACHABLE');
  assert.equal(packAdmissionStatusDiagnostic('Local Host Pack trust status was denied').code, 'PACK_STATUS_HOST_DENIED');
  assert.equal(packAdmissionStatusDiagnostic('Local Host Pack trust status returned an invalid response').code, 'PACK_STATUS_INVALID_RESPONSE');
});

test('native ACL diagnostics never expose appended URLs, secrets or capability details', () => {
  const result = packAdmissionStatusDiagnostic('signed_pack_admission_status not allowed. secret-fixture http://localhost:18765/?token=private');
  assert.equal(result.code, 'PACK_STATUS_NATIVE_DENIED');
  assert.doesNotMatch(JSON.stringify(result), /secret-fixture|localhost|18765|token|private/);
});

test('unknown errors fail closed without stringifying objects or showing raw data', () => {
  for (const error of [
    'constructor', 'toString', 'secret-fixture',
    new Error('Local Host Pack trust status was denied secret-fixture'),
    null, undefined, 401,
    { message: 'secret-fixture', toString() { throw new Error('must not stringify'); } },
  ]) {
    const result = packAdmissionStatusDiagnostic(error);
    assert.equal(result.code, 'PACK_STATUS_UNKNOWN');
    assert.doesNotMatch(JSON.stringify(result), /secret-fixture/);
  }
});

test('callers cannot mutate the shared diagnostic mapping', () => {
  const first = packAdmissionStatusDiagnostic('Local Host Pack trust status was denied');
  first.code = 'tampered';
  assert.equal(packAdmissionStatusDiagnostic('Local Host Pack trust status was denied').code, 'PACK_STATUS_HOST_DENIED');
});

test('malformed Error objects do not invoke getters or break recovery', () => {
  const getterError = new Error();
  Object.defineProperty(getterError, 'message', { get() { throw new Error('secret getter'); } });
  const invalidMessage = Object.assign(new Error(), { message: 42 });
  for (const error of [getterError, invalidMessage]) {
    assert.equal(packAdmissionStatusDiagnostic(error).code, 'PACK_STATUS_UNKNOWN');
  }
});
