# Task 3: Portainer Deployment Client With Rollback

## Status

DONE_WITH_CONCERNS

## Implementation

- Added `scripts/deploy/portainer-client.mjs`.
  - Provides `PortainerClient` with stack snapshot and update requests using API-key authentication, pull/prune options, and operation-specific timeouts.
  - Provides exact `QOBUZ_LIBRARIAN_VERSION` manifest rendering and environment upsert behavior.
  - Provides health probing with retries.
  - Provides target deployment with verified rollback, including distinct rollback-verified and rollback-incident errors.
- Added `scripts/deploy/portainer-client.test.mjs` with the Task 3 tests for manifest rendering, environment updates, Portainer requests, health checks, and rollback.
- Added `scripts/deploy/portainer-release.mjs`.
  - Validates required deployment-entrypoint configuration, including `QOBUZ_HEALTH_URL`.
  - Validates release tags through `parseQobuzTag`.
  - Reads the Portainer Compose manifest, runs deployment, and writes GitHub step-summary outcomes without logging sensitive details.

## Scope Check

- `QOBUZ_HEALTH_URL` is used only by the release entrypoint and deployment call.
- `deploy/portainer/docker-compose.yml` was not changed and does not contain `QOBUZ_HEALTH_URL`.

## TDD And Verification

- Created the prescribed client test file before production implementation.
- Attempted the required RED command: `node --test scripts/deploy/portainer-client.test.mjs`.
- The command could not run because this environment has no `node` executable. Checks also found no `nodejs`, `bun`, or `deno` executable.
- Performed static review of all three new files and whitespace checks using `git diff --no-index --check`; no whitespace errors were reported.
- Automated test execution remains unverified until Node.js is available.

## Commit

- `45cc891 feat: add portainer release client`

## Concern

The implementation is committed, but the required Node test suite could not execute in this environment because no JavaScript runtime is installed.

## Review Fix Report

- Updated `scripts/deploy/portainer-client.test.mjs` so the rollback scenario returns HTTP 500 for the target health probe and HTTP 200 for the post-rollback health probe.
- Added an assertion that both health probes were consumed, proving the test verifies successful rollback rather than only rollback initiation.
- Production implementation was left unchanged.
- Attempted: `node --test scripts/deploy/portainer-client.test.mjs`
  - Exact output: `/usr/bin/bash: line 3: node: command not found`
  - Exit status: `127`
- Runtime checks: `node`, `nodejs`, `bun`, and `deno` were all unavailable.
- Ran `git diff --check`; no whitespace errors were reported.
- Minor future/final-review consideration: direct `runRelease` tests are not required by the Task 3 brief, but remain a possible follow-up for broader entrypoint coverage.
