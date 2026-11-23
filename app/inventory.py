"""Inventory (盘点) booklets and the per-archive reverse index.

A booklet ("清册") is the set of archives published on the backup volume at
the moment an inventory reads it. Booklets live in a *separate* persistent
directory (``INVENTORY_ROOT``, default ``/inventory``) that must not be on
the backup disk: after the disk is pulled, an operator can still look up
which physical disk holds a given archive id.

Design rules implemented here (see the task statement):

* The request carries a disk number ("盘号"). It must equal the volume id
  the mount entry currently presents, otherwise the request is rejected.
* The booklet contains exactly the archives already published when the
  read starts. A single ``scandir`` snapshot names the sidecars that will
  be read; archives published while the read is in flight are not part of
  this booklet (they are picked up by the next inventory). Uploads are
  never blocked.
* At most one inventory may run for a given volume: a second request gets
  HTTP 409 ``INVENTORY_BUSY``.
* Before the booklet is committed, the entry is re-probed: it must still
  resolve to the same volume id and the same device that were read. A swap
  at commit time fails the inventory and leaves the previous booklet in
  place.
* Any failure (swap, read error, write error) leaves the last successful
  booklet untouched -- the new booklet is only ever published through an
  atomic temp-file rename.
* The reverse index (archive id -> volumes whose latest successful
  booklet includes it) is also persisted in ``INVENTORY_ROOT`` and rebuilt
  from the booklets whenever it is missing or unreadable, so queries keep
  working after a service restart even if the index file was lost.
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
import uuid
from dataclasses import dataclass

from .store import _META_SUFFIX, _open_dir
from .volume import Probe, VolumeError, VolumeGuard

_INVENTORY_DIR = "volumes"
_INDEX_NAME = "archive-index.json"
_TMP_SUFFIX = ".tmp"
# Volume ids come from pre-provisioned .volume_id files; restrict booklet
# filenames so a hostile id can never escape INVENTORY_ROOT.
_VOLUME_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_READ_LIMIT = 8 * 1024 * 1024


@dataclass
class _Busy:
    """Marker for an inventory currently running on this volume."""

    inventory_id: str


class InventoryStore:
    def __init__(self, guard: VolumeGuard, root_path: str):
        self.guard = guard
        self.root_path = root_path
        # One slot per volume id; the entry inside is replaced atomically.
        self._busy: dict[str, _Busy] = {}
        self._busy_guard = threading.Lock()
        self._index_lock = threading.Lock()
        self._init_error: VolumeError | None = None
        self._volumes_dir = os.path.join(self.root_path, _INVENTORY_DIR)
        try:
            os.makedirs(self._volumes_dir, exist_ok=True)
        except OSError as exc:
            # Defer the failure to first real use: constructing the app
            # must not crash just because the default /inventory path is
            # not writable in a local test sandbox.
            self._init_error = VolumeError(
                "INVENTORY_STORE_UNAVAILABLE",
                500,
                f"cannot initialise inventory directory {self.root_path!r}: {exc}",
            )
        else:
            # Best-effort index rebuild at startup; all query paths also
            # rebuild lazily, so a failure here must not prevent the
            # service from serving.
            try:
                self._load_index()
            except VolumeError:
                pass

    def _ensure_ready(self) -> None:
        if self._init_error is not None:
            raise self._init_error

    # ------------------------------------------------------------ paths

    @staticmethod
    def _validate_volume_id(volume_id: str) -> str:
        if not volume_id or not _VOLUME_ID_RE.match(volume_id):
            raise VolumeError(
                "INVALID_VOLUME_ID",
                400,
                "disk number / volume id contains unsupported characters",
            )
        return volume_id

    @staticmethod
    def _validate_archive_id(archive_id: str) -> str:
        try:
            uuid.UUID(archive_id)
        except (ValueError, AttributeError, TypeError):
            raise VolumeError(
                "INVALID_ARCHIVE_ID", 400, "archive id must be a valid uuid hex"
            )
        return archive_id

    def _volume_dir(self, volume_id: str) -> str:
        return os.path.join(self._volumes_dir, volume_id)

    def _booklet_path(self, volume_id: str) -> str:
        return os.path.join(self._volume_dir(volume_id), "booklet.json")

    def _index_path(self) -> str:
        return os.path.join(self.root_path, _INDEX_NAME)

    # ------------------------------------------------------ atomic writes

    def _atomic_write_json(self, path: str, payload: dict) -> None:
        tmp_path = f"{path}.{uuid.uuid4().hex}{_TMP_SUFFIX}"
        try:
            fd = os.open(
                tmp_path,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC,
                0o644,
            )
            try:
                os.write(fd, json.dumps(payload, ensure_ascii=False).encode("utf-8"))
                os.fsync(fd)
            finally:
                os.close(fd)
            os.rename(tmp_path, path)
            # Persist the rename itself as far as the filesystem allows.
            dir_fd = os.open(os.path.dirname(path), os.O_RDONLY | os.O_CLOEXEC)
            try:
                os.fsync(dir_fd)
            except OSError:
                pass
            finally:
                os.close(dir_fd)
        except OSError as exc:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise VolumeError(
                "INVENTORY_WRITE_FAILED",
                503,
                f"failed to persist inventory data under {self.root_path!r}: {exc}",
            ) from exc

    # ----------------------------------------------------------- booklets

    def _read_booklet_file(self, volume_id: str) -> dict | None:
        path = self._booklet_path(volume_id)
        try:
            fd = os.open(path, os.O_RDONLY | os.O_CLOEXEC)
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise VolumeError(
                "INVENTORY_READ_FAILED",
                503,
                f"cannot read booklet for volume {volume_id!r}: {exc}",
            ) from exc
        try:
            return json.loads(os.read(fd, _READ_LIMIT))
        finally:
            os.close(fd)

    def get_booklet(self, volume_id: str) -> dict | None:
        """Latest successfully committed booklet, or ``None``.

        Reads only the persistent inventory directory -- never the backup
        volume -- so it answers after the disk has been pulled.
        """
        self._ensure_ready()
        self._validate_volume_id(volume_id)
        return self._read_booklet_file(volume_id)

    def list_booklets(self) -> list[dict]:
        self._ensure_ready()
        base = self._volumes_dir
        try:
            names = sorted(os.listdir(base))
        except OSError as exc:
            raise VolumeError(
                "INVENTORY_READ_FAILED",
                503,
                f"cannot list inventory directory: {exc}",
            ) from exc
        summaries: list[dict] = []
        for name in names:
            booklet = self._read_booklet_file(name)
            if booklet is not None:
                summaries.append(
                    {
                        "volume_id": booklet["volume_id"],
                        "inventory_id": booklet["inventory_id"],
                        "committed_at": booklet["committed_at"],
                        "archive_count": len(booklet["archives"]),
                    }
                )
        return summaries

    # -------------------------------------------------------------- index

    def _load_index(self) -> dict:
        try:
            fd = os.open(self._index_path(), os.O_RDONLY | os.O_CLOEXEC)
        except FileNotFoundError:
            return self._rebuild_index()
        except OSError as exc:
            raise VolumeError(
                "INVENTORY_READ_FAILED",
                503,
                f"cannot open archive index: {exc}",
            ) from exc
        try:
            try:
                index = json.loads(os.read(fd, 16 * 1024 * 1024))
            except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                # Corrupt/partial index: the booklets remain the source of
                # truth, rebuild from them.
                return self._rebuild_index()
        finally:
            os.close(fd)
        if not isinstance(index, dict) or "archives" not in index:
            return self._rebuild_index()
        return index

    def _rebuild_index(self) -> dict:
        """Rebuild the archive->volume reverse index from every booklet."""
        archives: dict[str, list[dict]] = {}
        volumes: dict[str, dict] = {}
        base = self._volumes_dir
        try:
            volume_dirs = sorted(os.listdir(base))
        except FileNotFoundError:
            volume_dirs = []
        except OSError as exc:
            raise VolumeError(
                "INVENTORY_READ_FAILED",
                503,
                f"cannot scan inventory directory: {exc}",
            ) from exc
        for volume_id in volume_dirs:
            booklet = self._read_booklet_file(volume_id)
            if booklet is None:
                continue
            committed_at = booklet["committed_at"]
            volumes[volume_id] = {
                "volume_id": volume_id,
                "inventory_id": booklet["inventory_id"],
                "committed_at": committed_at,
                "archive_count": len(booklet["archives"]),
            }
            for archive in booklet["archives"]:
                archives.setdefault(archive["id"], []).append(
                    {
                        "volume_id": volume_id,
                        "inventory_id": booklet["inventory_id"],
                        "committed_at": committed_at,
                    }
                )
        for hits in archives.values():
            hits.sort(key=lambda item: item["committed_at"], reverse=True)
        index = {
            "version": 1,
            "updated_at": time.time(),
            "volumes": volumes,
            "archives": archives,
        }
        self._atomic_write_json(self._index_path(), index)
        return index

    def lookup_archive(self, archive_id: str) -> list[dict]:
        """Volumes whose *latest successful* booklet contains the archive.

        Queries only the persistent inventory directory. Works while an
        inventory is uncommitted, after the disk is pulled and after a
        service restart.
        """
        self._validate_archive_id(archive_id)
        self._ensure_ready()
        with self._index_lock:
            index = self._load_index()
        return list(index.get("archives", {}).get(archive_id, []))

    # ------------------------------------------------------------ inventory

    def _acquire_slot(self, volume_id: str) -> _Busy:
        slot = _Busy(inventory_id=uuid.uuid4().hex)
        with self._busy_guard:
            existing = self._busy.get(volume_id)
            if existing is not None:
                raise VolumeError(
                    "INVENTORY_BUSY",
                    409,
                    f"an inventory of volume {volume_id!r} is already in "
                    f"progress (inventory_id={existing.inventory_id})",
                )
            self._busy[volume_id] = slot
        return slot

    def _release_slot(self, volume_id: str, slot: _Busy) -> None:
        with self._busy_guard:
            if self._busy.get(volume_id) is slot:
                del self._busy[volume_id]

    def _read_archives_snapshot(self, probe: Probe) -> list[dict]:
        """Read the archives already published at read start.

        The sidecar name set is captured with one ``scandir`` call and is
        the frozen boundary of this booklet: anything published afterwards
        (a new sidecar atomically renamed in) is simply absent from the
        set and belongs to the next inventory.
        """
        try:
            archives_fd = _open_dir("archives", probe.fd)
        except FileNotFoundError:
            return []
        try:
            with os.scandir(archives_fd) as iterator:
                sidecars = sorted(
                    entry.name
                    for entry in iterator
                    if entry.name.endswith(_META_SUFFIX)
                )
            entries: list[dict] = []
            for name in sidecars:
                try:
                    meta_fd = os.open(
                        name, os.O_RDONLY | os.O_CLOEXEC, dir_fd=archives_fd
                    )
                except FileNotFoundError:
                    # Renamed/removed between snapshot and open: not part of
                    # a stable "already published" set, defer to next run.
                    continue
                try:
                    try:
                        meta = json.loads(os.read(meta_fd, _READ_LIMIT))
                    except (json.JSONDecodeError, UnicodeDecodeError, OSError) as exc:
                        raise VolumeError(
                            "INVENTORY_READ_FAILED",
                            503,
                            f"cannot parse archive metadata {name!r}: {exc}",
                        ) from exc
                finally:
                    os.close(meta_fd)
                data_name = name[: -len(_META_SUFFIX)] + ".tar"
                try:
                    st = os.stat(data_name, dir_fd=archives_fd)
                except FileNotFoundError:
                    # Sidecar present but data file not (yet) committed:
                    # the archive is not fully published, skip it.
                    continue
                except OSError as exc:
                    raise VolumeError(
                        "INVENTORY_READ_FAILED",
                        503,
                        f"cannot stat archive data {data_name!r}: {exc}",
                    ) from exc
                # Report the on-disk size, mirroring GET /backups.
                meta["size"] = st.st_size
                entries.append(meta)
            entries.sort(key=lambda item: item.get("id", ""))
            return entries
        finally:
            os.close(archives_fd)

    def run_inventory(self, requested_volume_id: str) -> dict:
        """Run one inventory for the given disk number / volume id."""
        volume_id = self._validate_volume_id(requested_volume_id)
        self._ensure_ready()

        # First probe: the mount entry must present exactly the disk number
        # the operator asked to inventory. probe_any() does not enforce the
        # upload-provisioned id: the inventory contract is "requested disk
        # number == volume id currently presented by the entry". Its fd
        # pins the volume for the whole read, so a swap cannot redirect
        # the read onto a replacement directory.
        probe = self.guard.probe_any()
        slot: _Busy | None = None
        try:
            if probe.volume_id != volume_id:
                raise VolumeError(
                    "VOLUME_ID_MISMATCH",
                    409,
                    f"requested disk number {volume_id!r} but the mount "
                    f"entry currently presents {probe.volume_id!r}",
                )
            original_dev = probe.dev
            slot = self._acquire_slot(volume_id)
            started_at = time.time()

            # Any read failure aborts before a single booklet byte is
            # written: the previous booklet remains authoritative.
            archives = self._read_archives_snapshot(probe)

            # The pinned read fd is closed before commit; commit re-probes
            # by path and additionally compares the captured device id.
            os.close(probe.fd)
            probe = None

            # Second probe at commit time: the entry must still present the
            # same volume id and the same device that were read.
            try:
                commit_probe = self.guard.probe_any()
            except VolumeError as exc:
                raise VolumeError(
                    "VOLUME_SWITCHED",
                    409,
                    "mount entry cannot be re-verified before commit "
                    f"({exc.code}); keeping the previous booklet",
                ) from exc
            try:
                if (
                    commit_probe.volume_id != volume_id
                    or commit_probe.dev != original_dev
                ):
                    raise VolumeError(
                        "VOLUME_SWITCHED",
                        409,
                        "mount entry no longer points at the volume that "
                        "was read; keeping the previous booklet",
                    )
                booklet = {
                    "version": 1,
                    "inventory_id": slot.inventory_id,
                    "volume_id": volume_id,
                    "started_at": started_at,
                    "committed_at": time.time(),
                    "archive_count": len(archives),
                    "archives": archives,
                }
                os.makedirs(self._volume_dir(volume_id), exist_ok=True)
                # Atomic tmp->rename: a save failure leaves the previous
                # booklet (if any) exactly where it was.
                self._atomic_write_json(self._booklet_path(volume_id), booklet)
            finally:
                os.close(commit_probe.fd)

            # Refresh the persistent reverse index from all booklets.
            with self._index_lock:
                index = self._rebuild_index()
            return {
                "inventory_id": slot.inventory_id,
                "volume_id": volume_id,
                "archive_count": len(archives),
                "started_at": started_at,
                "committed_at": booklet["committed_at"],
                "indexed_volumes": len(index.get("volumes", {})),
            }
        finally:
            if probe is not None:
                os.close(probe.fd)
            if slot is not None:
                self._release_slot(volume_id, slot)
