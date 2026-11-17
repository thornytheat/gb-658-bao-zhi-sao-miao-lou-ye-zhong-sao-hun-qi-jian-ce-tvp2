"""SQLite schema for catalog registration / merge / retraction.

Two identities are deliberately kept apart:

* ``catalog_scans.catalog_id`` is the *current owner* and changes when catalogs
  are merged or a merge is retracted.
* ``original_catalog_id`` / ``original_key`` / ``original_business_key`` never
  change.  Acceptance credentials issued against an old catalog therefore keep
  verifying after a merge without rewriting any key.

``catalog_migrations`` is the ledger that makes a merge resumable and a
retraction scoped: only scans actually moved by that merge are restored, and a
retry never inserts a scan twice.
"""

from app.store import connect

DDL = """
CREATE TABLE IF NOT EXISTS catalogs (
    catalog_id      TEXT PRIMARY KEY,
    label           TEXT NOT NULL DEFAULT '',
    issue           TEXT NOT NULL DEFAULT '',
    edition         TEXT NOT NULL DEFAULT '',
    status          TEXT NOT NULL DEFAULT 'ACTIVE',
    version         INTEGER NOT NULL DEFAULT 1,
    merged_into     TEXT,
    registration_id TEXT,
    expected_pages  INTEGER,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS catalog_registrations (
    registration_id TEXT PRIMARY KEY,
    catalog_id      TEXT NOT NULL,
    content_sha256  TEXT NOT NULL,
    input_bytes     INTEGER NOT NULL DEFAULT 0,
    accepted        INTEGER NOT NULL DEFAULT 0,
    quarantined     INTEGER NOT NULL DEFAULT 0,
    tail_complete   INTEGER NOT NULL DEFAULT 1,
    created_at      TEXT NOT NULL,
    UNIQUE (catalog_id, content_sha256)
);
CREATE INDEX IF NOT EXISTS idx_catalog_registrations_catalog
    ON catalog_registrations (catalog_id, created_at);

CREATE TABLE IF NOT EXISTS catalog_scans (
    scan_id               TEXT PRIMARY KEY,
    catalog_id            TEXT NOT NULL,
    registration_id       TEXT NOT NULL DEFAULT '',
    original_catalog_id   TEXT NOT NULL,
    original_key          TEXT NOT NULL,
    original_business_key TEXT NOT NULL,
    business_key          TEXT NOT NULL,
    issue                 TEXT NOT NULL,
    edition               TEXT NOT NULL,
    page                  TEXT NOT NULL,
    file_name             TEXT NOT NULL DEFAULT '',
    sha256                TEXT NOT NULL DEFAULT '',
    phash                 TEXT NOT NULL DEFAULT '',
    clarity               INTEGER NOT NULL DEFAULT 0,
    header_issue          TEXT,
    line_no               INTEGER NOT NULL DEFAULT 0,
    raw_row               TEXT NOT NULL DEFAULT '',
    status                TEXT NOT NULL DEFAULT 'ACTIVE',
    conflict_id           TEXT,
    duplicate_of          TEXT,
    created_at            TEXT NOT NULL,
    UNIQUE (original_catalog_id, original_key)
);
CREATE INDEX IF NOT EXISTS idx_catalog_scans_owner
    ON catalog_scans (catalog_id, business_key);
CREATE INDEX IF NOT EXISTS idx_catalog_scans_original
    ON catalog_scans (original_catalog_id, business_key);
CREATE INDEX IF NOT EXISTS idx_catalog_scans_sha
    ON catalog_scans (catalog_id, business_key, sha256);

CREATE TABLE IF NOT EXISTS catalog_quarantine (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    catalog_id      TEXT NOT NULL,
    registration_id TEXT NOT NULL,
    line_no         INTEGER,
    reason_code     TEXT NOT NULL,
    raw_value       TEXT,
    created_at      TEXT NOT NULL,
    UNIQUE (registration_id, line_no, reason_code)
);
CREATE INDEX IF NOT EXISTS idx_catalog_quarantine_catalog
    ON catalog_quarantine (catalog_id);

CREATE TABLE IF NOT EXISTS catalog_merges (
    merge_id                     TEXT PRIMARY KEY,
    source_catalog_id            TEXT NOT NULL,
    target_catalog_id            TEXT NOT NULL,
    status                       TEXT NOT NULL,
    source_version_before        INTEGER NOT NULL,
    target_version_before        INTEGER NOT NULL,
    source_version_after         INTEGER,
    target_version_after         INTEGER,
    source_expected_pages_before INTEGER,
    target_expected_pages_before INTEGER,
    target_expected_pages_after  INTEGER,
    basis                        TEXT NOT NULL DEFAULT '{}',
    result                       TEXT NOT NULL DEFAULT '{}',
    source_version_retracted     INTEGER,
    target_version_retracted     INTEGER,
    created_at                   TEXT NOT NULL,
    completed_at                 TEXT,
    undone_at                    TEXT
);
CREATE INDEX IF NOT EXISTS idx_catalog_merges_pair
    ON catalog_merges (source_catalog_id, target_catalog_id, status);

CREATE TABLE IF NOT EXISTS catalog_migrations (
    merge_id              TEXT NOT NULL,
    scan_id               TEXT NOT NULL,
    from_catalog_id       TEXT NOT NULL,
    to_catalog_id         TEXT NOT NULL,
    previous_status       TEXT NOT NULL DEFAULT 'ACTIVE',
    previous_conflict_id  TEXT,
    previous_duplicate_of TEXT,
    migrated_at           TEXT NOT NULL,
    PRIMARY KEY (merge_id, scan_id)
);
CREATE INDEX IF NOT EXISTS idx_catalog_migrations_target
    ON catalog_migrations (to_catalog_id, scan_id);

CREATE TABLE IF NOT EXISTS catalog_conflicts (
    conflict_id            TEXT PRIMARY KEY,
    merge_id               TEXT,
    catalog_id             TEXT NOT NULL,
    business_key           TEXT NOT NULL,
    page                   TEXT,
    target_scan_ids        TEXT NOT NULL DEFAULT '[]',
    source_scan_ids        TEXT NOT NULL DEFAULT '[]',
    previous_target_status TEXT NOT NULL DEFAULT '{}',
    status                 TEXT NOT NULL DEFAULT 'PENDING',
    resolution             TEXT,
    note                   TEXT,
    resolved_by            TEXT,
    created_at             TEXT NOT NULL,
    resolved_at            TEXT
);
CREATE INDEX IF NOT EXISTS idx_catalog_conflicts_merge
    ON catalog_conflicts (merge_id, status);
CREATE INDEX IF NOT EXISTS idx_catalog_conflicts_key
    ON catalog_conflicts (catalog_id, business_key, status);

CREATE TABLE IF NOT EXISTS catalog_conflict_moves (
    merge_id           TEXT NOT NULL,
    conflict_id        TEXT NOT NULL,
    from_catalog_id    TEXT NOT NULL,
    to_catalog_id      TEXT NOT NULL,
    kind               TEXT NOT NULL,
    target_conflict_id TEXT,
    absorbed_scan_ids  TEXT NOT NULL DEFAULT '[]',
    joined_scan_ids    TEXT NOT NULL DEFAULT '[]',
    snapshot           TEXT NOT NULL DEFAULT '{}',
    created_at         TEXT NOT NULL,
    PRIMARY KEY (merge_id, conflict_id)
);
"""


def ensure_schema() -> None:
    """Create catalog tables on the currently configured database path."""
    with connect() as db:
        db.executescript(DDL)
