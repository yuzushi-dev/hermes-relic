#!/usr/bin/env bash
set -euo pipefail

relic_repo_root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
relic_artifact_dir=${1:-dist}

cd "$relic_repo_root"
export SOURCE_DATE_EPOCH=${SOURCE_DATE_EPOCH:-$(git log -1 --format=%ct)}
exec uv build --wheel --out-dir "$relic_artifact_dir"
