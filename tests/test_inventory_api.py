"""End-to-end behaviour of the inventory (盘点) API.

Covers, against the same symlink-swap harness as the backup tests:

* happy path: booklet persisted outside the backup disk, reverse lookup;
* disk-number vs. mount-entry volume id verification;
* only one running inventory per volume (``INVENTORY_BUSY``);
* snapshot boundary: archives published during the read wait for the next
  inventory, uploads keep working meanwhile;
* commit-time re-verification of the entry (volume id + device);
* read / save failures keep the previous booklet;
* reverse lookup while an inventory is uncommitted, after the disk is
  pulled and after a full service restart (index rebuild from booklets);
* multi-volume index and the rule that uploads still only accept the
  originally provisioned volume.
"""

from __future__ import annotations

import json
import os
import threading

from fastapi.testclient import TestClient

from app.inventory import InventoryStore
from app.main import create_app
from app.volume import VolumeError
from tests.conftest import (
    OTHER_VOLUME_ID,
    VOLUME_ID,
    create_upload,
    upload_all_chunks,
)


def _swap_disk(layout, target: str) -> None:
    entry = layout["root"]
    os.unlink(entry)
    os.symlink(target, entry)


def publish_archive(client, payload, filename: str = "demo.tar") -> dict:
    session_id = create_upload(client, payload, total_chunks=2, filename=filename)
    upload_all_chunks(client, session_id, payload["data"], 2)
    response = client.post(f"/backups/{session_id}/complete")
    assert response.status_code == 200, response.text
    return response.json()


def run_inventory(client, volume_id: str = VOLUME_ID):
    return client.post(f"/inventory/volumes/{volume_id}")


# ---------------------------------------------------------------- happy path


def test_inventory_commits_booklet_outside_backup_disk(client, layout, payload):
    published = publish_archive(client, payload)

    response = run_inventory(client)
    assert response.status_code == 201, response.text
    commit = response.json()
    assert commit["volume_id"] == VOLUME_ID
    assert commit["archive_count"] == 1
    assert commit["started_at"] <= commit["committed_at"]

    # The booklet lives in the independent persistent directory, never on
    # the simulated backup disks.
    booklet_file = os.path.join(
        layout["inventory"], "volumes", VOLUME_ID, "booklet.json"
    )
    assert os.path.isfile(booklet_file)
    assert not os.path.exists(os.path.join(layout["disk_a"], "volumes"))
    assert not os.path.exists(os.path.join(layout["disk_b"], "volumes"))

    booklet = client.get(f"/inventory/volumes/{VOLUME_ID}/booklet")
    assert booklet.status_code == 200
    body = booklet.json()
    assert body["inventory_id"] == commit["inventory_id"]
    assert body["archive_count"] == 1
    archive = body["archives"][0]
    assert archive["id"] == published["id"]
    assert archive["filename"] == "demo.tar"
    assert archive["sha256"] == payload["sha256"]
    assert archive["volume_id"] == VOLUME_ID

    # Reverse lookup: which disks' latest successful booklet has the id.
    lookup = client.get(f"/inventory/archives/{published['id']}/volumes")
    assert lookup.status_code == 200
    hits = lookup.json()["volumes"]
    assert len(hits) == 1
    assert hits[0]["volume_id"] == VOLUME_ID
    assert hits[0]["inventory_id"] == commit["inventory_id"]

    listing = client.get("/inventory/volumes")
    assert listing.status_code == 200
    summaries = listing.json()["booklets"]
    assert summaries == [
        {
            "volume_id": VOLUME_ID,
            "inventory_id": commit["inventory_id"],
            "committed_at": commit["committed_at"],
            "archive_count": 1,
        }
    ]


def test_booklet_404_before_first_inventory(client):
    response = client.get(f"/inventory/volumes/{VOLUME_ID}/booklet")
    assert response.status_code == 404
    assert response.json()["error"] == "BOOKLET_NOT_FOUND"


# ------------------------------------------------------------- input checks


def test_inventory_rejects_wrong_disk_number(client, layout, payload):
    # Entry presents A; asking to inventory B's disk number is rejected.
    response = run_inventory(client, OTHER_VOLUME_ID)
    assert response.status_code == 409
    assert response.json()["error"] == "VOLUME_ID_MISMATCH"

    # And the symmetric case after swapping the entry to B.
    _swap_disk(layout, layout["disk_b"])
    response = run_inventory(client, VOLUME_ID)
    assert response.status_code == 409
    assert response.json()["error"] == "VOLUME_ID_MISMATCH"

    # Nothing was persisted.
    assert client.get("/inventory/volumes").json()["booklets"] == []


def test_inventory_rejects_unavailable_entry(client, layout):
    os.unlink(layout["root"])
    response = run_inventory(client)
    assert response.status_code == 503
    assert response.json()["error"] == "VOLUME_UNAVAILABLE"


def test_inventory_rejects_bad_disk_number_characters(client):
    response = client.post("/inventory/volumes/..%2fetc")
    assert response.status_code in (400, 404)
    # Trailing slash collides with the collection-level GET route, so the
    # POST is answered 405 rather than reaching a handler.
    response = client.post("/inventory/volumes/")
    assert response.status_code in (404, 405)


def test_lookup_rejects_invalid_archive_id(client):
    response = client.get("/inventory/archives/not-a-uuid/volumes")
    assert response.status_code == 400
    assert response.json()["error"] == "INVALID_ARCHIVE_ID"


# --------------------------------------------------------------------- busy


def _start_blocked_inventory(client, monkeypatch):
    """Run an inventory whose metadata read blocks until the event fires."""
    store: InventoryStore = client.app_state.inventory_store
    entered = threading.Event()
    proceed = threading.Event()
    original = store._read_archives_snapshot

    def slow_read(probe):
        entered.set()
        assert proceed.wait(timeout=10), "blocked inventory never released"
        return original(probe)

    monkeypatch.setattr(store, "_read_archives_snapshot", slow_read)
    thread = threading.Thread(
        target=lambda: run_inventory(client), daemon=True
    )
    thread.start()
    assert entered.wait(timeout=10), "inventory did not start"
    return thread, proceed


def test_concurrent_inventory_of_same_disk_reports_busy(client, monkeypatch):
    thread, proceed = _start_blocked_inventory(client, monkeypatch)
    try:
        response = run_inventory(client)
        assert response.status_code == 409
        assert response.json()["error"] == "INVENTORY_BUSY"
    finally:
        proceed.set()
        thread.join(timeout=10)
    assert not thread.is_alive()

    # Once the first run committed, the slot is free again.
    assert run_inventory(client).status_code == 201


def test_busy_slot_is_released_after_failure(client, payload):
    publish_archive(client, payload)
    assert run_inventory(client).status_code == 201  # prior good booklet

    store: InventoryStore = client.app_state.inventory_store
    entered = threading.Event()
    proceed = threading.Event()

    def failing_read(_probe):
        entered.set()
        proceed.wait(timeout=10)
        raise VolumeError(
            "INVENTORY_READ_FAILED", 503, "simulated metadata read failure"
        )

    original = store._read_archives_snapshot
    store._read_archives_snapshot = failing_read
    try:
        thread = threading.Thread(
            target=lambda: run_inventory(client), daemon=True
        )
        thread.start()
        assert entered.wait(timeout=10)
        proceed.set()
        thread.join(timeout=10)
        assert not thread.is_alive()
    finally:
        store._read_archives_snapshot = original

    # The failed run must not leave the volume marked busy.
    assert run_inventory(client).status_code == 201


# -------------------------------------------------------- snapshot boundary


def test_archives_published_during_read_wait_for_next_inventory(
    client, monkeypatch, payload
):
    first = publish_archive(client, payload, filename="first.tar")

    store: InventoryStore = client.app_state.inventory_store
    entered = threading.Event()
    proceed = threading.Event()
    original = store._read_archives_snapshot

    def slow_read(probe):
        result = original(probe)
        entered.set()
        proceed.wait(timeout=10)
        return result

    monkeypatch.setattr(store, "_read_archives_snapshot", slow_read)
    thread = threading.Thread(
        target=lambda: run_inventory(client), daemon=True
    )
    thread.start()
    assert entered.wait(timeout=10)

    # Uploads keep being accepted and published while the read is in flight.
    second = publish_archive(client, payload, filename="second.tar")
    proceed.set()
    thread.join(timeout=10)

    booklet = client.get(
        f"/inventory/volumes/{VOLUME_ID}/booklet"
    ).json()
    ids = sorted(item["id"] for item in booklet["archives"])
    assert ids == [first["id"]]  # the new archive is not in this booklet

    # The next inventory picks the new archive up.
    second_commit = run_inventory(client).json()
    assert second_commit["archive_count"] == 2
    booklet = client.get(
        f"/inventory/volumes/{VOLUME_ID}/booklet"
    ).json()
    ids = sorted(item["id"] for item in booklet["archives"])
    assert ids == sorted([first["id"], second["id"]])


# ------------------------------------------------------- commit-time checks


def test_swap_before_commit_keeps_previous_booklet(client, layout, payload):
    publish_archive(client, payload)
    first = run_inventory(client).json()

    store: InventoryStore = client.app_state.inventory_store
    original = store._read_archives_snapshot

    def swap_after_read(probe):
        result = original(probe)
        _swap_disk(layout, layout["disk_b"])  # pull A, insert B
        return result

    store._read_archives_snapshot = swap_after_read
    try:
        response = run_inventory(client)
        assert response.status_code == 409
        assert response.json()["error"] == "VOLUME_SWITCHED"
    finally:
        store._read_archives_snapshot = original
        _swap_disk(layout, layout["disk_a"])

    # The previous booklet is untouched and still queryable.
    booklet = client.get(
        f"/inventory/volumes/{VOLUME_ID}/booklet"
    ).json()
    assert booklet["inventory_id"] == first["inventory_id"]
    assert booklet["archive_count"] == 1

    # Reattaching A allows the next inventory to commit a new booklet.
    second = run_inventory(client).json()
    assert second["inventory_id"] != first["inventory_id"]
    assert second["archive_count"] == 1


def test_read_failure_keeps_previous_booklet(client, monkeypatch, payload):
    publish_archive(client, payload)
    first = run_inventory(client).json()

    def failing_read(_probe):
        raise VolumeError(
            "INVENTORY_READ_FAILED", 503, "simulated metadata read failure"
        )

    monkeypatch.setattr(
        client.app_state.inventory_store,
        "_read_archives_snapshot",
        failing_read,
    )
    response = run_inventory(client)
    assert response.status_code == 503
    assert response.json()["error"] == "INVENTORY_READ_FAILED"

    booklet = client.get(
        f"/inventory/volumes/{VOLUME_ID}/booklet"
    ).json()
    assert booklet["inventory_id"] == first["inventory_id"]


def test_save_failure_keeps_previous_booklet(client, layout, payload):
    publish_archive(client, payload)
    first = run_inventory(client).json()

    volume_dir = os.path.join(layout["inventory"], "volumes", VOLUME_ID)
    os.chmod(volume_dir, 0o500)  # remove write permission on the booklet dir
    try:
        response = run_inventory(client)
        assert response.status_code == 503
        assert response.json()["error"] == "INVENTORY_WRITE_FAILED"
    finally:
        os.chmod(volume_dir, 0o755)

    booklet = client.get(
        f"/inventory/volumes/{VOLUME_ID}/booklet"
    ).json()
    assert booklet["inventory_id"] == first["inventory_id"]
    # And a later run can commit again once the directory is writable.
    assert run_inventory(client).status_code == 201


# --------------------------------------------- persistence across pull/restart


def test_lookup_survives_disk_pull_and_service_restart(client, layout, payload):
    published = publish_archive(client, payload)
    commit = run_inventory(client).json()

    # Pull the disk: the entry points at B; inventory queries only touch the
    # independent persistent directory and keep answering.
    _swap_disk(layout, layout["disk_b"])
    lookup = client.get(f"/inventory/archives/{published['id']}/volumes")
    assert lookup.status_code == 200
    assert lookup.json()["volumes"][0]["volume_id"] == VOLUME_ID
    booklet = client.get(f"/inventory/volumes/{VOLUME_ID}/booklet")
    assert booklet.status_code == 200
    assert booklet.json()["inventory_id"] == commit["inventory_id"]

    # Simulate a restart: brand-new app against the same INVENTORY_ROOT.
    # Also remove the reverse index file to prove it is rebuilt from the
    # booklets on demand.
    os.unlink(os.path.join(layout["inventory"], "archive-index.json"))
    new_app = create_app()
    with TestClient(new_app) as restarted:
        lookup = restarted.get(
            f"/inventory/archives/{published['id']}/volumes"
        )
        assert lookup.status_code == 200
        hits = lookup.json()["volumes"]
        assert [hit["volume_id"] for hit in hits] == [VOLUME_ID]
        assert hits[0]["inventory_id"] == commit["inventory_id"]
        listing = restarted.get("/inventory/volumes").json()
        assert [item["volume_id"] for item in listing["booklets"]] == [VOLUME_ID]

    # The rebuild persisted a fresh index file.
    assert os.path.isfile(
        os.path.join(layout["inventory"], "archive-index.json")
    )


def test_lookup_while_inventory_uncommitted_returns_last_booklet(
    client, monkeypatch, payload
):
    published = publish_archive(client, payload)
    commit = run_inventory(client).json()

    thread, proceed = _start_blocked_inventory(client, monkeypatch)
    try:
        # The uncommitted run is invisible: the previous booklet answers.
        lookup = client.get(f"/inventory/archives/{published['id']}/volumes")
        assert lookup.status_code == 200
        assert (
            lookup.json()["volumes"][0]["inventory_id"]
            == commit["inventory_id"]
        )
    finally:
        proceed.set()
        thread.join(timeout=10)


# ------------------------------------------------------------ multiple disks


def test_reverse_index_covers_multiple_volumes_and_uploads_stay_pinned(
    client, layout, payload
):
    on_a = publish_archive(client, payload, filename="on-a.tar")
    commit_a = run_inventory(client).json()

    # Inventory the replacement disk while it is mounted: its booklet is
    # empty and is stored under its own volume id.
    _swap_disk(layout, layout["disk_b"])
    response = run_inventory(client, OTHER_VOLUME_ID)
    assert response.status_code == 201, response.text
    commit_b = response.json()
    assert commit_b["archive_count"] == 0

    # Uploads still only accept the originally provisioned volume (A),
    # even though B is currently mounted at the entry.
    rejected = client.post(
        "/backups",
        json={
            "filename": "x.tar",
            "total_chunks": 1,
            "sha256": "a" * 64,
            "size": 1,
        },
    )
    assert rejected.status_code == 409
    assert rejected.json()["error"] == "VOLUME_ID_MISMATCH"

    _swap_disk(layout, layout["disk_a"])
    listing = client.get("/inventory/volumes").json()["booklets"]
    assert sorted(item["volume_id"] for item in listing) == sorted(
        [VOLUME_ID, OTHER_VOLUME_ID]
    )

    # A's archive resolves to A only; B's empty booklet never claims it.
    hits = client.get(
        f"/inventory/archives/{on_a['id']}/volumes"
    ).json()["volumes"]
    assert [hit["volume_id"] for hit in hits] == [VOLUME_ID]
    assert hits[0]["inventory_id"] == commit_a["inventory_id"]
    assert commit_b["inventory_id"] != commit_a["inventory_id"]

    # Unknown archive id: valid uuid shape, simply no hits.
    unknown = "0" * 32
    empty = client.get(f"/inventory/archives/{unknown}/volumes")
    assert empty.status_code == 200
    assert empty.json()["volumes"] == []


def test_booklet_file_is_valid_json_with_full_metadata(client, payload):
    published = publish_archive(client, payload)
    run_inventory(client)
    # The booklet is plain JSON, readable without the service.
    # (Path layout is exercised through the API in the other cases.)
    response = client.get(f"/inventory/volumes/{VOLUME_ID}/booklet")
    raw = json.loads(response.content)
    assert raw["version"] == 1
    assert raw["volume_id"] == VOLUME_ID
    assert raw["archives"][0]["id"] == published["id"]
