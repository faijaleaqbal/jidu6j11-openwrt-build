#!/bin/sh
# Download the release artifacts and run the full verification locally.
#
# Usage: sh verify_release.sh <run-id>
set -e

RUN="${1:?usage: verify_release.sh <run-id>}"
HERE="$(cd "$(dirname "$0")" && pwd)"
cd "$HERE"

TOK=$(awk -F= '/^password=/{print $2}' /tmp/gcred.txt)
API="https://api.github.com/repos/faijaleaqbal/jidu6j11-openwrt-build"

echo "=== run $RUN ==="
curl -s -H "Authorization: Bearer $TOK" "$API/actions/runs/$RUN" | python -c "
import sys,json
d=json.load(sys.stdin)
print('status  :', d['status'], d.get('conclusion'))
print('commit  :', d['head_sha'])
print('url     :', d['html_url'])
"

echo
echo "=== steps ==="
curl -s -H "Authorization: Bearer $TOK" "$API/actions/runs/$RUN/jobs" | python -c "
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
echo "=== release assets ==="
curl -s -H "Authorization: Bearer $TOK" "$API/releases" | python -c "
import sys,json
for r in json.load(sys.stdin):
    print('tag:', r['tag_name'], '|', r['name'])
    for a in r.get('assets', []):
        print(f\"   {a['name']:70s} {a['size']:>10d} B\")
"

echo
echo "=== downloading artifacts ==="
AID=$(curl -s -H "Authorization: Bearer $TOK" "$API/actions/runs/$RUN/artifacts" | python -c "
import sys,json
a=json.load(sys.stdin).get('artifacts',[])
print(a[0]['id'] if a else '')
")
if [ -z "$AID" ]; then
  echo "no artifacts uploaded for this run"
  exit 1
fi
rm -rf release-artifacts release-artifacts.zip
mkdir -p release-artifacts
curl -sL -H "Authorization: Bearer $TOK" "$API/actions/artifacts/$AID/zip" -o release-artifacts.zip
python3 -c "
import zipfile
z=zipfile.ZipFile('release-artifacts.zip')
z.extractall('release-artifacts')
print('extracted', len(z.namelist()), 'entries')
"
ls -la release-artifacts/ | head -20

echo
echo "=== artifact acceptance tests ==="
ARTIFACT_DIR="$HERE/release-artifacts" python3 tests/test_artifacts.py

echo
echo "=== feature tests ==="
python3 tests/test_features.py

echo
echo "=== smoke test ==="
sh tests/smoke_test.sh

echo
echo "ALL CHECKS COMPLETE"
