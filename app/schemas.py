"""Pydantic request/response models."""

from __future__ import annotations

import re

from pydantic import BaseModel, Field, field_validator

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
# Conservative filename rule: no path separators, no NUL, no dot-only names.
_FILENAME_RE = re.compile(r"^[^/\x00]+$")


class CreateUploadRequest(BaseModel):
    filename: str = Field(..., min_length=1, max_length=255)
    total_chunks: int = Field(..., ge=1, le=100_000)
    sha256: str
    size: int = Field(..., ge=0, le=1024 * 1024 * 1024 * 1024)  # 1 TiB cap

    @field_validator("filename")
    @classmethod
    def _check_filename(cls, value: str) -> str:
        if not _FILENAME_RE.match(value) or value in (".", ".."):
            raise ValueError("filename must not contain path separators")
        return value

    @field_validator("sha256")
    @classmethod
    def _check_sha256(cls, value: str) -> str:
        value = value.lower()
        if not _SHA256_RE.match(value):
            raise ValueError("sha256 must be 64 lowercase hex characters")
        return value


class CreateUploadResponse(BaseModel):
    session_id: str
    filename: str
    total_chunks: int
    volume_id: str


class ChunkAck(BaseModel):
    session_id: str
    index: int
    received: int
    uploaded_chunks: int
    total_chunks: int


class CompleteResponse(BaseModel):
    id: str
    filename: str
    size: int
    sha256: str
    volume_id: str


class ArchiveEntry(BaseModel):
    id: str
    filename: str
    sha256: str
    size: int
    volume_id: str
    created_at: float
    uploaded_at: float


class ManifestResponse(BaseModel):
    volume_id: str
    archives: list[ArchiveEntry]


class HealthResponse(BaseModel):
    status: str  # "ok" | "degraded"
    root_path: str
    expected_volume_id: str
    actual_volume_id: str | None = None
    error: str | None = None
