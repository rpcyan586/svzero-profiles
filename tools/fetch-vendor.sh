#!/usr/bin/env bash
# Fetch third-party validators used by the pipeline. Not redistributed: see
# tools/vendor/README.md for why each is fetched rather than committed.
#
#   ./tools/fetch-vendor.sh          fetch anything missing, verify, report
#   ./tools/fetch-vendor.sh --check  report only, exit 1 if something is absent
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEST="$HERE/vendor"
mkdir -p "$DEST"

# name|url|sha256
ITEMS=(
"OrcaSlicer_profile_validator|https://github.com/SoftFever/Orca_tools/releases/download/1/OrcaSlicer_profile_validator|4e52df93460f867422c5143155856016966b9654c5a0526e708ad9f1f2ebbace"
)

check_only=0
[ "${1:-}" = "--check" ] && check_only=1
missing=0
for item in "${ITEMS[@]}"; do
  IFS='|' read -r name url want <<<"$item"
  path="$DEST/$name"
  if [ -f "$path" ]; then
    got=$(sha256sum "$path" | cut -d' ' -f1)
    if [ "$got" = "$want" ]; then echo "  ok       $name"; continue; fi
    echo "  CHECKSUM $name: have $got, expected $want" >&2
    [ "$check_only" = 1 ] && { missing=1; continue; }
    rm -f "$path"
  fi
  if [ "$check_only" = 1 ]; then echo "  absent   $name"; missing=1; continue; fi
  echo "  fetching $name"
  curl -sSL -o "$path" "$url"
  got=$(sha256sum "$path" | cut -d' ' -f1)
  if [ "$got" != "$want" ]; then
    echo "  CHECKSUM MISMATCH for $name: got $got, expected $want" >&2
    echo "  The upstream release may have been replaced. Do NOT trust it;" >&2
    echo "  verify by hand before updating the expected hash." >&2
    rm -f "$path"; exit 1
  fi
  chmod +x "$path"
  echo "  ok       $name  ($(du -h "$path" | cut -f1))"
done
exit $missing
