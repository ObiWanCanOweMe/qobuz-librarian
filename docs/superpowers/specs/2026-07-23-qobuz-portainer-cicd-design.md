# Qobuz Librarian Portainer CI/CD Design

**Date:** 2026-07-23

## Goal

Give `ObiWanCanOweMe/qobuz-librarian` a CI/CD pipeline shaped like the
Obiwave pipeline, scaled down for a single Dockerized service. Pull requests
and pushes should continue to validate the application. Production should
deploy only from immutable release tags through Portainer, using the same live
host paths and container identity that are already running successfully on
`ark`.

## Decisions

- GitHub repository: `ObiWanCanOweMe/qobuz-librarian`.
- Production host: `ark`, managed by Portainer.
- Container registry: fork-owned GHCR image
  `ghcr.io/obiwancanoweme/qobuz-librarian`.
- Production trigger: release tags created from the release workflow, not
  branch heads or `latest`.
- Production health probe: `http://10.20.0.9:8666/healthz`.
- Current live container contract is preserved:
  - container name: `qobuz-librarian`;
  - host bind: `10.20.0.9:8666->8666`;
  - music: `/mnt/bowl/media2/qobuz-librarian:/music`;
  - staging: `/mnt/NVMe/container-data/qobuz-librarian/staging:/staging`;
  - config: `/mnt/NVMe/container-data/qobuz-librarian/config:/config`;
  - data: `/mnt/NVMe/container-data/qobuz-librarian/data:/data`;
  - upgrade backups:
    `/mnt/NVMe/container-data/qobuz-librarian/upgrade_backups:/upgrade_backups`.
- Migration-only mounts such as `/migrate-source` and `/migrate-dest` are not
  part of the release-controlled production manifest. An operator can add them
  temporarily in Portainer for a deliberate migration window.

## Runtime Architecture

Portainer owns an image-only Compose manifest at
`deploy/portainer/docker-compose.yml`. The manifest contains no `build:` entry
and does not depend on a Git checkout on `ark`. It references the exact release
image through the required `QOBUZ_LIBRARIAN_VERSION` environment variable.

The production stack keeps qobuz-librarian's durable data in the existing host
directories. No CI job copies, prunes, or mutates the music library, staging
directory, config database, app data, or upgrade backups directly. Deployment
only replaces the container image and Compose definition through Portainer.

The published port remains bound to `10.20.0.9:8666`, matching the current
healthy container. The container continues to listen internally on port `8666`
and keeps the same volume paths expected by the application.

## Continuous Integration

The existing test workflow remains the baseline for ordinary development:
Python test matrix, ruff linting, and CSS asset build validation. CI should add
deployment contract checks that prove:

- the Portainer manifest renders with representative non-secret values;
- release tag parsing accepts only the intended qobuz release tags;
- workflow dependencies require tests and smoke checks before publishing;
- official JavaScript and Docker actions stay on the reviewed major versions;
- deploy scripts do not print secrets or require mutable image tags.

The release workflow repeats the application gates before it publishes an
image, because GitHub Actions cannot make one workflow depend on a separate
workflow's historical success for a release event.

## Release Images

Release Please maintains release PRs from commits on `main`. Merging a release
PR creates the version tag and GitHub Release. A personal access token secret
may be used for Release Please so the created tag triggers downstream publish
workflows; if it is absent, Release Please can still update the release PR but
automatic image publication may not fire.

The publish workflow builds
`ghcr.io/obiwancanoweme/qobuz-librarian:<release-tag>` for
`linux/amd64,linux/arm64`. It keeps the existing Docker smoke test and arm64
cross-build gate, then pushes only the immutable release tag. Production does
not use `latest`.

The workflow records the pushed digest in the job summary. A preflight check
should fail before the build if the exact release image tag already exists,
preventing accidental tag reuse.

## Image Scanning

A Trivy scan runs after image publication. It uploads SARIF to GitHub code
scanning and initially reports findings without failing the release. Once a
clean baseline exists, the scan can be tightened to fail on critical or high
severity issues.

The scan workflow can also run on a schedule against the newest stable qobuz
release tag.

## Portainer Deployment

The production deployment job uses GitHub's protected `production` Environment.
It requires these Environment variables and secrets:

- secret: `PORTAINER_API_KEY`;
- variable: `PORTAINER_URL`;
- variable: `PORTAINER_STACK_ID`;
- variable: `PORTAINER_ENDPOINT_ID`;
- variable: `QOBUZ_HEALTH_URL=http://10.20.0.9:8666/healthz`.

The deploy script reads the Portainer stack and stack file, stores a snapshot,
renders the release manifest with the target image tag, and updates the stack
with `PullImage` and `Prune` enabled. It changes only the release-controlled
manifest and version variable; operator-managed environment values remain in
Portainer.

Workflow output must avoid printing the Portainer API key, full request
headers, or complete environment payloads.

## Verification and Rollback

After Portainer accepts the update, the workflow polls
`QOBUZ_HEALTH_URL` until the new container reports healthy or the retry budget
expires. The qobuz health endpoint is sufficient for this first pipeline
because the application is a private maintenance tool and the current live
container already exposes a Docker health check built around the same service.

If deployment or verification fails, the workflow restores the exact stack
definition and environment snapshot captured before the update. It then probes
the health endpoint again. A failed target release always leaves a failed
GitHub Actions run, even when rollback succeeds.

If rollback verification also fails, the workflow stops after reporting an
operator incident. It must not enter an unbounded retry loop.

## Migration to Portainer

The first production cutover is a one-time maintenance operation:

1. Create the Portainer stack from the image-only qobuz manifest using the
   currently deployed release tag.
2. Supply the same host paths, port bind, user/group settings, auth settings,
   and qobuz configuration values currently used by the Compose deployment.
3. Stop the existing manually managed container before starting the Portainer
   stack to avoid two qobuz-librarian containers touching the same config,
   data, staging, and music paths.
4. Start the Portainer stack and verify `http://10.20.0.9:8666/healthz`.
5. Confirm the web UI, current auth state, repair history, queue state, beets
   database, staging area, and music library are still visible.
6. Retain the previous Compose file and data directories unchanged until the
   Portainer-managed stack has survived a controlled restart.

## Testing and Acceptance

The implementation is accepted when:

- pull requests run tests, lint, asset build, Docker smoke, and deployment
  contract checks without deploying;
- Release Please can create release PRs for `main`;
- a release tag publishes the exact fork-owned GHCR image for amd64 and arm64;
- image scans report to GitHub code scanning;
- Portainer deploys the exact immutable release tag;
- the qobuz health endpoint passes after deployment;
- a controlled bad health URL demonstrates rollback to the prior Portainer
  stack snapshot;
- production no longer depends on `dinkeyes/qobuz-librarian:latest`;
- the existing music/config/data/staging/backup directories survive container
  recreation unchanged.

## Non-goals

- Moving qobuz-librarian to a public reverse proxy.
- Replacing Portainer with SSH-driven Compose deployment.
- Deploying every merge to `main` directly to production.
- Reorganizing qobuz application code beyond what is needed for CI/CD tests.
- Migrating, pruning, or rewriting the music library as part of deployment.
