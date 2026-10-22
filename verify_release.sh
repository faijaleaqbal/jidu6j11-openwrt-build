#!/bin/sh
# Download the release artifacts and run the full verification locally.
#
#   sh verify_release.sh              -> verify the published release (v1.0.0)
#   sh verify_release.sh <run-id>     -> also report on a specific CI run
#
# The release is public, so no GitHub token is needed. If GITHUB_TOKEN is
# set (or /tmp/gcred.txt exists) it is used for the extra CI-run details.
set -e

HERE="$(cd "$(dirname "$0")" && pwd)"
cd "$HERE"

REPO="faijaleaqbal/jidu6j11-openwrt-build"
TAG="${TAG:-v1.0.0}"
RUN="${1:-}"
API="https://api.github.com/repos/$REPO"
DL="https://github.com/$REPO/releases/download/$TAG"

TOK="${GITHUB_TOKEN:-}"
if [ -z "$TOK" ] && [ -f /tmp/gcred.txt ]; then
	TOK=$(awk -F= '/^password=/{print $2}' /tmp/gcred.txt)
fi
if [ -n "$TOK" ]; then
	AUTH="Authorization: Bearer $TOK"
else
	AUTH="X-No-Auth: 1"
fi

echo "=== release $TAG ==="
if [ -n "$RUN" ]; then
	curl -s -H "$AUTH" "$API/actions/runs/$RUN" | python -c "
import sys,json
d=json.load(sys.stdin)
print('run     :', d['id'])
print('status  :', d['status'], d.get('conclusion'))
print('commit  :', d['head_sha'])
print('url     :', d['html_url'])
"
	echo
	echo "=== steps ==="
	curl -s -H "$AUTH" "$API/actions/runs/$RUN/jobs" | python -c "
import sys,json
for j in json.load(sys.stdin).get('jobs',[]):
    for s in j.get('steps',[]):
        if s['name'] in ('Build firmware','Feature tests (offline, mocked OpenWrt environment)',
                         'End-to-end smoke test (simulated first boot)',
                         'Verify rootfs contents (BDF + feature scripts)',
                         'Acceptance tests (user point of view)',
                         'Upload artifacts','Publish release'):
            print(f\"  {str(s.get('conclusion')):10s} {s['name']}\")
"
	echo
fi

echo "=== published assets ==="
curl -s -H "$AUTH" "$API/releases/tags/$TAG" | python -c "
import sys,json
r=json.load(sys.stdin)
print('published:', r.get('published_at'))
for a in r.get('assets',[]):
    print(f\"   {a['name']:70s} {a['size']:>10d} B\")
"

echo
echo "=== downloading the release assets ==="
rm -rf release-artifacts
mkdir -p release-artifacts

# Resolve the asset names from the API instead of hardcoding them, so a
# rename upstream can never silently skip a file here.
curl -s -H "$AUTH" "$API/releases/tags/$TAG" | python -c "
import sys,json
r=json.load(sys.stdin)
for a in r.get('assets',[]):
    sys.stdout.write(a['name'] + '\n')
" > .asset-names

while IFS= read -r f; do
	# Python's print() emits CRLF on Windows, so a trailing CR survives the
	# read and turns the URL into garbage (curl exit 3, "URL malformat").
	# Strip it before using the name.
	f=$(printf '%s' "$f" | tr -d '\r')
	[ -n "$f" ] || continue
	if curl -sfL "$DL/$f" -o "release-artifacts/$f"; then
		echo "  got $f"
	else
		echo "  WARNING: could not download $f" >&2
		rm -f "release-artifacts/$f"
	fi
done < .asset-names
rm -f .asset-names
ls -la release-artifacts/

echo
echo "=== checksum verification (sha256sum -c) ==="
if [ -f release-artifacts/sha256sums.txt ]; then
	( cd release-artifacts && sha256sum -c sha256sums.txt )
else
	echo "sha256sums.txt missing" >&2
	exit 1
fi

echo
echo "=== artifact acceptance tests ==="
# Relative to $HERE on purpose: the script has already cd'ed there, and a
# relative path is the only form that both a native Windows Python and a
# Linux one resolve identically. Passing an MSYS-style absolute path
# ("/d/hermes/...") makes a native Windows Python build "D:\d\hermes\...".
ARTIFACT_DIR="release-artifacts" python3 tests/test_artifacts.py

echo
echo "=== feature tests ==="
python3 tests/test_features.py

echo
echo "=== smoke test ==="
sh tests/smoke_test.sh

echo
echo "ALL CHECKS COMPLETE"
