"""End-to-end behaviour of the single-volume backup API."""

from __future__ import annotations

import hashlib
import os

from tests.conftest import (
    VOLUME_ID,
    create_upload,
    upload_all_chunks,
)


def _swap_disk(layout, target: str) -> None:
    """Point the entry symlink at another disk (simulated pull/swap)."""
    entry = layout["root"]
    os.unlink(entry)
    os.symlink(target, entry)


def test_happy_path_publishes_and_lists(client, payload):
    session_id = create_upload(client, payload, total_chunks=3)
    upload_all_chunks(client, session_id, payload["data"], 3)

    response = client.post(f"/backups/{session_id}/complete")
    assert response.status_code == 200, response.text
    published = response.json()
    assert published["sha256"] == payload["sha256"]
    assert published["volume_id"] == VOLUME_ID
    assert published["size"] == payload["size"]

    manifest = client.get("/backups").json()
    assert manifest["volume_id"] == VOLUME_ID
    assert len(manifest["archives"]) == 1
    entry = manifest["archives"][0]
    assert entry["id"] == published["id"]
    assert entry["filename"] == "demo.tar"

    download = client.get(f"/backups/archive/{published['id']}/download")
    assert download.status_code == 200
    assert download.content == payload["data"]
    assert hashlib.sha256(download.content).hexdigest() == payload["sha256"]


def test_create_rejects_wrong_volume_id(client, layout, payload):
    _swap_disk(layout, layout["disk_b"])
    response = client.post(
        "/backups",
        json={
            "filename": "demo.tar",
            "total_chunks": 1,
            "sha256": payload["sha256"],
            "size": payload["size"],
        },
    )
    assert response.status_code == 409
    assert response.json()["error"] == "VOLUME_ID_MISMATCH"
    # Nothing was created on the replacement disk.
    assert os.listdir(layout["disk_b"]) == [".volume_id"]


def test_create_rejects_unavailable_entry(client, layout, payload):
    os.unlink(layout["root"])  # entry gone -> system-disk fallback risk
    response = client.post(
        "/backups",
        json={
            "filename": "demo.tar",
            "total_chunks": 1,
            "sha256": payload["sha256"],
            "size": payload["size"],
        },
    )
    assert response.status_code == 503
    assert response.json()["error"] == "VOLUME_UNAVAILABLE"


def test_swap_during_upload_blocks_publish_and_never_touches_new_disk(
    client, layout, payload
):
    session_id = create_upload(client, payload, total_chunks=4)
    bounds = [i * payload["size"] // 4 for i in range(5)]
    for index in range(3):
        chunk = payload["data"][bounds[index] : bounds[index + 1]]
        response = client.put(
            f"/backups/{session_id}/chunks/{index}", content=chunk
        )
        assert response.status_code == 200

    # "Pull" disk A and insert disk B while the upload is in flight.
    _swap_disk(layout, layout["disk_b"])

    # Chunk writes keep using the pinned descriptor of the original volume;
    # the replacement directory must stay untouched.
    response = client.put(
        f"/backups/{session_id}/chunks/3",
        content=payload["data"][bounds[3] : bounds[4]],
    )
    assert response.status_code == 200
    assert os.listdir(layout["disk_b"]) == [".volume_id"]

    # Finalization re-probes the path: it now resolves to B, so the upload
    # is rejected and nothing is published on A either.
    response = client.post(f"/backups/{session_id}/complete")
    assert response.status_code == 409
    assert response.json()["error"] == "VOLUME_SWITCHED"

    # No archives visible through the B entry...
    assert client.get("/backups").status_code == 409
    # ...and nothing leaked into B's directory tree.
    for root, _dirs, files in os.walk(layout["disk_b"]):
        assert files == [".volume_id"] or files == []

    # Reattach disk A: old state is still there, but the failed upload was
    # never published.
    _swap_disk(layout, layout["disk_a"])
    manifest = client.get("/backups").json()
    assert manifest["archives"] == []
    # Staging of the dead session was reaped via pinned descriptors.
    staging = os.path.join(layout["disk_a"], ".staging")
    assert os.path.isdir(staging)
    assert os.listdir(staging) == []


def test_reattach_restores_access_to_previously_published_archive(
    client, layout, payload
):
    session_id = create_upload(client, payload, total_chunks=2)
    upload_all_chunks(client, session_id, payload["data"], 2)
    published = client.post(f"/backups/{session_id}/complete").json()

    _swap_disk(layout, layout["disk_b"])
    # While B is mounted, the API refuses to serve a manifest.
    response = client.get("/backups")
    assert response.status_code == 409
    assert response.json()["error"] == "VOLUME_ID_MISMATCH"

    # Put A back: the old archive is queryable again.
    _swap_disk(layout, layout["disk_a"])
    manifest = client.get("/backups").json()
    assert [item["id"] for item in manifest["archives"]] == [published["id"]]
    download = client.get(f"/backups/archive/{published['id']}/download")
    assert download.status_code == 200
    assert download.content == payload["data"]


def test_checksum_mismatch_is_rejected_and_not_listed(client, layout, payload):
    bad = hashlib.sha256(b"different").hexdigest()
    bad_session = client.post(
        "/backups",
        json={
            "filename": "bad.tar",
            "total_chunks": 2,
            "sha256": bad,
            "size": payload["size"],
        },
    ).json()["session_id"]
    upload_all_chunks(client, bad_session, payload["data"], 2)
    response = client.post(f"/backups/{bad_session}/complete")
    assert response.status_code == 422
    assert response.json()["error"] == "CHECKSUM_MISMATCH"

    # Client can abort the failed session; manifest stays empty and the
    # staging area is reaped through the pinned descriptors.
    assert client.delete(f"/backups/{bad_session}").status_code == 204
    assert client.get("/backups").json()["archives"] == []
    assert os.listdir(os.path.join(layout["disk_a"], ".staging")) == []


def test_duplicate_and_out_of_range_chunks_are_rejected(client, payload):
    session_id = create_upload(client, payload, total_chunks=2)
    response = client.put(f"/backups/{session_id}/chunks/0", content=b"abc")
    assert response.status_code == 200
    response = client.put(f"/backups/{session_id}/chunks/0", content=b"abc")
    assert response.status_code == 409
    assert response.json()["error"] == "CHUNK_CONFLICT"
    response = client.put(f"/backups/{session_id}/chunks/2", content=b"x")
    assert response.status_code == 400
    assert response.json()["error"] == "CHUNK_OUT_OF_RANGE"


def test_incomplete_session_cannot_complete(client, payload):
    session_id = create_upload(client, payload, total_chunks=3)
    client.put(f"/backups/{session_id}/chunks/0", content=b"x")
    response = client.post(f"/backups/{session_id}/complete")
    assert response.status_code == 409
    assert response.json()["error"] == "CHUNKS_INCOMPLETE"


def test_manifest_ignores_tmp_and_sidecarless_files(client, layout, payload):
    session_id = create_upload(client, payload, total_chunks=1)
    upload_all_chunks(client, session_id, payload["data"], 1)
    published = client.post(f"/backups/{session_id}/complete").json()

    archives_dir = os.path.join(layout["disk_a"], "archives")
    # Simulate crash leftovers.
    with open(os.path.join(archives_dir, "deadbeef.tmp"), "wb") as fh:
        fh.write(b"partial")
    with open(os.path.join(archives_dir, "orphan.tar"), "wb") as fh:
        fh.write(b"no sidecar")
    with open(os.path.join(archives_dir, "noside.meta.json"), "wb") as fh:
        fh.write(b"{}")

    ids = [item["id"] for item in client.get("/backups").json()["archives"]]
    assert ids == [published["id"]]


def test_health_reports_swap(client, layout):
    ok = client.get("/health").json()
    assert ok["status"] == "ok"
    assert ok["actual_volume_id"] == VOLUME_ID

    _swap_disk(layout, layout["disk_b"])
    degraded = client.get("/health").json()
    assert degraded["status"] == "degraded"
    assert "VOLUME_ID_MISMATCH" in degraded["error"]


def test_device_identity_mismatch_also_blocks_publish(client, layout, payload):
    """st_dev mismatch must be rejected even if an id file were copied."""
    session_id = create_upload(client, payload, total_chunks=1)
    upload_all_chunks(client, session_id, payload["data"], 1)

    mount_dir = os.path.join(layout["tmp"], "tmpfs_mount")
    os.mkdir(mount_dir)
    import subprocess

    # tmpfs needs CAP_SYS_ADMIN; skip where the sandbox forbids it.
    proc = subprocess.run(
        ["mount", "-t", "tmpfs", "tmpfs", mount_dir],
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        import pytest

        pytest.skip(f"tmpfs mount not permitted: {proc.stderr.strip()}")
    try:
        with open(
            os.path.join(mount_dir, ".volume_id"), "w", encoding="utf-8"
        ) as fh:
            fh.write(VOLUME_ID)  # same id, different device
        _swap_disk(layout, mount_dir)

        response = client.post(f"/backups/{session_id}/complete")
        assert response.status_code == 409
        assert response.json()["error"] == "VOLUME_SWITCHED"
    finally:
        _swap_disk(layout, layout["disk_a"])
        subprocess.run(["umount", mount_dir], check=True)
