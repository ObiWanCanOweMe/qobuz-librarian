import test from 'node:test';
import assert from 'node:assert/strict';
import { readdir, readFile } from 'node:fs/promises';

const workflowDir = new URL('../../.github/workflows/', import.meta.url);

async function workflow(name) {
  return readFile(new URL(name, workflowDir), 'utf8');
}

function jobBlock(text, name) {
  const marker = `  ${name}:\n`;
  const start = text.indexOf(marker);
  assert.notEqual(start, -1, `missing ${name} job`);
  const bodyStart = start + marker.length;
  const nextJob = text.slice(bodyStart).search(/\n  [\w-]+:\n/);
  return text.slice(bodyStart, nextJob === -1 ? undefined : bodyStart + nextJob);
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

test('test workflow runs deployment contract checks and Docker smoke without deployment secrets', async () => {
  const text = await workflow('test.yml');
  assert.match(text, /deployment-contract:/);
  assert.match(text, /npm run test:deployment-contract/);
  const dockerSmoke = jobBlock(text, 'docker-smoke');
  assert.match(dockerSmoke, /bash scripts\/smoke_test\.sh/);
  assert.doesNotMatch(text, /PORTAINER_API_KEY/);
  assert.doesNotMatch(text, /secrets\.PORTAINER_/);
});

test('Docker workflow publishes immutable GHCR release tags with authenticated Docker preflight', async () => {
  const text = await workflow('docker.yml');
  assert.match(text, /on:\s*\n\s+release:\s*\n\s+types: \[published\]/);
  assert.match(text, /ghcr\.io\/obiwancanoweme\/qobuz-librarian/);
  const tagPreflight = jobBlock(text, 'tag-preflight');
  assert.match(tagPreflight, /docker\/login-action@v3/);
  assert.match(tagPreflight, /docker buildx imagetools inspect "\$IMAGE_REF"/);
  assert.match(tagPreflight, /Image tag already exists/);
  assert.doesNotMatch(text, /assert-image-tag-absent\.mjs/);
  assert.match(text, /scripts\/deploy\/portainer-release\.mjs/);
  assert.doesNotMatch(text, /dinkeyes\/qobuz-librarian/);
  assert.doesNotMatch(text, /type=raw,value=latest/);
});

test('Docker tag preflight fails closed unless GHCR reports the manifest is absent', async () => {
  const text = await workflow('docker.yml');
  const tagPreflight = jobBlock(text, 'tag-preflight');
  assert.doesNotMatch(tagPreflight, /docker buildx imagetools inspect[^\n]*\|\|\s*true/);
  assert.match(tagPreflight, /INSPECT_OUTPUT="\$\(docker buildx imagetools inspect "\$IMAGE_REF" 2>&1\)"/);
  assert.match(tagPreflight, /grep -Eq?i?[^\n]*(manifest unknown|manifest.*not found)/i);
  assert.match(tagPreflight, /:\s*not found/);
  assert.match(tagPreflight, /Unable to prove image tag is absent/);
});

test('Docker workflow deploys only after verify, preflight, build, and scan', async () => {
  const text = await workflow('docker.yml');
  assert.match(text, /build-and-push:\s*\n\s+runs-on:[\s\S]*?needs: \[verify, tag-preflight\]/);
  assert.match(text, /scan-image:\s*\n\s+needs: \[verify, tag-preflight, build-and-push\]/);
  assert.match(text, /deploy-production:\s*\n\s+needs: \[verify, tag-preflight, build-and-push, scan-image\]/);
  assert.match(text, /environment: production/);
  const deploy = jobBlock(text, 'deploy-production');
  assert.match(deploy, /runs-on: \[self-hosted, ark\]/);
  assert.match(deploy, /QOBUZ_HEALTH_URL: \$\{\{ vars\.QOBUZ_HEALTH_URL \}\}/);
});

test('Docker release verification repeats assets and deployment contract gates before publishing', async () => {
  const text = await workflow('docker.yml');
  const verify = jobBlock(text, 'verify');
  assert.match(verify, /actions\/setup-node@v4/);
  assert.match(verify, /node-version: "24"/);
  assert.match(verify, /npm ci --no-audit --no-fund/);
  assert.match(verify, /npm run build/);
  assert.match(verify, /npm run test:deployment-contract/);
  assert.match(verify, /python -m pytest -q/);
  assert.match(verify, /ruff check src tests/);
  assert.match(verify, /bash scripts\/smoke_test\.sh/);
  assert.match(verify, /docker buildx build --platform linux\/arm64/);
});

test('Docker release verification checks that the published tag commit is on main', async () => {
  const text = await workflow('docker.yml');
  const verify = jobBlock(text, 'verify');
  assert.match(verify, /actions\/checkout@v7\s*\n\s+with:\s*\n\s+fetch-depth: 0/);
  assert.match(verify, /git merge-base --is-ancestor "\$GITHUB_SHA" origin\/main/);
  assert.match(verify, /reachable from origin\/main/);
});

test('Docker workflow scopes package and SARIF write permissions to the jobs that need them', async () => {
  const text = await workflow('docker.yml');
  assert.match(text, /^permissions:\n  contents: read$/m);
  assert.doesNotMatch(text, /^  packages: write$/m);
  assert.doesNotMatch(text, /^  security-events: write$/m);

  const buildAndPush = jobBlock(text, 'build-and-push');
  assert.match(buildAndPush, /permissions:\s*\n\s+contents: read\s*\n\s+packages: write/);
  assert.equal((text.match(/security-events: write/g) ?? []).length, 1);
  assert.match(jobBlock(text, 'scan-image'), /security-events: write/);
  assert.doesNotMatch(jobBlock(text, 'deploy-production'), /packages: write/);
});

test('image scan workflow uploads SARIF and is report-only', async () => {
  const text = await workflow('scan-images.yml');
  assert.match(text, /aquasecurity\/trivy-action@/);
  assert.match(text, /github\/codeql-action\/upload-sarif@v3/);
  assert.match(text, /exit-code: "0"/);
  assert.match(text, /workflow_call:/);
  assert.match(text, /schedule:/);
});

test('scheduled image scans select only stable semver release tags', async () => {
  const text = await workflow('scan-images.yml');
  const resolveTag = jobBlock(text, 'resolve-tag');
  assert.match(
    resolveTag,
    /grep -E '\^v\(0\|\[1-9\]\[0-9\]\*\)\\\.\(0\|\[1-9\]\[0-9\]\*\)\\\.\(0\|\[1-9\]\[0-9\]\*\)\$'/,
  );
  assert.match(resolveTag, /grep -E[^\n]*\|\s*sort -Vr/);
});

test('image scan workflow scopes permissions to the minimum each job needs', async () => {
  const text = await workflow('scan-images.yml');
  assert.doesNotMatch(text, /^permissions:/m);

  const resolveTag = jobBlock(text, 'resolve-tag');
  assert.match(resolveTag, /permissions:\s*\n\s+contents: read/);
  assert.doesNotMatch(resolveTag, /packages: read/);
  assert.doesNotMatch(resolveTag, /security-events: write/);

  const scan = jobBlock(text, 'scan');
  assert.match(scan, /permissions:\s*\n\s+contents: read\s*\n\s+packages: read\s*\n\s+security-events: write/);
});
