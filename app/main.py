"""FastAPI application wiring."""

from __future__ import annotations

import os
from urllib.parse import quote

from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse, StreamingResponse

from .schemas import (
    ArchiveEntry,
    ArchiveVolumeHit,
    ArchiveVolumesResponse,
    BookletListResponse,
    BookletResponse,
    BookletSummary,
    ChunkAck,
    CompleteResponse,
    CreateUploadRequest,
    CreateUploadResponse,
    HealthResponse,
    InventoryArchiveEntry,
    InventoryCommitResponse,
    ManifestResponse,
)
from .inventory import InventoryStore
from .store import SessionStore
from .volume import VolumeError, VolumeGuard


def create_app() -> FastAPI:
    root_path = os.environ.get("BACKUP_ROOT", "/backups/data")
    expected_id = os.environ.get("BACKUP_VOLUME_ID", "")
    max_chunk = int(os.environ.get("BACKUP_MAX_CHUNK_BYTES", str(64 * 1024 * 1024)))
    # Independent persistent directory: must NOT live on the backup disk,
    # so booklets survive the disk being pulled.
    inventory_root = os.environ.get("INVENTORY_ROOT", "/inventory")

    guard = VolumeGuard(root_path=root_path, expected_volume_id=expected_id)
    store = SessionStore(guard)
    inventory = InventoryStore(guard, root_path=inventory_root)

    app = FastAPI(
        title="Single-volume backup API",
        version="1.0.0",
        description=(
            "Chunked archive uploads that pin the backup mount and refuse "
            "to publish whenever the entry no longer resolves to the "
            "provisioned volume."
        ),
    )

    @app.exception_handler(VolumeError)
    async def _volume_error_handler(_: Request, exc: VolumeError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content={"error": exc.code, "detail": str(exc)},
        )

    @app.get("/health", response_model=HealthResponse)
    async def health() -> HealthResponse:
        # Health is always HTTP 200 so simple monitors can read the body;
        # a non-ok "status" field signals volume problems.
        try:
            probe = guard.probe()
        except VolumeError as exc:
            return HealthResponse(
                status="degraded",
                root_path=root_path,
                expected_volume_id=expected_id,
                error=f"{exc.code}: {exc}",
            )
        try:
            return HealthResponse(
                status="ok",
                root_path=root_path,
                expected_volume_id=expected_id,
                actual_volume_id=probe.volume_id,
            )
        finally:
            os.close(probe.fd)

    @app.post("/backups", response_model=CreateUploadResponse, status_code=201)
    async def create_upload(payload: CreateUploadRequest) -> CreateUploadResponse:
        # Entry availability + provisioned volume id are checked here before
        # any upload is accepted; rejection propagates via VolumeError.
        session = store.create_session(
            filename=payload.filename,
            total_chunks=payload.total_chunks,
            sha256=payload.sha256,
            size=payload.size,
        )
        return CreateUploadResponse(
            session_id=session.id,
            filename=session.filename,
            total_chunks=session.total_chunks,
            volume_id=session.volume_id,
        )

    @app.put(
        "/backups/{session_id}/chunks/{index}",
        response_model=ChunkAck,
    )
    async def put_chunk(
        session_id: str, index: int, request: Request
    ) -> ChunkAck:
        data = await request.body()
        if len(data) > max_chunk:
            return JSONResponse(
                status_code=413,
                content={
                    "error": "CHUNK_TOO_LARGE",
                    "detail": (
                        f"chunk is {len(data)} bytes, limit is {max_chunk}"
                    ),
                },
            )
        uploaded_chunks, total_chunks = store.write_chunk(session_id, index, data)
        return ChunkAck(
            session_id=session_id,
            index=index,
            received=len(data),
            uploaded_chunks=uploaded_chunks,
            total_chunks=total_chunks,
        )

    @app.post(
        "/backups/{session_id}/complete",
        response_model=CompleteResponse,
    )
    async def complete(session_id: str) -> CompleteResponse:
        result = store.complete_session(session_id)
        return CompleteResponse(**result)

    @app.delete("/backups/{session_id}", status_code=204)
    async def abort(session_id: str) -> Response:
        store.abort_session(session_id)
        return Response(status_code=204)

    @app.get("/backups", response_model=ManifestResponse)
    async def list_archives() -> ManifestResponse:
        entries = store.list_archives()
        return ManifestResponse(
            volume_id=expected_id,
            archives=[ArchiveEntry(**entry) for entry in entries],
        )

    @app.get("/backups/archive/{archive_id}/download")
    async def download(archive_id: str):
        file_obj, meta = store.open_archive(archive_id)
        filename = quote(meta.get("filename", f"{archive_id}.tar"))

        def _iter():
            try:
                while True:
                    block = file_obj.read(1024 * 1024)
                    if not block:
                        break
                    yield block
            finally:
                file_obj.close()

        return StreamingResponse(
            _iter(),
            media_type="application/x-tar",
            headers={
                "Content-Length": str(os.fstat(file_obj.fileno()).st_size),
                "Content-Disposition": (
                    f"attachment; filename*=UTF-8''{filename}"
                ),
            },
        )

    # ------------------------------------------------------------ inventory

    @app.post(
        "/inventory/volumes/{volume_id}",
        response_model=InventoryCommitResponse,
        status_code=201,
    )
    def run_inventory(volume_id: str) -> InventoryCommitResponse:
        # The path parameter is the disk number ("盘号") the operator
        # supplies; InventoryStore verifies it against the volume id the
        # mount entry currently presents. Sync endpoint: the blocking disk
        # read runs in the Starlette threadpool so concurrent requests
        # (e.g. a second inventory getting a busy response) are not stalled.
        result = inventory.run_inventory(volume_id)
        return InventoryCommitResponse(**result)

    @app.get(
        "/inventory/volumes/{volume_id}/booklet",
        response_model=BookletResponse,
    )
    def get_booklet(volume_id: str) -> BookletResponse:
        booklet = inventory.get_booklet(volume_id)
        if booklet is None:
            return JSONResponse(
                status_code=404,
                content={
                    "error": "BOOKLET_NOT_FOUND",
                    "detail": (
                        f"no successful inventory has been committed for "
                        f"volume {volume_id!r} yet"
                    ),
                },
            )
        return BookletResponse(
            version=booklet["version"],
            inventory_id=booklet["inventory_id"],
            volume_id=booklet["volume_id"],
            started_at=booklet["started_at"],
            committed_at=booklet["committed_at"],
            archive_count=booklet["archive_count"],
            archives=[
                InventoryArchiveEntry(**item) for item in booklet["archives"]
            ],
        )

    @app.get("/inventory/volumes", response_model=BookletListResponse)
    def list_booklets() -> BookletListResponse:
        summaries = inventory.list_booklets()
        return BookletListResponse(
            booklets=[BookletSummary(**item) for item in summaries]
        )

    @app.get(
        "/inventory/archives/{archive_id}/volumes",
        response_model=ArchiveVolumesResponse,
    )
    def lookup_archive_volumes(
        archive_id: str,
    ) -> ArchiveVolumesResponse:
        # Read-only lookup against the persistent inventory directory:
        # answers while an inventory is uncommitted, after the disk is
        # pulled and after a service restart.
        hits = inventory.lookup_archive(archive_id)
        return ArchiveVolumesResponse(
            archive_id=archive_id,
            volumes=[ArchiveVolumeHit(**item) for item in hits],
        )

    # Exposed for tests / diagnostics; not part of the HTTP contract.
    app.state.inventory_store = inventory
    app.state.session_store = store
    app.state.volume_guard = guard

    return app


# Module-level app for ``uvicorn app.main:app``.
app = create_app()
