import test from 'node:test';
import assert from 'node:assert/strict';

import { assertImageTagAbsent } from './assert-image-tag-absent.mjs';

test('passes when GHCR reports the tag is absent', async () => {
  const calls = [];
  await assertImageTagAbsent('ghcr.io/obiwancanoweme/qobuz-librarian:v0.11.3', {
    token: 'secret',
    fetchImpl: async (url, options) => {
      calls.push({ url, options });
      return { status: 404, ok: false, text: async () => '' };
    },
  });

  assert.equal(calls.length, 1);
  assert.match(calls[0].url, /\/v2\/obiwancanoweme\/qobuz-librarian\/manifests\/v0\.11\.3$/);
  assert.equal(calls[0].options.headers.Authorization, 'Bearer secret');
});

test('fails when the exact image tag already exists', async () => {
  await assert.rejects(
    () => assertImageTagAbsent('ghcr.io/obiwancanoweme/qobuz-librarian:v0.11.3', {
      fetchImpl: async () => ({ status: 200, ok: true, text: async () => '{}' }),
    }),
    /already exists/,
  );
});

test('rejects unsupported image refs before calling the registry', async () => {
  await assert.rejects(
    () => assertImageTagAbsent('docker.io/dinkeyes/qobuz-librarian:latest', {
      fetchImpl: async () => { throw new Error('fetch should not be called'); },
    }),
    /Unsupported image ref/,
  );
});
