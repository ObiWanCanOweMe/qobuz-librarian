import test from 'node:test';
import assert from 'node:assert/strict';

import {
  DeploymentRolledBackError,
  PortainerClient,
  renderReleaseManifest,
  deployWithRollback,
  probeHealth,
  upsertEnv,
} from './portainer-client.mjs';

test('renders only the required qobuz version token', () => {
  const manifest = 'image: ghcr.io/obiwancanoweme/qobuz-librarian:${QOBUZ_LIBRARIAN_VERSION:?required}\n';
  assert.equal(
    renderReleaseManifest(manifest, 'v0.11.3'),
    'image: ghcr.io/obiwancanoweme/qobuz-librarian:v0.11.3\n',
  );
  assert.throws(() => renderReleaseManifest('image: latest\n', 'v0.11.3'), /no exact QOBUZ_LIBRARIAN_VERSION/);
});

test('upserts the release env without duplicating operator settings', () => {
  assert.deepEqual(
    upsertEnv([{ name: 'TZ', value: 'America/New_York' }], 'QOBUZ_LIBRARIAN_VERSION', 'v0.11.3'),
    [
      { name: 'TZ', value: 'America/New_York' },
      { name: 'QOBUZ_LIBRARIAN_VERSION', value: 'v0.11.3' },
    ],
  );
});

test('PortainerClient snapshots stack file and updates with pull/prune', async () => {
  const calls = [];
  const client = new PortainerClient({
    baseUrl: 'https://portainer.example',
    apiKey: 'secret',
    stackId: '7',
    endpointId: '3',
    signalFactory: () => undefined,
    fetchImpl: async (url, options = {}) => {
      calls.push({ url, options });
      if (url.endsWith('/api/stacks/7')) return { ok: true, text: async () => JSON.stringify({ Env: [] }) };
      if (url.endsWith('/api/stacks/7/file')) return { ok: true, text: async () => JSON.stringify({ StackFileContent: 'old' }) };
      if (url.endsWith('/api/stacks/7?endpointId=3')) return { ok: true, text: async () => '' };
      throw new Error(`unexpected URL ${url}`);
    },
  });

  assert.deepEqual(await client.snapshotStack(), { Env: [], StackFileContent: 'old' });
  await client.updateStack({ Env: [], StackFileContent: 'new' });
  assert.equal(calls[2].options.method, 'PUT');
  assert.match(calls[2].options.body, /"PullImage":true/);
  assert.match(calls[2].options.body, /"Prune":true/);
  assert.equal(calls[2].options.headers['X-API-Key'], 'secret');
});

test('probeHealth accepts the qobuz health endpoint', async () => {
  await probeHealth('http://example.test/healthz', {
    fetchImpl: async () => ({ status: 200, json: async () => ({ ok: true }) }),
    attempts: 1,
  });
});

test('deployWithRollback restores the previous snapshot when health fails', async () => {
  const updates = [];
  const healthStatuses = [500, 200];
  const client = {
    snapshotStack: async () => ({
      Env: [{ name: 'QOBUZ_LIBRARIAN_VERSION', value: 'v0.11.2' }],
      StackFileContent: 'image: old\n',
    }),
    updateStack: async (snapshot) => updates.push(snapshot),
  };

  await assert.rejects(
    () => deployWithRollback({
      client,
      manifest: 'image: ${QOBUZ_LIBRARIAN_VERSION:?required}\n',
      targetVersion: 'v0.11.3',
      healthUrl: 'http://example.test/healthz',
      attempts: 1,
      retryDelayMs: 0,
      fetchImpl: async () => ({ status: healthStatuses.shift(), json: async () => ({}) }),
    }),
    DeploymentRolledBackError,
  );
  assert.deepEqual(healthStatuses, []);
  assert.equal(updates.length, 2);
  assert.equal(updates[0].Env.find((entry) => entry.name === 'QOBUZ_LIBRARIAN_VERSION').value, 'v0.11.3');
  assert.equal(updates[1].StackFileContent, 'image: old\n');
});
