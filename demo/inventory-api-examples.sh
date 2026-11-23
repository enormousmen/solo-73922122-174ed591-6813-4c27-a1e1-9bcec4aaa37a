#!/usr/bin/env bash
# Plain curl recipes for the inventory (盘点) API. Assumes the service is
# running on localhost:8080 (docker compose) or override BASE below.
#
# The key scenario: after the backup disk has been pulled, the operator
# still needs to know *which physical disk* holds a given archive id. The
# booklets live in the independent INVENTORY_ROOT volume, so these GETs
# keep answering without any backup disk mounted.
set -euo pipefail

BASE="${BASE:-http://localhost:8080}"
# The disk number is the pre-provisioned volume id on the physical disk.
DISK_A="VOL-DISK-A-0001"
DISK_B="VOL-DISK-B-0002"

echo "### 1. run an inventory: disk number is checked against the mounted volume id"
echo "###    201 = booklet committed; 409 VOLUME_ID_MISMATCH = wrong disk number;"
echo "###    409 INVENTORY_BUSY = an inventory of this disk is already running"
curl -sS -X POST "${BASE}/inventory/volumes/${DISK_A}" -w '\nHTTP %{http_code}\n'

echo
echo "### 2. read the latest successful booklet (full archive register of the disk)"
curl -fsS "${BASE}/inventory/volumes/${DISK_A}/booklet" | python3 -m json.tool

echo
echo "### 3. list every disk that has a committed booklet"
curl -fsS "${BASE}/inventory/volumes" | python3 -m json.tool

echo
echo "### 4. AFTER PULLING THE DISK: which disk does this archive id live on?"
echo "###    (replace ARCHIVE_ID with a real id returned by /backups or a booklet)"
ARCHIVE_ID="${ARCHIVE_ID:-00000000000000000000000000000000}"
curl -fsS "${BASE}/inventory/archives/${ARCHIVE_ID}/volumes" | python3 -m json.tool
# Expected shape when the id was on disk A:
#   {"archive_id":"...","volumes":[{"volume_id":"VOL-DISK-A-0001",
#    "inventory_id":"...","committed_at":...}]}
# An unknown (but well-formed) id returns {"archive_id":"...","volumes":[]}.

echo
echo "### 5. a real end-to-end lookup: take an id from the manifest while a disk"
echo "###    is mounted, then (simulated pull) query it again without the disk."
AID=$(curl -fsS "${BASE}/backups" \
    | python3 -c 'import sys,json;a=json.load(sys.stdin)["archives"];print(a[0]["id"] if a else "")')
if [ -n "${AID}" ]; then
    echo "archive id from manifest: ${AID}"
    curl -fsS "${BASE}/inventory/archives/${AID}/volumes" | python3 -m json.tool
    echo
    echo ">>> now pull the backup disk (docker compose exec backup-api umount /backups/data)"
    echo ">>> and re-run the same GET -- it still returns the disk number:"
    echo "curl -fsS ${BASE}/inventory/archives/${AID}/volumes"
else
    echo "(no published archive yet; publish one via demo/api-examples.sh first)"
fi
