import assert from 'node:assert/strict';
import test from 'node:test';

import {resolveSetupVerificationState} from './setupVerification';

const healthy = {
  isSetupDone: true,
  runtimeReady: false,
  runtimeStatus: 'starting' as const,
  runtimeDisconnected: false,
  defaultsBootstrapRequired: false,
};

test('setup verification stays closed during cold start and panel-only readiness', () => {
  assert.equal(resolveSetupVerificationState(healthy), 'checking');
  assert.equal(resolveSetupVerificationState({
    ...healthy,
    runtimeStatus: 'panel_ready',
  }), 'checking');
});

test('only coherent runtime health opens runtime routes', () => {
  assert.equal(resolveSetupVerificationState({
    ...healthy,
    runtimeReady: true,
    runtimeStatus: 'runtime_ready',
  }), 'verified');
  assert.equal(resolveSetupVerificationState({
    ...healthy,
    runtimeReady: true,
    runtimeStatus: 'starting',
  }), 'checking');
  assert.equal(resolveSetupVerificationState({
    ...healthy,
    runtimeStatus: 'runtime_ready',
  }), 'checking');
});

test('Host catalog verification opens Profile ceremony without launch readiness', () => {
  assert.equal(resolveSetupVerificationState({
    ...healthy,
    isSetupDone: false,
    runtimeStatus: 'panel_ready',
    hostCatalogVerified: true,
    profileCeremonyAvailable: true,
    defaultsBootstrapRequired: false,
  }), 'verified');
  assert.equal(resolveSetupVerificationState({
    ...healthy,
    isSetupDone: false,
    runtimeStatus: 'panel_ready',
    hostCatalogVerified: true,
    profileCeremonyAvailable: false,
  }), 'needs_setup');
});

test('Defaults bootstrap blocks the generic ceremony before existing-profile activation', () => {
  assert.equal(resolveSetupVerificationState({
    ...healthy,
    isSetupDone: false,
    runtimeStatus: 'panel_ready',
    hostCatalogVerified: true,
    profileCeremonyAvailable: true,
    defaultsBootstrapRequired: true,
  }), 'needs_setup');

  assert.equal(resolveSetupVerificationState({
    ...healthy,
    isSetupDone: false,
    runtimeStatus: 'panel_ready',
    hostCatalogVerified: true,
    profileCeremonyAvailable: true,
    defaultsBootstrapRequired: false,
  }), 'verified');
});

test('reconfirmation, errors, disconnects, and incomplete setup fail closed', () => {
  assert.equal(resolveSetupVerificationState({
    ...healthy,
    runtimeStatus: 'profile_reconfirmation_required',
  }), 'needs_reconfirm');
  assert.equal(resolveSetupVerificationState({
    ...healthy,
    runtimeStatus: 'error',
  }), 'denied');
  assert.equal(resolveSetupVerificationState({
    ...healthy,
    runtimeReady: true,
    runtimeStatus: 'runtime_ready',
    runtimeDisconnected: true,
  }), 'denied');
  assert.equal(resolveSetupVerificationState({
    ...healthy,
    isSetupDone: false,
  }), 'needs_setup');
});
