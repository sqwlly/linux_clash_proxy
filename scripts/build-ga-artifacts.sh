#!/usr/bin/env bash

set -euo pipefail

ROOT_DIR="$(cd "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
OUT_DIR="${1:-${ROOT_DIR}/dist/ga}"

# 版本解析顺序：CPROXY_VERSION 环境变量 > 精确命中的 git tag > pyproject.toml
VERSION="${CPROXY_VERSION:-}"
VERSION="${VERSION#v}"
if [ -z "$VERSION" ]; then
    VERSION="$(git -C "$ROOT_DIR" describe --tags --exact-match 2>/dev/null || true)"
    VERSION="${VERSION#v}"
fi
if [ -z "$VERSION" ]; then
    VERSION="$(awk -F'"' '/^version = "/ { print $2; exit }' "${ROOT_DIR}/pyproject.toml")"
fi
VERSION="${VERSION:-0.1.0}"
if [[ "$VERSION" == *"/"* || "$VERSION" == *"\\"* || "$VERSION" == *".."* ]]; then
    echo "error: invalid version string: ${VERSION}" >&2
    exit 1
fi
ARCHIVE="cproxy-${VERSION}-source.tar.gz"

mkdir -p "$OUT_DIR"

# 仅打包 git 已追踪文件，防止把未追踪的本地配置/凭据打进发布包
git -C "$ROOT_DIR" ls-files --cached -z \
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

python3 - "${OUT_DIR}/provenance.json" \
    "${CREATED_AT}" "${GIT_COMMIT}" "${GIT_DIRTY}" "${ARCHIVE}" "${ARCHIVE_SHA256}" "${VERSION}" <<'PY'
import json
import sys
from pathlib import Path

out_path, created_at, git_commit, git_dirty, archive, archive_sha256, version = sys.argv[1:]
payload = {
    "builder": "scripts/build-ga-artifacts.sh",
    "created_at": created_at,
    "git_commit": git_commit,
    "git_dirty_tracked_count": int(git_dirty),
    "source_archive": archive,
    "source_archive_sha256": archive_sha256,
    "version": version,
}
Path(out_path).write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
PY

printf 'GA artifacts written to %s\n' "$OUT_DIR"
printf '%s  %s\n' "$ARCHIVE_SHA256" "$ARCHIVE"
