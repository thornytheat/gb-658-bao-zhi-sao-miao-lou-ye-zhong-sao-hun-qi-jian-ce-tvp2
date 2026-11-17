"""Domain logic for catalog registration, merge, retraction and lookup.

Design notes
------------
* A scan keeps its *original identity*: ``original_catalog_id``,
  ``original_key`` and ``original_business_key`` never change.  Merging only
  changes the *current owner* (``catalog_id``), so credentials issued against
  an old directory still resolve.
* A merge never writes a second scan row.  It updates ownership and records the
  move in ``catalog_migrations``; retrying or resuming a merge therefore cannot
  duplicate scans.
* Overlapping pages never lose scans.  An exact copy (same ``sha256``) is
  marked ``DUPLICATE`` so it is not counted twice; anything else is held as
  ``CONFLICT`` with both sides kept until a human adjudicates.  Clarity is never
  used to drop a scan.
* Merges, retractions, registrations and adjudications use per-catalog
  ``version`` numbers.  Callers may pass the versions they observed; a mismatch
  is rejected as a stale operation.
* Retraction replays only the migration ledger of one merge.  Scans that joined
  later, or that a later merge moved onward, are untouched.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from hashlib import sha256
from zoneinfo import ZoneInfo

from app.errors import DomainError
from app.store import connect
from app.modules.m1_intake.parser import parse_manifest
from app.modules.m2_issue.rules import normalize_edition, normalize_issue, normalize_page
from app.modules.m7_catalogs.schema import ensure_schema

TZ = ZoneInfo("Asia/Shanghai")

SCAN_ACTIVE = "ACTIVE"
SCAN_DUPLICATE = "DUPLICATE"
SCAN_CONFLICT = "CONFLICT"
SCAN_SUPERSEDED = "SUPERSEDED"

MERGE_IN_PROGRESS = "IN_PROGRESS"
MERGE_COMPLETED = "COMPLETED"
MERGE_RETRACTED = "RETRACTED"

CONFLICT_PENDING = "PENDING"
CONFLICT_RESOLVED = "RESOLVED"
CONFLICT_CANCELLED = "CANCELLED"

CATALOG_ACTIVE = "ACTIVE"
CATALOG_MERGED = "MERGED"

PRESENT_STATUSES = (SCAN_ACTIVE, SCAN_CONFLICT)
RESOLUTIONS = ("KEEP_TARGET", "KEEP_SOURCE", "KEEP_BOTH")


# ---------------------------------------------------------------------------
# helpers


def _row_factory(cursor, row):
    """Return plain dicts: callers may annotate them before serializing."""
    return {column[0]: row[index] for index, column in enumerate(cursor.description)}


def _connect():
    db = connect()
    db.row_factory = _row_factory
    # Explicit transaction control: BEGIN IMMEDIATE / COMMIT / ROLLBACK only.
    db.isolation_level = None
    return db


def _rollback(db) -> None:
    """Roll back only when a transaction is actually open."""
    if db.in_transaction:
        try:
            db.execute("ROLLBACK")
        except sqlite3.OperationalError:
            pass


def now_iso() -> str:
    return datetime.now(TZ).isoformat()


def _json(value, default):
    if value is None:
        return default
    if isinstance(value, str):
        try:
            return json.loads(value)
        except (TypeError, ValueError):
            return value
    return value


def _int(value, default=0) -> int:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


def _first(payload: dict, *names, default=None):
    for name in names:
        if name in payload and payload[name] not in (None, ""):
            return payload[name]
    return default


def _canonical(value) -> str:
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _digest(*parts) -> str:
    return sha256("\x1f".join(str(part) for part in parts).encode("utf-8")).hexdigest()


def _catalog(db, catalog_id: str) -> dict:
    row = db.execute("SELECT * FROM catalogs WHERE catalog_id = ?", (catalog_id,)).fetchone()
    if row is None:
        raise DomainError("CATALOG_NOT_FOUND", f"目录不存在: {catalog_id}", 404)
    return row


def _maybe_catalog(db, catalog_id: str) -> dict | None:
    return db.execute("SELECT * FROM catalogs WHERE catalog_id = ?", (catalog_id,)).fetchone()


def _merge(db, merge_id: str) -> dict:
    row = db.execute("SELECT * FROM catalog_merges WHERE merge_id = ?", (merge_id,)).fetchone()
    if row is None:
        raise DomainError("MERGE_NOT_FOUND", f"合并记录不存在: {merge_id}", 404)
    return row


def _catalog_tip(db, catalog_id: str) -> str:
    """Follow ``merged_into`` so new intake lands in the surviving catalog."""
    seen = set()
    current = catalog_id
    while current and current not in seen:
        seen.add(current)
        row = _maybe_catalog(db, current)
        if row is None or row["status"] != CATALOG_MERGED or not row["merged_into"]:
            return current
        current = row["merged_into"]
    return current


def _in_progress_merge(db, catalog_id: str) -> dict | None:
    return db.execute(
        "SELECT * FROM catalog_merges WHERE status = ? "
        "AND (source_catalog_id = ? OR target_catalog_id = ?) ORDER BY created_at LIMIT 1",
        (MERGE_IN_PROGRESS, catalog_id, catalog_id),
    ).fetchone()


def _require_version(payload: dict, names: tuple[str, ...]) -> int | None:
    value = _first(payload, *names)
    if value is None:
        return None
    return _int(value, default=-1)


def _check_version(catalog: dict, expected: int | None, role: str) -> None:
    if expected is None:
        return
    if int(catalog["version"]) != expected:
        raise DomainError(
            "STALE_CATALOG",
            f"{role}目录版本已变化：期望 {expected}，当前 {catalog['version']}",
            409,
        )


def _bump(db, catalog_id: str) -> int:
    db.execute(
        "UPDATE catalogs SET version = version + 1, updated_at = ? WHERE catalog_id = ?",
        (now_iso(), catalog_id),
    )
    row = db.execute("SELECT version FROM catalogs WHERE catalog_id = ?", (catalog_id,)).fetchone()
    if row is None:
        raise DomainError("CATALOG_NOT_FOUND", f"目录不存在: {catalog_id}", 404)
    return int(row["version"])


# ---------------------------------------------------------------------------
# manifest input


def _normalized_row(raw: dict) -> tuple[dict | None, dict | None]:
    """Normalize one accepted manifest row or return quarantine evidence."""
    issue_raw = str(raw.get("issue") or "").strip()
    edition_raw = str(raw.get("edition") or "").strip()
    page_raw = str(raw.get("page") or "").strip()
    if not issue_raw or not page_raw:
        return None, {"line_no": raw.get("line_no"), "reason_code": "EMPTY_VALUE", "raw_value": _canonical(raw)}
    try:
        issue = normalize_issue(issue_raw)
    except (TypeError, ValueError):
        return None, {"line_no": raw.get("line_no"), "reason_code": "BAD_ISSUE", "raw_value": _canonical(raw)}
    try:
        edition = normalize_edition(edition_raw) if edition_raw else ""
    except (TypeError, ValueError):
        return None, {"line_no": raw.get("line_no"), "reason_code": "BAD_EDITION", "raw_value": _canonical(raw)}
    try:
        page = normalize_page(page_raw)
    except (TypeError, ValueError):
        return None, {"line_no": raw.get("line_no"), "reason_code": "BAD_PAGE", "raw_value": _canonical(raw)}
    header_issue = raw.get("header_issue")
    if header_issue is not None:
        header_issue = str(header_issue).strip() or None
        if header_issue:
            try:
                header_issue = normalize_issue(header_issue)
            except (TypeError, ValueError):
                # Keep the literal header: M5 rules decide later.
                pass
    return {
        "line_no": _int(raw.get("line_no"), 0),
        "issue": issue,
        "edition": edition,
        "page": page,
        "file_name": str(raw.get("file_name") or "").strip(),
        "sha256": str(raw.get("sha256") or "").strip().lower(),
        "phash": str(raw.get("phash") or "").strip().lower(),
        "clarity": _int(raw.get("clarity"), 0),
        "header_issue": header_issue,
        "raw_row": _canonical(raw),
    }, None


def _manifest_source(payload: dict) -> tuple[str | None, list[dict] | None, bytes, str]:
    text = _first(payload, "manifest", "text", "content", "csv", "payload")
    if text is not None and not isinstance(text, str):
        raise DomainError("BAD_MANIFEST", "manifest 必须是清单文本", 422)
    rows = _first(payload, "rows", "records", "scans")
    if rows is not None and not isinstance(rows, list):
        raise DomainError("BAD_MANIFEST", "rows 必须是记录数组", 422)
    if text is None and rows is None:
        raise DomainError("EMPTY_MANIFEST", "缺少清单内容", 422)
    if text is not None:
        content = text.encode("utf-8")
        return text, None, content, sha256(content).hexdigest()
    content = _canonical(rows).encode("utf-8")
    return None, rows, content, sha256(content).hexdigest()


def _parse_input(text: str | None, rows: list[dict] | None) -> tuple[list[dict], list[dict], bool]:
    """Return (normalized rows, quarantine, tail_complete)."""
    accepted_raw: list[dict] = []
    quarantine: list[dict] = []
    tail_complete = True
    if text is not None:
        accepted_raw, parsed_bad = parse_manifest(text)
        tail_complete = (not text) or text.endswith("\n")
        for item in parsed_bad:
            quarantine.append(
                {
                    "line_no": item.get("line_no"),
                    "reason_code": item.get("reason_code", "BAD_ROW"),
                    "raw_value": item.get("raw", item.get("raw_value", "")),
                }
            )
    else:
        for index, raw in enumerate(rows or [], start=2):
            if not isinstance(raw, dict):
                quarantine.append({"line_no": index, "reason_code": "BAD_ROW", "raw_value": str(raw)})
                continue
            item = dict(raw)
            item.setdefault("line_no", index)
            accepted_raw.append(item)
    normalized: list[dict] = []
    for raw in accepted_raw:
        item, bad = _normalized_row(raw)
        if bad:
            quarantine.append(bad)
        else:
            normalized.append(item)
    return normalized, quarantine, tail_complete


# ---------------------------------------------------------------------------
# scan insert / collision handling


def _conflict_id_for(merge_id: str | None, catalog_id: str, business_key: str, stamp: str) -> str:
    # Unique per occurrence: a page may conflict again after adjudication.
    return "CFL-" + _digest(merge_id or "registration", catalog_id, business_key, stamp)[:20]


def _conflict_dict(db, conflict_id: str) -> dict:
    row = db.execute("SELECT * FROM catalog_conflicts WHERE conflict_id = ?", (conflict_id,)).fetchone()
    if row is None:
        raise DomainError("CONFLICT_NOT_FOUND", f"冲突不存在: {conflict_id}", 404)
    item = dict(row)
    item["target_scan_ids"] = _json(item["target_scan_ids"], [])
    item["source_scan_ids"] = _json(item["source_scan_ids"], [])
    item["previous_target_status"] = _json(item["previous_target_status"], {})
    return item


def _pending_conflicts(db, catalog_id: str, business_key: str) -> list[dict]:
    rows = db.execute(
        "SELECT conflict_id FROM catalog_conflicts WHERE catalog_id = ? AND business_key = ? "
        "AND status = ? ORDER BY created_at",
        (catalog_id, business_key, CONFLICT_PENDING),
    ).fetchall()
    return [_conflict_dict(db, row["conflict_id"]) for row in rows]


def _hold_for_conflict(db, merge_id: str | None, catalog_id: str, scan: dict, stamp: str) -> str:
    """Attach an incoming scan to the pending conflict for its page.

    Active scans already on the page become the target side and are held too.
    Nothing is dropped or ranked by clarity: every colliding scan keeps status
    ``CONFLICT`` until a human adjudicates.
    """
    key = scan["business_key"]
    targets = db.execute(
        "SELECT * FROM catalog_scans WHERE catalog_id = ? AND business_key = ? AND status = ? "
        "AND scan_id != ? ORDER BY scan_id",
        (catalog_id, key, SCAN_ACTIVE, scan["scan_id"]),
    ).fetchall()
    pending = _pending_conflicts(db, catalog_id, key)
    if pending:
        conflict = pending[0]
        conflict_id = conflict["conflict_id"]
        target_ids = list(conflict["target_scan_ids"])
        for item in targets:
            if item["scan_id"] not in target_ids:
                target_ids.append(item["scan_id"])
        source_ids = list(conflict["source_scan_ids"])
        if scan["scan_id"] not in source_ids:
            source_ids.append(scan["scan_id"])
        previous = dict(conflict["previous_target_status"])
        for item in targets:
            previous.setdefault(item["scan_id"], {"status": SCAN_ACTIVE, "conflict_id": None})
        db.execute(
            "UPDATE catalog_conflicts SET target_scan_ids = ?, source_scan_ids = ?, "
            "previous_target_status = ? WHERE conflict_id = ?",
            (_canonical(target_ids), _canonical(source_ids), _canonical(previous), conflict_id),
        )
    else:
        conflict_id = _conflict_id_for(merge_id, catalog_id, key, stamp)
        previous = {
            item["scan_id"]: {"status": item["status"], "conflict_id": item["conflict_id"]}
            for item in targets
        }
        db.execute(
            "INSERT INTO catalog_conflicts "
            "(conflict_id, merge_id, catalog_id, business_key, page, target_scan_ids, source_scan_ids, "
            "previous_target_status, status, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                conflict_id,
                merge_id,
                catalog_id,
                key,
                scan["page"],
                _canonical([item["scan_id"] for item in targets]),
                _canonical([scan["scan_id"]]),
                _canonical(previous),
                CONFLICT_PENDING,
                stamp,
            ),
        )
    for item in targets:
        db.execute(
            "UPDATE catalog_scans SET status = ?, conflict_id = ? WHERE scan_id = ?",
            (SCAN_CONFLICT, conflict_id, item["scan_id"]),
        )
    db.execute(
        "UPDATE catalog_scans SET status = ?, conflict_id = ? WHERE scan_id = ?",
        (SCAN_CONFLICT, conflict_id, scan["scan_id"]),
    )
    return conflict_id


def _insert_scan(db, owner_id: str, original_catalog_id: str, registration_id: str, item: dict,
                 status: str, duplicate_of: str | None, stamp: str) -> dict:
    base_key = "{}|{}|{}|{}".format(item["issue"], item["edition"], item["page"], item["file_name"] or "-")
    original_key = base_key
    suffix = 1
    while True:
        row = db.execute(
            "SELECT * FROM catalog_scans WHERE original_catalog_id = ? AND original_key = ?",
            (original_catalog_id, original_key),
        ).fetchone()
        if row is None:
            break
        if row["sha256"] == item["sha256"] and row["line_no"] == item["line_no"]:
            # Identical re-registration: return the existing scan, add nothing.
            return row
        suffix += 1
        original_key = (
            f"{base_key}@{registration_id}" if suffix == 2 else f"{base_key}@{registration_id}#{suffix - 1}"
        )
    original_business_key = "|".join((item["issue"], item["edition"], item["page"]))
    scan_id = "SCN-" + _digest(
        original_catalog_id, registration_id, item["line_no"], original_key, item["sha256"]
    )[:24]
    db.execute(
        "INSERT OR IGNORE INTO catalog_scans "
        "(scan_id, catalog_id, registration_id, original_catalog_id, original_key, "
        "original_business_key, business_key, issue, edition, page, file_name, sha256, phash, "
        "clarity, header_issue, line_no, raw_row, status, duplicate_of, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            scan_id,
            owner_id,
            registration_id,
            original_catalog_id,
            original_key,
            original_business_key,
            original_business_key,
            item["issue"],
            item["edition"],
            item["page"],
            item["file_name"],
            item["sha256"],
            item["phash"],
            item["clarity"],
            item["header_issue"],
            item["line_no"],
            item["raw_row"],
            status,
            duplicate_of,
            stamp,
        ),
    )
    return db.execute("SELECT * FROM catalog_scans WHERE scan_id = ?", (scan_id,)).fetchone()


def _ingest(db, owner_id: str, original_catalog_id: str, registration_id: str,
            items: list[dict], stamp: str) -> list[dict]:
    scans = []
    for item in items:
        business_key = "|".join((item["issue"], item["edition"], item["page"]))
        item["business_key"] = business_key
        peers = db.execute(
            "SELECT * FROM catalog_scans WHERE catalog_id = ? AND business_key = ? "
            "AND status IN (?, ?, ?) ORDER BY scan_id",
            (owner_id, business_key, SCAN_ACTIVE, SCAN_DUPLICATE, SCAN_CONFLICT),
        ).fetchall()
        copy = next(
            (
                peer
                for peer in peers
                if item["sha256"]
                and peer["sha256"] == item["sha256"]
                and peer["status"] in (SCAN_ACTIVE, SCAN_DUPLICATE)
            ),
            None,
        )
        if copy is not None:
            scans.append(
                _insert_scan(
                    db, owner_id, original_catalog_id, registration_id, item,
                    SCAN_DUPLICATE, copy["scan_id"], stamp,
                )
            )
            continue
        if peers:
            scan = _insert_scan(
                db, owner_id, original_catalog_id, registration_id, item, SCAN_ACTIVE, None, stamp
            )
            if scan["status"] == SCAN_ACTIVE:
                _hold_for_conflict(db, None, owner_id, scan, stamp)
                scan = db.execute("SELECT * FROM catalog_scans WHERE scan_id = ?", (scan["scan_id"],)).fetchone()
            scans.append(scan)
            continue
        scans.append(
            _insert_scan(db, owner_id, original_catalog_id, registration_id, item, SCAN_ACTIVE, None, stamp)
        )
    return scans


# ---------------------------------------------------------------------------
# registration


def register_catalog(payload: dict | None = None) -> dict:
    """Register a real manifest under one directory (catalog) identity.

    ``catalog_id`` may be supplied explicitly; otherwise it is derived from
    ``issue``/``edition``.  Re-posting identical manifest bytes to the same
    catalog is idempotent and adds no scans.
    """
    payload = dict(payload or {})
    ensure_schema()
    catalog_id = _first(payload, "catalog_id", "directory_id", "catalog", "id")
    if not catalog_id:
        issue = _first(payload, "issue")
        edition = _first(payload, "edition")
        if issue and edition:
            catalog_id = f"{issue}|{edition}"
        else:
            raise DomainError("MISSING_CATALOG", "登记需要 catalog_id 或 issue+edition", 422)
    catalog_id = str(catalog_id).strip()
    if not catalog_id:
        raise DomainError("MISSING_CATALOG", "catalog_id 不能为空", 422)

    text, rows, content, content_sha = _manifest_source(payload)
    expected_version = _require_version(payload, ("expected_version", "version", "if_version"))

    db = _connect()
    try:
        # The write transaction starts before the replay check so a concurrent
        # registration cannot slip both requests past it.
        db.execute("BEGIN IMMEDIATE")
        existing_reg = db.execute(
            "SELECT * FROM catalog_registrations WHERE catalog_id = ? AND content_sha256 = ?",
            (catalog_id, content_sha),
        ).fetchone()
        if existing_reg is not None:
            scans = db.execute(
                "SELECT * FROM catalog_scans WHERE registration_id = ? ORDER BY line_no, scan_id",
                (existing_reg["registration_id"],),
            ).fetchall()
            db.execute("COMMIT")
            return {
                "created": False,
                "replayed": True,
                "catalog": _catalog(db, catalog_id),
                "registration": existing_reg,
                "accepted": 0,
                "quarantined": 0,
                "scans": scans,
                "duplicate_scans": sum(1 for scan in scans if scan["status"] == SCAN_DUPLICATE),
                "conflict_scans": sum(1 for scan in scans if scan["status"] == SCAN_CONFLICT),
                "quarantine": [],
                "tail_complete": bool(existing_reg["tail_complete"]),
            }

        catalog = _maybe_catalog(db, catalog_id)
        if catalog is None:
            issue = _first(payload, "issue", default="")
            edition = _first(payload, "edition", default="")
            try:
                issue = normalize_issue(issue) if issue else ""
            except (TypeError, ValueError):
                issue = str(issue)
            try:
                edition = normalize_edition(edition) if edition else ""
            except (TypeError, ValueError):
                edition = str(edition)
            stamp = now_iso()
            db.execute(
                "INSERT INTO catalogs (catalog_id, label, issue, edition, status, version, "
                "registration_id, expected_pages, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, 1, NULL, ?, ?, ?)",
                (
                    catalog_id,
                    str(_first(payload, "label", "name", default=catalog_id)),
                    issue,
                    edition,
                    CATALOG_ACTIVE,
                    _int(_first(payload, "expected_pages"), 0) or None,
                    stamp,
                    stamp,
                ),
            )
            catalog = _catalog(db, catalog_id)
        else:
            _check_version(catalog, expected_version, "目录")
            busy = _in_progress_merge(db, catalog_id)
            if busy is not None:
                raise DomainError(
                    "MERGE_IN_PROGRESS",
                    f"目录正在合并 {busy['merge_id']}，拒绝并发的清单登记",
                    409,
                )
            issue = _first(payload, "issue")
            edition = _first(payload, "edition")
            if issue or edition:
                try:
                    norm_issue = normalize_issue(issue) if issue else catalog["issue"]
                    norm_edition = normalize_edition(edition) if edition else catalog["edition"]
                except (TypeError, ValueError):
                    norm_issue, norm_edition = catalog["issue"], catalog["edition"]
                db.execute(
                    "UPDATE catalogs SET issue = ?, edition = ?, label = ?, updated_at = ? WHERE catalog_id = ?",
                    (
                        norm_issue or "",
                        norm_edition or "",
                        str(_first(payload, "label", "name", default=catalog["label"])),
                        now_iso(),
                        catalog_id,
                    ),
                )
                catalog = _catalog(db, catalog_id)

        owner_id = _catalog_tip(db, catalog_id)
        if _in_progress_merge(db, owner_id) is not None:
            raise DomainError(
                "MERGE_IN_PROGRESS",
                "归属目录正在合并，拒绝并发的清单登记",
                409,
            )

        normalized, quarantine, tail_complete = _parse_input(text, rows)

        registration_id = str(
            _first(payload, "registration_id", default="REG-" + _digest(catalog_id, content_sha)[:16])
        )
        stamp = now_iso()
        db.execute(
            "INSERT INTO catalog_registrations "
            "(registration_id, catalog_id, content_sha256, input_bytes, accepted, quarantined, tail_complete, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                registration_id,
                catalog_id,
                content_sha,
                len(content),
                len(normalized),
                len(quarantine),
                1 if tail_complete else 0,
                stamp,
            ),
        )

        scan_rows = _ingest(db, owner_id, catalog_id, registration_id, normalized, stamp)

        for bad in quarantine:
            db.execute(
                "INSERT OR IGNORE INTO catalog_quarantine "
                "(catalog_id, registration_id, line_no, reason_code, raw_value, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    owner_id,
                    registration_id,
                    bad.get("line_no"),
                    bad["reason_code"],
                    str(bad.get("raw_value", "")),
                    stamp,
                ),
            )

        expected_pages = _int(_first(payload, "expected_pages"), 0)
        if expected_pages <= 0:
            expected_pages = max((int(item["page"]) for item in normalized), default=0)
        if expected_pages > _int(catalog["expected_pages"], 0):
            db.execute(
                "UPDATE catalogs SET expected_pages = ?, updated_at = ? WHERE catalog_id = ?",
                (expected_pages, now_iso(), owner_id),
            )

        version = _bump(db, catalog_id)
        owner_version = version
        if owner_id != catalog_id:
            owner_version = _bump(db, owner_id)
        db.execute(
            "UPDATE catalogs SET registration_id = ?, updated_at = ? WHERE catalog_id = ?",
            (registration_id, now_iso(), catalog_id),
        )
        db.execute("COMMIT")
        return {
            "created": True,
            "replayed": False,
            "catalog": _catalog(db, catalog_id),
            "catalog_version": version,
            "owner_catalog": _catalog(db, owner_id),
            "owner_version": owner_version,
            "registration": db.execute(
                "SELECT * FROM catalog_registrations WHERE registration_id = ?", (registration_id,)
            ).fetchone(),
            "accepted": len(normalized),
            "quarantined": len(quarantine),
            "scans": scan_rows,
            "duplicate_scans": sum(1 for scan in scan_rows if scan["status"] == SCAN_DUPLICATE),
            "conflict_scans": sum(1 for scan in scan_rows if scan["status"] == SCAN_CONFLICT),
            "quarantine": quarantine,
            "tail_complete": tail_complete,
        }
    except Exception:
        _rollback(db)
        raise
    finally:
        db.close()


# ---------------------------------------------------------------------------
# merge


def _merge_id_for(source_id: str, target_id: str, source_version: int, target_version: int, basis: str) -> str:
    return "MRG-" + _digest(source_id, target_id, source_version, target_version, basis)[:20]


def _migrate_one_scan(db, merge: dict, scan: dict, stamp: str) -> str:
    """Move one scan to the merge target while preserving its original identity."""
    target_id = merge["target_catalog_id"]
    conflict_id = scan["conflict_id"]
    outcome = "MOVED"

    if scan["status"] == SCAN_ACTIVE:
        peers = db.execute(
            "SELECT * FROM catalog_scans WHERE catalog_id = ? AND business_key = ? AND status = ? "
            "ORDER BY scan_id",
            (target_id, scan["business_key"], SCAN_ACTIVE),
        ).fetchall()
        pending = _pending_conflicts(db, target_id, scan["business_key"])
        if pending:
            conflict_id = _hold_for_conflict(db, merge["merge_id"], target_id, scan, stamp)
            db.execute(
                "UPDATE catalog_scans SET catalog_id = ?, status = ?, conflict_id = ? WHERE scan_id = ?",
                (target_id, SCAN_CONFLICT, conflict_id, scan["scan_id"]),
            )
            outcome = "CONFLICT_JOINED"
        elif peers:
            same = [item for item in peers if item["sha256"] and item["sha256"] == scan["sha256"]]
            if len(same) == len(peers):
                keep = same[0]
                db.execute(
                    "UPDATE catalog_scans SET catalog_id = ?, status = ?, duplicate_of = ?, "
                    "conflict_id = NULL WHERE scan_id = ?",
                    (target_id, SCAN_DUPLICATE, keep["scan_id"], scan["scan_id"]),
                )
                conflict_id = None
                outcome = "DUPLICATE"
            else:
                conflict_id = _hold_for_conflict(db, merge["merge_id"], target_id, scan, stamp)
                db.execute(
                    "UPDATE catalog_scans SET catalog_id = ?, status = ?, conflict_id = ? WHERE scan_id = ?",
                    (target_id, SCAN_CONFLICT, conflict_id, scan["scan_id"]),
                )
                outcome = "CONFLICT"
        else:
            db.execute(
                "UPDATE catalog_scans SET catalog_id = ?, conflict_id = NULL WHERE scan_id = ?",
                (target_id, scan["scan_id"]),
            )
    else:
        # Duplicate / conflict / superseded scans move as evidence; their
        # status is preserved so the reason for not counting them survives.
        db.execute(
            "UPDATE catalog_scans SET catalog_id = ? WHERE scan_id = ?",
            (target_id, scan["scan_id"]),
        )
        outcome = "MOVED_" + scan["status"]

    db.execute(
        "INSERT OR IGNORE INTO catalog_migrations "
        "(merge_id, scan_id, from_catalog_id, to_catalog_id, previous_status, previous_conflict_id, "
        "previous_duplicate_of, migrated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            merge["merge_id"],
            scan["scan_id"],
            merge["source_catalog_id"],
            target_id,
            scan["status"],
            scan["conflict_id"],
            scan["duplicate_of"],
            stamp,
        ),
    )
    return outcome


def _transfer_source_conflicts(db, merge: dict, stamp: str) -> None:
    """Carry pending conflicts that belong to the source catalog to the target.

    An identical pending page conflict in the target absorbs the source one;
    otherwise the conflict is re-pointed.  ``catalog_conflict_moves`` records
    what happened so retraction can put the conflict back.
    """
    source_id = merge["source_catalog_id"]
    target_id = merge["target_catalog_id"]
    source_conflicts = db.execute(
        "SELECT * FROM catalog_conflicts WHERE catalog_id = ? AND status = ? ORDER BY created_at",
        (source_id, CONFLICT_PENDING),
    ).fetchall()
    for row in source_conflicts:
        conflict = _conflict_dict(db, row["conflict_id"])
        snapshot = _canonical(conflict)
        target_pending = [
            item
            for item in _pending_conflicts(db, target_id, conflict["business_key"])
            if item["conflict_id"] != conflict["conflict_id"]
        ]
        if target_pending:
            target = target_pending[0]
            live_conflict_id = target["conflict_id"]
            absorbed = conflict["target_scan_ids"] + conflict["source_scan_ids"]
            source_ids = list(target["source_scan_ids"])
            for scan_id in absorbed:
                if scan_id not in source_ids:
                    source_ids.append(scan_id)
            db.execute(
                "UPDATE catalog_conflicts SET source_scan_ids = ? WHERE conflict_id = ?",
                (_canonical(source_ids), live_conflict_id),
            )
            db.execute(
                "UPDATE catalog_conflicts SET status = ?, resolved_at = ? WHERE conflict_id = ?",
                (CONFLICT_CANCELLED, stamp, conflict["conflict_id"]),
            )
            db.execute(
                "INSERT OR IGNORE INTO catalog_conflict_moves "
                "(merge_id, conflict_id, from_catalog_id, to_catalog_id, kind, target_conflict_id, "
                "absorbed_scan_ids, snapshot, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    merge["merge_id"],
                    conflict["conflict_id"],
                    source_id,
                    target_id,
                    "ABSORBED",
                    live_conflict_id,
                    _canonical(absorbed),
                    snapshot,
                    stamp,
                ),
            )
            # The cancelled source record must not keep any scan attached.
            db.execute(
                "UPDATE catalog_scans SET conflict_id = ? WHERE conflict_id = ?",
                (live_conflict_id, conflict["conflict_id"]),
            )
        else:
            live_conflict_id = conflict["conflict_id"]
            db.execute(
                "UPDATE catalog_conflicts SET catalog_id = ? WHERE conflict_id = ?",
                (target_id, live_conflict_id),
            )
            db.execute(
                "INSERT OR IGNORE INTO catalog_conflict_moves "
                "(merge_id, conflict_id, from_catalog_id, to_catalog_id, kind, target_conflict_id, "
                "absorbed_scan_ids, snapshot, created_at) VALUES (?, ?, ?, ?, ?, NULL, '[]', ?, ?)",
                (
                    merge["merge_id"],
                    conflict["conflict_id"],
                    source_id,
                    target_id,
                    "MOVED",
                    snapshot,
                    stamp,
                ),
            )
        # Active scans already in the target on this page join the same hold:
        # merging must not leave one page both "active" and "pending".
        joined = db.execute(
            "SELECT * FROM catalog_scans WHERE catalog_id = ? AND business_key = ? AND status = ? "
            "ORDER BY scan_id",
            (target_id, conflict["business_key"], SCAN_ACTIVE),
        ).fetchall()
        if joined:
            held = _conflict_dict(db, live_conflict_id)
            targets = list(held["target_scan_ids"])
            for item in joined:
                if item["scan_id"] not in targets:
                    targets.append(item["scan_id"])
                db.execute(
                    "UPDATE catalog_scans SET status = ?, conflict_id = ? WHERE scan_id = ?",
                    (SCAN_CONFLICT, live_conflict_id, item["scan_id"]),
                )
            db.execute(
                "UPDATE catalog_conflicts SET target_scan_ids = ? WHERE conflict_id = ?",
                (_canonical(targets), live_conflict_id),
            )
            db.execute(
                "UPDATE catalog_conflict_moves SET joined_scan_ids = ? "
                "WHERE merge_id = ? AND conflict_id = ?",
                (
                    _canonical([item["scan_id"] for item in joined]),
                    merge["merge_id"],
                    conflict["conflict_id"],
                ),
            )


def _finalize_merge(db, merge: dict) -> dict:
    source_id = merge["source_catalog_id"]
    target_id = merge["target_catalog_id"]
    stamp = now_iso()
    source = _catalog(db, source_id)

    _transfer_source_conflicts(db, merge, stamp)

    expected = max(_int(source["expected_pages"], 0), _int(merge["target_expected_pages_before"], 0))
    max_page = db.execute(
        "SELECT MAX(CAST(page AS INTEGER)) AS max_page FROM catalog_scans WHERE catalog_id = ?",
        (target_id,),
    ).fetchone()
    expected = max(expected, _int(max_page["max_page"], 0))
    db.execute(
        "UPDATE catalogs SET expected_pages = ?, updated_at = ? WHERE catalog_id = ?",
        (expected or None, stamp, target_id),
    )
    db.execute(
        "UPDATE catalogs SET status = ?, merged_into = ?, updated_at = ? WHERE catalog_id = ?",
        (CATALOG_MERGED, target_id, stamp, source_id),
    )

    totals = db.execute(
        "SELECT COUNT(*) AS moved, "
        "SUM(CASE WHEN s.status = 'DUPLICATE' THEN 1 ELSE 0 END) AS duplicates, "
        "SUM(CASE WHEN s.status = 'CONFLICT' THEN 1 ELSE 0 END) AS conflicts "
        "FROM catalog_migrations m JOIN catalog_scans s ON s.scan_id = m.scan_id "
        "WHERE m.merge_id = ?",
        (merge["merge_id"],),
    ).fetchone()
    conflict_records = db.execute(
        "SELECT COUNT(*) AS n FROM catalog_conflicts WHERE merge_id = ?", (merge["merge_id"],)
    ).fetchone()
    result = {
        "moved": _int(totals["moved"], 0),
        "duplicates": _int(totals["duplicates"], 0),
        "conflicts": _int(totals["conflicts"], 0),
        "conflict_records": _int(conflict_records["n"], 0),
        "target_scan_total": _int(
            db.execute(
                "SELECT COUNT(*) AS n FROM catalog_scans WHERE catalog_id = ?", (target_id,)
            ).fetchone()["n"],
            0,
        ),
        "target_expected_pages": expected,
    }
    db.execute(
        "UPDATE catalog_merges SET status = ?, completed_at = ?, "
        "target_expected_pages_after = ?, result = ? WHERE merge_id = ?",
        (MERGE_COMPLETED, stamp, expected, _canonical(result), merge["merge_id"]),
    )
    db.execute("COMMIT")
    return _merge_detail(db, merge["merge_id"])


def _resume_merge(db, merge: dict, max_migrations: int) -> dict:
    source = _catalog(db, merge["source_catalog_id"])
    target = _catalog(db, merge["target_catalog_id"])
    # Both versions were reserved when the merge started; a concurrent
    # registration bumps one of them and makes this retry stale.
    if int(source["version"]) != int(merge["source_version_after"]) or int(
        target["version"]
    ) != int(merge["target_version_after"]):
        raise DomainError("STALE_CATALOG", "目录版本已变化，不能继续陈旧的合并", 409)

    processed = 0
    while True:
        if max_migrations and processed >= max_migrations:
            return {**_merge_detail(db, merge["merge_id"]), "replayed": False, "paused": True}
        scan = db.execute(
            "SELECT s.* FROM catalog_scans s WHERE s.catalog_id = ? AND s.created_at <= ? "
            "AND NOT EXISTS (SELECT 1 FROM catalog_migrations m "
            "WHERE m.merge_id = ? AND m.scan_id = s.scan_id) "
            "ORDER BY s.original_catalog_id, s.line_no, s.scan_id LIMIT 1",
            (merge["source_catalog_id"], merge["created_at"], merge["merge_id"]),
        ).fetchone()
        if scan is None:
            break
        try:
            db.execute("BEGIN IMMEDIATE")
            _migrate_one_scan(db, merge, scan, now_iso())
            db.execute("COMMIT")
        except Exception:
            _rollback(db)
            raise
        processed += 1

    try:
        db.execute("BEGIN IMMEDIATE")
        return _finalize_merge(db, merge)
    except Exception:
        _rollback(db)
        raise


def _completed_merge_for_pair(db, source_id: str, target_id: str) -> dict | None:
    return db.execute(
        "SELECT * FROM catalog_merges WHERE source_catalog_id = ? AND target_catalog_id = ? "
        "AND status = ? ORDER BY created_at DESC LIMIT 1",
        (source_id, target_id, MERGE_COMPLETED),
    ).fetchone()


def merge_catalogs(payload: dict | None = None) -> dict:
    """Merge ``source_catalog_id`` into ``target_catalog_id``.

    The call is resumable: repeating it (or passing the same ``merge_id``)
    after an interruption continues at the first scan without a migration
    ledger entry.  Repeating a completed merge replays it without new scans.
    """
    payload = dict(payload or {})
    ensure_schema()
    source_id = str(
        _first(payload, "source_catalog_id", "source", "from_catalog_id", "from_catalog", "from") or ""
    ).strip()
    target_id = str(
        _first(payload, "target_catalog_id", "target", "to_catalog_id", "to_catalog", "to") or ""
    ).strip()
    if not source_id or not target_id:
        raise DomainError("MISSING_CATALOG", "合并需要 source_catalog_id 与 target_catalog_id", 422)
    if source_id == target_id:
        raise DomainError("INVALID_MERGE", "不能把目录合并到自身", 422)

    basis_value = _first(payload, "basis", "mapping_basis", "evidence", default={})
    if not isinstance(basis_value, (dict, list, str)):
        basis_value = str(basis_value)
    basis = _canonical(basis_value)

    max_migrations = _int(_first(payload, "max_migrations", "limit", "chunk_size"), 0)
    expected_source = _require_version(payload, ("source_version", "expected_source_version", "from_version"))
    expected_target = _require_version(payload, ("target_version", "expected_target_version", "to_version"))
    client_merge_id = _first(payload, "merge_id")

    db = _connect()
    try:
        existing = None
        if client_merge_id:
            existing = db.execute(
                "SELECT * FROM catalog_merges WHERE merge_id = ?", (str(client_merge_id),)
            ).fetchone()
        if existing is None and not client_merge_id:
            existing = db.execute(
                "SELECT * FROM catalog_merges WHERE source_catalog_id = ? AND target_catalog_id = ? "
                "AND status = ? ORDER BY created_at DESC LIMIT 1",
                (source_id, target_id, MERGE_IN_PROGRESS),
            ).fetchone()

        if existing is not None:
            if existing["status"] in (MERGE_COMPLETED, MERGE_RETRACTED):
                return {**_merge_detail(db, existing["merge_id"]), "replayed": True}
            return _resume_merge(db, existing, max_migrations)

        # Reserve both versions before touching anything: open the write
        # transaction first, then read the catalog rows inside it.  A
        # concurrent registration either commits before this point (and is
        # seen here / caught by the version check) or blocks until the
        # reservation commits, after which it is rejected as in-progress.
        db.execute("BEGIN IMMEDIATE")
        source = _maybe_catalog(db, source_id)
        target = _maybe_catalog(db, target_id)
        if source is None or target is None:
            missing = source_id if source is None else target_id
            raise DomainError("CATALOG_NOT_FOUND", f"目录不存在: {missing}", 404)

        # A retry after completion must not create a second merge.
        if source["status"] == CATALOG_MERGED and source["merged_into"] == target_id:
            completed = _completed_merge_for_pair(db, source_id, target_id)
            if completed is not None:
                db.execute("COMMIT")
                return {**_merge_detail(db, completed["merge_id"]), "replayed": True}
        if source["status"] != CATALOG_ACTIVE or source["merged_into"]:
            raise DomainError("INVALID_MERGE", f"源目录不可合并: {source_id}", 422)
        if target["status"] != CATALOG_ACTIVE or target["merged_into"]:
            raise DomainError("INVALID_MERGE", f"目标目录不可合并: {target_id}", 422)
        busy = _in_progress_merge(db, source_id) or _in_progress_merge(db, target_id)
        if busy is not None:
            raise DomainError(
                "MERGE_IN_PROGRESS", f"目录存在未完成的合并 {busy['merge_id']}", 409
            )

        _check_version(source, expected_source, "源")
        _check_version(target, expected_target, "目标")

        merge_id = str(
            client_merge_id
            or _merge_id_for(source_id, target_id, source["version"], target["version"], basis)
        )
        if db.execute("SELECT 1 FROM catalog_merges WHERE merge_id = ?", (merge_id,)).fetchone():
            raise DomainError("DUPLICATE_MERGE", f"合并标识已存在: {merge_id}", 409)

        source_after = int(source["version"]) + 1
        target_after = int(target["version"]) + 1
        stamp = now_iso()
        auto_basis = {
            "source_catalog_id": source_id,
            "target_catalog_id": target_id,
            "source_issue": source["issue"],
            "source_edition": source["edition"],
            "target_issue": target["issue"],
            "target_edition": target["edition"],
            "source_scan_total": _int(
                db.execute(
                    "SELECT COUNT(*) AS n FROM catalog_scans WHERE catalog_id = ?", (source_id,)
                ).fetchone()["n"],
                0,
            ),
            "target_scan_total": _int(
                db.execute(
                    "SELECT COUNT(*) AS n FROM catalog_scans WHERE catalog_id = ?", (target_id,)
                ).fetchone()["n"],
                0,
            ),
            "requested_by": str(_first(payload, "operator", "requested_by", default="")),
        }
        merged_basis = {"request": _json(basis, basis_value), "facts": auto_basis}
        db.execute(
            "UPDATE catalogs SET version = ?, updated_at = ? WHERE catalog_id = ?",
            (source_after, stamp, source_id),
        )
        db.execute(
            "UPDATE catalogs SET version = ?, updated_at = ? WHERE catalog_id = ?",
            (target_after, stamp, target_id),
        )
        db.execute(
            "INSERT INTO catalog_merges "
            "(merge_id, source_catalog_id, target_catalog_id, status, source_version_before, "
            "target_version_before, source_version_after, target_version_after, "
            "source_expected_pages_before, target_expected_pages_before, basis, result, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, '{}', ?)",
            (
                merge_id,
                source_id,
                target_id,
                MERGE_IN_PROGRESS,
                int(source["version"]),
                int(target["version"]),
                source_after,
                target_after,
                source["expected_pages"],
                target["expected_pages"],
                _canonical(merged_basis),
                stamp,
            ),
        )
        db.execute("COMMIT")
        merge = _merge(db, merge_id)
        return _resume_merge(db, merge, max_migrations)
    except Exception:
        _rollback(db)
        raise
    finally:
        db.close()


# ---------------------------------------------------------------------------
# retraction


def _restore_conflict_after_move(db, move: dict, restored: set[str], stamp: str) -> dict:
    """Undo one conflict transfer made by the merge being retracted."""
    snapshot = _json(move["snapshot"], {})
    kind = move["kind"]
    detail = {"conflict_id": move["conflict_id"], "kind": kind, "restored": False}
    joined = _json(move["joined_scan_ids"], [])
    if kind == "ABSORBED":
        live = move["target_conflict_id"]
        target = db.execute(
            "SELECT * FROM catalog_conflicts WHERE conflict_id = ?", (live,)
        ).fetchone()
        if target is not None:
            # Start from the live record so scans that joined after the merge
            # stay held; only the scans this merge absorbed are removed.
            absorbed = [scan_id for scan_id in _json(move["absorbed_scan_ids"], []) if scan_id in restored]
            source_ids = [
                scan_id for scan_id in _json(target["source_scan_ids"], []) if scan_id not in absorbed
            ]
            db.execute(
                "UPDATE catalog_conflicts SET source_scan_ids = ? WHERE conflict_id = ?",
                (_canonical(source_ids), live),
            )
    # Scans that the merge caused to join a hold must not stay held by a
    # conflict record that is going away.
    for scan_id in joined:
        if scan_id in restored:
            continue
        row = db.execute("SELECT * FROM catalog_scans WHERE scan_id = ?", (scan_id,)).fetchone()
        live = move["target_conflict_id"] if kind == "ABSORBED" else move["conflict_id"]
        if row is not None and row["conflict_id"] == live:
            db.execute(
                "UPDATE catalog_scans SET status = ?, conflict_id = NULL WHERE scan_id = ?",
                (SCAN_ACTIVE, scan_id),
            )
    if not snapshot:
        return detail
    target_ids = [scan_id for scan_id in snapshot.get("target_scan_ids", []) if scan_id in restored]
    source_ids = [scan_id for scan_id in snapshot.get("source_scan_ids", []) if scan_id in restored]
    previous = {
        scan_id: value
        for scan_id, value in snapshot.get("previous_target_status", {}).items()
        if scan_id in restored
    }
    status = CONFLICT_PENDING if (target_ids or source_ids) else CONFLICT_CANCELLED
    db.execute(
        "UPDATE catalog_conflicts SET catalog_id = ?, target_scan_ids = ?, source_scan_ids = ?, "
        "previous_target_status = ?, status = ?, resolved_at = ? WHERE conflict_id = ?",
        (
            move["from_catalog_id"],
            _canonical(target_ids),
            _canonical(source_ids),
            _canonical(previous),
            status,
            None if status == CONFLICT_PENDING else stamp,
            move["conflict_id"],
        ),
    )
    detail["restored"] = True
    detail["status"] = status
    _release_orphan_holds(db, move["conflict_id"], target_ids + source_ids)
    _settle_conflict(db, move["conflict_id"], stamp)
    if kind == "ABSORBED" and move["target_conflict_id"]:
        _settle_conflict(db, move["target_conflict_id"], stamp)
    return detail


def _release_orphan_holds(db, conflict_id: str, keep: list[str]) -> list[str]:
    """Release scans still pointing at a conflict that no longer lists them.

    Later registrations can join a pending conflict.  When a retraction takes
    that conflict back to its old identity, those later scans must not stay
    stuck in ``CONFLICT`` against a record that no longer contains them.
    """
    keep_set = set(keep)
    released = []
    for row in db.execute(
        "SELECT * FROM catalog_scans WHERE conflict_id = ? ORDER BY scan_id", (conflict_id,)
    ).fetchall():
        if row["scan_id"] in keep_set:
            continue
        db.execute(
            "UPDATE catalog_scans SET status = ?, conflict_id = NULL WHERE scan_id = ?",
            (SCAN_ACTIVE, row["scan_id"]),
        )
        released.append(row["scan_id"])
    return released


def _settle_conflict(db, conflict_id: str, stamp: str) -> dict | None:
    """Re-check a pending conflict after some of its scans were detached."""
    row = db.execute(
        "SELECT * FROM catalog_conflicts WHERE conflict_id = ?", (conflict_id,)
    ).fetchone()
    if row is None or row["status"] != CONFLICT_PENDING:
        return None
    return _settle_hold(db, row["catalog_id"], row["business_key"], conflict_id, stamp)


def _settle_hold(db, catalog_id: str, business_key: str, conflict_id: str, stamp: str) -> dict:
    """Leave a page consistent after some of its holds were released.

    * 0 or 1 held scan left -> release it and cancel the conflict.
    * 2+ held scans left -> keep them held under a pending conflict, so a
      retraction never silently drops or clears a real collision.
    """
    rows = db.execute(
        "SELECT * FROM catalog_scans WHERE catalog_id = ? AND business_key = ? "
        "AND conflict_id = ? AND status = ? ORDER BY scan_id",
        (catalog_id, business_key, conflict_id, SCAN_CONFLICT),
    ).fetchall()
    if len(rows) <= 1:
        for row in rows:
            db.execute(
                "UPDATE catalog_scans SET status = ?, conflict_id = NULL WHERE scan_id = ?",
                (SCAN_ACTIVE, row["scan_id"]),
            )
        conflict = db.execute(
            "SELECT * FROM catalog_conflicts WHERE conflict_id = ?", (conflict_id,)
        ).fetchone()
        if conflict is not None and conflict["status"] == CONFLICT_PENDING:
            db.execute(
                "UPDATE catalog_conflicts SET status = ?, resolved_at = ? WHERE conflict_id = ?",
                (CONFLICT_CANCELLED, stamp, conflict_id),
            )
        return {"conflict_id": conflict_id, "status": CONFLICT_CANCELLED, "held": len(rows)}
    ids = [row["scan_id"] for row in rows]
    db.execute(
        "UPDATE catalog_conflicts SET target_scan_ids = ?, source_scan_ids = '[]', "
        "previous_target_status = ? WHERE conflict_id = ?",
        (
            _canonical(ids),
            _canonical({scan_id: {"status": SCAN_ACTIVE, "conflict_id": None} for scan_id in ids}),
            conflict_id,
        ),
    )
    return {"conflict_id": conflict_id, "status": CONFLICT_PENDING, "held": len(ids)}


def _rehold_page(db, catalog_id: str, business_key: str, stamp: str) -> str | None:
    """Open a hold when a page is left with two genuinely different scans."""
    pending = _pending_conflicts(db, catalog_id, business_key)
    if pending:
        return pending[0]["conflict_id"]
    active = db.execute(
        "SELECT * FROM catalog_scans WHERE catalog_id = ? AND business_key = ? AND status = ? "
        "ORDER BY scan_id",
        (catalog_id, business_key, SCAN_ACTIVE),
    ).fetchall()
    if len(active) < 2:
        return None
    if len({scan["sha256"] for scan in active if scan["sha256"]}) == 1 and all(
        scan["sha256"] for scan in active
    ):
        first = active[0]
        for scan in active[1:]:
            db.execute(
                "UPDATE catalog_scans SET status = ?, duplicate_of = ? WHERE scan_id = ?",
                (SCAN_DUPLICATE, first["scan_id"], scan["scan_id"]),
            )
        return None
    conflict_id = _conflict_id_for(None, catalog_id, business_key, stamp)
    db.execute(
        "INSERT INTO catalog_conflicts "
        "(conflict_id, merge_id, catalog_id, business_key, page, target_scan_ids, source_scan_ids, "
        "previous_target_status, status, created_at) VALUES (?, NULL, ?, ?, ?, ?, '[]', ?, ?, ?)",
        (
            conflict_id,
            catalog_id,
            business_key,
            active[0]["page"],
            _canonical([scan["scan_id"] for scan in active]),
            _canonical({scan["scan_id"]: {"status": SCAN_ACTIVE, "conflict_id": None} for scan in active}),
            CONFLICT_PENDING,
            stamp,
        ),
    )
    for scan in active:
        db.execute(
            "UPDATE catalog_scans SET status = ?, conflict_id = ? WHERE scan_id = ?",
            (SCAN_CONFLICT, conflict_id, scan["scan_id"]),
        )
    return conflict_id


def _detach_from_conflict(db, conflict: dict, scan_id: str) -> dict:
    """Remove one scan from a pending conflict record owned by another merge."""
    target_ids = [item for item in conflict["target_scan_ids"] if item != scan_id]
    source_ids = [item for item in conflict["source_scan_ids"] if item != scan_id]
    previous = dict(conflict["previous_target_status"])
    previous.pop(scan_id, None)
    db.execute(
        "UPDATE catalog_conflicts SET target_scan_ids = ?, source_scan_ids = ?, "
        "previous_target_status = ? WHERE conflict_id = ?",
        (_canonical(target_ids), _canonical(source_ids), _canonical(previous), conflict["conflict_id"]),
    )
    return _conflict_dict(db, conflict["conflict_id"])


def retract_merge(payload: dict | None = None) -> dict:
    """Undo exactly the ownership moves made by one merge.

    Scans that joined later, or that a later merge moved onward, are never
    touched.  Scans whose same-page conflict was already adjudicated stay where
    the adjudication put them; a pending conflict created by this merge is
    cancelled and its target-side statuses restored.
    """
    payload = dict(payload or {})
    ensure_schema()
    merge_id = _first(payload, "merge_id")
    if not merge_id:
        raise DomainError("MISSING_MERGE", "撤回需要 merge_id", 422)
    merge_id = str(merge_id)

    db = _connect()
    try:
        merge = _merge(db, merge_id)
        if merge["status"] == MERGE_RETRACTED:
            return {**_merge_detail(db, merge_id), "replayed": True}
        if merge["status"] != MERGE_COMPLETED:
            raise DomainError("MERGE_NOT_COMPLETED", "只能撤回已完成的合并", 409)

        source_id = merge["source_catalog_id"]
        target_id = merge["target_catalog_id"]
        db.execute("BEGIN IMMEDIATE")
        source = _catalog(db, source_id)
        target = _catalog(db, target_id)
        expected_source = _require_version(payload, ("source_version", "expected_source_version", "from_version"))
        expected_target = _require_version(payload, ("target_version", "expected_target_version", "to_version"))
        # Default to the versions the merge produced: any later registration,
        # adjudication or merge bumps a version, so retraction based on the
        # post-merge view is rejected as stale.  Re-reading the current
        # versions and passing them deliberately retracts while keeping the
        # scans that arrived later untouched.
        if expected_source is None:
            expected_source = int(merge["source_version_after"])
        if expected_target is None:
            expected_target = int(merge["target_version_after"])
        if target["status"] == CATALOG_MERGED:
            raise DomainError(
                "MERGE_CHAINED",
                f"目标目录已并入 {target['merged_into']}，请先撤回后续合并",
                409,
            )
        _check_version(source, expected_source, "源")
        _check_version(target, expected_target, "目标")
        if source["status"] != CATALOG_MERGED or source["merged_into"] != target_id:
            raise DomainError("INVALID_RETRACT", "源目录不是本次合并产生的状态", 409)

        stamp = now_iso()
        migrations = db.execute(
            "SELECT m.*, s.catalog_id AS current_catalog, s.conflict_id AS current_conflict, "
            "s.status AS current_status, s.duplicate_of AS current_duplicate "
            "FROM catalog_migrations m JOIN catalog_scans s ON s.scan_id = m.scan_id "
            "WHERE m.merge_id = ? ORDER BY m.scan_id",
            (merge_id,),
        ).fetchall()
        restored: list[str] = []
        skipped_later: list[str] = []
        preserved_decisions: list[str] = []
        detached: list[dict] = []
        for item in migrations:
            if item["current_catalog"] != target_id:
                skipped_later.append(item["scan_id"])
                continue
            status = item["previous_status"]
            duplicate_of = item["previous_duplicate_of"]
            # A later adjudication may have superseded this scan.  Ownership
            # still returns to the source (this merge is what moved it), but
            # the human verdict survives the retraction.
            if item["current_status"] == SCAN_SUPERSEDED and status != SCAN_SUPERSEDED:
                status = SCAN_SUPERSEDED
                duplicate_of = item["current_duplicate"]
                preserved_decisions.append(item["scan_id"])
            # A pending conflict owned by an earlier/later merge must forget
            # this scan; otherwise it would keep a reference to a scan that is
            # back in the source catalog.
            if item["current_conflict"]:
                holder = _conflict_dict(db, item["current_conflict"])
                if holder["merge_id"] != merge_id and holder["status"] == CONFLICT_PENDING:
                    detached.append(_detach_from_conflict(db, holder, item["scan_id"]))
            db.execute(
                "UPDATE catalog_scans SET catalog_id = ?, status = ?, conflict_id = ?, duplicate_of = ? "
                "WHERE scan_id = ?",
                (
                    item["from_catalog_id"],
                    status,
                    item["previous_conflict_id"],
                    duplicate_of,
                    item["scan_id"],
                ),
            )
            restored.append(item["scan_id"])
        restored_set = set(restored)

        cancelled = []
        touched_keys: set[tuple[str, str]] = set()
        for row in db.execute(
            "SELECT * FROM catalog_conflicts WHERE merge_id = ? AND status = ? ORDER BY created_at",
            (merge_id, CONFLICT_PENDING),
        ).fetchall():
            conflict = _conflict_dict(db, row["conflict_id"])
            restored_targets = []
            for scan_id, prev in conflict["previous_target_status"].items():
                target_scan = db.execute("SELECT * FROM catalog_scans WHERE scan_id = ?", (scan_id,)).fetchone()
                if target_scan is None:
                    continue
                if target_scan["catalog_id"] != conflict["catalog_id"]:
                    continue
                if target_scan["conflict_id"] != conflict["conflict_id"]:
                    continue
                db.execute(
                    "UPDATE catalog_scans SET status = ?, conflict_id = ? WHERE scan_id = ?",
                    (prev.get("status", SCAN_ACTIVE), prev.get("conflict_id"), scan_id),
                )
                restored_targets.append(scan_id)
            # Scans that joined this hold after the merge (typically a later
            # registration) are not part of the retraction; release them so the
            # page-level check below can hold the page again if needed.
            released = []
            for scan in db.execute(
                "SELECT * FROM catalog_scans WHERE conflict_id = ? AND catalog_id = ?",
                (conflict["conflict_id"], conflict["catalog_id"]),
            ).fetchall():
                if scan["scan_id"] in restored_targets:
                    continue
                if scan["scan_id"] in restored_set:
                    continue
                db.execute(
                    "UPDATE catalog_scans SET status = ?, conflict_id = NULL WHERE scan_id = ?",
                    (SCAN_ACTIVE, scan["scan_id"]),
                )
                released.append(scan["scan_id"])
            db.execute(
                "UPDATE catalog_conflicts SET status = ?, resolved_at = ? WHERE conflict_id = ?",
                (CONFLICT_CANCELLED, stamp, conflict["conflict_id"]),
            )
            touched_keys.add((conflict["catalog_id"], conflict["business_key"]))
            cancelled.append(
                {
                    "conflict_id": conflict["conflict_id"],
                    "restored_target_scans": restored_targets,
                    "released_later_scans": released,
                }
            )

        conflict_moves = []
        for move in db.execute(
            "SELECT * FROM catalog_conflict_moves WHERE merge_id = ? ORDER BY created_at", (merge_id,)
        ).fetchall():
            conflict_moves.append(_restore_conflict_after_move(db, move, restored_set, stamp))
            moved = db.execute(
                "SELECT * FROM catalog_conflicts WHERE conflict_id = ?", (move["conflict_id"],)
            ).fetchone()
            if moved is not None:
                touched_keys.add((moved["catalog_id"], moved["business_key"]))
                touched_keys.add((move["to_catalog_id"], moved["business_key"]))

        for catalog_id, business_key in sorted(touched_keys):
            if not business_key:
                continue
            _rehold_page(db, catalog_id, business_key, stamp)

        if _int(target["expected_pages"], 0) == _int(merge["target_expected_pages_after"], 0):
            db.execute(
                "UPDATE catalogs SET expected_pages = ?, updated_at = ? WHERE catalog_id = ?",
                (merge["target_expected_pages_before"], stamp, target_id),
            )

        db.execute(
            "UPDATE catalogs SET status = ?, merged_into = NULL, updated_at = ? WHERE catalog_id = ?",
            (CATALOG_ACTIVE, stamp, source_id),
        )
        source_after = _bump(db, source_id)
        target_after = _bump(db, target_id)
        result = dict(_json(merge["result"], {}))
        result.update(
            {
                "retracted": True,
                "restored": len(restored),
                "restored_scan_ids": restored,
                "skipped_moved_later": skipped_later,
                "preserved_later_decisions": preserved_decisions,
                "detached_from_foreign_conflicts": detached,
                "cancelled_conflicts": cancelled,
                "restored_conflict_moves": conflict_moves,
            }
        )
        db.execute(
            "UPDATE catalog_merges SET status = ?, undone_at = ?, source_version_retracted = ?, "
            "target_version_retracted = ?, result = ? WHERE merge_id = ?",
            (MERGE_RETRACTED, stamp, source_after, target_after, _canonical(result), merge_id),
        )
        db.execute("COMMIT")
        return {**_merge_detail(db, merge_id), "replayed": False}
    except Exception:
        _rollback(db)
        raise
    finally:
        db.close()


# ---------------------------------------------------------------------------
# conflict adjudication


def resolve_conflict(conflict_id: str, payload: dict | None = None) -> dict:
    payload = dict(payload or {})
    ensure_schema()
    value = _first(payload, "resolution", "decision", "choice")
    if value is None:
        raise DomainError("BAD_RESOLUTION", "需要 resolution: KEEP_TARGET/KEEP_SOURCE/KEEP_BOTH", 422)
    resolution = str(value).strip().upper()
    if resolution not in RESOLUTIONS:
        raise DomainError("BAD_RESOLUTION", "resolution 需为 KEEP_TARGET / KEEP_SOURCE / KEEP_BOTH", 422)

    db = _connect()
    try:
        db.execute("BEGIN IMMEDIATE")
        conflict = _conflict_dict(db, conflict_id)
        if conflict["status"] != CONFLICT_PENDING:
            raise DomainError("CONFLICT_RESOLVED", f"冲突已处理: {conflict['status']}", 409)
        catalog = _catalog(db, conflict["catalog_id"])
        expected = _require_version(payload, ("expected_version", "version", "catalog_version"))
        _check_version(catalog, expected, "目录")

        target_ids = conflict["target_scan_ids"]
        source_ids = conflict["source_scan_ids"]
        if resolution == "KEEP_TARGET":
            keep, drop = target_ids, source_ids
        elif resolution == "KEEP_SOURCE":
            keep, drop = source_ids, target_ids
        else:
            keep, drop = target_ids + source_ids, []

        stamp = now_iso()
        for scan_id in keep:
            db.execute(
                "UPDATE catalog_scans SET status = ?, conflict_id = NULL WHERE scan_id = ?",
                (SCAN_ACTIVE, scan_id),
            )
        for scan_id in drop:
            db.execute(
                "UPDATE catalog_scans SET status = ?, conflict_id = NULL WHERE scan_id = ?",
                (SCAN_SUPERSEDED, scan_id),
            )
        db.execute(
            "UPDATE catalog_conflicts SET status = ?, resolution = ?, note = ?, resolved_by = ?, "
            "resolved_at = ? WHERE conflict_id = ?",
            (
                CONFLICT_RESOLVED,
                resolution,
                str(_first(payload, "note", "reason", default="")),
                str(_first(payload, "resolved_by", "operator", default="")),
                stamp,
                conflict_id,
            ),
        )
        version = _bump(db, conflict["catalog_id"])
        db.execute("COMMIT")
        return {
            "conflict": _conflict_dict(db, conflict_id),
            "resolution": resolution,
            "catalog_version": version,
        }
    except Exception:
        _rollback(db)
        raise
    finally:
        db.close()


# ---------------------------------------------------------------------------
# queries


def _merge_detail(db, merge_id: str) -> dict:
    merge = _merge(db, merge_id)
    merge["basis"] = _json(merge["basis"], {})
    merge["result"] = _json(merge["result"], {})
    merge["migrations"] = db.execute(
        "SELECT m.*, s.original_catalog_id, s.original_key, s.original_business_key, "
        "s.business_key, s.status, s.catalog_id AS current_catalog_id "
        "FROM catalog_migrations m JOIN catalog_scans s ON s.scan_id = m.scan_id "
        "WHERE m.merge_id = ? ORDER BY m.scan_id",
        (merge_id,),
    ).fetchall()
    merge["migration_count"] = len(merge["migrations"])
    merge["conflicts"] = [
        _conflict_dict(db, row["conflict_id"])
        for row in db.execute(
            "SELECT conflict_id FROM catalog_conflicts WHERE merge_id = ? ORDER BY created_at", (merge_id,)
        ).fetchall()
    ]
    return merge


def list_merges(source_catalog_id: str | None = None, target_catalog_id: str | None = None,
                status: str | None = None) -> list[dict]:
    ensure_schema()
    db = _connect()
    try:
        query = "SELECT merge_id FROM catalog_merges WHERE 1=1"
        params: list = []
        if source_catalog_id:
            query += " AND source_catalog_id = ?"
            params.append(source_catalog_id)
        if target_catalog_id:
            query += " AND target_catalog_id = ?"
            params.append(target_catalog_id)
        if status:
            query += " AND status = ?"
            params.append(status)
        query += " ORDER BY created_at DESC"
        return [_merge_detail(db, row["merge_id"]) for row in db.execute(query, params).fetchall()]
    finally:
        db.close()


def get_merge(merge_id: str) -> dict:
    ensure_schema()
    db = _connect()
    try:
        return _merge_detail(db, merge_id)
    finally:
        db.close()


def _page_view(scans: list[dict], expected_pages: int | None) -> dict:
    pages = sorted({int(scan["page"]) for scan in scans if scan["status"] in PRESENT_STATUSES})
    present = set(pages)
    expected = _int(expected_pages, 0)
    if expected <= 0:
        expected = max(present) if present else 0
    expected_set = set(range(1, expected + 1)) if expected > 0 else set()
    active_by_page: dict[int, int] = {}
    for scan in scans:
        if scan["status"] == SCAN_ACTIVE:
            page = int(scan["page"])
            active_by_page[page] = active_by_page.get(page, 0) + 1
    missing = sorted(expected_set - present)
    unexpected = sorted(present - expected_set)
    return {
        "expected_pages": expected,
        "page_numbers": pages,
        "page_count": len(pages),
        "missing_pages": [f"{page:03d}" for page in missing],
        "missing": missing,
        "missing_count": len(missing),
        "unexpected_pages": [f"{page:03d}" for page in unexpected],
        "duplicate_pages": [f"{page:03d}" for page, count in sorted(active_by_page.items()) if count > 1],
        "coverage": f"{len(present) / expected:.3f}" if expected else "0.000",
    }


def _account(db, catalog_id: str) -> dict:
    catalog = _catalog(db, catalog_id)
    scans = db.execute(
        "SELECT * FROM catalog_scans WHERE catalog_id = ? "
        "ORDER BY CAST(page AS INTEGER), scan_id",
        (catalog_id,),
    ).fetchall()
    status_counts = {status: 0 for status in (SCAN_ACTIVE, SCAN_DUPLICATE, SCAN_CONFLICT, SCAN_SUPERSEDED)}
    for scan in scans:
        status_counts[scan["status"]] = status_counts.get(scan["status"], 0) + 1
    origin: dict[str, int] = {}
    for scan in scans:
        origin[scan["original_catalog_id"]] = origin.get(scan["original_catalog_id"], 0) + 1
    pending = [
        _conflict_dict(db, row["conflict_id"])
        for row in db.execute(
            "SELECT conflict_id FROM catalog_conflicts WHERE catalog_id = ? AND status = ? "
            "ORDER BY created_at",
            (catalog_id, CONFLICT_PENDING),
        ).fetchall()
    ]
    registrations = db.execute(
        "SELECT * FROM catalog_registrations WHERE catalog_id = ? ORDER BY created_at", (catalog_id,)
    ).fetchall()
    pages = _page_view(scans, _int(catalog["expected_pages"], 0))
    return {
        "catalog_id": catalog_id,
        "catalog": catalog,
        "total_scans": len(scans),
        "active": status_counts.get(SCAN_ACTIVE, 0),
        "duplicates": status_counts.get(SCAN_DUPLICATE, 0),
        "conflicts": status_counts.get(SCAN_CONFLICT, 0),
        "superseded": status_counts.get(SCAN_SUPERSEDED, 0),
        "status_counts": status_counts,
        "pending_conflicts": pending,
        "pending_conflict_count": len(pending),
        "registrations": registrations,
        "registration_count": len(registrations),
        "original_catalogs": origin,
        "original_catalog_count": len(origin),
        **pages,
        "complete": not pages["missing"] and not pages["unexpected_pages"] and not pending,
        "balanced": len(scans) == sum(status_counts.values()),
    }


def catalog_account(catalog_id: str) -> dict:
    ensure_schema()
    db = _connect()
    try:
        return _account(db, catalog_id)
    finally:
        db.close()


def catalog_detail(catalog_id: str) -> dict:
    ensure_schema()
    db = _connect()
    try:
        catalog = _catalog(db, catalog_id)
        merged_from = [
            _merge_detail(db, row["merge_id"])
            for row in db.execute(
                "SELECT merge_id FROM catalog_merges WHERE target_catalog_id = ? ORDER BY created_at",
                (catalog_id,),
            ).fetchall()
        ]
        merged_into = [
            _merge_detail(db, row["merge_id"])
            for row in db.execute(
                "SELECT merge_id FROM catalog_merges WHERE source_catalog_id = ? ORDER BY created_at",
                (catalog_id,),
            ).fetchall()
        ]
        registrations = db.execute(
            "SELECT * FROM catalog_registrations WHERE catalog_id = ? ORDER BY created_at", (catalog_id,)
        ).fetchall()
        conflicts = [
            _conflict_dict(db, row["conflict_id"])
            for row in db.execute(
                "SELECT DISTINCT c.conflict_id FROM catalog_conflicts c "
                "LEFT JOIN catalog_scans s ON s.conflict_id = c.conflict_id "
                "WHERE c.catalog_id = ? OR s.original_catalog_id = ? ORDER BY c.created_at",
                (catalog_id, catalog_id),
            ).fetchall()
        ]
        return {
            **catalog,
            "account": _account(db, catalog_id),
            "registrations": registrations,
            "merged_from": merged_from,
            "merged_into": merged_into,
            "conflicts": conflicts,
        }
    finally:
        db.close()


def list_catalogs() -> list[dict]:
    ensure_schema()
    db = _connect()
    try:
        return db.execute("SELECT * FROM catalogs ORDER BY created_at, catalog_id").fetchall()
    finally:
        db.close()


def list_scans(catalog_id: str, view: str = "current") -> dict:
    """List scans by current ownership (default) or by original identity."""
    ensure_schema()
    db = _connect()
    try:
        _catalog(db, catalog_id)
        if view == "original":
            rows = db.execute(
                "SELECT * FROM catalog_scans WHERE original_catalog_id = ? "
                "ORDER BY CAST(page AS INTEGER), line_no, scan_id",
                (catalog_id,),
            ).fetchall()
        else:
            rows = db.execute(
                "SELECT * FROM catalog_scans WHERE catalog_id = ? "
                "ORDER BY CAST(page AS INTEGER), line_no, scan_id",
                (catalog_id,),
            ).fetchall()
        return {"catalog_id": catalog_id, "view": view, "total": len(rows), "scans": rows}
    finally:
        db.close()


def list_quarantine(catalog_id: str) -> dict:
    ensure_schema()
    db = _connect()
    try:
        _catalog(db, catalog_id)
        rows = db.execute(
            "SELECT * FROM catalog_quarantine WHERE catalog_id = ? ORDER BY registration_id, line_no",
            (catalog_id,),
        ).fetchall()
        distribution: dict[str, int] = {}
        for row in rows:
            distribution[row["reason_code"]] = distribution.get(row["reason_code"], 0) + 1
        return {
            "catalog_id": catalog_id,
            "total": len(rows),
            "reason_distribution": distribution,
            "rows": rows,
        }
    finally:
        db.close()


def list_conflicts(catalog_id: str | None = None, status: str | None = None) -> list[dict]:
    ensure_schema()
    db = _connect()
    try:
        query = "SELECT conflict_id FROM catalog_conflicts WHERE 1=1"
        params: list = []
        if catalog_id:
            query += " AND catalog_id = ?"
            params.append(catalog_id)
        if status:
            query += " AND status = ?"
            params.append(status)
        query += " ORDER BY created_at"
        return [_conflict_dict(db, row["conflict_id"]) for row in db.execute(query, params).fetchall()]
    finally:
        db.close()


def get_conflict(conflict_id: str) -> dict:
    ensure_schema()
    db = _connect()
    try:
        return _conflict_dict(db, conflict_id)
    finally:
        db.close()


def verify_credential(payload: dict | None = None) -> dict:
    """Resolve a credential issued against an original catalog identity.

    The lookup uses ``original_catalog_id`` plus the original business key (or
    original file key), never the current owner, so a merge cannot break an
    existing acceptance reference.  The reply shows the current owner, the
    merge that moved it and that merge's mapping basis.
    """
    payload = dict(payload or {})
    ensure_schema()
    catalog_id = _first(payload, "catalog_id", "original_catalog_id", "directory_id")
    if not catalog_id:
        raise DomainError("MISSING_CATALOG", "核验需要原始 catalog_id", 422)
    catalog_id = str(catalog_id)
    business_key = _first(payload, "business_key", "original_business_key")
    original_key = _first(payload, "original_key", "file_key")
    if not business_key and not original_key:
        issue = _first(payload, "issue")
        edition = _first(payload, "edition")
        page = _first(payload, "page")
        if issue and page:
            try:
                issue = normalize_issue(issue)
                edition = normalize_edition(edition) if edition else ""
                page = normalize_page(page)
            except (TypeError, ValueError) as exc:
                raise DomainError("BAD_KEY", "凭据业务键格式不正确", 422) from exc
            business_key = "|".join((issue, edition, page))
    if not business_key and not original_key:
        raise DomainError("MISSING_KEY", "核验需要 business_key 或 original_key", 422)

    db = _connect()
    try:
        known = _maybe_catalog(db, catalog_id)
        query = "SELECT * FROM catalog_scans WHERE original_catalog_id = ?"
        params: list = [catalog_id]
        if business_key:
            query += " AND original_business_key = ?"
            params.append(str(business_key))
        if original_key:
            query += " AND original_key = ?"
            params.append(str(original_key))
        query += " ORDER BY line_no, scan_id"
        scans = db.execute(query, params).fetchall()
        mapped = []
        for scan in scans:
            migration = db.execute(
                "SELECT m.*, g.basis FROM catalog_migrations m "
                "LEFT JOIN catalog_merges g ON g.merge_id = m.merge_id "
                "WHERE m.scan_id = ? ORDER BY m.migrated_at DESC, m.merge_id LIMIT 1",
                (scan["scan_id"],),
            ).fetchone()
            mapped.append(
                {
                    "scan_id": scan["scan_id"],
                    "original_catalog_id": scan["original_catalog_id"],
                    "original_key": scan["original_key"],
                    "original_business_key": scan["original_business_key"],
                    "current_catalog_id": scan["catalog_id"],
                    "status": scan["status"],
                    "moved": scan["catalog_id"] != scan["original_catalog_id"],
                    "merge_id": migration["merge_id"] if migration else None,
                    "mapping_basis": _json(migration["basis"], None) if migration else None,
                    "current_business_key": scan["business_key"],
                }
            )
        return {
            "catalog_id": catalog_id,
            "catalog_known": known is not None,
            "business_key": business_key,
            "original_key": original_key,
            "verified": bool(scans),
            "exists": bool(scans),
            "moved": any(item["moved"] for item in mapped),
            "current_catalog_ids": sorted({item["current_catalog_id"] for item in mapped}),
            "scans": mapped,
        }
    finally:
        db.close()


# Convenience aliases used by callers that prefer short names.
def register(payload: dict | None = None) -> dict:
    return register_catalog(payload)


def merge(payload: dict | None = None) -> dict:
    return merge_catalogs(payload)


def retract(payload: dict | None = None) -> dict:
    return retract_merge(payload)
