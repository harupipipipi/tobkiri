import assert from 'node:assert/strict';
import test from 'node:test';
import {profileLaunchFailure} from './profileLaunchFailure';

test('Setup and Home decode typed launch recovery without exposing native detail', () => {
  const result = profileLaunchFailure(JSON.stringify({code: 'RUNTIME_BACKEND_UNAVAILABLE', action: 'open_packs_to_prepare_packvm', detail: '/private/test-secret'}));
  assert.equal(result.recovery, 'packs');
  assert.match(result.message, /Open Packs/);
  assert.doesNotMatch(result.message, /test-secret|RUNTIME_BACKEND_UNAVAILABLE/);
});

test('unknown launch failures stay redacted', () => {
  for (const error of ['private diagnostic', new Error('/private/path'), '{bad json', {detail: 'private'}]) {
    const result = profileLaunchFailure(error);
    assert.equal(result.recovery, null);
    assert.doesNotMatch(result.message, /private|bad json/);
  }
});

test('Profile reactivation failures give actionable recovery', () => {
  const result = profileLaunchFailure(JSON.stringify({code: 'PROFILE_RERESOLUTION_REQUIRED', action: 'reactivate_or_reresolve_profile'}));
  assert.equal(result.recovery, 'profile');
  assert.match(result.message, /reactivate/);
});
