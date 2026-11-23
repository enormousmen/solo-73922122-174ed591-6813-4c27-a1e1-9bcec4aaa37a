#!/usr/bin/env bash
# Plain curl recipes for every API endpoint. Assumes the service is running
# on localhost:8080 (docker compose) or override BASE below.
set -euo pipefail

BASE="${BASE:-http://localhost:8080}"
SHA=$(printf 'a%.0s' {1..64})  # 64 hex chars; replace with a real sha256

echo "### health"
curl -fsS "${BASE}/health"; echo

echo "### create upload session"
SID=$(curl -fsS -X POST "${BASE}/backups" \
    -H 'Content-Type: application/json' \
    -d '{"filename":"demo.tar","total_chunks":2,"sha256":"'"${SHA}"'","size":11}' \
    | python3 -c 'import sys,json;print(json.load(sys.stdin)["session_id"])')
echo "session=${SID}"

echo "### upload chunks"
printf 'hello ' > /tmp/chunk-0
printf 'world'  > /tmp/chunk-1
curl -fsS -X PUT "${BASE}/backups/${SID}/chunks/0" --data-binary @/tmp/chunk-0; echo
curl -fsS -X PUT "${BASE}/backups/${SID}/chunks/1" --data-binary @/tmp/chunk-1; echo

echo "### finalize (this example fails the checksum on purpose -> 422)"
curl -s -X POST "${BASE}/backups/${SID}/complete"; echo

echo "### abort a session"
curl -fsS -X DELETE "${BASE}/backups/${SID}" -o /dev/null -w 'HTTP %{http_code}\n'

echo "### list published archives"
curl -fsS "${BASE}/backups"; echo
