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
