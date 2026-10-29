#!/usr/bin/env bash
# Harmless demo "deployment" for the Approval Line guard demo.
# It deploys nothing: it only writes one JSON record to demo-target/output/.
set -euo pipefail

target="${1:-}"
if [[ "$target" != "production" && "$target" != "staging" ]]; then
  echo "usage: ./deploy.sh production|staging" >&2
  exit 64
fi
release="${RELEASE:-v2.4}"
if [[ ! "$release" =~ ^[A-Za-z0-9._-]{1,40}$ ]]; then
  echo "RELEASE must be 1-40 characters: letters, digits, . _ -" >&2
  exit 64
fi

out_dir="$(cd "$(dirname "$0")" && pwd)/output"
mkdir -p "$out_dir"
stamp="$(date -u +%Y%m%dT%H%M%SZ)"
record="$out_dir/deploy-$target-$stamp.json"
printf '{\n  "simulated": true,\n  "target": "%s",\n  "release": "%s",\n  "executed_at": "%s",\n  "note": "demo only: no real system was touched"\n}\n' \
  "$target" "$release" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" > "$record"
echo "Simulated $target deployment of $release recorded in output/$(basename "$record")"
