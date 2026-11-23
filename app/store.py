"""Upload sessions and published-archive management.

All filesystem operations use ``*at(..., dir_fd=<pinned fd>)`` calls. A
session holds three descriptors opened (and pinned) at session creation:

* ``root_fd``     - the backup entry itself;
* ``staging_fd``  - ``<entry>/.staging`` (lives on the same volume so the
  final rename is atomic);
* ``session_fd``  - ``<entry>/.staging/<session-id>``, where chunks are
  written and assembled.

If the disk is swapped under the mount point while an upload is in flight,
the *path* now resolves elsewhere, but these descriptors still reference the
original mount. Writes either keep landing on the original volume or fail
with EIO/ENXIO (e.g. after a lazy unmount) — they never touch the
replacement directory.
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
import time
import uuid
from dataclasses import dataclass, field

from .volume import Probe, VolumeError, VolumeGuard

_STAGING_DIR = ".staging"
_META_SUFFIX = ".meta.json"
_TMP_SUFFIX = ".tmp"

# Session is torn down automatically when one of these errors is raised at
# finalization time (the entry can no longer serve this upload).
_FATAL_SESSION_ERRORS = frozenset(
    {
        "VOLUME_SWITCHED",
        "VOLUME_IO_ERROR",
        "VOLUME_UNAVAILABLE",
        "VOLUME_ID_MISMATCH",
        "NOT_CONFIGURED",
    }
)


def _open_dir(path: str, dir_fd: int) -> int:
    # O_RDONLY|O_DIRECTORY (rather than O_PATH) so the descriptor can also
    # back readdir/scandir; it still pins the mount and serves as dir_fd for
    # every *at() call below.
    return os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC, dir_fd=dir_fd)


def _ensure_dir(name: str, parent_fd: int) -> int:
    """``mkdir -p`` one level relative to a pinned parent descriptor."""
    try:
        os.mkdir(name, 0o755, dir_fd=parent_fd)
    except FileExistsError:
        pass
    except OSError as exc:
        raise VolumeError(
            "VOLUME_UNAVAILABLE",
            503,
            f"cannot create directory {name!r} on backup volume: {exc}",
        ) from exc
    return _open_dir(name, parent_fd)


def _chunk_name(index: int) -> str:
    return f"chunk-{index:08d}"


@dataclass
class Session:
    id: str
    filename: str
    total_chunks: int
    sha256: str
    size: int
    created_at: float
    # Pinned descriptors (all referencing the original mount).
    root_fd: int
    staging_fd: int
    session_fd: int
    original_dev: int
    volume_id: str
    uploaded: set[int] = field(default_factory=set)
    finished: bool = False
    aborted: bool = False
    lock: threading.Lock = field(default_factory=threading.Lock)

    def close_fds(self) -> None:
        for fd in (self.session_fd, self.staging_fd, self.root_fd):
            try:
                os.close(fd)
            except OSError:
                pass


class SessionStore:
    def __init__(self, guard: VolumeGuard):
        self.guard = guard
        self._sessions: dict[str, Session] = {}
        self._lock = threading.Lock()

    # ------------------------------------------------------------------ open

    def create_session(
        self, filename: str, total_chunks: int, sha256: str, size: int
    ) -> Session:
        """Verify the entry, pin the mount, and create the staging area."""
        probe: Probe = self.guard.probe()  # raises VolumeError on bad entry
        try:
            staging_fd = _ensure_dir(_STAGING_DIR, probe.fd)
            session_id = uuid.uuid4().hex
            try:
                os.mkdir(session_id, 0o755, dir_fd=staging_fd)
            except OSError as exc:
                os.close(staging_fd)
                raise VolumeError(
                    "VOLUME_UNAVAILABLE",
                    503,
                    f"cannot create staging session: {exc}",
                ) from exc
            session_fd = _open_dir(session_id, staging_fd)
        except VolumeError:
            os.close(probe.fd)
            raise

        session = Session(
            id=session_id,
            filename=filename,
            total_chunks=total_chunks,
            sha256=sha256,
            size=size,
            created_at=time.time(),
            root_fd=probe.fd,
            staging_fd=staging_fd,
            session_fd=session_fd,
            original_dev=probe.dev,
            volume_id=probe.volume_id,
        )
        with self._lock:
            self._sessions[session_id] = session
        return session

    def _get(self, session_id: str) -> Session:
        with self._lock:
            session = self._sessions.get(session_id)
        if session is None:
            raise VolumeError("SESSION_NOT_FOUND", 404, "unknown upload session")
        if session.aborted or session.finished:
            raise VolumeError(
                "SESSION_CLOSED", 409, "upload session is already closed"
            )
        return session

    # --------------------------------------------------------------- chunks

    def write_chunk(
        self, session_id: str, index: int, data: bytes
    ) -> tuple[int, int]:
        """Write one chunk via the pinned session directory.

        Returns ``(uploaded_chunks, total_chunks)``.
        """
        session = self._get(session_id)
        if index < 0 or index >= session.total_chunks:
            raise VolumeError(
                "CHUNK_OUT_OF_RANGE",
                400,
                f"chunk index {index} out of range 0..{session.total_chunks - 1}",
            )
        name = _chunk_name(index)
        with session.lock:
            if index in session.uploaded:
                raise VolumeError(
                    "CHUNK_CONFLICT", 409, f"chunk {index} already uploaded"
                )
            # openat() relative to the pinned session directory: even after a
            # disk swap this write cannot land in the replacement directory.
            try:
                fd = os.open(
                    name,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC,
                    0o644,
                    dir_fd=session.session_fd,
                )
                try:
                    os.write(fd, data)
                finally:
                    os.close(fd)
            except OSError as exc:
                # EIO / ENXIO happen when the pinned volume is gone (e.g.
                # after a lazy unmount). Surface as 503; nothing was written
                # anywhere else.
                raise VolumeError(
                    "VOLUME_IO_ERROR",
                    503,
                    f"write to original volume failed: {exc}",
                ) from exc
            session.uploaded.add(index)
            return len(session.uploaded), session.total_chunks

    # ------------------------------------------------------------- complete

    def complete_session(self, session_id: str) -> dict:
        session = self._get(session_id)
        with session.lock:
            if len(session.uploaded) != session.total_chunks:
                missing = sorted(
                    set(range(session.total_chunks)) - session.uploaded
                )
                raise VolumeError(
                    "CHUNKS_INCOMPLETE",
                    409,
                    f"missing {len(missing)} chunk(s), first: "
                    f"{missing[:5]}",
                )

            # Path-based re-verification *and* device identity check. This is
            # the authoritative gate: a swapped entry is rejected, and the
            # archive is never published on the original volume either.
            probe = None
            try:
                try:
                    probe = self.guard.probe()
                except VolumeError as exc:
                    # At finalization time any failure to re-verify the same
                    # entry means the original volume is no longer there.
                    raise VolumeError(
                        "VOLUME_SWITCHED",
                        409,
                        "backup entry no longer resolves to the original "
                        f"volume ({exc.code}); refusing to publish this "
                        "archive",
                    ) from exc
                path_matches = probe.volume_id == session.volume_id
                try:
                    pinned_dev = VolumeGuard.pinned_dev(session.root_fd)
                except OSError as exc:
                    raise VolumeError(
                        "VOLUME_SWITCHED",
                        409,
                        f"original volume is no longer reachable: {exc}",
                    ) from exc
                if not path_matches or probe.dev != session.original_dev:
                    raise VolumeError(
                        "VOLUME_SWITCHED",
                        409,
                        "backup entry no longer resolves to the original "
                        "volume; refusing to publish this archive",
                    )
                if pinned_dev != session.original_dev:
                    # Defensive: a pinned descriptor must never change device.
                    raise VolumeError(
                        "VOLUME_SWITCHED",
                        409,
                        "pinned mount changed identity; refusing to publish",
                    )
                archive_id = uuid.uuid4().hex
                tmp_name = f"{archive_id}{_TMP_SUFFIX}"
                final_name = f"{archive_id}.tar"
                hasher = hashlib.sha256()
                total = 0

                # Assemble chunks into a temp file through the pinned fd.
                try:
                    out_fd = os.open(
                        tmp_name,
                        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC,
                        0o644,
                        dir_fd=session.staging_fd,
                    )
                    try:
                        for i in range(session.total_chunks):
                            chunk_fd = os.open(
                                _chunk_name(i),
                                os.O_RDONLY | os.O_CLOEXEC,
                                dir_fd=session.session_fd,
                            )
                            try:
                                while True:
                                    block = os.read(chunk_fd, 1024 * 1024)
                                    if not block:
                                        break
                                    hasher.update(block)
                                    total += len(block)
                                    os.write(out_fd, block)
                            finally:
                                os.close(chunk_fd)
                    finally:
                        os.close(out_fd)
                except OSError as exc:
                    raise VolumeError(
                        "VOLUME_IO_ERROR",
                        503,
                        f"failed to assemble archive on original volume: {exc}",
                    ) from exc

                if hasher.hexdigest() != session.sha256:
                    self._best_effort_unlink(session.staging_fd, tmp_name)
                    raise VolumeError(
                        "CHECKSUM_MISMATCH",
                        422,
                        "assembled archive does not match the declared "
                        "sha256 digest",
                    )
                if total != session.size:
                    self._best_effort_unlink(session.staging_fd, tmp_name)
                    raise VolumeError(
                        "SIZE_MISMATCH",
                        422,
                        f"assembled size {total} != declared {session.size}",
                    )

                try:
                    archives_fd = _ensure_dir("archives", session.root_fd)
                except VolumeError:
                    self._best_effort_unlink(session.staging_fd, tmp_name)
                    raise
                try:
                    # Sidecar first (tmp -> atomic rename), then the archive.
                    # A listing only counts entries whose sidecar is in
                    # place, so interrupted publishes never show up.
                    meta = {
                        "id": archive_id,
                        "filename": session.filename,
                        "sha256": session.sha256,
                        "size": total,
                        "volume_id": session.volume_id,
                        "created_at": time.time(),
                        "uploaded_at": session.created_at,
                    }
                    meta_tmp = f"{archive_id}{_META_SUFFIX}{_TMP_SUFFIX}"
                    meta_name = f"{archive_id}{_META_SUFFIX}"
                    try:
                        meta_fd = os.open(
                            meta_tmp,
                            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC,
                            0o644,
                            dir_fd=archives_fd,
                        )
                        try:
                            os.write(
                                meta_fd,
                                json.dumps(meta, ensure_ascii=False).encode("utf-8"),
                            )
                        finally:
                            os.close(meta_fd)
                        os.rename(
                            meta_tmp, meta_name, src_dir_fd=archives_fd,
                            dst_dir_fd=archives_fd,
                        )
                        os.rename(
                            tmp_name,
                            final_name,
                            src_dir_fd=session.staging_fd,
                            dst_dir_fd=archives_fd,
                        )
                    except OSError as exc:
                        raise VolumeError(
                            "VOLUME_IO_ERROR",
                            503,
                            f"failed to publish archive: {exc}",
                        ) from exc
                finally:
                    os.close(archives_fd)
            except VolumeError as exc:
                if probe is not None:
                    os.close(probe.fd)
                # Volume-level failures make the session unusable: tear the
                # staging area down through the *pinned* descriptors only,
                # so a replacement directory is never touched. Checksum /
                # size errors keep the session for client-side abort/retry.
                if exc.code in _FATAL_SESSION_ERRORS:
                    self._cleanup_session(session)
                    session.aborted = True
                    with self._lock:
                        self._sessions.pop(session_id, None)
                    session.close_fds()
                raise
            else:
                os.close(probe.fd)

            # Published. Remove the staging session via pinned fds only.
            self._cleanup_session(session)
            session.finished = True
            with self._lock:
                self._sessions.pop(session_id, None)
            session.close_fds()
            return {
                "id": archive_id,
                "filename": session.filename,
                "size": total,
                "sha256": session.sha256,
                "volume_id": session.volume_id,
            }

    # --------------------------------------------------------------- abort

    def abort_session(self, session_id: str) -> None:
        with self._lock:
            session = self._sessions.get(session_id)
        if session is None:
            raise VolumeError("SESSION_NOT_FOUND", 404, "unknown upload session")
        if session.finished:
            raise VolumeError(
                "SESSION_CLOSED", 409, "upload session is already closed"
            )
        with session.lock:
            if not session.aborted:
                self._cleanup_session(session)
                session.aborted = True
            with self._lock:
                self._sessions.pop(session_id, None)
            session.close_fds()

    def _cleanup_session(self, session: Session) -> None:
        """Delete chunks and the session directory using pinned fds.

        All paths are resolved through the descriptors captured at session
        creation, so a swapped-in directory is never touched. Failures (e.g.
        EIO after an unmount) are swallowed: remnants stay on the original
        disk and can be reaped later; we never escalate to path-based I/O.
        """
        try:
            with os.scandir(session.session_fd) as iterator:
                names = [entry.name for entry in iterator]
            for name in names:
                try:
                    os.unlink(name, dir_fd=session.session_fd)
                except OSError:
                    pass
            os.rmdir(session.id, dir_fd=session.staging_fd)
        except OSError:
            pass

    @staticmethod
    def _best_effort_unlink(dir_fd: int, name: str) -> None:
        try:
            os.unlink(name, dir_fd=dir_fd)
        except OSError:
            pass

    # ------------------------------------------------------------ archives

    def list_archives(self) -> list[dict]:
        probe = self.guard.probe()
        try:
            try:
                archives_fd = _open_dir("archives", probe.fd)
            except FileNotFoundError:
                return []
            try:
                entries: list[dict] = []
                with os.scandir(archives_fd) as iterator:
                    names = sorted(entry.name for entry in iterator)
                for name in names:
                    if not name.endswith(_META_SUFFIX):
                        # .tmp leftovers and data files without a committed
                        # sidecar are intentionally invisible.
                        continue
                    try:
                        meta_fd = os.open(
                            name, os.O_RDONLY | os.O_CLOEXEC, dir_fd=archives_fd
                        )
                    except OSError:
                        continue
                    try:
                        meta = json.loads(os.read(meta_fd, 8 * 1024 * 1024))
                    finally:
                        os.close(meta_fd)
                    archive_file = name[: -len(_META_SUFFIX)] + ".tar"
                    try:
                        st = os.stat(archive_file, dir_fd=archives_fd)
                    except OSError:
                        # Sidecar exists but data file missing: not a
                        # completely published archive.
                        continue
                    meta["size"] = st.st_size
                    entries.append(meta)
                return entries
            finally:
                os.close(archives_fd)
        finally:
            os.close(probe.fd)

    def open_archive(self, archive_id: str):
        """Return (file-object, metadata) for a published archive.

        The caller is responsible for closing the file object.
        """
        try:
            uuid.UUID(archive_id)
        except (ValueError, AttributeError):
            raise VolumeError("ARCHIVE_NOT_FOUND", 404, "unknown archive id")
        probe = self.guard.probe()
        try:
            try:
                archives_fd = _open_dir("archives", probe.fd)
            except FileNotFoundError:
                raise VolumeError(
                    "ARCHIVE_NOT_FOUND", 404, "unknown archive id"
                ) from None
            try:
                meta_name = f"{archive_id}{_META_SUFFIX}"
                try:
                    meta_fd = os.open(
                        meta_name, os.O_RDONLY | os.O_CLOEXEC, dir_fd=archives_fd
                    )
                except FileNotFoundError:
                    raise VolumeError(
                        "ARCHIVE_NOT_FOUND", 404, "unknown archive id"
                    ) from None
                try:
                    meta = json.loads(os.read(meta_fd, 8 * 1024 * 1024))
                finally:
                    os.close(meta_fd)
                data_name = f"{archive_id}.tar"
                try:
                    data_fd = os.open(
                        data_name, os.O_RDONLY | os.O_CLOEXEC, dir_fd=archives_fd
                    )
                except FileNotFoundError:
                    raise VolumeError(
                        "ARCHIVE_NOT_FOUND", 404, "unknown archive id"
                    ) from None
            finally:
                os.close(archives_fd)
        finally:
            os.close(probe.fd)
        return os.fdopen(data_fd, "rb"), meta
