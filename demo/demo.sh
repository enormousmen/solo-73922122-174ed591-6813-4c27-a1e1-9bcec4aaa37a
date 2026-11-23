#!/usr/bin/env bash
# End-to-end demo *inside* the running container (privileged).
#
#   docker compose exec backup-api /srv/app/demo/demo.sh
#
# It exercises, in order:
#   1. pre-upload volume-id rejection after disk B is swapped in;
#   2. entry unavailable (umount -> empty system-disk directory) rejection;
#   3. disk swap *during* an upload: chunks keep landing on the pinned
#      original volume, finalization refuses to publish, B stays untouched;
#   4. disk A reattached: a normal upload completes and old archives are
#      queryable again.
set -euo pipefail

BASE="${BASE:-http://localhost:8080}"
ENTRY=/backups/data

say() { printf '\n\033[1;36m== %s ==\033[0m\n' "$*"; }
fail() { printf '\033[1;31mUNEXPECTED: %s\033[0m\n' "$*" >&2; exit 1; }

# --- tiny JSON helper (python3 is present in the image) --------------------
json() { python3 -c 'import sys,json;print(json.load(sys.stdin)'"$1"')'; }

mount_a() { mount --bind /disks/disk-a "${ENTRY}"; }
mount_b() { mount --bind /disks/disk-b "${ENTRY}"; }
unmount()  { umount "${ENTRY}"; }

say "0. baseline health"
curl -fsS "${BASE}/health" | python3 -m json.tool

say "1. swap in disk B BEFORE upload -> create must be rejected (409)"
mount_b
echo "entry now presents: $(cat "${ENTRY}/.volume_id")"
code=$(curl -s -o /tmp/r1.json -w '%{http_code}' -X POST "${BASE}/backups" \
    -H 'Content-Type: application/json' \
    -d '{"filename":"demo.tar","total_chunks":1,"sha256":"'$(printf '0%.0s' {1..64})'","size":1}')
[ "${code}" = "409" ] || fail "expected 409, got ${code}: $(cat /tmp/r1.json)"
cat /tmp/r1.json | python3 -m json.tool
[ "$(ls -A /disks/disk-b)" = ".volume_id" ] || fail "disk B was modified!"

say "2. umount entry (path falls back to empty system-disk dir) -> 503"
unmount
code=$(curl -s -o /tmp/r2.json -w '%{http_code}' -X POST "${BASE}/backups" \
    -H 'Content-Type: application/json' \
    -d '{"filename":"demo.tar","total_chunks":1,"sha256":"'$(printf '0%.0s' {1..64})'","size":1}')
[ "${code}" = "503" ] || fail "expected 503, got ${code}: $(cat /tmp/r2.json)"
cat /tmp/r2.json | python3 -m json.tool

say "3. reattach disk A, start an upload, then swap disks mid-flight"
mount_a
payload=/tmp/payload.bin
head -c 1048576 /dev/urandom > "${payload}"
sha=$(sha256sum "${payload}" | cut -d' ' -f1)
size=$(stat -c '%s' "${payload}")
sid=$(curl -fsS -X POST "${BASE}/backups" -H 'Content-Type: application/json' \
    -d '{"filename":"demo.tar","total_chunks":4,"sha256":"'"${sha}"'","size":'"${size}"'}' \
    | json "['session_id']")
echo "session ${sid}"

split -n 4 -d -a 1 "${payload}" /tmp/chunk-
for i in 0 1; do
    curl -fsS -X PUT "${BASE}/backups/${sid}/chunks/${i}" \
        --data-binary @"/tmp/chunk-${i}" -o /tmp/ack.json
    echo "chunk ${i}: $(cat /tmp/ack.json)"
done

echo "--- pulling disk A and inserting disk B while upload is open ---"
unmount
mount_b
echo "entry now presents: $(cat "${ENTRY}/.volume_id")"

# Remaining chunks are written through the pinned descriptor; they must
# keep succeeding and must never create files on disk B.
for i in 2 3; do
    curl -fsS -X PUT "${BASE}/backups/${sid}/chunks/${i}" \
        --data-binary @"/tmp/chunk-${i}" -o /tmp/ack.json
    echo "chunk ${i} after swap: $(cat /tmp/ack.json)"
done
[ "$(ls -A /disks/disk-b)" = ".volume_id" ] || fail "disk B was modified!"

code=$(curl -s -o /tmp/r3.json -w '%{http_code}' -X POST "${BASE}/backups/${sid}/complete")
[ "${code}" = "409" ] || fail "expected 409 on complete, got ${code}: $(cat /tmp/r3.json)"
echo "finalization rejected: $(cat /tmp/r3.json)"
code=$(curl -s -o /tmp/r3b.json -w '%{http_code}' "${BASE}/backups")
[ "${code}" = "409" ] || fail "manifest through B must be 409, got ${code}"
[ "$(ls -A /disks/disk-b)" = ".volume_id" ] || fail "disk B was modified!"

say "4. reattach disk A: old state returns; complete a normal upload"
unmount
mount_a
curl -fsS "${BASE}/health" | python3 -m json.tool
# The failed session was reaped on finalization; staging must be empty.
[ -z "$(ls -A /disks/disk-a/.staging 2>/dev/null || true)" ] || fail "staging not reaped"

sid2=$(curl -fsS -X POST "${BASE}/backups" -H 'Content-Type: application/json' \
    -d '{"filename":"demo.tar","total_chunks":4,"sha256":"'"${sha}"'","size":'"${size}"'}' \
    | json "['session_id']")
for i in 0 1 2 3; do
    curl -fsS -X PUT "${BASE}/backups/${sid2}/chunks/${i}" \
        --data-binary @"/tmp/chunk-${i}" -o /dev/null
done
published=$(curl -fsS -X POST "${BASE}/backups/${sid2}/complete")
echo "published: ${published}"
aid=$(echo "${published}" | json "['id']")

say "5. manifest and download on the reattached original disk"
curl -fsS "${BASE}/backups" | python3 -m json.tool
curl -fsS "${BASE}/backups/archive/${aid}/download" -o /tmp/roundtrip.bin
cmp "${payload}" /tmp/roundtrip.bin && echo "downloaded archive matches the upload byte-for-byte"

say "ALL DEMO CHECKS PASSED"
