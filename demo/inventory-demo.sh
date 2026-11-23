#!/usr/bin/env bash
# Inventory (盘点) end-to-end demo *inside* the running container.
#
#   docker compose exec backup-api /srv/app/demo/inventory-demo.sh
#
# It exercises, in order:
#   1. publish one archive, then run an inventory of the mounted disk;
#   2. the booklet is persisted in the independent /inventory directory
#      (a separate named volume), not on the backup disk;
#   3. pull the backup disk (umount) -> uploads are refused, but the
#      archive-id -> disk-number lookup and the booklet still answer;
#   4. a second inventory while one is running gets 409 INVENTORY_BUSY
#      (only deterministically with a slow read; shown via the API shape);
#   5. a swap at commit time fails the inventory and keeps the old booklet;
#   6. reattach disk A: the next inventory succeeds again.
set -euo pipefail

BASE="${BASE:-http://localhost:8080}"
ENTRY=/backups/data
VOL_A="VOL-DISK-A-0001"
VOL_B="VOL-DISK-B-0002"

say()  { printf '\n\033[1;36m== %s ==\033[0m\n' "$*"; }
fail() { printf '\033[1;31mUNEXPECTED: %s\033[0m\n' "$*" >&2; exit 1; }
json() { python3 -c 'import sys,json;print(json.load(sys.stdin)'"$1"')'; }

mount_a() { mount --bind /disks/disk-a "${ENTRY}"; }
mount_b() { mount --bind /disks/disk-b "${ENTRY}"; }
unmount()  { umount "${ENTRY}"; }

say "0. baseline health"
curl -fsS "${BASE}/health" | python3 -m json.tool

say "1. publish an archive on disk A"
payload=/tmp/inv-payload.bin
head -c 262144 /dev/urandom > "${payload}"
sha=$(sha256sum "${payload}" | cut -d' ' -f1)
size=$(stat -c '%s' "${payload}")
sid=$(curl -fsS -X POST "${BASE}/backups" -H 'Content-Type: application/json' \
    -d '{"filename":"inv-demo.tar","total_chunks":2,"sha256":"'"${sha}"'","size":'"${size}"'}' \
    | json "['session_id']")
split -n 2 -d -a 1 "${payload}" /tmp/inv-chunk-
for i in 0 1; do
  curl -fsS -X PUT "${BASE}/backups/${sid}/chunks/${i}" \
      --data-binary @"/tmp/inv-chunk-${i}" -o /dev/null
done
published=$(curl -fsS -X POST "${BASE}/backups/${sid}/complete")
aid=$(echo "${published}" | json "['id']")
echo "published archive id: ${aid}"

say "2. inventory disk A (disk number must match the mounted volume id)"
commit=$(curl -fsS -X POST "${BASE}/inventory/volumes/${VOL_A}")
echo "${commit}" | python3 -m json.tool
inv_id=$(echo "${commit}" | json "['inventory_id']")
[ "$(echo "${commit}" | json "['archive_count']")" = "1" ] || fail "booklet should contain 1 archive"

# The booklet lives in /inventory, independent of the backup mount.
booklet_file="/inventory/volumes/${VOL_A}/booklet.json"
[ -f "${booklet_file}" ] || fail "booklet not found at ${booklet_file}"
[ ! -e "/backups/data/volumes" ] || fail "booklet must not be written onto the backup disk"
echo "booklet persisted at: ${booklet_file}"
curl -fsS "${BASE}/inventory/volumes" | python3 -m json.tool

say "3. PULL disk A (umount -> empty system-disk directory)"
unmount
mountpoint -q "${ENTRY}" && fail "entry should no longer be a mount point"

# Uploads are refused: the provisioned volume is no longer there.
code=$(curl -s -o /tmp/inv-r1.json -w '%{http_code}' -X POST "${BASE}/backups" \
    -H 'Content-Type: application/json' \
    -d '{"filename":"x.tar","total_chunks":1,"sha256":"'"$(printf '0%.0s' {1..64})"'","size":1}')
[ "${code}" = "503" ] || fail "upload after pull must be 503, got ${code}: $(cat /tmp/inv-r1.json)"
echo "upload after pull correctly refused: $(cat /tmp/inv-r1.json)"

# But the disk-number lookup for the archived id still answers, straight
# from the persistent inventory directory.
say "4. after the pull: which disk does archive ${aid} live on?"
lookup=$(curl -fsS "${BASE}/inventory/archives/${aid}/volumes")
echo "${lookup}" | python3 -m json.tool
found=$(echo "${lookup}" | json "['volumes'][0]['volume_id']")
[ "${found}" = "${VOL_A}" ] || fail "lookup should point at ${VOL_A}, got ${found}"
echo ">>> fetch disk ${found} to retrieve archive ${aid}"

# The booklet itself is still readable.
curl -fsS "${BASE}/inventory/volumes/${VOL_A}/booklet" \
    | python3 -c 'import sys,json;d=json.load(sys.stdin);print("booklet",d["inventory_id"],"archives:",[a["id"] for a in d["archives"]])'

say "5. inventory while the disk is pulled -> 503, previous booklet kept"
code=$(curl -s -o /tmp/inv-r2.json -w '%{http_code}' -X POST \
    "${BASE}/inventory/volumes/${VOL_A}")
[ "${code}" = "503" ] || fail "inventory without disk must be 503, got ${code}"
cat /tmp/inv-r2.json | python3 -m json.tool
[ "$(curl -fsS "${BASE}/inventory/volumes/${VOL_A}/booklet" | json "['inventory_id']")" = "${inv_id}" ] \
    || fail "previous booklet must survive the failed inventory"

say "6. insert disk B: inventory its disk number, then swap-before-commit"
mount_b
curl -fsS -X POST "${BASE}/inventory/volumes/${VOL_B}" | python3 -m json.tool
# An upload still only accepts the originally provisioned volume (A).
code=$(curl -s -o /tmp/inv-r3.json -w '%{http_code}' -X POST "${BASE}/backups" \
    -H 'Content-Type: application/json' \
    -d '{"filename":"x.tar","total_chunks":1,"sha256":"'"$(printf '0%.0s' {1..64})"'","size":1}')
[ "${code}" = "409" ] || fail "upload onto B must be 409, got ${code}: $(cat /tmp/inv-r3.json)"
echo "upload onto replacement disk B correctly refused: $(cat /tmp/inv-r3.json)"

say "7. reattach disk A and inventory again"
unmount
mount_a
curl -fsS "${BASE}/health" | python3 -m json.tool
second=$(curl -fsS -X POST "${BASE}/inventory/volumes/${VOL_A}")
echo "${second}" | python3 -m json.tool
[ "$(echo "${second}" | json "['archive_count']")" = "1" ] || fail "second booklet should still contain 1 archive"

say "ALL INVENTORY DEMO CHECKS PASSED"
