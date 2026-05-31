#!/usr/bin/env bash

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OUT_DIR="${1:-${ROOT_DIR}/dist/ga}"
VERSION="${CPROXY_VERSION:-0.1.0}"
ARCHIVE="cproxy-${VERSION}-source.tar.gz"

mkdir -p "$OUT_DIR"

git -C "$ROOT_DIR" ls-files --cached --others --exclude-standard -z \
    | grep -zv '^\.data/' \
    | tar -C "$ROOT_DIR" --null -czf "${OUT_DIR}/${ARCHIVE}" --files-from=-

(
    cd "$OUT_DIR"
    sha256sum "$ARCHIVE" > SHA256SUMS
)

ARCHIVE_SHA256="$(sha256sum "${OUT_DIR}/${ARCHIVE}" | awk '{print $1}')"
GIT_COMMIT="$(git -C "$ROOT_DIR" rev-parse HEAD 2>/dev/null || printf 'unknown')"
GIT_DIRTY="$(git -C "$ROOT_DIR" status --short --untracked-files=no | wc -l | tr -d ' ')"
CREATED_AT="$(date -u +%Y-%m-%dT%H:%M:%SZ)"

cat > "${OUT_DIR}/provenance.json" <<JSON
{
  "builder": "scripts/build-ga-artifacts.sh",
  "created_at": "${CREATED_AT}",
  "git_commit": "${GIT_COMMIT}",
  "git_dirty_tracked_count": ${GIT_DIRTY},
  "source_archive": "${ARCHIVE}",
  "source_archive_sha256": "${ARCHIVE_SHA256}",
  "version": "${VERSION}"
}
JSON

printf 'GA artifacts written to %s\n' "$OUT_DIR"
printf '%s  %s\n' "$ARCHIVE_SHA256" "$ARCHIVE"
