#!/usr/bin/env bash
# Build an offline release bundle on a connected staging host of the target platform
# (Ubuntu 24.04 amd64, CPython 3.12):
#
#   scripts/build-release.sh [OUT_DIR]      -> OUT_DIR/labmon-<version>/ and .tar.gz (+ .sha256)
#
# Bundle: wheelhouse (hash-locked dependencies + the labmon wheel), prebuilt web UI, deployment
# assets, requirements.txt with hashes, SBOM.json, RELEASE.json and SHA256SUMS.
set -euo pipefail
cd "$(dirname "$0")/.."
OUT="${1:-dist}"
VERSION=$(.venv/bin/python -c 'import labmon; print(labmon.__version__)')
COMMIT=$(git rev-parse HEAD)
NAME="labmon-$VERSION"
B="$OUT/$NAME"
rm -rf "$B" && mkdir -p "$B/wheelhouse"

uv export --frozen --no-dev --no-emit-project --format requirements-txt -o "$B/requirements.txt" >/dev/null
uvx --quiet --python 3.12 pip download --quiet --only-binary=:all: --dest "$B/wheelhouse" \
    --python-version 3.12 --platform manylinux2014_x86_64 --platform manylinux_2_17_x86_64 \
    --platform manylinux_2_28_x86_64 --platform any -r "$B/requirements.txt"
uv build --wheel --out-dir "$B/wheelhouse" >/dev/null
(cd web && npm ci --silent && npm run --silent build >/dev/null)
cp -r web/dist "$B/web"
mkdir -p "$B/deploy" && cp -r deploy/systemd deploy/nginx deploy/labmon.env.example deploy/install.sh "$B/deploy/"
cp LICENSE "$B/"
.venv/bin/python scripts/sbom.py > "$B/SBOM.json"
cat > "$B/RELEASE.json" <<EOF
{"name": "labmon", "version": "$VERSION", "commit": "$COMMIT",
 "built_at": "$(date -u +%Y-%m-%dT%H:%M:%SZ)", "platform": "ubuntu-24.04-amd64-cp312",
 "schema_head": "$(.venv/bin/python -c 'from labmon.server.migrate import head_revision; print(head_revision())')",
 "simulator_only": true}
EOF
(cd "$B" && find . -type f ! -name SHA256SUMS -print0 | sort -z | xargs -0 sha256sum > SHA256SUMS)
tar -C "$OUT" -czf "$OUT/$NAME.tar.gz" "$NAME"
(cd "$OUT" && sha256sum "$NAME.tar.gz" > "$NAME.tar.gz.sha256")
echo "$OUT/$NAME.tar.gz"
