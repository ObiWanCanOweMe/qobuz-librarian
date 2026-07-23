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
