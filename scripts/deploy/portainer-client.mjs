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
