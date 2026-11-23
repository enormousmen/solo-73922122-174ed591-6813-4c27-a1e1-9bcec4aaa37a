"""Shared pytest fixtures.

The local (non-privileged) test harness simulates disk swaps by replacing a
symlink that serves as the backup entry. Volume-id mismatch therefore
exercises the real safety path; device-id mismatch is covered separately
where a tmpfs is available.
"""

from __future__ import annotations

import hashlib
import os

import pytest
from fastapi.testclient import TestClient

from app.main import create_app

VOLUME_ID = "VOL-DISK-A-0001"
OTHER_VOLUME_ID = "VOL-DISK-B-0002"
ID_FILE = ".volume_id"


def _write_volume_id(directory: str, volume_id: str) -> None:
    with open(os.path.join(directory, ID_FILE), "w", encoding="utf-8") as fh:
        fh.write(volume_id)


@pytest.fixture()
def layout(tmp_path):
    """Create disks A/B and a symlink entry pointing at A."""
    disk_a = tmp_path / "diskA"
    disk_b = tmp_path / "diskB"
    disk_a.mkdir()
    disk_b.mkdir()
    _write_volume_id(str(disk_a), VOLUME_ID)
    _write_volume_id(str(disk_b), OTHER_VOLUME_ID)

    entry = tmp_path / "entry"
    entry.symlink_to(disk_a)
    return {
        "root": str(entry),
        "disk_a": str(disk_a),
        "disk_b": str(disk_b),
        "tmp": str(tmp_path),
    }


@pytest.fixture()
def client(layout, monkeypatch):
    monkeypatch.setenv("BACKUP_ROOT", layout["root"])
    monkeypatch.setenv("BACKUP_VOLUME_ID", VOLUME_ID)
    # Inventory booklets live in their own persistent directory, never on
    # the simulated backup disks.
    inventory_root = os.path.join(layout["tmp"], "inventory")
    os.makedirs(inventory_root, exist_ok=True)
    monkeypatch.setenv("INVENTORY_ROOT", inventory_root)
    layout["inventory"] = inventory_root
    app = create_app()
    with TestClient(app) as test_client:
        test_client.app_state = app.state
        yield test_client


@pytest.fixture()
def payload():
    data = os.urandom(50_000)
    digest = hashlib.sha256(data).hexdigest()
    return {"data": data, "sha256": digest, "size": len(data)}


def create_upload(client_, payload_, total_chunks: int = 3, filename="demo.tar"):
    response = client_.post(
        "/backups",
        json={
            "filename": filename,
            "total_chunks": total_chunks,
            "sha256": payload_["sha256"],
            "size": payload_["size"],
        },
    )
    assert response.status_code == 201, response.text
    return response.json()["session_id"]


def upload_all_chunks(client_, session_id, data, total_chunks: int):
    bounds = [i * len(data) // total_chunks for i in range(total_chunks + 1)]
    chunks = [data[bounds[i] : bounds[i + 1]] for i in range(total_chunks)]
    for index, chunk in enumerate(chunks):
        response = client_.put(
            f"/backups/{session_id}/chunks/{index}", content=chunk
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["received"] == len(chunk)
    return chunks
