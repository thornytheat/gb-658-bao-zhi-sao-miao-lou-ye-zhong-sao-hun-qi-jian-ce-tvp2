from fastapi.testclient import TestClient
import pytest

import app.store as store
import app.modules.m7_catalogs.service as catalogs
from app.errors import DomainError
from app.main import app

HEADER = "issue,edition,page,file_name,sha256,phash,clarity,header_issue\n"


def manifest(*rows: tuple) -> str:
    body = "".join(
        f"{issue},01,{page},{name},{digest * 64},0f0f,{clarity},{issue}\n"
        for issue, page, name, digest, clarity in rows
    )
    return HEADER + body


def issue_manifest(pages, *, digest="a", clarity=90, issue="2026-11-01", prefix="scan"):
    return manifest(*[(issue, page, f"{prefix}_{page}.tif", f"{digest}{page}", clarity) for page in pages])


@pytest.fixture(autouse=True)
def isolated_db(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "DATABASE_PATH", tmp_path / "catalogs.db")


def register(catalog_id, text, **extra):
    return catalogs.register_catalog({"catalog_id": catalog_id, "manifest": text, **extra})


# ---------------------------------------------------------------------------
# registration


def test_register_manifest_keeps_line_numbers_and_totals():
    result = register("CAT-A", issue_manifest(["001", "002", "004"]))
    assert result["accepted"] == 3
    assert [scan["line_no"] for scan in result["scans"]] == [2, 3, 4]
    account = catalogs.catalog_account("CAT-A")
    assert account["total_scans"] == 3
    assert account["missing_pages"] == ["003"]
    assert account["page_numbers"] == [1, 2, 4]


def test_register_real_sample_dirty_manifest_quarantines_bad_lines():
    text = (
        HEADER
        + "2026-11-01,01,001,a.tif," + "a" * 64 + ",0f0f,90,2026-11-01\n"
        + "2026-11-01,01,002\n"
        + HEADER
        + "2026-11-01,01,004,d.tif," + "d" * 64 + ",0f0f,84,2026-11-02\n"
    )
    result = register("CAT-A", text)
    assert result["accepted"] == 2
    assert result["quarantined"] == 1
    assert result["quarantine"][0]["reason_code"] == "COLUMN_COUNT"
    assert result["quarantine"][0]["line_no"] == 3


def test_register_identical_bytes_is_idempotent():
    text = issue_manifest(["001"])
    first = register("CAT-A", text)
    second = register("CAT-A", text)
    assert first["created"] is True
    assert second["created"] is False and second["replayed"] is True
    assert catalogs.catalog_account("CAT-A")["total_scans"] == 1


def test_truncated_tail_is_kept_and_flagged():
    result = register("CAT-A", HEADER + "2026-11-01,01,001,a.tif,abc")
    assert result["tail_complete"] is False
    assert result["quarantined"] >= 1


# ---------------------------------------------------------------------------
# merge: identity, duplicates, conflicts


def test_merge_keeps_original_identity_for_old_credentials():
    register("CAT-A", issue_manifest(["001", "002"], prefix="a"))
    register("CAT-B", issue_manifest(["003"], prefix="b"))
    result = catalogs.merge_catalogs(
        {"source_catalog_id": "CAT-A", "target_catalog_id": "CAT-B", "basis": {"note": "同版两目录"}}
    )
    assert result["status"] == "COMPLETED"
    assert result["result"]["moved"] == 2

    # The scan is now owned by CAT-B ...
    scans = catalogs.list_scans("CAT-B")["scans"]
    assert {scan["original_catalog_id"] for scan in scans} == {"CAT-A", "CAT-B"}

    # ... but the credential issued against CAT-A still verifies, unchanged.
    verified = catalogs.verify_credential({"catalog_id": "CAT-A", "business_key": "2026-11-01|01|001"})
    assert verified["verified"] is True
    scan = verified["scans"][0]
    assert scan["original_catalog_id"] == "CAT-A"
    assert scan["current_catalog_id"] == "CAT-B"
    assert scan["moved"] is True
    assert scan["merge_id"] == result["merge_id"]
    assert scan["mapping_basis"]["request"] == {"note": "同版两目录"}
    assert scan["mapping_basis"]["facts"]["source_catalog_id"] == "CAT-A"


def test_exact_copy_is_duplicate_not_an_extra_page():
    register("CAT-A", issue_manifest(["001"], digest="a", prefix="a"))
    register("CAT-B", issue_manifest(["001"], digest="a", prefix="b"))
    result = catalogs.merge_catalogs({"source_catalog_id": "CAT-A", "target_catalog_id": "CAT-B"})
    account = catalogs.catalog_account("CAT-B")
    assert result["result"]["duplicates"] == 1
    assert account["total_scans"] == 2
    assert account["active"] == 1 and account["duplicates"] == 1
    assert account["duplicate_pages"] == []
    assert account["pending_conflict_count"] == 0


def test_same_page_different_scan_is_held_not_dropped_by_clarity():
    register("CAT-A", issue_manifest(["001"], digest="a", clarity=10, prefix="faint"))
    register("CAT-B", issue_manifest(["001"], digest="b", clarity=99, prefix="sharp"))
    result = catalogs.merge_catalogs({"source_catalog_id": "CAT-A", "target_catalog_id": "CAT-B"})
    account = catalogs.catalog_account("CAT-B")
    assert result["result"]["conflicts"] == 1
    assert account["total_scans"] == 2
    assert account["conflicts"] == 2
    assert account["pending_conflict_count"] == 1
    names = {scan["file_name"] for scan in catalogs.list_scans("CAT-B")["scans"]}
    assert names == {"faint_001.tif", "sharp_001.tif"}
    conflict = account["pending_conflicts"][0]
    assert conflict["status"] == "PENDING"
    assert len(conflict["target_scan_ids"]) == 1 and len(conflict["source_scan_ids"]) == 1


def test_conflict_resolution_keeps_loser_as_evidence():
    register("CAT-A", issue_manifest(["001"], digest="a", prefix="a"))
    register("CAT-B", issue_manifest(["001"], digest="b", prefix="b"))
    merged = catalogs.merge_catalogs({"source_catalog_id": "CAT-A", "target_catalog_id": "CAT-B"})
    conflict = merged["conflicts"][0]
    result = catalogs.resolve_conflict(conflict["conflict_id"], {"resolution": "KEEP_BOTH"})
    assert result["conflict"]["status"] == "RESOLVED"
    account = catalogs.catalog_account("CAT-B")
    assert account["active"] == 2 and account["conflicts"] == 0
    assert account["pending_conflict_count"] == 0


def test_merge_recomputes_page_gaps_over_union():
    register("CAT-A", issue_manifest(["001", "002", "005"], prefix="a"))
    register("CAT-B", issue_manifest(["001", "003", "004"], prefix="b"))
    catalogs.merge_catalogs({"source_catalog_id": "CAT-A", "target_catalog_id": "CAT-B"})
    account = catalogs.catalog_account("CAT-B")
    assert account["page_numbers"] == [1, 2, 3, 4, 5]
    assert account["missing_pages"] == []
    assert account["expected_pages"] == 5


# ---------------------------------------------------------------------------
# retraction


def test_retract_restores_only_this_merge_and_keeps_later_scans():
    register("CAT-A", issue_manifest(["001", "002"], prefix="a"))
    register("CAT-B", issue_manifest(["001", "003"], prefix="b"))
    merged = catalogs.merge_catalogs({"source_catalog_id": "CAT-A", "target_catalog_id": "CAT-B"})

    # New manifest arrives after the merge; intake goes to the live catalog.
    late = register("CAT-A", issue_manifest(["004"], prefix="late"))
    assert late["scans"][0]["catalog_id"] == "CAT-B"
    assert late["scans"][0]["original_catalog_id"] == "CAT-A"

    # The default versions describe the merge itself, so this late arrival
    # makes the retraction stale.
    with pytest.raises(DomainError) as excinfo:
        catalogs.retract_merge({"merge_id": merged["merge_id"]})
    assert excinfo.value.code == "STALE_CATALOG"

    # Re-reading both versions lets the same retraction proceed, and it only
    # returns the scans this merge moved.
    result = catalogs.retract_merge(
        {
            "merge_id": merged["merge_id"],
            "source_version": catalogs.catalog_account("CAT-A")["catalog"]["version"],
            "target_version": catalogs.catalog_account("CAT-B")["catalog"]["version"],
        }
    )
    assert result["status"] == "RETRACTED"
    assert result["result"]["restored"] == 2

    source = catalogs.catalog_account("CAT-A")
    target = catalogs.catalog_account("CAT-B")
    assert source["total_scans"] == 2
    assert target["total_scans"] == 3  # its own two + the late scan
    assert "late_004.tif" in {scan["file_name"] for scan in catalogs.list_scans("CAT-B")["scans"]}
    # The late scan keeps its original identity but stays where it landed; the
    # original view of CAT-A also contains the scans this retraction restored.
    late_original = {
        scan["file_name"]: scan["catalog_id"] for scan in catalogs.list_scans("CAT-A", "original")["scans"]
    }
    assert late_original["late_004.tif"] == "CAT-B"
    assert late_original["a_001.tif"] == "CAT-A"
    assert late_original["a_002.tif"] == "CAT-A"


def test_retract_is_idempotent():
    register("CAT-A", issue_manifest(["001"]))
    register("CAT-B", issue_manifest(["002"]))
    merged = catalogs.merge_catalogs({"source_catalog_id": "CAT-A", "target_catalog_id": "CAT-B"})
    first = catalogs.retract_merge({"merge_id": merged["merge_id"]})
    second = catalogs.retract_merge({"merge_id": merged["merge_id"]})
    assert second["replayed"] is True
    assert second["status"] == "RETRACTED"
    assert catalogs.catalog_account("CAT-A")["total_scans"] == 1
    assert catalogs.catalog_account("CAT-B")["total_scans"] == 1


def test_retract_rejects_stale_directory_version():
    register("CAT-A", issue_manifest(["001"]))
    register("CAT-B", issue_manifest(["002"]))
    merged = catalogs.merge_catalogs({"source_catalog_id": "CAT-A", "target_catalog_id": "CAT-B"})
    version = catalogs.catalog_account("CAT-B")["catalog"]["version"]
    register("CAT-B", issue_manifest(["003"], prefix="new"))
    with pytest.raises(DomainError) as excinfo:
        catalogs.retract_merge({"merge_id": merged["merge_id"], "target_version": version})
    assert excinfo.value.code == "STALE_CATALOG"


def test_retract_returns_pending_conflict_to_source():
    register("CAT-A", issue_manifest(["001"], digest="a", prefix="a"))
    register("CAT-B", issue_manifest(["001"], digest="b", prefix="b"))
    merged = catalogs.merge_catalogs({"source_catalog_id": "CAT-A", "target_catalog_id": "CAT-B"})
    catalogs.retract_merge({"merge_id": merged["merge_id"]})
    source = catalogs.catalog_account("CAT-A")
    target = catalogs.catalog_account("CAT-B")
    assert source["total_scans"] == 1 and source["active"] == 1
    assert target["total_scans"] == 1 and target["active"] == 1
    assert source["pending_conflict_count"] == 0 and target["pending_conflict_count"] == 0


def test_source_internal_conflict_moves_with_catalog_and_comes_back():
    # CAT-A already has a pending conflict on page 001 before the merge.
    register("CAT-A", manifest(*[
        ("2026-11-01", "001", "a1.tif", "a", 90),
        ("2026-11-01", "001", "a1b.tif", "b", 95),
    ]))
    register("CAT-B", issue_manifest(["003"], prefix="b"))
    assert catalogs.catalog_account("CAT-A")["pending_conflict_count"] == 1
    merged = catalogs.merge_catalogs({"source_catalog_id": "CAT-A", "target_catalog_id": "CAT-B"})
    target = catalogs.catalog_account("CAT-B")
    assert target["pending_conflict_count"] == 1
    conflict = target["pending_conflicts"][0]
    assert conflict["catalog_id"] == "CAT-B"
    # The conflict predates the merge; the merge ledger, not the conflict row,
    # records that this merge moved it.
    moves = catalogs.get_merge(merged["merge_id"])["result"]
    assert moves["moved"] == 2

    catalogs.retract_merge({"merge_id": merged["merge_id"]})
    source = catalogs.catalog_account("CAT-A")
    target = catalogs.catalog_account("CAT-B")
    assert source["pending_conflict_count"] == 1
    assert target["pending_conflict_count"] == 0
    names = {scan["file_name"] for scan in catalogs.list_scans("CAT-A")["scans"]}
    assert names == {"a1.tif", "a1b.tif"}


def test_both_catalogs_conflicted_same_page_merge_then_retract():
    register("CAT-A", manifest(*[
        ("2026-11-01", "001", "a1.tif", "a", 90),
        ("2026-11-01", "001", "a1b.tif", "b", 91),
    ]))
    register("CAT-B", manifest(*[
        ("2026-11-01", "001", "b1.tif", "c", 92),
        ("2026-11-01", "001", "b1b.tif", "d", 93),
    ]))
    merged = catalogs.merge_catalogs({"source_catalog_id": "CAT-A", "target_catalog_id": "CAT-B"})
    target = catalogs.catalog_account("CAT-B")
    assert target["total_scans"] == 4
    assert target["pending_conflict_count"] == 1
    assert len(target["pending_conflicts"][0]["source_scan_ids"]) >= 2

    catalogs.retract_merge({"merge_id": merged["merge_id"]})
    assert catalogs.catalog_account("CAT-A")["total_scans"] == 2
    assert catalogs.catalog_account("CAT-B")["total_scans"] == 2
    assert catalogs.catalog_account("CAT-A")["pending_conflict_count"] == 1
    assert catalogs.catalog_account("CAT-B")["pending_conflict_count"] == 1


def test_late_scan_joining_merge_conflict_is_kept_and_reheld_on_retract():
    register("CAT-A", issue_manifest(["001"], digest="a", prefix="a"))
    register("CAT-B", issue_manifest(["001"], digest="b", prefix="b"))
    merged = catalogs.merge_catalogs({"source_catalog_id": "CAT-A", "target_catalog_id": "CAT-B"})
    late = register("CAT-B", issue_manifest(["001"], digest="z", prefix="late"))
    assert late["scans"][0]["status"] == "CONFLICT"

    catalogs.retract_merge(
        {
            "merge_id": merged["merge_id"],
            "source_version": catalogs.catalog_account("CAT-A")["catalog"]["version"],
            "target_version": catalogs.catalog_account("CAT-B")["catalog"]["version"],
        }
    )
    target = catalogs.catalog_account("CAT-B")
    scans = {scan["file_name"]: scan for scan in catalogs.list_scans("CAT-B")["scans"]}
    # The merged scan went back to CAT-A; the late scan and CAT-B's own scan
    # are genuinely different scans on one page, so they are held again
    # instead of being silently promoted or dropped.
    assert "a_001.tif" not in scans
    assert scans["late_001.tif"]["status"] == "CONFLICT"
    assert scans["b_001.tif"]["status"] == "CONFLICT"
    assert target["pending_conflict_count"] == 1
    conflict = target["pending_conflicts"][0]
    assert set(conflict["target_scan_ids"]) == {scans["b_001.tif"]["scan_id"], scans["late_001.tif"]["scan_id"]}
    assert conflict["source_scan_ids"] == []


def test_retract_does_not_leave_reference_to_moved_scan_in_foreign_conflict():
    # CAT-B owns a pending conflict before the merge; CAT-A has a distinct scan
    # for the same page.  After retraction CAT-B's conflict must not reference
    # the scan that went back to CAT-A.
    register("CAT-A", issue_manifest(["001", "002"], digest="a", prefix="a"))
    register("CAT-B", manifest(*[
        ("2026-11-01", "001", "b1.tif", "b", 90),
        ("2026-11-01", "001", "b1b.tif", "c", 91),
    ]))
    merged = catalogs.merge_catalogs({"source_catalog_id": "CAT-A", "target_catalog_id": "CAT-B"})
    target = catalogs.catalog_account("CAT-B")
    catalogs.retract_merge(
        {
            "merge_id": merged["merge_id"],
            "source_version": catalogs.catalog_account("CAT-A")["catalog"]["version"],
            "target_version": target["catalog"]["version"],
        }
    )
    conflict = catalogs.catalog_account("CAT-B")["pending_conflicts"][0]
    referenced = set(conflict["target_scan_ids"]) | set(conflict["source_scan_ids"])
    scans = {scan["scan_id"]: scan for scan in catalogs.list_scans("CAT-B")["scans"]}
    assert referenced <= set(scans)
    assert all(scans[scan_id]["status"] == "CONFLICT" for scan_id in referenced)


def test_chained_merge_must_be_retracted_first():
    register("CAT-A", issue_manifest(["001"]))
    register("CAT-B", issue_manifest(["002"]))
    register("CAT-C", issue_manifest(["003"]))
    first = catalogs.merge_catalogs({"source_catalog_id": "CAT-A", "target_catalog_id": "CAT-B"})
    catalogs.merge_catalogs({"source_catalog_id": "CAT-B", "target_catalog_id": "CAT-C"})
    with pytest.raises(DomainError) as excinfo:
        catalogs.retract_merge({"merge_id": first["merge_id"]})
    assert excinfo.value.code == "MERGE_CHAINED"


# ---------------------------------------------------------------------------
# concurrency / resumption


def test_stale_merge_version_is_rejected():
    register("CAT-A", issue_manifest(["001"]))
    register("CAT-B", issue_manifest(["002"]))
    version = catalogs.catalog_account("CAT-A")["catalog"]["version"]
    register("CAT-A", issue_manifest(["003"], prefix="late"))
    with pytest.raises(DomainError) as excinfo:
        catalogs.merge_catalogs(
            {"source_catalog_id": "CAT-A", "target_catalog_id": "CAT-B", "source_version": version}
        )
    assert excinfo.value.code == "STALE_CATALOG"
    assert catalogs.catalog_account("CAT-A")["total_scans"] == 2


def test_stale_registration_version_is_rejected():
    register("CAT-A", issue_manifest(["001"]))
    version = catalogs.catalog_account("CAT-A")["catalog"]["version"]
    register("CAT-A", issue_manifest(["002"], prefix="second"))
    with pytest.raises(DomainError) as excinfo:
        register("CAT-A", issue_manifest(["003"], prefix="third"), expected_version=version)
    assert excinfo.value.code == "STALE_CATALOG"


def test_interrupted_merge_resumes_without_duplicate_scans():
    register("CAT-A", issue_manifest(["001", "002", "003", "004"]))
    register("CAT-B", issue_manifest(["001", "002", "003", "004"], prefix="b"))
    paused = catalogs.merge_catalogs(
        {"source_catalog_id": "CAT-A", "target_catalog_id": "CAT-B", "max_migrations": 2}
    )
    assert paused["status"] == "IN_PROGRESS"
    assert paused["paused"] is True
    assert paused["migration_count"] == 2
    assert catalogs.catalog_account("CAT-B")["total_scans"] == 6

    resumed = catalogs.merge_catalogs({"source_catalog_id": "CAT-A", "target_catalog_id": "CAT-B"})
    assert resumed["status"] == "COMPLETED"
    assert resumed["migration_count"] == 4
    account = catalogs.catalog_account("CAT-B")
    assert account["total_scans"] == 8
    assert account["duplicates"] == 4
    assert account["pending_conflict_count"] == 0


def test_registration_during_merge_is_rejected():
    register("CAT-A", issue_manifest(["001", "002", "003"]))
    register("CAT-B", issue_manifest(["004"]))
    catalogs.merge_catalogs(
        {"source_catalog_id": "CAT-A", "target_catalog_id": "CAT-B", "max_migrations": 1}
    )
    with pytest.raises(DomainError) as excinfo:
        register("CAT-B", issue_manifest(["005"], prefix="concurrent"))
    assert excinfo.value.code == "MERGE_IN_PROGRESS"


def test_completed_merge_retry_replays_without_new_migration():
    register("CAT-A", issue_manifest(["001"]))
    register("CAT-B", issue_manifest(["002"]))
    first = catalogs.merge_catalogs({"source_catalog_id": "CAT-A", "target_catalog_id": "CAT-B"})
    second = catalogs.merge_catalogs({"source_catalog_id": "CAT-A", "target_catalog_id": "CAT-B"})
    assert second["replayed"] is True
    assert second["merge_id"] == first["merge_id"]
    assert catalogs.catalog_account("CAT-B")["total_scans"] == 2


# ---------------------------------------------------------------------------
# HTTP surface


def test_http_register_merge_retract_and_queries():
    client = TestClient(app)
    registered = client.post(
        "/api/catalogs/register", json={"catalog_id": "CAT-A", "manifest": issue_manifest(["001", "002"])}
    )
    assert registered.status_code == 200
    client.post(
        "/api/catalogs/register", json={"catalog_id": "CAT-B", "manifest": issue_manifest(["003"], prefix="b")}
    )
    merged = client.post(
        "/api/catalogs/merge", json={"source_catalog_id": "CAT-A", "target_catalog_id": "CAT-B"}
    )
    assert merged.status_code == 200 and merged.json()["status"] == "COMPLETED"

    account = client.get("/api/catalogs/CAT-B/account")
    assert account.status_code == 200
    assert account.json()["page_numbers"] == [1, 2, 3]
    assert account.json()["missing_pages"] == []

    verified = client.post(
        "/api/catalogs/verify", json={"catalog_id": "CAT-A", "business_key": "2026-11-01|01|001"}
    )
    assert verified.status_code == 200 and verified.json()["verified"] is True

    retracted = client.post(
        "/api/catalogs/retract", json={"merge_id": merged.json()["merge_id"]}
    )
    assert retracted.status_code == 200 and retracted.json()["status"] == "RETRACTED"

    scans = client.get("/api/catalogs/CAT-B/scans")
    assert scans.status_code == 200 and scans.json()["total"] == 1


def test_http_stale_operation_returns_409():
    client = TestClient(app)
    client.post("/api/catalogs/register", json={"catalog_id": "CAT-A", "manifest": issue_manifest(["001"])})
    client.post("/api/catalogs/register", json={"catalog_id": "CAT-B", "manifest": issue_manifest(["002"])})
    response = client.post(
        "/api/catalogs/merge",
        json={"source_catalog_id": "CAT-A", "target_catalog_id": "CAT-B", "source_version": 99},
    )
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "STALE_CATALOG"


def test_catalogs_page_is_available():
    client = TestClient(app)
    response = client.get("/catalogs")
    assert response.status_code == 200 and "panel" in response.text
