#!/usr/bin/env bash

set -euo pipefail

OUT_DIR="${1:-dist/ga}"

test -f "${OUT_DIR}/SHA256SUMS"
test -f "${OUT_DIR}/provenance.json"

(
    cd "$OUT_DIR"
    sha256sum -c SHA256SUMS >/dev/null
)

python3 - "$OUT_DIR/provenance.json" "$OUT_DIR/SHA256SUMS" <<'PY'
import hashlib
import json
import sys
from pathlib import Path

prov_path = Path(sys.argv[1])
sums_path = Path(sys.argv[2])
data = json.loads(prov_path.read_text(encoding="utf-8"))
required = {"builder", "created_at", "git_commit", "source_archive", "source_archive_sha256", "version"}
missing = sorted(required - set(data))
if missing:
    raise SystemExit(f"missing provenance fields: {', '.join(missing)}")

name = str(data["source_archive"])
if not name or Path(name).name != name or name in {".", ".."} or "/" in name or "\\" in name:
    raise SystemExit(f"invalid source_archive name: {name!r}")

archive = prov_path.parent / name
if not archive.is_file():
    raise SystemExit(f"missing archive: {archive}")

actual_sha = hashlib.sha256(archive.read_bytes()).hexdigest()
prov_sha = str(data["source_archive_sha256"]).strip().lower()
if actual_sha != prov_sha:
    raise SystemExit(f"provenance sha256 mismatch: expected {prov_sha}, got {actual_sha}")

sums_entries: dict[str, str] = {}
for raw_line in sums_path.read_text(encoding="utf-8").splitlines():
    line = raw_line.strip()
    if not line:
        continue
    parts = line.split(None, 1)
    if len(parts) != 2:
        raise SystemExit(f"malformed SHA256SUMS line: {raw_line!r}")
    digest, filename = parts[0].lower(), parts[1].lstrip("*")
    if not filename or Path(filename).name != filename or "/" in filename or "\\" in filename:
        raise SystemExit(f"invalid filename in SHA256SUMS: {filename!r}")
    sums_entries[filename] = digest

if name not in sums_entries:
    raise SystemExit(f"archive {name!r} missing from SHA256SUMS")
if sums_entries[name] != actual_sha:
    raise SystemExit(f"SHA256SUMS mismatch for {name}: expected {sums_entries[name]}, got {actual_sha}")

print("GA artifact verification passed")
PY
