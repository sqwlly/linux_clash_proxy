#!/usr/bin/env bash

set -euo pipefail

OUT_DIR="${1:-dist/ga}"

test -f "${OUT_DIR}/SHA256SUMS"
test -f "${OUT_DIR}/provenance.json"

(
    cd "$OUT_DIR"
    sha256sum -c SHA256SUMS >/dev/null
)

python3 - "$OUT_DIR/provenance.json" <<'PY'
import json
import sys
from pathlib import Path

path = Path(sys.argv[1])
data = json.loads(path.read_text(encoding="utf-8"))
required = {"builder", "created_at", "git_commit", "source_archive", "source_archive_sha256", "version"}
missing = sorted(required - set(data))
if missing:
    raise SystemExit(f"missing provenance fields: {', '.join(missing)}")
archive = path.parent / data["source_archive"]
if not archive.is_file():
    raise SystemExit(f"missing archive: {archive}")
print("GA artifact verification passed")
PY
