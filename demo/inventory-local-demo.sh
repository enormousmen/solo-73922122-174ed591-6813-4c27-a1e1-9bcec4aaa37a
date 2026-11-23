#!/usr/bin/env bash
# Local, privilege-free inventory demo: disk pull/swap is simulated by
# repointing a symlink that serves as the backup entry (same mechanism as
# local-demo.sh). Booklets live in a separate directory outside both
# simulated disks.
#
#   ./demo/inventory-local-demo.sh
#
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "${HERE}/.." && pwd)"
WORK="$(mktemp -d)"
PORT="$(python3 -c 'import socket;s=socket.socket();s.bind(("127.0.0.1",0));print(s.getsockname()[1]);s.close()')"
BASE="http://127.0.0.1:${PORT}"
VOL_A="VOL-DISK-A-0001"
VOL_B="VOL-DISK-B-0002"
SERVER_PID=""

cleanup() {
    if [ -n "${SERVER_PID}" ]; then
        kill "${SERVER_PID}" 2>/dev/null || true
        for _ in 1 2 3 4 5; do
            kill -0 "${SERVER_PID}" 2>/dev/null || break
            sleep 0.2
        done
    fi
    echo; echo "demo workspace left at: ${WORK}"
}
trap cleanup EXIT

say()  { printf '\n\033[1;36m== %s ==\033[0m\n' "$*"; }
fail() { printf '\033[1;31mUNEXPECTED: %s\033[0m\n' "$*" >&2; exit 1; }
json() { python3 -c 'import sys,json;print(json.load(sys.stdin)'"$1"')'; }

mkdir -p "${WORK}/disk-a" "${WORK}/disk-b" "${WORK}/inventory"
printf '%s' "${VOL_A}" > "${WORK}/disk-a/.volume_id"
printf '%s' "${VOL_B}" > "${WORK}/disk-b/.volume_id"
ln -s "${WORK}/disk-a" "${WORK}/entry"

PY="${ROOT}/.venv/bin/python"
[ -x "${PY}" ] || PY=python3

say "starting API (BACKUP_ROOT=${WORK}/entry, INVENTORY_ROOT=${WORK}/inventory)"
BACKUP_ROOT="${WORK}/entry" BACKUP_VOLUME_ID="${VOL_A}" \
    INVENTORY_ROOT="${WORK}/inventory" \
    "${PY}" -m uvicorn app.main:app --host 127.0.0.1 --port "${PORT}" \
    --app-dir "${ROOT}" >"${WORK}/server.log" 2>&1 &
SERVER_PID=$!
for _ in $(seq 1 50); do
    kill -0 "${SERVER_PID}" 2>/dev/null || { cat "${WORK}/server.log" >&2; exit 1; }
    curl -fsS "${BASE}/health" >/dev/null 2>&1 && break
    sleep 0.2
done
curl -fsS "${BASE}/health" >/dev/null || { cat "${WORK}/server.log" >&2; exit 1; }

say "1. publish an archive"
payload="${WORK}/payload.bin"
head -c 262144 /dev/urandom > "${payload}"
sha=$(sha256sum "${payload}" | cut -d' ' -f1)
size=$(stat -c '%s' "${payload}")
sid=$(curl -fsS -X POST "${BASE}/backups" -H 'Content-Type: application/json' \
    -d '{"filename":"inv-demo.tar","total_chunks":2,"sha256":"'"${sha}"'","size":'"${size}"'}' \
    | json "['session_id']")
split -n 2 -d -a 1 "${payload}" "${WORK}/chunk-"
for i in 0 1; do
    curl -fsS -X PUT "${BASE}/backups/${sid}/chunks/${i}" \
        --data-binary @"${WORK}/chunk-${i}" -o /dev/null
done
aid=$(curl -fsS -X POST "${BASE}/backups/${sid}/complete" | json "['id']")
echo "archive id: ${aid}"

say "2. run inventory with the disk number (must match mounted volume id)"
commit=$(curl -fsS -X POST "${BASE}/inventory/volumes/${VOL_A}")
echo "${commit}" | python3 -m json.tool
inv_id=$(echo "${commit}" | json "['inventory_id']")
[ -f "${WORK}/inventory/volumes/${VOL_A}/booklet.json" ] || fail "booklet missing"
[ ! -e "${WORK}/disk-a/volumes" ] || fail "booklet must not be written on the backup disk"

# Wrong disk number -> 409.
code=$(curl -s -o "${WORK}/r.json" -w '%{http_code}' -X POST \
    "${BASE}/inventory/volumes/${VOL_B}")
[ "${code}" = "409" ] || fail "wrong disk number must be 409, got ${code}"
[ "$(json "['error']" < "${WORK}/r.json")" = "VOLUME_ID_MISMATCH" ] || fail "expected VOLUME_ID_MISMATCH"
echo "wrong disk number correctly rejected: $(cat "${WORK}/r.json")"

say "3. PULL disk A (symlink removed: entry falls back to nothing)"
rm "${WORK}/entry"
code=$(curl -s -o "${WORK}/r.json" -w '%{http_code}' -X POST "${BASE}/backups" \
    -H 'Content-Type: application/json' \
    -d '{"filename":"x.tar","total_chunks":1,"sha256":"'"$(printf '0%.0s' {1..64})"'","size":1}')
[ "${code}" = "503" ] || fail "upload after pull must be 503, got ${code}"
echo "upload after pull refused: $(cat "${WORK}/r.json")"

say "4. lookup the disk number for archive ${aid} AFTER the pull"
lookup=$(curl -fsS "${BASE}/inventory/archives/${aid}/volumes")
echo "${lookup}" | python3 -m json.tool
found=$(echo "${lookup}" | json "['volumes'][0]['volume_id']")
[ "${found}" = "${VOL_A}" ] || fail "lookup must resolve to ${VOL_A}"
echo ">>> archive ${aid} is on disk ${found}"

# Booklet still readable, and an inventory without the disk fails but keeps it.
curl -fsS "${BASE}/inventory/volumes/${VOL_A}/booklet" \
    | python3 -c 'import sys,json;d=json.load(sys.stdin);print("booklet:",d["inventory_id"])'
code=$(curl -s -o "${WORK}/r.json" -w '%{http_code}' -X POST \
    "${BASE}/inventory/volumes/${VOL_A}")
[ "${code}" = "503" ] || fail "inventory with no disk must be 503, got ${code}"
[ "$(curl -fsS "${BASE}/inventory/volumes/${VOL_A}/booklet" | json "['inventory_id']")" = "${inv_id}" ] \
    || fail "previous booklet must be kept"
echo "failed inventory kept the previous booklet"

say "5. insert disk B: inventory B; uploads still pinned to A"
ln -s "${WORK}/disk-b" "${WORK}/entry"
curl -fsS -X POST "${BASE}/inventory/volumes/${VOL_B}" | python3 -m json.tool
code=$(curl -s -o "${WORK}/r.json" -w '%{http_code}' -X POST "${BASE}/backups" \
    -H 'Content-Type: application/json' \
    -d '{"filename":"x.tar","total_chunks":1,"sha256":"'"$(printf '0%.0s' {1..64})"'","size":1}')
[ "${code}" = "409" ] || fail "upload onto B must be 409, got ${code}"
echo "upload onto B refused: $(cat "${WORK}/r.json")"

say "6. reattach A: second inventory commits a fresh booklet"
ln -sfn "${WORK}/disk-a" "${WORK}/entry"
second=$(curl -fsS -X POST "${BASE}/inventory/volumes/${VOL_A}")
echo "${second}" | python3 -m json.tool
[ "$(echo "${second}" | json "['archive_count']")" = "1" ] || fail "booklet must still contain 1 archive"
[ "$(echo "${second}" | json "['inventory_id']")" != "${inv_id}" ] || fail "expected a new inventory id"

say "7. restart the service: lookup still answers from the booklets"
# Note: hardened sandboxes may block kill(2); the original server is left
# running in that case, so the restart uses a fresh ephemeral port instead
# of reusing the old one. The semantics under test -- a brand-new app
# process reading the same INVENTORY_ROOT -- are identical.
kill "${SERVER_PID}" 2>/dev/null || true
PORT2="$(python3 -c 'import socket;s=socket.socket();s.bind(("127.0.0.1",0));print(s.getsockname()[1]);s.close()')"
BASE2="http://127.0.0.1:${PORT2}"
# Remove the reverse index to prove it is rebuilt from the booklets.
rm -f "${WORK}/inventory/archive-index.json"
BACKUP_ROOT="${WORK}/entry" BACKUP_VOLUME_ID="${VOL_A}" \
    INVENTORY_ROOT="${WORK}/inventory" \
    "${PY}" -m uvicorn app.main:app --host 127.0.0.1 --port "${PORT2}" \
    --app-dir "${ROOT}" >"${WORK}/server2.log" 2>&1 &
SERVER2_PID=$!
for _ in $(seq 1 50); do
    kill -0 "${SERVER2_PID}" 2>/dev/null || { cat "${WORK}/server2.log" >&2; exit 1; }
    curl -fsS "${BASE2}/health" >/dev/null 2>&1 && break
    sleep 0.2
done
curl -fsS "${BASE2}/health" >/dev/null || { cat "${WORK}/server2.log" >&2; exit 1; }
hits=$(curl -fsS "${BASE2}/inventory/archives/${aid}/volumes" \
    | json "['volumes'][0]['volume_id']")
[ "${hits}" = "${VOL_A}" ] || fail "post-restart lookup must still resolve to ${VOL_A}"
echo "post-restart lookup OK: ${aid} -> ${hits}"
SERVER_PID="${SERVER2_PID}"  # cleanup tracks the process we started last

say "ALL LOCAL INVENTORY DEMO CHECKS PASSED"
