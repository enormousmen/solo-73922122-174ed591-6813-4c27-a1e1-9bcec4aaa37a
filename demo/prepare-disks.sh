#!/usr/bin/env bash
# Provision the two simulated "physical disks" with their volume id files.
# Run once on the host before `docker compose up`.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DISK_A="${HERE}/disks/disk-a"
DISK_B="${HERE}/disks/disk-b"

mkdir -p "${DISK_A}" "${DISK_B}"
printf 'VOL-DISK-A-0001' > "${DISK_A}/.volume_id"
printf 'VOL-DISK-B-0002' > "${DISK_B}/.volume_id"

echo "prepared:"
echo "  ${DISK_A}/.volume_id -> $(cat "${DISK_A}/.volume_id")"
echo "  ${DISK_B}/.volume_id -> $(cat "${DISK_B}/.volume_id")"
