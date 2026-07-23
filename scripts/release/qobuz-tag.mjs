const QOBUZ_TAG = /^v(?<version>0|[1-9]\d*)\.(?<minor>0|[1-9]\d*)\.(?<patch>0|[1-9]\d*)$/;

export function parseQobuzTag(tag) {
  const match = QOBUZ_TAG.exec(tag ?? '');
  if (!match?.groups) {
    throw new Error(`Invalid qobuz release tag: ${tag}`);
  }
  return { version: `${match.groups.version}.${match.groups.minor}.${match.groups.patch}` };
}
