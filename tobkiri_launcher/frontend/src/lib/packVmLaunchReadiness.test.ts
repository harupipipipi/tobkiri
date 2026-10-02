import assert from 'node:assert/strict';
import test from 'node:test';

import type {ApiPackVMDoctor} from './apiTypes';
import {packVmLaunchBlockedReason, profileLaunchBlockedReason} from './packVmLaunchReadiness';

const doctor: ApiPackVMDoctor = {
  ready: true,
  backend_id: 'macos-vz',
  platform: 'macos-arm64',
  instance: 'tobkiri-packvm-v4',
  reason: null,
  attestation_digest: 'a'.repeat(64),
};

test('an unknown or healthy doctor does not suppress Host launch preparation', () => {
  assert.equal(packVmLaunchBlockedReason(doctor), null);
  assert.equal(packVmLaunchBlockedReason(null), null);
});

test('Windows without a PackVM backend requires the bundled runtime, not Docker', () => {
  const reason = packVmLaunchBlockedReason({
    ...doctor,
    ready: false,
    platform: 'windows-amd64',
    reason: 'This build can provision PackVM only on macOS on Apple Silicon.',
    attestation_digest: null,
  }) ?? '';
  assert.match(reason, /bundled QEMU\/WHPX.*#1494/);
  assert.doesNotMatch(reason, /Docker/);
});

test('recoverable macOS provisioning remains available through Launch', () => {
  assert.equal(packVmLaunchBlockedReason({
    ...doctor,
    ready: false,
    reason: 'not provisioned',
    attestation_digest: null,
  }), null);
});

test('a recoverable Windows WHPX backend is not classified as unsupported', () => {
  assert.equal(packVmLaunchBlockedReason({
    ...doctor,
    ready: false,
    backend_id: 'windows-whpx',
    platform: 'windows-amd64',
    reason: 'PackVM has not completed explicit provisioning',
    attestation_digest: null,
  }), null);
});

test('Profile authorization is required even for a verified Shell and healthy VM', () => {
  for (const [activeProfileReady, launchReady] of [[false, true], [true, false], [false, false]]) {
    assert.match(profileLaunchBlockedReason({activeProfileReady, launchReady, packVmDoctor: doctor}) ?? '', /Profile.*not ready/);
  }
});

test('authorized Profile launch keeps recoverable runtime preparation available', () => {
  for (const packVmDoctor of [null, doctor, {...doctor, ready: false, reason: 'not provisioned', attestation_digest: null}]) {
    assert.equal(profileLaunchBlockedReason({activeProfileReady: true, launchReady: true, packVmDoctor}), null);
  }
});

test('Profile launch shares the terminal unsupported Windows gate', () => {
  assert.match(profileLaunchBlockedReason({
    activeProfileReady: true, launchReady: true,
    packVmDoctor: {...doctor, ready: false, platform: 'windows-amd64', reason: 'This build can provision PackVM only on macOS on Apple Silicon.', attestation_digest: null},
  }) ?? '', /bundled QEMU/);
});
