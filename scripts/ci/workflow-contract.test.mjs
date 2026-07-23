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
