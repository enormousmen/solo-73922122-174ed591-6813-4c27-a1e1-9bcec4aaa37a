#!/usr/bin/env bash
# Local, privilege-free demo: disk pull/swap is simulated by repointing a
# symlink that serves as the backup entry. All safety logic is identical;
# only the "disk" mechanics differ from the privileged Docker demo.
#
#   ./demo/local-demo.sh
#
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "${HERE}/.." && pwd)"
WORK="$(mktemp -d)"
# Pick an ephemeral free port instead of a fixed one, so concurrent or
# leftover demo runs cannot silently talk to a stale server.
PORT="$(python3 -c 'import socket;s=socket.socket();s.bind(("127.0.0.1",0));print(s.getsockname()[1]);s.close()')"
BASE="http://127.0.0.1:${PORT}"
VOL_A="VOL-DISK-A-0001"
VOL_B="VOL-DISK-B-0002"
SERVER_PID=""

cleanup() {
    if [ -n "${SERVER_PID}" ]; then
        # Best-effort shutdown. Some hardened sandboxes block kill(2)
        # entirely; in that case the ephemeral-port server is simply left
        # behind and never reused (a new run picks a fresh free port).
        kill "${SERVER_PID}" 2>/dev/null || true
        # Wait only briefly so a blocked kill cannot hang the script.
        for _ in 1 2 3 4 5; do
            kill -0 "${SERVER_PID}" 2>/dev/null || break
            sleep 0.2
        done
    fi
    echo; echo "demo workspace left at: ${WORK}"
}
trap cleanup EXIT

say() { printf '\n\033[1;36m== %s ==\033[0m\n' "$*"; }
fail() { printf '\033[1;31mUNEXPECTED: %s\033[0m\n' "$*" >&2; exit 1; }
json() { python3 -c 'import sys,json;print(json.load(sys.stdin)'"$1"')'; }

mkdir -p "${WORK}/disk-a" "${WORK}/disk-b"
printf '%s' "${VOL_A}" > "${WORK}/disk-a/.volume_id"
printf '%s' "${VOL_B}" > "${WORK}/disk-b/.volume_id"
ln -s "${WORK}/disk-a" "${WORK}/entry"

# Pick an interpreter: project venv if present, else system python3.
PY="${ROOT}/.venv/bin/python"
[ -x "${PY}" ] || PY=python3

say "starting API (BACKUP_ROOT=${WORK}/entry, expected ${VOL_A})"
BACKUP_ROOT="${WORK}/entry" BACKUP_VOLUME_ID="${VOL_A}" \
    "${PY}" -m uvicorn app.main:app --host 127.0.0.1 --port "${PORT}" \
    --app-dir "${ROOT}" >"${WORK}/server.log" 2>&1 &
SERVER_PID=$!
for _ in $(seq 1 50); do
    kill -0 "${SERVER_PID}" 2>/dev/null || {
        echo "server exited early; log:" >&2
        cat "${WORK}/server.log" >&2
        exit 1
    }
    curl -fsS "${BASE}/health" >/dev/null 2>&1 && break
    sleep 0.2
done
curl -fsS "${BASE}/health" >/dev/null || {
    echo "server did not become healthy; log:" >&2
    cat "${WORK}/server.log" >&2
    exit 1
}
curl -fsS "${BASE}/health" | python3 -m json.tool

say "1. swap entry to disk B before upload -> POST /backups rejected (409)"
ln -sfn "${WORK}/disk-b" "${WORK}/entry"
code=$(curl -s -o "${WORK}/r1.json" -w '%{http_code}' -X POST "${BASE}/backups" \
    -H 'Content-Type: application/json' \
    -d '{"filename":"demo.tar","total_chunks":1,"sha256":"'$(printf '0%.0s' {1..64})'","size":1}')
[ "${code}" = "409" ] || fail "expected 409, got ${code}: $(cat "${WORK}/r1.json")"
cat "${WORK}/r1.json" | python3 -m json.tool
[ "$(ls -A "${WORK}/disk-b")" = ".volume_id" ] || fail "disk B was modified!"

say "2. remove entry entirely (system-disk fallback risk) -> 503"
rm "${WORK}/entry"
code=$(curl -s -o "${WORK}/r2.json" -w '%{http_code}' -X POST "${BASE}/backups" \
    -H 'Content-Type: application/json' \
    -d '{"filename":"demo.tar","total_chunks":1,"sha256":"'$(printf '0%.0s' {1..64})'","size":1}')
[ "${code}" = "503" ] || fail "expected 503, got ${code}: $(cat "${WORK}/r2.json")"
cat "${WORK}/r2.json" | python3 -m json.tool

say "3. reattach disk A, start upload, swap disks mid-flight"
ln -s "${WORK}/disk-a" "${WORK}/entry"
payload="${WORK}/payload.bin"
head -c 1048576 /dev/urandom > "${payload}"
sha=$(sha256sum "${payload}" | cut -d' ' -f1)
size=$(stat -c '%s' "${payload}")
sid=$(curl -fsS -X POST "${BASE}/backups" -H 'Content-Type: application/json' \
    -d '{"filename":"demo.tar","total_chunks":4,"sha256":"'"${sha}"'","size":'"${size}"'}' \
    | json "['session_id']")
echo "session ${sid}"
split -n 4 -d -a 1 "${payload}" "${WORK}/chunk-"
for i in 0 1; do
    curl -fsS -X PUT "${BASE}/backups/${sid}/chunks/${i}" \
        --data-binary @"${WORK}/chunk-${i}" | python3 -m json.tool
done

echo "--- swap: entry now points at disk B while the upload is open ---"
ln -sfn "${WORK}/disk-b" "${WORK}/entry"
for i in 2 3; do
    # Pinned fd keeps the write on the original volume; B never sees it.
    curl -fsS -X PUT "${BASE}/backups/${sid}/chunks/${i}" \
        --data-binary @"${WORK}/chunk-${i}" | python3 -m json.tool
done
[ "$(ls -A "${WORK}/disk-b")" = ".volume_id" ] || fail "disk B was modified!"

code=$(curl -s -o "${WORK}/r3.json" -w '%{http_code}' -X POST "${BASE}/backups/${sid}/complete")
[ "${code}" = "409" ] || fail "expected 409, got ${code}: $(cat "${WORK}/r3.json")"
echo "finalization rejected: $(cat "${WORK}/r3.json")"
code=$(curl -s -o "${WORK}/r3b.json" -w '%{http_code}' "${BASE}/backups")
[ "${code}" = "409" ] || fail "manifest through B must be 409, got ${code}"

say "4. reattach disk A: failed upload was never published; staging reaped"
ln -sfn "${WORK}/disk-a" "${WORK}/entry"
curl -fsS "${BASE}/backups" | python3 -m json.tool
[ -z "$(ls -A "${WORK}/disk-a/.staging" 2>/dev/null || true)" ] || fail "staging not reaped"

say "5. complete a fresh upload end-to-end and verify round-trip"
sid2=$(curl -fsS -X POST "${BASE}/backups" -H 'Content-Type: application/json' \
    -d '{"filename":"demo.tar","total_chunks":4,"sha256":"'"${sha}"'","size":'"${size}"'}' \
    | json "['session_id']")
for i in 0 1 2 3; do
    curl -fsS -X PUT "${BASE}/backups/${sid2}/chunks/${i}" \
        --data-binary @"${WORK}/chunk-${i}" -o /dev/null
done
published=$(curl -fsS -X POST "${BASE}/backups/${sid2}/complete")
echo "${published}" | python3 -m json.tool
aid=$(echo "${published}" | json "['id']")
curl -fsS "${BASE}/backups/archive/${aid}/download" -o "${WORK}/roundtrip.bin"
cmp "${payload}" "${WORK}/roundtrip.bin" && echo "round-trip OK: bytes identical"

say "ALL LOCAL DEMO CHECKS PASSED"
