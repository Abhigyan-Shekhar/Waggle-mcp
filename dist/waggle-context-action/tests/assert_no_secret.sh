#!/usr/bin/env bash
set -euo pipefail

marker="$1"
shift

echo "secret search paths: $*"
if grep -R -F -n -- "$marker" "$@"; then
  echo "fixture secret found"
  exit 1
fi
echo "grep status: 1 (no fixed-string matches)"
echo "fixture secret absent"
