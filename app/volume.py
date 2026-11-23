"""Volume entry verification and mount-pinning primitives.

The backup entry (e.g. ``/backups/data``) is normally a mount point of the
backup disk. If that disk is pulled, the same *path* may fall back onto the
system disk, or a replacement disk may be mounted over it. Two mechanisms are
used to stay safe:

1. ``VolumeGuard.probe()`` validates the **path** before any upload is
   accepted: the entry must be reachable and the pre-provisioned volume id
   file inside it must match the configured id.
2. A successful probe returns an ``O_PATH`` file descriptor opened on the
   entry directory. That fd pins the original mount for its whole lifetime.
   Every subsequent write / rename / unlink is performed relative to this fd
   (``openat``/``mkdirat``/``renameat``/``unlinkat`` via ``dir_fd=``), so a
   remount at the same path can never redirect our I/O onto the replacement
   directory or the system disk.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

# O_PATH is Linux-specific; the whole design targets Linux containers/hosts.
_O_PATH = getattr(os, "O_PATH", 0o10000000)


class VolumeError(Exception):
    """Raised for every volume / session related rejection.

    Carries an HTTP status code and a stable machine-readable error code so
    the API layer can render it directly.
    """

    def __init__(self, code: str, status_code: int, message: str):
        super().__init__(message)
        self.code = code
        self.status_code = status_code


@dataclass
class Probe:
    """Result of a successful path-based probe.

    ``fd`` is an open ``O_PATH`` descriptor pinning the probed mount; the
    caller owns it and must close it (or transfer ownership, e.g. into an
    upload session).
    """

    root_path: str
    dev: int
    volume_id: str
    fd: int


class VolumeGuard:
    def __init__(
        self,
        root_path: str,
        expected_volume_id: str,
        id_filename: str = ".volume_id",
    ):
        self.root_path = root_path
        self.expected_volume_id = expected_volume_id.strip()
        self.id_filename = id_filename

    def probe(self) -> Probe:
        """Validate the entry *path* and pin its mount.

        Raises :class:`VolumeError`:

        * ``NOT_CONFIGURED`` (HTTP 500) when no expected volume id is set;
        * ``VOLUME_UNAVAILABLE`` (HTTP 503) when the entry cannot be
          stat'ed / opened at all;
        * ``VOLUME_ID_MISMATCH`` (HTTP 409) when the entry is reachable but
          the pre-provisioned volume id is missing or differs.
        """
        if not self.expected_volume_id:
            raise VolumeError(
                "NOT_CONFIGURED",
                500,
                "BACKUP_VOLUME_ID is not configured; refusing any access",
            )
        try:
            st = os.stat(self.root_path)
        except OSError as exc:
            raise VolumeError(
                "VOLUME_UNAVAILABLE",
                503,
                f"backup entry {self.root_path!r} is unavailable: {exc}",
            ) from exc

        try:
            root_fd = os.open(
                self.root_path, _O_PATH | os.O_DIRECTORY | os.O_CLOEXEC
            )
        except OSError as exc:
            raise VolumeError(
                "VOLUME_UNAVAILABLE",
                503,
                f"cannot open backup entry {self.root_path!r}: {exc}",
            ) from exc

        try:
            try:
                id_fd = os.open(
                    self.id_filename, os.O_RDONLY | os.O_CLOEXEC, dir_fd=root_fd
                )
            except OSError as exc:
                raise VolumeError(
                    "VOLUME_ID_MISMATCH",
                    409,
                    f"volume id file {self.id_filename!r} is missing or "
                    f"unreadable at the backup entry: {exc}",
                ) from exc
            try:
                raw = os.read(id_fd, 4096)
            finally:
                os.close(id_fd)
            volume_id = raw.decode("utf-8", errors="replace").strip()
        except VolumeError:
            os.close(root_fd)
            raise

        if volume_id != self.expected_volume_id:
            os.close(root_fd)
            raise VolumeError(
                "VOLUME_ID_MISMATCH",
                409,
                f"expected volume id {self.expected_volume_id!r} but entry "
                f"{self.root_path!r} presents {volume_id!r}",
            )

        return Probe(
            root_path=self.root_path,
            dev=st.st_dev,
            volume_id=volume_id,
            fd=root_fd,
        )

    def probe_any(self) -> Probe:
        """Probe the entry *without* enforcing the provisioned volume id.

        Used by the inventory flow, whose rule is "the operator-supplied
        disk number must match whatever volume the entry currently
        presents" -- an operator may inventory the currently mounted
        backup disk even though uploads remain pinned to the provisioned
        one. The entry must still exist, open, and carry a readable
        volume id file; the returned fd still pins the mount for the
        whole read.
        """
        try:
            st = os.stat(self.root_path)
        except OSError as exc:
            raise VolumeError(
                "VOLUME_UNAVAILABLE",
                503,
                f"backup entry {self.root_path!r} is unavailable: {exc}",
            ) from exc

        try:
            root_fd = os.open(
                self.root_path, _O_PATH | os.O_DIRECTORY | os.O_CLOEXEC
            )
        except OSError as exc:
            raise VolumeError(
                "VOLUME_UNAVAILABLE",
                503,
                f"cannot open backup entry {self.root_path!r}: {exc}",
            ) from exc

        try:
            try:
                id_fd = os.open(
                    self.id_filename, os.O_RDONLY | os.O_CLOEXEC, dir_fd=root_fd
                )
            except OSError as exc:
                raise VolumeError(
                    "VOLUME_ID_MISMATCH",
                    409,
                    f"volume id file {self.id_filename!r} is missing or "
                    f"unreadable at the backup entry: {exc}",
                ) from exc
            try:
                raw = os.read(id_fd, 4096)
            finally:
                os.close(id_fd)
            volume_id = raw.decode("utf-8", errors="replace").strip()
        except VolumeError:
            os.close(root_fd)
            raise

        if not volume_id:
            os.close(root_fd)
            raise VolumeError(
                "VOLUME_ID_MISMATCH",
                409,
                f"volume id file {self.id_filename!r} at the backup entry "
                "is empty",
            )

        return Probe(
            root_path=self.root_path,
            dev=st.st_dev,
            volume_id=volume_id,
            fd=root_fd,
        )

    @staticmethod
    def pinned_dev(fd: int) -> int:
        """Device id of the mount a pinned descriptor actually points at."""
        return os.fstat(fd).st_dev
