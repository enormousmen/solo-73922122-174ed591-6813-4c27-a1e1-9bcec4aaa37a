"""FastAPI application wiring."""

from __future__ import annotations

import os
from urllib.parse import quote

from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse, StreamingResponse

from .schemas import (
    ArchiveEntry,
    ChunkAck,
    CompleteResponse,
    CreateUploadRequest,
    CreateUploadResponse,
    HealthResponse,
    ManifestResponse,
)
from .store import SessionStore
from .volume import VolumeError, VolumeGuard


def create_app() -> FastAPI:
    root_path = os.environ.get("BACKUP_ROOT", "/backups/data")
    expected_id = os.environ.get("BACKUP_VOLUME_ID", "")
    max_chunk = int(os.environ.get("BACKUP_MAX_CHUNK_BYTES", str(64 * 1024 * 1024)))

    guard = VolumeGuard(root_path=root_path, expected_volume_id=expected_id)
    store = SessionStore(guard)

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

    return app


# Module-level app for ``uvicorn app.main:app``.
app = create_app()
