#!/bin/sh
# Download the release artifacts and run the full verification locally.
#
#   sh verify_release.sh              -> verify the published release (v1.0.0)
#   sh verify_release.sh <run-id>     -> also verify a specific CI run's steps
#
# The release is public, so no GitHub token is needed. If GITHUB_TOKEN is
# set (or an owned /tmp/gcred.txt exists) it is used for the extra CI-run
# details. The token is handed to curl through a --header @file (never on
# argv, so it does not show up in `ps`), into a mktemp staging dir that is
# cleaned up by a trap, and release-artifacts/ is only replaced AFTER the
# staged copy passes checksum + provenance checks.
#
# Provenance: the downloaded sha256sums.txt must byte-match (modulo CR)
# release-manifest.sha256, the tracked copy of the last verified manifest.
# A re-released tag or tampered manifest then fails loudly, even on a
# machine with no prior download.
set -eu
(set -o pipefail) 2>/dev/null && set -o pipefail

# --- dependency checks ------------------------------------------------------
for _tool in curl awk tr sha256sum mktemp; do
	command -v "$_tool" >/dev/null 2>&1 || {
		echo "ERROR: required tool '$_tool' not found in PATH" >&2
		exit 1
	}
done

# --- resolve the python interpreter once ------------------------------------
# Native Windows Python installs usually lack python3; Debian/Ubuntu lack
# python. Resolve once here instead of guessing at each call site.
if command -v python3 >/dev/null 2>&1; then
	PY=python3
elif command -v python >/dev/null 2>&1; then
	PY=python
else
	echo "ERROR: no python interpreter found (tried python3, python)" >&2
	exit 1
fi

HERE="$(cd "$(dirname "$0")" && pwd)"
cd "$HERE"

# --- staging dir + trap cleanup ----------------------------------------------
# Everything the script produces (asset list, auth header, downloads) lives
# in $STAGE; the EXIT trap removes it even on a traceback or Ctrl-C. The
# public release-artifacts/ dir is only touched after verification passes.
STAGE=$(mktemp -d)
trap 'rm -rf "$STAGE"' EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

REPO="faijaleaqbal/jidu6j11-openwrt-build"
TAG="${TAG:-v1.0.0}"
RUN="${1:-}"
API="https://api.github.com/repos/$REPO"
DL="https://github.com/$REPO/releases/download/$TAG"

# --- input validation ---------------------------------------------------------
# TAG and RUN end up inside URLs; refuse anything that could steer the
# request somewhere else or mangle it.
case $TAG in
	''|*[!A-Za-z0-9._-]*)
		echo "ERROR: TAG='$TAG' is empty or contains characters outside [A-Za-z0-9._-]" >&2
		exit 1;;
esac
if [ -n "$RUN" ]; then
	case $RUN in
		*[!0-9]*)
			echo "ERROR: run id must be numeric, got '$RUN'" >&2
			exit 1;;
	esac
fi

# --- credential handling ------------------------------------------------------
TOK="${GITHUB_TOKEN:-}"
if [ -z "$TOK" ] && [ -f /tmp/gcred.txt ]; then
	if [ -O /tmp/gcred.txt ]; then
		# substr($0, 10) keeps everything after "password=" including any '='
		# inside the secret (print $2 with -F= would truncate at the first =).
		TOK=$(awk -F= '/^password=/{print substr($0, 10); exit}' /tmp/gcred.txt)
	else
		echo "WARNING: /tmp/gcred.txt exists but is not owned by this user; ignoring it" >&2
	fi
fi
AUTHFILE=""
if [ -n "$TOK" ]; then
	case $TOK in
		*[!A-Za-z0-9_-]*)
			echo "ERROR: GitHub token contains unexpected characters (charset [A-Za-z0-9_-]); refusing to send it" >&2
			exit 1;;
	esac
	# 0600 + header file: curl reads the token from the file, so it never
	# appears on the command line / in a process listing.
	AUTHFILE="$STAGE/.auth"
	(umask 077 && printf 'Authorization: Bearer %s\n' "$TOK" > "$AUTHFILE")
fi

fetch() {
	# -L is essential: release download URLs 302-redirect to the CDN.
	if [ -n "$AUTHFILE" ]; then
		curl -sS -L --fail --retry 3 --retry-delay 2 --max-time 900 --header "@$AUTHFILE" "$@"
	else
		curl -sS -L --fail --retry 3 --retry-delay 2 --max-time 900 "$@"
	fi
}

echo "=== release $TAG ==="
if [ -n "$RUN" ]; then
	fetch "$API/actions/runs/$RUN" | $PY -c "
import sys,json
d=json.load(sys.stdin)
print('run     :', d.get('id'))
print('status  :', d.get('status'), d.get('conclusion'))
print('commit  :', d.get('head_sha'))
print('url     :', d.get('html_url'))
"
	echo
	echo "=== steps ==="
	# Strict check: every expected step must exist AND conclude 'success'.
	# A renamed or skipped step is a verification failure, never a silent
	# shorter report.
	fetch "$API/actions/runs/$RUN/jobs" | $PY -c "
import sys,json
REQUIRED = [
    'Build firmware',
    'Feature tests (offline, mocked OpenWrt environment)',
    'End-to-end smoke test (simulated first boot)',
    'Verify rootfs contents (BDF + feature scripts)',
    'Acceptance tests (user point of view)',
    'Upload artifacts',
    'Publish release',
]
data = json.load(sys.stdin)
seen = {}
for j in data.get('jobs', []):
    for s in j.get('steps', []):
        if s['name'] in REQUIRED:
            seen[s['name']] = s.get('conclusion')
failed = []
for name in REQUIRED:
    c = seen.get(name)
    if c is None:
        print('  MISSING  %s' % name)
        failed.append(name)
    elif c != 'success':
        print('  %-8s %s' % (str(c), name))
        failed.append(name)
    else:
        print('  success  %s' % name)
if failed:
    sys.stdout.flush()
    print('ERROR: %d required CI step(s) missing or not successful: %s'
          % (len(failed), failed), file=sys.stderr)
    sys.exit(1)
"
	echo
fi

echo "=== published assets ==="
RELEASE_JSON="$STAGE/release.json"
fetch -o "$RELEASE_JSON" "$API/releases/tags/$TAG"

$PY - "$RELEASE_JSON" "$STAGE/asset-names" <<'PYEOF'
import sys, json
with open(sys.argv[1], encoding='utf-8') as fh:
    r = json.load(fh)
print('published:', r.get('published_at'))
assets = r.get('assets') or []
for a in assets:
    print('   %-70s %10d B' % (a['name'], a['size']))
names = [a['name'] for a in assets]
if not names:
    print('ERROR: release has zero assets; refusing to continue', file=sys.stderr)
    sys.exit(1)
# newline='\n' stops native Windows Python from emitting CRLF lines, which
# used to poison the download URLs (curl exit 3, 'URL malformat').
with open(sys.argv[2], 'w', encoding='utf-8', newline='\n') as fh:
    for n in names:
        fh.write(n + '\n')
PYEOF

echo
echo "=== downloading the release assets (staging) ==="
ASSETDIR="$STAGE/assets"
mkdir -p "$ASSETDIR"
while IFS= read -r f; do
	[ -n "$f" ] || continue
	case $f in
		# Allowlist on [A-Za-z0-9._-]: blocks '/' and '\' path traversal,
		# stray CR/LF, option-looking names, and spaces, for both the curl
		# write and the [now gone] delete path.
		*[!A-Za-z0-9._-]*)
			echo "  WARNING: refusing unsafe asset name: $f" >&2
			continue;;
	esac
	if fetch -o "$ASSETDIR/$f" "$DL/$f"; then
		echo "  got $f"
	else
		echo "  WARNING: could not download $f" >&2
		rm -f "$ASSETDIR/$f"
	fi
done < "$STAGE/asset-names"

echo
echo "=== checksum verification (sha256sum -c) ==="
if [ ! -f "$ASSETDIR/sha256sums.txt" ]; then
	echo "ERROR: sha256sums.txt missing from release assets" >&2
	exit 1
fi
# A failed download warns above, then fails hard right here: the missing
# file makes sha256sum -c exit 1, and set -eu stops the script before any
# test runs against a partial artifact set.
( cd "$ASSETDIR" && sha256sum -c sha256sums.txt )

echo
echo "=== provenance check (published sums vs tracked baseline) ==="
# release-manifest.sha256 is tracked in git and records the manifest of the
# release this script verified when the baseline was written. It must exist;
# its absence is never silently tolerated -- that would disable provenance.
BASELINE="release-manifest.sha256"
if [ ! -f "$BASELINE" ]; then
	echo "ERROR: $BASELINE missing; cannot verify release provenance" >&2
	echo "ERROR: refusing to replace release-artifacts/" >&2
	exit 1
fi
# Normalize CRLF (autocrlf checkouts) before comparing bytes.
tr -d '\r' < "$ASSETDIR/sha256sums.txt" > "$STAGE/sums-published.txt"
tr -d '\r' < "$BASELINE" > "$STAGE/sums-baseline.txt"
if cmp -s "$STAGE/sums-published.txt" "$STAGE/sums-baseline.txt"; then
	echo "$BASELINE matches the published release"
else
	echo "ERROR: published sha256sums.txt differs from tracked baseline $BASELINE" >&2
	echo "ERROR: re-released tag or tampered artifacts; refusing to replace release-artifacts/" >&2
	exit 1
fi

echo
echo "=== replacing release-artifacts with the verified copy ==="
# Only now: the staged copy passed sha256 -c against a manifest that byte-
# matches the tracked provenance baseline in $BASELINE.
rm -rf release-artifacts
cp -R "$ASSETDIR" release-artifacts
ls -la release-artifacts/

echo
echo "=== artifact acceptance tests ==="
# Relative to $HERE on purpose: the script has already cd'ed there, and a
# relative path is the only form that both a native Windows Python and a
# Linux one resolve identically. Passing an MSYS-style absolute path
# ("/d/hermes/...") makes a native Windows Python build "D:\d\hermes...".
ARTIFACT_DIR="release-artifacts" $PY tests/test_artifacts.py

echo
echo "=== feature tests ==="
$PY tests/test_features.py

echo
echo "=== smoke test ==="
sh tests/smoke_test.sh

echo
echo "ALL CHECKS COMPLETE"
