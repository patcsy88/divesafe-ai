#!/bin/bash
# usage: tools/web-build-compare.sh <.next dir> <output list>
# One line per file under .next (not cache/, not trace, not diagnostics/): a hash of its content after
# masking the values Next makes random on every build, then the path with the build id masked.
# Masked: the build id; 42-hex server action ids; in *.nft.json trace files, 32-hex content hashes; in
# prerender-manifest.json, the preview mode id and keys; the absolute path of the build folder
# (required-server-files.*, so two checkouts compare); server/preview-props.json (random keys only)
# is listed without a hash. Static chunk and media names are content hashes, so they are replaced by
# "*" and those lines compare as a sorted multiset of content hashes.
# Both builds must use one fixed NEXT_SERVER_ACTIONS_ENCRYPTION_KEY (comparison only, never deployed).
# Any error stops the script with no output list, so a failed run cannot compare as identical.
set -euo pipefail
export LC_ALL=C
d=$1
out=$2
id=$(cat "$d/BUILD_ID")
root=$(cd "$d/.." && pwd -P)
test -n "$id"
tmp=$(mktemp)
list=$(mktemp)
trap 'rm -f "$tmp" "$tmp.files" "$list"' EXIT
cd "$d"
find . -type f ! -path './cache/*' ! -path './trace*' ! -path './diagnostics/*' | sed 's#^\./##' |
  sort > "$tmp.files"
: > "$list"
while IFS= read -r f; do
  case "$f" in
    *.nft.json) x='s/"[0-9a-f]{32}"/"HASH"/g;' ;;
    prerender-manifest.json)
      x='s/"(previewModeId|previewModeSigningKey|previewModeEncryptionKey)":\s*"[^"]*"/"$1":"X"/g;' ;;
    *) x='' ;;
  esac
  if [ "$f" = server/preview-props.json ]; then
    h=preview-props---
  else
    R=$root I=$id perl -pe "s/\\Q\$ENV{R}\\E/WEBROOT/g; s/\\Q\$ENV{I}\\E/BUILDID/g; s/\\b[0-9a-f]{42}\\b/ACTIONID/g; $x" \
      "$f" > "$tmp"
    h=$(shasum -a 256 < "$tmp" | cut -c1-16)
    test ${#h} -eq 16
  fi
  printf '%s %s\n' "$h" "${f//$id/BUILDID}" >> "$list"
done < "$tmp.files"
test -s "$list"
sed -E 's# static/(chunks|media)/[^ /]+$# static/\1/*#' "$list" | sort -k2 -k1 > "$out"
