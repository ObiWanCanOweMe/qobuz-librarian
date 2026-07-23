#!/usr/bin/env node

import { readFile } from 'node:fs/promises';
import { fileURLToPath } from 'node:url';

const VERSION_TOKEN = '${QOBUZ_LIBRARIAN_VERSION:?required}';
const UNRESOLVED_VERSION = /\$\{QOBUZ_LIBRARIAN_VERSION[^}]*\}/;
const IMAGE_LINE = /^\s*image:\s*(.*?)\s*$/gm;
const STABLE_RELEASE_TAG = /^v(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$/;
const PRODUCTION_IMAGE = new RegExp(
  `^ghcr\\.io/obiwancanoweme/qobuz-librarian:${STABLE_RELEASE_TAG.source.slice(1, -1)}$`,
);

export function validatePortainerCompose({ composeText }) {
  if (/^\s*build\s*:/m.test(composeText)) {
    throw new Error('Portainer manifest must not contain build entries');
  }
  let hasProductionImage = false;
  for (const match of composeText.matchAll(IMAGE_LINE)) {
    const imageReference = match[1].replace(/^(['"])(.*)\1$/, '$2');
    const tag = imageReference.slice(imageReference.lastIndexOf(':') + 1);
    if (!STABLE_RELEASE_TAG.test(tag)) {
      throw new Error('Portainer manifest must use a stable release image tag; latest image tags are not allowed');
    }
    if (!PRODUCTION_IMAGE.test(imageReference)) {
      throw new Error('Portainer manifest must use the production image ghcr.io/obiwancanoweme/qobuz-librarian');
    }
    hasProductionImage = true;
  }
  if (!hasProductionImage) {
    throw new Error('Portainer manifest must define the production image');
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
