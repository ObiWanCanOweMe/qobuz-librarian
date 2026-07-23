import test from 'node:test';
import assert from 'node:assert/strict';
import { mkdtemp, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

import { renderPortainerCompose, validatePortainerCompose } from './validate-portainer-compose.mjs';

test('renders the production Portainer manifest with the required image tag', async () => {
  const rendered = await renderPortainerCompose({
    composePath: new URL('../../deploy/portainer/docker-compose.yml', import.meta.url),
    env: {
      QOBUZ_LIBRARIAN_VERSION: 'v0.11.3',
      PUID: '1000',
      PGID: '1000',
      TZ: 'America/New_York',
    },
  });

  assert.match(rendered, /ghcr\.io\/obiwancanoweme\/qobuz-librarian:v0\.11\.3/);
  assert.match(rendered, /container_name: qobuz-librarian/);
  assert.match(rendered, /10\.20\.0\.9:\$\{WEB_PORT:-8666\}:8666/);
  assert.match(rendered, /\/mnt\/bowl\/media2\/qobuz-librarian:\/music/);
  assert.match(rendered, /\/mnt\/NVMe\/container-data\/qobuz-librarian\/staging:\/staging/);
  assert.match(rendered, /\/mnt\/NVMe\/container-data\/qobuz-librarian\/config:\/config/);
  assert.match(rendered, /\/mnt\/NVMe\/container-data\/qobuz-librarian\/data:\/data/);
  assert.match(rendered, /\/mnt\/NVMe\/container-data\/qobuz-librarian\/upgrade_backups:\/upgrade_backups/);
  assert.doesNotMatch(rendered, /\/migrate-source|\/migrate-dest/);
  assert.doesNotMatch(rendered, /dinkeyes\/qobuz-librarian:latest/);
  assert.doesNotMatch(rendered, /\$\{QOBUZ_LIBRARIAN_VERSION/);
});

test('rejects mutable image tags and source-build manifests', () => {
  assert.throws(
    () => validatePortainerCompose({ composeText: 'services:\n  app:\n    image: dinkeyes/qobuz-librarian:latest\n' }),
    /latest image/i,
  );
  assert.throws(
    () => validatePortainerCompose({ composeText: 'services:\n  app:\n    build: .\n' }),
    /build entries/i,
  );
});

test('rejects non-release image tags', () => {
  for (const tag of ['main', 'latest', 'v0.11.3-rc.1', 'v0.11.3-fork']) {
    assert.throws(
      () => validatePortainerCompose({ composeText: `services:\n  app:\n    image: ghcr.io/obiwancanoweme/qobuz-librarian:${tag}\n` }),
      /stable release image tag/i,
      `expected ${tag} to be rejected`,
    );
  }
});

test('requires QOBUZ_LIBRARIAN_VERSION before rendering', async () => {
  const dir = await mkdtemp(join(tmpdir(), 'qobuz-compose-'));
  const composePath = join(dir, 'compose.yml');
  await writeFile(composePath, 'services:\n  app:\n    image: ghcr.io/x/y:${QOBUZ_LIBRARIAN_VERSION:?required}\n');

  await assert.rejects(
    () => renderPortainerCompose({ composePath, env: {} }),
    /QOBUZ_LIBRARIAN_VERSION/,
  );
});
