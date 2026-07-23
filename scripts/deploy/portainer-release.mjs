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
