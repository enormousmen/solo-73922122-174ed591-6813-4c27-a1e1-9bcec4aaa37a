#!/bin/sh
# Bind-mount "disk A" over the backup entry before starting the API.
#
# The backup entry /backups/data is a mount point. Pulling disk A is
# simulated by `umount /backups/data` (the empty directory underneath
# remains, i.e. the system-disk fallback case); inserting the replacement
# disk is `mount --bind /disks/disk-b /backups/data`.
set -eu

mkdir -p /backups/data /disks/disk-a /disks/disk-b

if ! mountpoint -q /backups/data; then
    mount --bind /disks/disk-a /backups/data
    echo "[entrypoint] mounted disk A at /backups/data"
fi

exec "$@"
