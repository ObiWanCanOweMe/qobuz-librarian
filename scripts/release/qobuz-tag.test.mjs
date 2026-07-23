import test from 'node:test';
import assert from 'node:assert/strict';

import { parseQobuzTag } from './qobuz-tag.mjs';

test('accepts stable semver qobuz release tags', () => {
  assert.deepEqual(parseQobuzTag('v0.11.3'), { version: '0.11.3' });
  assert.deepEqual(parseQobuzTag('v12.4.99'), { version: '12.4.99' });
});

test('rejects prerelease, mutable, and fork-unrelated tags', () => {
  for (const tag of ['0.11.3', 'latest', 'main', 'v0.11', 'v0.11.3-rc.1', 'v0.11.3-obiwave.1']) {
    assert.throws(() => parseQobuzTag(tag), /Invalid qobuz release tag/);
  }
});
