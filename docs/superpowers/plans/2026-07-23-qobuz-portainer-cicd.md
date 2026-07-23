# Qobuz Portainer CI/CD Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a Portainer-backed CI/CD pipeline for `ObiWanCanOweMe/qobuz-librarian` that validates PRs, publishes immutable fork-owned GHCR images, scans them, deploys releases through Portainer, and rolls back on failed health checks.

**Architecture:** Keep the current application and Docker image intact while adding a small Node-based release/deploy toolchain under `scripts/`. Production uses an image-only Portainer Compose manifest with `QOBUZ_LIBRARIAN_VERSION` as the only release-controlled value. GitHub Actions reuse existing tests and smoke checks, then deploy the exact release tag through Portainer's API.

**Tech Stack:** GitHub Actions, Release Please, Docker Buildx, GHCR, Trivy, Node 24 built-in `node:test`, Portainer REST API, existing Python/ruff/pytest/Tailwind/Docker smoke tooling.

## Global Constraints

- Repository: `ObiWanCanOweMe/qobuz-librarian`.
- Production host: `ark`, managed by Portainer.
- Container registry: `ghcr.io/obiwancanoweme/qobuz-librarian`.
- Production trigger: release tags, not branch heads or `latest`.
- Production health probe variable: `QOBUZ_HEALTH_URL`, expected production value `http://10.20.0.9:8666/healthz`.
- Required image version variable: `QOBUZ_LIBRARIAN_VERSION`.
- Preserve container name `qobuz-librarian`.
- Preserve host bind `10.20.0.9:8666->8666`.
- Preserve music mount `/mnt/bowl/media2/qobuz-librarian:/music`.
- Preserve staging mount `/mnt/NVMe/container-data/qobuz-librarian/staging:/staging`.
- Preserve config mount `/mnt/NVMe/container-data/qobuz-librarian/config:/config`.
- Preserve data mount `/mnt/NVMe/container-data/qobuz-librarian/data:/data`.
- Preserve upgrade backup mount `/mnt/NVMe/container-data/qobuz-librarian/upgrade_backups:/upgrade_backups`.
- Do not include migration-only mounts `/migrate-source` or `/migrate-dest` in the release-controlled production manifest.
- CI/CD must not copy, prune, mutate, migrate, or rewrite the music library.
- Production must not depend on `dinkeyes/qobuz-librarian:latest`.

---

## File Structure

- Create `deploy/portainer/docker-compose.yml`: image-only production Compose manifest for the Portainer stack.
- Create `scripts/release/qobuz-tag.mjs`: validates release tags used by image publication and deployment.
- Create `scripts/release/qobuz-tag.test.mjs`: unit tests for release tag parsing.
- Create `scripts/ci/assert-image-tag-absent.mjs`: fails release preflight if the exact GHCR tag already exists.
- Create `scripts/ci/assert-image-tag-absent.test.mjs`: mocked fetch tests for image tag preflight.
- Create `scripts/ci/validate-portainer-compose.mjs`: verifies the Portainer manifest renders without `latest`, `build:`, migration mounts, or unresolved version placeholders.
- Create `scripts/ci/validate-portainer-compose.test.mjs`: manifest contract tests.
- Create `scripts/ci/workflow-contract.test.mjs`: workflow dependency and action-version tests.
- Create `scripts/deploy/portainer-client.mjs`: small Portainer client, manifest rendering, health probe, snapshot, deploy, and rollback logic.
- Create `scripts/deploy/portainer-client.test.mjs`: mocked client/fetch tests for successful deploy and rollback failure paths.
- Create `scripts/deploy/portainer-release.mjs`: GitHub Actions entrypoint that reads env vars and calls the Portainer client.
- Create `release-please-config.json`: Release Please config for the Python project.
- Create `.release-please-manifest.json`: initial version manifest, matching `pyproject.toml` version `0.11.2`.
- Modify `.github/workflows/test.yml`: add deployment contract job.
- Modify `.github/workflows/docker.yml`: convert release publishing to GHCR immutable release tags and add Portainer deployment.
- Create `.github/workflows/release-please.yml`: opens/updates release PRs from `main`.
- Create `.github/workflows/scan-images.yml`: reusable, scheduled, and manual Trivy SARIF scans.
- Modify `package.json`: add `test:deployment-contract` script.
- Do not modify `CHANGELOG.md` in this implementation; Release Please owns later changelog updates through release PRs.

---

### Task 1: Portainer Manifest And Manifest Contract Tests

**Files:**
- Create: `deploy/portainer/docker-compose.yml`
- Create: `scripts/ci/validate-portainer-compose.mjs`
- Create: `scripts/ci/validate-portainer-compose.test.mjs`
- Modify: `package.json`

**Interfaces:**
- Produces: `renderPortainerCompose({ composePath, env }) -> Promise<string>`
- Produces: `validatePortainerCompose({ composeText }) -> void`
- Consumes: no previous task output

- [ ] **Step 1: Add the failing manifest tests**

Create `scripts/ci/validate-portainer-compose.test.mjs`:

```js
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

test('requires QOBUZ_LIBRARIAN_VERSION before rendering', async () => {
  const dir = await mkdtemp(join(tmpdir(), 'qobuz-compose-'));
  const composePath = join(dir, 'compose.yml');
  await writeFile(composePath, 'services:\n  app:\n    image: ghcr.io/x/y:${QOBUZ_LIBRARIAN_VERSION:?required}\n');

  await assert.rejects(
    () => renderPortainerCompose({ composePath, env: {} }),
    /QOBUZ_LIBRARIAN_VERSION/,
  );
});
```

- [ ] **Step 2: Run the failing test**

Run: `node --test scripts/ci/validate-portainer-compose.test.mjs`

Expected: FAIL with `Cannot find module` for `validate-portainer-compose.mjs`.

- [ ] **Step 3: Add the Portainer manifest**

Create `deploy/portainer/docker-compose.yml`:

```yaml
services:
  qobuz-librarian:
    image: ghcr.io/obiwancanoweme/qobuz-librarian:${QOBUZ_LIBRARIAN_VERSION:?required}
    container_name: qobuz-librarian
    restart: unless-stopped
    init: true
    mem_limit: ${QL_MEM_LIMIT:-16g}
    pids_limit: 256
    security_opt:
      - "no-new-privileges:true"
    cap_drop:
      - ALL
    cap_add:
      - CHOWN
      - DAC_OVERRIDE
      - FOWNER
      - SETUID
      - SETGID
    ports:
      - "10.20.0.9:${WEB_PORT:-8666}:8666"
    environment:
      PUID: "${PUID:-1000}"
      PGID: "${PGID:-1000}"
      TZ: "${TZ:-}"
      QOBUZ_USER_AUTH_TOKEN: "${QOBUZ_USER_AUTH_TOKEN:-}"
      QOBUZ_USER_ID: "${QOBUZ_USER_ID:-}"
      QOBUZ_USER_AUTH_TOKEN_FILE: "${QOBUZ_USER_AUTH_TOKEN_FILE:-}"
      MUSIC_ROOT: /music
      STAGING_DIR: /staging
      DATA_DIR: /data
      UPGRADE_BACKUP_DIR: /upgrade_backups
      MIGRATE_SRC: ""
      MIGRATE_DEST: ""
      BEETS_CONFIG_DIR: /config/beets
      BEETS_DB_PATH: /config/beets/musiclibrary.db
      BEETS_PATH_DEFAULT: "${BEETS_PATH_DEFAULT:-}"
      BEETS_PATH_SINGLETON: "${BEETS_PATH_SINGLETON:-}"
      BEETS_PATH_COMP: "${BEETS_PATH_COMP:-}"
      BEETS_PLUGINS: "${BEETS_PLUGINS:-}"
      STREAMRIP_CONFIG: /config/streamrip/config.toml
      WEB_HOST: "0.0.0.0"
      WEB_PORT: "8666"
      WEB_AUTH: "${WEB_AUTH:-}"
      WEB_AUTH_USER: "${WEB_AUTH_USER:-}"
      WEB_AUTH_PASSWORD: "${WEB_AUTH_PASSWORD:-}"
      WEB_AUTH_PASSWORD_FILE: "${WEB_AUTH_PASSWORD_FILE:-}"
      FORWARDED_ALLOW_IPS: "${FORWARDED_ALLOW_IPS:-127.0.0.1}"
      QL_CHECK_VOLUMES: "${QL_CHECK_VOLUMES:-1}"
      QL_CLI_ONLY: "${QL_CLI_ONLY:-}"
      QOBUZ_API_BASE: https://www.qobuz.com/api.json/0.2
      QOBUZ_APP_ID: "798273057"
      RIP_TIMEOUT: "${RIP_TIMEOUT:-900}"
      DELAY_BETWEEN: "${DELAY_BETWEEN:-1.0}"
      ARTIST_API_DELAY: "${ARTIST_API_DELAY:-0.0}"
      ARTIST_SCAN_WORKERS: "${ARTIST_SCAN_WORKERS:-4}"
      SEARCH_LIMIT: "${SEARCH_LIMIT:-25}"
      ARTIST_CATALOG_LIMIT: "${ARTIST_CATALOG_LIMIT:-1000}"
      MISSING_ALBUMS_MIN_TRACKS: "${MISSING_ALBUMS_MIN_TRACKS:-4}"
      STREAMRIP_QUALITY: "${STREAMRIP_QUALITY:-4}"
      LYRICS_ENABLED: "${LYRICS_ENABLED:-true}"
      LYRICS_FORMAT: "${LYRICS_FORMAT:-embed}"
      LYRICS_PROVIDERS: "${LYRICS_PROVIDERS:-}"
      PREFER_HIRES: "${PREFER_HIRES:-true}"
      CONSOLIDATE: "${CONSOLIDATE:-false}"
      MIGRATE_MULTI_ARTIST: "${MIGRATE_MULTI_ARTIST:-false}"
      ARTWORK: "${ARTWORK:-sidecar}"
      AUTO_LIBRARY_SCAN: "${AUTO_LIBRARY_SCAN:-true}"
      NEW_RELEASE_CHECK_INTERVAL: "${NEW_RELEASE_CHECK_INTERVAL:-86400}"
      NEW_RELEASE_MAX_AGE_DAYS: "${NEW_RELEASE_MAX_AGE_DAYS:-365}"
      ARTIST_CATALOG_CACHE_TTL: "${ARTIST_CATALOG_CACHE_TTL:-604800}"
      UPGRADE_SCAN_ENABLED: "${UPGRADE_SCAN_ENABLED:-true}"
      AUTO_UPGRADE_ENABLED: "${AUTO_UPGRADE_ENABLED:-false}"
      UPGRADE_BACKUP_RETENTION_DAYS: "${UPGRADE_BACKUP_RETENTION_DAYS:-7}"
      UPGRADE_SINGLES_ENABLED: "${UPGRADE_SINGLES_ENABLED:-false}"
      CAPPED_RETENTION_DAYS: "${CAPPED_RETENTION_DAYS:-90}"
      DOWNSAMPLE_HIRES_ENABLED: "${DOWNSAMPLE_HIRES_ENABLED:-false}"
      DOWNSAMPLE_KEEP_ORIGINALS: "${DOWNSAMPLE_KEEP_ORIGINALS:-}"
      FUZZY_DIR_THRESH: "${FUZZY_DIR_THRESH:-0.78}"
      ARTIST_NAME_THRESH: "${ARTIST_NAME_THRESH:-0.85}"
      ARTIST_DIR_MATCH_THRESH: "${ARTIST_DIR_MATCH_THRESH:-0.65}"
      CONSOLIDATE_THRESH: "${CONSOLIDATE_THRESH:-0.70}"
      AUTO_SAFE_TITLE_SIM_THRESH: "${AUTO_SAFE_TITLE_SIM_THRESH:-0.85}"
      BEETS_TIMEOUT: "${BEETS_TIMEOUT:-600}"
      BEETS_MAX_ATTEMPTS: "${BEETS_MAX_ATTEMPTS:-2}"
      BEETS_RETRY_PAUSE: "${BEETS_RETRY_PAUSE:-30}"
      RATE_LIMIT_COOLDOWN: "${RATE_LIMIT_COOLDOWN:-30}"
      RESAMPLE_WORKERS: "${RESAMPLE_WORKERS:-4}"
      MIN_FREE_STAGING_MB: "${MIN_FREE_STAGING_MB:-500}"
      REPAIR_LOOKUP_MIN_INTERVAL: "${REPAIR_LOOKUP_MIN_INTERVAL:-0.05}"
      REPAIR_CACHE_ENABLED: "${REPAIR_CACHE_ENABLED:-true}"
      REPAIR_CACHE_TTL_DAYS: "${REPAIR_CACHE_TTL_DAYS:-30}"
      EXCLUDE_LIVE_ALBUMS: "${EXCLUDE_LIVE_ALBUMS:-false}"
      LOG_LEVEL: "${LOG_LEVEL:-INFO}"
      POST_JOB_HOOK: "${POST_JOB_HOOK:-}"
      POST_JOB_HOOK_TIMEOUT: "${POST_JOB_HOOK_TIMEOUT:-10}"
      QL_WEB_FETCH_TIMEOUT: "${QL_WEB_FETCH_TIMEOUT:-12.0}"
      QL_WEB_TEST_AUTH_TIMEOUT: "${QL_WEB_TEST_AUTH_TIMEOUT:-8.0}"
    volumes:
      - /mnt/bowl/media2/qobuz-librarian:/music
      - /mnt/NVMe/container-data/qobuz-librarian/staging:/staging
      - /mnt/NVMe/container-data/qobuz-librarian/config:/config
      - /mnt/NVMe/container-data/qobuz-librarian/data:/data
      - /mnt/NVMe/container-data/qobuz-librarian/upgrade_backups:/upgrade_backups
    logging:
      driver: json-file
      options:
        max-size: "10m"
        max-file: "3"
```

- [ ] **Step 4: Implement manifest rendering and validation**

Create `scripts/ci/validate-portainer-compose.mjs`:

```js
#!/usr/bin/env node

import { readFile } from 'node:fs/promises';
import { fileURLToPath } from 'node:url';

const VERSION_TOKEN = '${QOBUZ_LIBRARIAN_VERSION:?required}';
const UNRESOLVED_VERSION = /\$\{QOBUZ_LIBRARIAN_VERSION[^}]*\}/;

export function validatePortainerCompose({ composeText }) {
  if (/^\s*build\s*:/m.test(composeText)) {
    throw new Error('Portainer manifest must not contain build entries');
  }
  if (/qobuz-librarian:latest|dinkeyes\/qobuz-librarian:latest/.test(composeText)) {
    throw new Error('Portainer manifest must not use a latest image');
  }
  if (/\/migrate-source|\/migrate-dest/.test(composeText)) {
    throw new Error('Portainer manifest must not include migration-only mounts');
  }
  if (UNRESOLVED_VERSION.test(composeText)) {
    throw new Error('Portainer manifest has unresolved QOBUZ_LIBRARIAN_VERSION');
  }
  if (!/container_name:\s*qobuz-librarian/.test(composeText)) {
    throw new Error('Portainer manifest must preserve container_name qobuz-librarian');
  }
}

export async function renderPortainerCompose({ composePath, env = process.env } = {}) {
  const version = env.QOBUZ_LIBRARIAN_VERSION?.trim();
  if (!version) {
    throw new Error('QOBUZ_LIBRARIAN_VERSION is required to render the Portainer manifest');
  }
  const raw = await readFile(composePath, 'utf8');
  if (!raw.includes(VERSION_TOKEN)) {
    throw new Error('Portainer manifest has no exact QOBUZ_LIBRARIAN_VERSION placeholder');
  }
  const rendered = raw.replaceAll(VERSION_TOKEN, version);
  validatePortainerCompose({ composeText: rendered });
  return rendered;
}

if (process.argv[1] && fileURLToPath(import.meta.url) === process.argv[1]) {
  const composePath = process.argv[2] ?? 'deploy/portainer/docker-compose.yml';
  renderPortainerCompose({ composePath })
    .then(() => console.log('Portainer manifest contract passed'))
    .catch((error) => {
      console.error(error.message);
      process.exitCode = 1;
    });
}
```

- [ ] **Step 5: Add the deployment contract npm script**

Modify `package.json` scripts to include:

```json
"test:deployment-contract": "node --test scripts/release/*.test.mjs scripts/ci/*.test.mjs scripts/deploy/*.test.mjs"
```

Keep the existing `build` and `watch` scripts unchanged.

- [ ] **Step 6: Run the manifest tests**

Run: `node --test scripts/ci/validate-portainer-compose.test.mjs`

Expected: PASS, 3 tests.

- [ ] **Step 7: Render with Docker Compose**

Run:

```bash
QOBUZ_LIBRARIAN_VERSION=v0.11.3 docker compose -f deploy/portainer/docker-compose.yml config --quiet
```

Expected: PASS with no output.

- [ ] **Step 8: Commit**

```bash
git add deploy/portainer/docker-compose.yml scripts/ci/validate-portainer-compose.mjs scripts/ci/validate-portainer-compose.test.mjs package.json
git commit -m "test: add portainer manifest contract"
```

---

### Task 2: Release Tag And Image Tag Preflight

**Files:**
- Create: `scripts/release/qobuz-tag.mjs`
- Create: `scripts/release/qobuz-tag.test.mjs`
- Create: `scripts/ci/assert-image-tag-absent.mjs`
- Create: `scripts/ci/assert-image-tag-absent.test.mjs`

**Interfaces:**
- Produces: `parseQobuzTag(tag: string) -> { version: string }`
- Produces: `assertImageTagAbsent(imageRef: string, options?: { fetchImpl, token }) -> Promise<void>`
- Consumes: no previous task output

- [ ] **Step 1: Add failing release tag tests**

Create `scripts/release/qobuz-tag.test.mjs`:

```js
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
```

- [ ] **Step 2: Run the failing tag tests**

Run: `node --test scripts/release/qobuz-tag.test.mjs`

Expected: FAIL with `Cannot find module` for `qobuz-tag.mjs`.

- [ ] **Step 3: Implement release tag parsing**

Create `scripts/release/qobuz-tag.mjs`:

```js
const QOBUZ_TAG = /^v(?<version>0|[1-9]\d*)\.(?<minor>0|[1-9]\d*)\.(?<patch>0|[1-9]\d*)$/;

export function parseQobuzTag(tag) {
  const match = QOBUZ_TAG.exec(tag ?? '');
  if (!match?.groups) {
    throw new Error(`Invalid qobuz release tag: ${tag}`);
  }
  return { version: `${match.groups.version}.${match.groups.minor}.${match.groups.patch}` };
}
```

- [ ] **Step 4: Add failing image preflight tests**

Create `scripts/ci/assert-image-tag-absent.test.mjs`:

```js
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
```

- [ ] **Step 5: Run the failing preflight tests**

Run: `node --test scripts/ci/assert-image-tag-absent.test.mjs`

Expected: FAIL with `Cannot find module` for `assert-image-tag-absent.mjs`.

- [ ] **Step 6: Implement image tag preflight**

Create `scripts/ci/assert-image-tag-absent.mjs`:

```js
#!/usr/bin/env node

import { fileURLToPath } from 'node:url';

function parseGhcrRef(imageRef) {
  const match = /^ghcr\.io\/(?<owner>[^/]+)\/(?<image>[^:]+):(?<tag>[^:]+)$/.exec(imageRef ?? '');
  if (!match?.groups) throw new Error(`Unsupported image ref: ${imageRef}`);
  return match.groups;
}

export async function assertImageTagAbsent(imageRef, { fetchImpl = fetch, token = process.env.GITHUB_TOKEN } = {}) {
  const { owner, image, tag } = parseGhcrRef(imageRef);
  const headers = { Accept: 'application/vnd.oci.image.manifest.v1+json' };
  if (token) headers.Authorization = `Bearer ${token}`;
  const response = await fetchImpl(`https://ghcr.io/v2/${owner}/${image}/manifests/${tag}`, {
    method: 'HEAD',
    headers,
  });
  if (response.status === 404) return;
  if (response.ok) throw new Error(`Image tag already exists: ${imageRef}`);
  throw new Error(`Unable to prove image tag is absent: ${imageRef} returned HTTP ${response.status}`);
}

if (process.argv[1] && fileURLToPath(import.meta.url) === process.argv[1]) {
  assertImageTagAbsent(process.argv[2])
    .then(() => console.log(`Image tag is absent: ${process.argv[2]}`))
    .catch((error) => {
      console.error(error.message);
      process.exitCode = 1;
    });
}
```

- [ ] **Step 7: Run the task tests**

Run:

```bash
node --test scripts/release/qobuz-tag.test.mjs scripts/ci/assert-image-tag-absent.test.mjs
```

Expected: PASS.

- [ ] **Step 8: Commit**

```bash
git add scripts/release/qobuz-tag.mjs scripts/release/qobuz-tag.test.mjs scripts/ci/assert-image-tag-absent.mjs scripts/ci/assert-image-tag-absent.test.mjs
git commit -m "test: add qobuz release tag contracts"
```

---

### Task 3: Portainer Deployment Client With Rollback

**Files:**
- Create: `scripts/deploy/portainer-client.mjs`
- Create: `scripts/deploy/portainer-client.test.mjs`
- Create: `scripts/deploy/portainer-release.mjs`

**Interfaces:**
- Consumes: `parseQobuzTag(tag: string)`
- Produces: `PortainerClient`
- Produces: `renderReleaseManifest(manifest: string, targetVersion: string) -> string`
- Produces: `deployWithRollback({ client, manifest, targetVersion, healthUrl, fetchImpl, attempts, retryDelayMs, sleep }) -> Promise<{ targetVersion, previousVersion }>`
- Produces: `runRelease({ env, readFile, appendFile, clientFactory, deploy, log }) -> Promise<object>`

- [ ] **Step 1: Add failing deployment client tests**

Create `scripts/deploy/portainer-client.test.mjs`:

```js
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
      fetchImpl: async () => ({ status: 500, json: async () => ({}) }),
    }),
    DeploymentRolledBackError,
  );
  assert.equal(updates.length, 2);
  assert.equal(updates[0].Env.find((entry) => entry.name === 'QOBUZ_LIBRARIAN_VERSION').value, 'v0.11.3');
  assert.equal(updates[1].StackFileContent, 'image: old\n');
});
```

- [ ] **Step 2: Run the failing deployment tests**

Run: `node --test scripts/deploy/portainer-client.test.mjs`

Expected: FAIL with `Cannot find module` for `portainer-client.mjs`.

- [ ] **Step 3: Implement the Portainer client**

Create `scripts/deploy/portainer-client.mjs` using the Obiwave client as the local pattern, with qobuz-specific version token and health probe:

```js
const DEFAULT_ATTEMPTS = 6;
const DEFAULT_RETRY_DELAY_MS = 5_000;
const DEFAULT_READ_TIMEOUT_MS = 15_000;
const DEFAULT_UPDATE_TIMEOUT_MS = 300_000;
const VERSION_TOKEN = '${QOBUZ_LIBRARIAN_VERSION:?required}';
const UNRESOLVED_VERSION = /\$\{QOBUZ_LIBRARIAN_VERSION[^}]*\}/;

export class DeploymentRolledBackError extends Error {
  constructor({ targetVersion, previousVersion, cause }) {
    super('Target deployment failed; rollback verified', { cause });
    this.name = 'DeploymentRolledBackError';
    this.targetVersion = targetVersion;
    this.previousVersion = previousVersion;
  }
}

export class RollbackIncidentError extends Error {
  constructor({ targetVersion, previousVersion, deploymentError, rollbackError }) {
    super('Target deployment failed and rollback failed or could not be verified', {
      cause: new AggregateError([deploymentError, rollbackError]),
    });
    this.name = 'RollbackIncidentError';
    this.targetVersion = targetVersion;
    this.previousVersion = previousVersion;
  }
}

export function upsertEnv(env, name, value) {
  const next = [];
  let found = false;
  for (const entry of env) {
    if (entry.name !== name) next.push({ ...entry });
    else if (!found) {
      next.push({ ...entry, value });
      found = true;
    }
  }
  if (!found) next.push({ name, value });
  return next;
}

export function renderReleaseManifest(manifest, targetVersion) {
  if (!manifest.includes(VERSION_TOKEN)) {
    throw new Error('Portainer manifest has no exact QOBUZ_LIBRARIAN_VERSION placeholder');
  }
  const rendered = manifest.replaceAll(VERSION_TOKEN, targetVersion);
  if (UNRESOLVED_VERSION.test(rendered)) {
    throw new Error('Portainer manifest has an unresolved QOBUZ_LIBRARIAN_VERSION placeholder');
  }
  return rendered;
}

export class PortainerClient {
  constructor({ baseUrl, apiKey, stackId, endpointId, fetchImpl = fetch, readTimeoutMs = DEFAULT_READ_TIMEOUT_MS, updateTimeoutMs = DEFAULT_UPDATE_TIMEOUT_MS, signalFactory = AbortSignal.timeout }) {
    Object.assign(this, { apiKey, stackId, endpointId, fetch: fetchImpl, readTimeoutMs, updateTimeoutMs, signalFactory });
    this.baseUrl = baseUrl.replace(/\/$/, '');
  }

  async request(path, { timeoutMs = this.readTimeoutMs, operation = 'request', ...options } = {}) {
    const response = await this.fetch(`${this.baseUrl}/api${path}`, {
      ...options,
      headers: {
        'Content-Type': 'application/json',
        'X-API-Key': this.apiKey,
        ...options.headers,
      },
      signal: this.signalFactory(timeoutMs),
    });
    if (!response.ok) throw new Error(`Portainer ${operation} failed with HTTP ${response.status}`);
    const text = await response.text();
    return text ? JSON.parse(text) : null;
  }

  async snapshotStack() {
    const [stack, file] = await Promise.all([
      this.request(`/stacks/${this.stackId}`),
      this.request(`/stacks/${this.stackId}/file`),
    ]);
    return { Env: stack.Env ?? [], StackFileContent: file.StackFileContent };
  }

  updateStack(snapshot) {
    return this.request(`/stacks/${this.stackId}?endpointId=${this.endpointId}`, {
      method: 'PUT',
      body: JSON.stringify({ ...snapshot, Prune: true, PullImage: true }),
      timeoutMs: this.updateTimeoutMs,
      operation: 'stack update',
    });
  }
}

async function retry(operation, { attempts = DEFAULT_ATTEMPTS, retryDelayMs = DEFAULT_RETRY_DELAY_MS, sleep = (milliseconds) => new Promise((resolve) => setTimeout(resolve, milliseconds)) } = {}) {
  let lastError;
  for (let attempt = 1; attempt <= attempts; attempt += 1) {
    try {
      return await operation();
    } catch (error) {
      lastError = error;
      if (attempt < attempts) await sleep(retryDelayMs);
    }
  }
  throw lastError;
}

export async function probeHealth(url, options = {}) {
  const { fetchImpl = fetch, probeTimeoutMs = 10_000, signalFactory = AbortSignal.timeout, ...retryOptions } = options;
  return retry(async () => {
    const response = await fetchImpl(url, { signal: signalFactory(probeTimeoutMs) });
    if (response.status !== 200) throw new Error(`Health probe failed with HTTP ${response.status}`);
    await response.json().catch(() => ({}));
  }, retryOptions);
}

function versionFrom(env) {
  return env.find((entry) => entry.name === 'QOBUZ_LIBRARIAN_VERSION')?.value ?? null;
}

export async function deployWithRollback({ client, manifest, targetVersion, healthUrl, fetchImpl = fetch, attempts = DEFAULT_ATTEMPTS, retryDelayMs = DEFAULT_RETRY_DELAY_MS, sleep }) {
  const renderedManifest = renderReleaseManifest(manifest, targetVersion);
  const snapshot = await client.snapshotStack();
  const previousVersion = versionFrom(snapshot.Env);
  const target = {
    Env: upsertEnv(snapshot.Env, 'QOBUZ_LIBRARIAN_VERSION', targetVersion),
    StackFileContent: renderedManifest,
  };
  try {
    await client.updateStack(target);
    await probeHealth(healthUrl, { fetchImpl, attempts, retryDelayMs, ...(sleep ? { sleep } : {}) });
  } catch (deploymentError) {
    try {
      await client.updateStack(snapshot);
      await probeHealth(healthUrl, { fetchImpl, attempts, retryDelayMs, ...(sleep ? { sleep } : {}) });
    } catch (rollbackError) {
      throw new RollbackIncidentError({ targetVersion, previousVersion, deploymentError, rollbackError });
    }
    throw new DeploymentRolledBackError({ targetVersion, previousVersion, cause: deploymentError });
  }
  return { targetVersion, previousVersion };
}
```

- [ ] **Step 4: Add the release entrypoint**

Create `scripts/deploy/portainer-release.mjs`:

```js
#!/usr/bin/env node

import { appendFile, readFile } from 'node:fs/promises';
import { fileURLToPath } from 'node:url';

import { parseQobuzTag } from '../release/qobuz-tag.mjs';
import { DeploymentRolledBackError, PortainerClient, RollbackIncidentError, deployWithRollback } from './portainer-client.mjs';

const REQUIRED_ENV = ['PORTAINER_URL', 'PORTAINER_API_KEY', 'PORTAINER_STACK_ID', 'PORTAINER_ENDPOINT_ID', 'QOBUZ_LIBRARIAN_VERSION', 'QOBUZ_HEALTH_URL'];
const MANIFEST_URL = new URL('../../deploy/portainer/docker-compose.yml', import.meta.url);

function releaseConfig(env) {
  const missing = REQUIRED_ENV.filter((name) => !env[name]?.trim());
  if (missing.length > 0) throw new Error(`Missing required release configuration: ${missing.join(', ')}`);
  parseQobuzTag(env.QOBUZ_LIBRARIAN_VERSION);
  return Object.fromEntries(REQUIRED_ENV.map((name) => [name, env[name]]));
}

export async function runRelease({ env = process.env, readFile: readFileImpl = readFile, appendFile: appendFileImpl = appendFile, clientFactory = (options) => new PortainerClient(options), deploy = deployWithRollback, log = console.log } = {}) {
  const config = releaseConfig(env);
  const manifest = await readFileImpl(MANIFEST_URL, 'utf8');
  const client = clientFactory({
    baseUrl: config.PORTAINER_URL,
    apiKey: config.PORTAINER_API_KEY,
    stackId: config.PORTAINER_STACK_ID,
    endpointId: config.PORTAINER_ENDPOINT_ID,
  });

  log(`Deploying Qobuz Librarian ${config.QOBUZ_LIBRARIAN_VERSION}`);
  try {
    const result = await deploy({
      client,
      manifest,
      targetVersion: config.QOBUZ_LIBRARIAN_VERSION,
      healthUrl: config.QOBUZ_HEALTH_URL,
    });
    if (env.GITHUB_STEP_SUMMARY) {
      await appendFileImpl(env.GITHUB_STEP_SUMMARY, `## Portainer deployment\n\n- Target version: \`${result.targetVersion}\`\n- Previous version: \`${result.previousVersion ?? '(not set)'}\`\n`, 'utf8');
    }
    return result;
  } catch (error) {
    if (error instanceof DeploymentRolledBackError && env.GITHUB_STEP_SUMMARY) {
      await appendFileImpl(env.GITHUB_STEP_SUMMARY, `## Portainer deployment failed safely\n\n- Target version: \`${error.targetVersion}\`\n- Previous version: \`${error.previousVersion ?? '(not set)'}\`\n- Status: **Target failed; rollback verified**\n`, 'utf8');
    }
    if (error instanceof RollbackIncidentError && env.GITHUB_STEP_SUMMARY) {
      await appendFileImpl(env.GITHUB_STEP_SUMMARY, `## Portainer rollback incident\n\n- Target version: \`${error.targetVersion}\`\n- Previous version: \`${error.previousVersion ?? '(not set)'}\`\n- Status: **ROLLBACK FAILED OR UNVERIFIED**\n`, 'utf8');
    }
    throw error;
  }
}

if (process.argv[1] && fileURLToPath(import.meta.url) === process.argv[1]) {
  runRelease().catch(() => {
    console.error('Portainer release failed; no sensitive deployment details were printed.');
    process.exitCode = 1;
  });
}
```

- [ ] **Step 5: Run deployment tests**

Run: `node --test scripts/deploy/portainer-client.test.mjs`

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add scripts/deploy/portainer-client.mjs scripts/deploy/portainer-client.test.mjs scripts/deploy/portainer-release.mjs
git commit -m "feat: add portainer release client"
```

---

### Task 4: Release Please And Workflow Contracts

**Files:**
- Create: `release-please-config.json`
- Create: `.release-please-manifest.json`
- Create: `.github/workflows/release-please.yml`
- Create: `scripts/ci/workflow-contract.test.mjs`

**Interfaces:**
- Consumes: `package.json` script `test:deployment-contract`
- Consumes: `scripts/release/qobuz-tag.mjs`
- Produces: Release Please workflow that targets `main`

- [ ] **Step 1: Add failing workflow contract tests**

Create `scripts/ci/workflow-contract.test.mjs`:

```js
import test from 'node:test';
import assert from 'node:assert/strict';
import { readdir, readFile } from 'node:fs/promises';

const workflowDir = new URL('../../.github/workflows/', import.meta.url);

async function workflow(name) {
  return readFile(new URL(name, workflowDir), 'utf8');
}

test('official JavaScript actions use current reviewed major versions', async () => {
  const files = (await readdir(workflowDir)).filter((name) => name.endsWith('.yml'));
  for (const file of files) {
    const text = await workflow(file);
    assert.doesNotMatch(text, /actions\/checkout@v[1-6]/, file);
    assert.doesNotMatch(text, /actions\/setup-python@v[1-5]/, file);
    assert.doesNotMatch(text, /docker\/build-push-action@v[1-6]/, file);
    assert.doesNotMatch(text, /docker\/setup-qemu-action@v[1-3]/, file);
    assert.doesNotMatch(text, /docker\/setup-buildx-action@v[1-2]/, file);
  }
});

test('Release Please targets main and can trigger downstream workflows with PAT fallback', async () => {
  const text = await workflow('release-please.yml');
  assert.match(text, /branches: \[main\]/);
  assert.match(text, /target-branch: main/);
  assert.match(text, /RELEASE_PLEASE_TOKEN/);
});

test('test workflow runs deployment contract checks without deployment secrets', async () => {
  const text = await workflow('test.yml');
  assert.match(text, /deployment-contract:/);
  assert.match(text, /npm run test:deployment-contract/);
  assert.doesNotMatch(text, /PORTAINER_API_KEY/);
});
```

- [ ] **Step 2: Run the failing workflow tests**

Run: `node --test scripts/ci/workflow-contract.test.mjs`

Expected: FAIL because `release-please.yml` does not exist.

- [ ] **Step 3: Add Release Please config**

Create `release-please-config.json`:

```json
{
  "$schema": "https://raw.githubusercontent.com/googleapis/release-please/main/schemas/config.json",
  "release-type": "python",
  "include-component-in-tag": false,
  "packages": {
    ".": {
      "package-name": "qobuz-librarian",
      "changelog-path": "CHANGELOG.md"
    }
  }
}
```

Create `.release-please-manifest.json`:

```json
{
  ".": "0.11.2"
}
```

- [ ] **Step 4: Add the Release Please workflow**

Create `.github/workflows/release-please.yml`:

```yaml
name: Release Please

on:
  push:
    branches: [main]

permissions:
  contents: write
  pull-requests: write

env:
  FORCE_JAVASCRIPT_ACTIONS_TO_NODE24: true

jobs:
  release-please:
    runs-on: ubuntu-latest
    steps:
      - uses: googleapis/release-please-action@v4
        with:
          target-branch: main
          token: ${{ secrets.RELEASE_PLEASE_TOKEN || secrets.GITHUB_TOKEN }}
          config-file: release-please-config.json
          manifest-file: .release-please-manifest.json
```

- [ ] **Step 5: Add the deployment contract job to test workflow**

Modify `.github/workflows/test.yml` by adding this job at the same level as `pytest`, `lint`, and `assets`:

```yaml
  deployment-contract:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v7

      - uses: actions/setup-node@v4
        with:
          node-version: "24"

      - name: Run deployment contract tests
        run: npm run test:deployment-contract

      - name: Render Portainer Compose configuration
        run: |
          QOBUZ_LIBRARIAN_VERSION=v0.11.3 docker compose -f deploy/portainer/docker-compose.yml config --quiet
```

- [ ] **Step 6: Run workflow contract tests**

Run: `node --test scripts/ci/workflow-contract.test.mjs`

Expected: PASS.

- [ ] **Step 7: Run the full deployment contract script**

Run: `npm run test:deployment-contract`

Expected: PASS across release, CI, and deploy tests.

- [ ] **Step 8: Commit**

```bash
git add release-please-config.json .release-please-manifest.json .github/workflows/release-please.yml .github/workflows/test.yml scripts/ci/workflow-contract.test.mjs package.json package-lock.json
git commit -m "ci: add release and deployment contracts"
```

---

### Task 5: GHCR Publish, Image Scan, And Portainer Deploy Workflows

**Files:**
- Modify: `.github/workflows/docker.yml`
- Create: `.github/workflows/scan-images.yml`
- Modify: `scripts/ci/workflow-contract.test.mjs`

**Interfaces:**
- Consumes: `parseQobuzTag(tag)`
- Consumes: `assertImageTagAbsent(imageRef)`
- Consumes: `scripts/deploy/portainer-release.mjs`
- Consumes: `deploy/portainer/docker-compose.yml`
- Produces: release-only Docker publish and deploy pipeline

- [ ] **Step 1: Extend workflow contract tests for publish/deploy**

Append these tests to `scripts/ci/workflow-contract.test.mjs`:

```js
test('Docker workflow publishes immutable GHCR release tags and never latest', async () => {
  const text = await workflow('docker.yml');
  assert.match(text, /on:\s*\n\s+release:\s*\n\s+types: \[published\]/);
  assert.match(text, /ghcr\.io\/obiwancanoweme\/qobuz-librarian/);
  assert.match(text, /assert-image-tag-absent\.mjs/);
  assert.match(text, /scripts\/deploy\/portainer-release\.mjs/);
  assert.doesNotMatch(text, /dinkeyes\/qobuz-librarian/);
  assert.doesNotMatch(text, /type=raw,value=latest/);
});

test('Docker workflow deploys only after verify, preflight, build, and scan', async () => {
  const text = await workflow('docker.yml');
  assert.match(text, /build-and-push:\s*\n\s+runs-on:[\s\S]*?needs: \[verify, tag-preflight\]/);
  assert.match(text, /scan-image:\s*\n\s+needs: \[verify, tag-preflight, build-and-push\]/);
  assert.match(text, /deploy-production:\s*\n\s+needs: \[verify, tag-preflight, build-and-push, scan-image\]/);
  assert.match(text, /environment: production/);
  assert.match(text, /QOBUZ_HEALTH_URL: \$\{\{ vars\.QOBUZ_HEALTH_URL \}\}/);
});

test('image scan workflow uploads SARIF and is report-only', async () => {
  const text = await workflow('scan-images.yml');
  assert.match(text, /aquasecurity\/trivy-action@/);
  assert.match(text, /github\/codeql-action\/upload-sarif@v3/);
  assert.match(text, /exit-code: "0"/);
  assert.match(text, /workflow_call:/);
  assert.match(text, /schedule:/);
});
```

- [ ] **Step 2: Run the failing workflow contract tests**

Run: `node --test scripts/ci/workflow-contract.test.mjs`

Expected: FAIL because `docker.yml` still targets Docker Hub and `scan-images.yml` does not exist.

- [ ] **Step 3: Add the scan workflow**

Create `.github/workflows/scan-images.yml`:

```yaml
name: Scan images

on:
  workflow_call:
    inputs:
      release_tag:
        description: Exact qobuz release tag to scan
        required: true
        type: string
  schedule:
    - cron: "0 6 * * 1"
  workflow_dispatch:
    inputs:
      release_tag:
        description: Exact qobuz release tag to scan
        required: true
        type: string

permissions:
  contents: read
  packages: read
  security-events: write

env:
  FORCE_JAVASCRIPT_ACTIONS_TO_NODE24: true

jobs:
  resolve-tag:
    runs-on: ubuntu-latest
    outputs:
      tag: ${{ steps.resolve.outputs.tag }}
    steps:
      - uses: actions/checkout@v7
        with:
          fetch-depth: 0

      - uses: actions/setup-node@v4
        with:
          node-version: "24"

      - name: Resolve and validate release tag
        id: resolve
        env:
          REQUESTED_TAG: ${{ inputs.release_tag }}
        run: |
          if [[ "$GITHUB_EVENT_NAME" == "schedule" ]]; then
            RELEASE_TAG="$(git tag --list 'v[0-9]*.[0-9]*.[0-9]*' --sort=-version:refname | head -1)"
          else
            RELEASE_TAG="$REQUESTED_TAG"
          fi
          RELEASE_TAG="$RELEASE_TAG" node -e "import('./scripts/release/qobuz-tag.mjs').then(({ parseQobuzTag }) => parseQobuzTag(process.env.RELEASE_TAG))"
          echo "tag=$RELEASE_TAG" >> "$GITHUB_OUTPUT"

  scan:
    needs: resolve-tag
    runs-on: ubuntu-latest
    steps:
      - uses: docker/login-action@v3
        with:
          registry: ghcr.io
          username: ${{ github.actor }}
          password: ${{ secrets.GITHUB_TOKEN }}

      - name: Scan qobuz-librarian
        uses: aquasecurity/trivy-action@ed142fd0673e97e23eac54620cfb913e5ce36c25 # v0.36.0
        with:
          image-ref: ghcr.io/obiwancanoweme/qobuz-librarian:${{ needs.resolve-tag.outputs.tag }}
          format: sarif
          output: trivy-qobuz-librarian.sarif
          severity: CRITICAL,HIGH
          exit-code: "0"

      - name: Upload SARIF to code scanning
        uses: github/codeql-action/upload-sarif@v3
        if: always()
        with:
          sarif_file: trivy-qobuz-librarian.sarif
          category: trivy-qobuz-librarian
```

- [ ] **Step 4: Replace Docker Hub release workflow with GHCR publish/deploy**

Modify `.github/workflows/docker.yml` so the top-level structure is:

```yaml
name: Docker

on:
  release:
    types: [published]

permissions:
  contents: read
  packages: write
  security-events: write

env:
  FORCE_JAVASCRIPT_ACTIONS_TO_NODE24: true

concurrency:
  group: docker-release-${{ github.event.release.tag_name }}
  cancel-in-progress: false
```

Keep the existing `verify` job, but add `actions/setup-node@v4` with Node 24 and this validation step before Python setup:

```yaml
      - name: Validate release tag
        env:
          RELEASE_TAG: ${{ github.event.release.tag_name }}
        run: node -e "import('./scripts/release/qobuz-tag.mjs').then(({ parseQobuzTag }) => parseQobuzTag(process.env.RELEASE_TAG))"
```

Add a `tag-preflight` job:

```yaml
  tag-preflight:
    needs: [verify]
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v7

      - uses: actions/setup-node@v4
        with:
          node-version: "24"

      - name: Prove the exact release tag is absent
        env:
          IMAGE_REF: ghcr.io/obiwancanoweme/qobuz-librarian:${{ github.event.release.tag_name }}
          GITHUB_TOKEN: ${{ secrets.GITHUB_TOKEN }}
        run: node scripts/ci/assert-image-tag-absent.mjs "$IMAGE_REF"
```

Change `build-and-push` to use GHCR and only the release tag:

```yaml
  build-and-push:
    runs-on: ubuntu-latest
    needs: [verify, tag-preflight]
    outputs:
      digest: ${{ steps.build.outputs.digest }}
    steps:
      - uses: actions/checkout@v7

      - uses: docker/setup-qemu-action@v4

      - uses: docker/setup-buildx-action@v3

      - uses: docker/login-action@v3
        with:
          registry: ghcr.io
          username: ${{ github.actor }}
          password: ${{ secrets.GITHUB_TOKEN }}

      - id: build
        uses: docker/build-push-action@v7
        with:
          context: .
          platforms: linux/amd64,linux/arm64
          push: true
          tags: ghcr.io/obiwancanoweme/qobuz-librarian:${{ github.event.release.tag_name }}
          labels: |
            org.opencontainers.image.version=${{ github.event.release.tag_name }}
            org.opencontainers.image.revision=${{ github.sha }}
          provenance: true
          sbom: true

      - name: Record immutable image digest
        run: |
          echo '### Published image' >> "$GITHUB_STEP_SUMMARY"
          echo '- Image: `ghcr.io/obiwancanoweme/qobuz-librarian`' >> "$GITHUB_STEP_SUMMARY"
          echo '- Tag: `${{ github.event.release.tag_name }}`' >> "$GITHUB_STEP_SUMMARY"
          echo '- Digest: `${{ steps.build.outputs.digest }}`' >> "$GITHUB_STEP_SUMMARY"
```

Add scan and production deploy jobs:

```yaml
  scan-image:
    needs: [verify, tag-preflight, build-and-push]
    uses: ./.github/workflows/scan-images.yml
    with:
      release_tag: ${{ github.event.release.tag_name }}
    permissions:
      contents: read
      packages: read
      security-events: write

  deploy-production:
    needs: [verify, tag-preflight, build-and-push, scan-image]
    runs-on: ubuntu-latest
    environment: production
    concurrency:
      group: qobuz-librarian-production
      cancel-in-progress: false
    timeout-minutes: 20
    steps:
      - uses: actions/checkout@v7

      - uses: actions/setup-node@v4
        with:
          node-version: "24"

      - name: Deploy immutable release through Portainer
        run: node scripts/deploy/portainer-release.mjs
        env:
          PORTAINER_URL: ${{ vars.PORTAINER_URL }}
          PORTAINER_API_KEY: ${{ secrets.PORTAINER_API_KEY }}
          PORTAINER_STACK_ID: ${{ vars.PORTAINER_STACK_ID }}
          PORTAINER_ENDPOINT_ID: ${{ vars.PORTAINER_ENDPOINT_ID }}
          QOBUZ_LIBRARIAN_VERSION: ${{ github.event.release.tag_name }}
          QOBUZ_HEALTH_URL: ${{ vars.QOBUZ_HEALTH_URL }}
```

- [ ] **Step 5: Run workflow contract tests**

Run: `node --test scripts/ci/workflow-contract.test.mjs`

Expected: PASS.

- [ ] **Step 6: Run all deployment contract tests**

Run: `npm run test:deployment-contract`

Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add .github/workflows/docker.yml .github/workflows/scan-images.yml scripts/ci/workflow-contract.test.mjs
git commit -m "ci: publish and deploy release images"
```

---

### Task 6: Full Verification And PR Preparation

**Files:**
- No new files expected
- Modify only files required by failing checks from Tasks 1-5

**Interfaces:**
- Consumes: all previous tasks
- Produces: verified branch ready for PR

- [ ] **Step 1: Run Python tests**

Run:

```bash
python -m pytest -q
```

Expected: PASS.

- [ ] **Step 2: Run lint**

Run:

```bash
ruff check src tests
```

Expected: PASS.

- [ ] **Step 3: Run asset build**

Run:

```bash
npm ci --no-audit --no-fund
npm run build
```

Expected: PASS and `src/qobuz_librarian/web/static/dist/app.css` remains generated.

- [ ] **Step 4: Run deployment contracts**

Run:

```bash
npm run test:deployment-contract
QOBUZ_LIBRARIAN_VERSION=v0.11.3 docker compose -f deploy/portainer/docker-compose.yml config --quiet
```

Expected: PASS.

- [ ] **Step 5: Run Docker smoke test**

Run:

```bash
bash scripts/smoke_test.sh
```

Expected: `SMOKE TEST PASSED`.

- [ ] **Step 6: Inspect final diff**

Run:

```bash
git status --short
git log --oneline --decorate -8
git diff origin/main...HEAD --stat
```

Expected: clean worktree except intended generated asset changes if `npm run build` updates CSS; commits are task-sized and only CI/CD files changed.

- [ ] **Step 7: Push and open a draft PR**

Run:

```bash
git push -u origin agent/qobuz-portainer-cicd-design
```

Open a draft PR titled:

```text
Add Portainer CI/CD for qobuz-librarian
```

Use this PR body:

```markdown
## Summary
- add an image-only Portainer production manifest for qobuz-librarian
- publish immutable GHCR release images and scan them with Trivy
- deploy release tags through Portainer with health verification and rollback

## Testing
- python -m pytest -q
- ruff check src tests
- npm run build
- npm run test:deployment-contract
- QOBUZ_LIBRARIAN_VERSION=v0.11.3 docker compose -f deploy/portainer/docker-compose.yml config --quiet
- bash scripts/smoke_test.sh
```

Expected: branch is pushed and draft PR exists against `main`.
