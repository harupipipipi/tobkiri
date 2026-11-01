import assert from 'node:assert/strict';
import test from 'node:test';

import { AVATAR_OPTIONS, DEFAULT_AVATAR, isBundledAvatar, profileInitial } from './avatar';

test('default viewer avatars are local deterministic values', () => {
  assert.equal(DEFAULT_AVATAR, '');
  assert.ok(AVATAR_OPTIONS.length >= 3);
  for (const avatar of AVATAR_OPTIONS) {
    assert.equal(isBundledAvatar(avatar), true);
    assert.doesNotMatch(avatar, /^https?:\/\//);
  }
});

test('profile initials fall back locally when no avatar is configured', () => {
  assert.equal(DEFAULT_AVATAR, '');
  assert.equal(profileInitial('Haru'), 'H');
  assert.equal(profileInitial(''), 'U');
});
