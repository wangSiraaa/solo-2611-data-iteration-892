"""SQLite connection, schema and transaction helpers."""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

SCHEMA_VERSION = 2

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS volumes (
    volume_id TEXT PRIMARY KEY,
    title     TEXT
);

CREATE TABLE IF NOT EXISTS pages (
    volume_id  TEXT NOT NULL REFERENCES volumes(volume_id) ON DELETE CASCADE,
    page_id    TEXT NOT NULL,
    section_id TEXT NOT NULL,
    label      TEXT NOT NULL,              -- display page number, always TEXT
    active     INTEGER NOT NULL CHECK (active IN (0, 1)),
    seq        INTEGER NOT NULL,           -- position in the physical order
    PRIMARY KEY (volume_id, page_id),
    UNIQUE (volume_id, seq)
);

CREATE INDEX IF NOT EXISTS pages_lookup
    ON pages (volume_id, section_id, label, active);
CREATE INDEX IF NOT EXISTS pages_seq
    ON pages (volume_id, seq);

CREATE TABLE IF NOT EXISTS citations (
    citation_id      TEXT PRIMARY KEY,
    text             TEXT NOT NULL,
    kind             TEXT NOT NULL CHECK (kind IN ('PINNED', 'RANGE')),
    volume_id        TEXT NOT NULL REFERENCES volumes(volume_id),
    position         INTEGER NOT NULL UNIQUE, -- stable export order
    created_revision INTEGER NOT NULL,
    origin_json      TEXT
);

CREATE TABLE IF NOT EXISTS pinned_members (
    citation_id TEXT NOT NULL REFERENCES citations(citation_id) ON DELETE CASCADE,
    position    INTEGER NOT NULL,
    volume_id   TEXT NOT NULL,
    page_id     TEXT NOT NULL,
    PRIMARY KEY (citation_id, position),
    FOREIGN KEY (volume_id, page_id)
        REFERENCES pages(volume_id, page_id)
);

-- Reverse lookup of pinned members is an indexed identity lookup.
CREATE INDEX IF NOT EXISTS pinned_members_page
    ON pinned_members (volume_id, page_id);
CREATE INDEX IF NOT EXISTS pinned_members_citation
    ON pinned_members (citation_id, position);

CREATE TABLE IF NOT EXISTS range_endpoints (
    citation_id    TEXT PRIMARY KEY REFERENCES citations(citation_id) ON DELETE CASCADE,
    volume_id      TEXT NOT NULL,
    start_page_id  TEXT NOT NULL,
    end_page_id    TEXT NOT NULL,
    FOREIGN KEY (volume_id, start_page_id)
        REFERENCES pages(volume_id, page_id),
    FOREIGN KEY (volume_id, end_page_id)
        REFERENCES pages(volume_id, page_id)
);

CREATE INDEX IF NOT EXISTS range_endpoints_start
    ON range_endpoints (volume_id, start_page_id);
CREATE INDEX IF NOT EXISTS range_endpoints_end
    ON range_endpoints (volume_id, end_page_id);

CREATE TABLE IF NOT EXISTS operation_log (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    request_index    INTEGER,
    batch_id         TEXT,
    request_json     TEXT NOT NULL,
    status           TEXT NOT NULL,       -- applied | noop | rejected
    catalog_revision INTEGER,
    error_code       TEXT,
    error_detail     TEXT,
    expectation      TEXT,
    expectation_met  INTEGER,
    recorded_at      TEXT NOT NULL
);

-- ------------------------------------------------------------------
-- Rights editions (权属版) and their declarations.
--
-- A declaration version is the set of rows sharing
-- (edition_id, ..., effective_date).  Its shares are non-negative
-- rationals summing to exactly 1.  Lookups pick the newest version whose
-- effective_date is <= the publication date, so a later correction never
-- back-fills an older issue.
-- ------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS rights_editions (
    edition_id  TEXT PRIMARY KEY,
    description TEXT
);

CREATE TABLE IF NOT EXISTS page_right_shares (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    edition_id     TEXT NOT NULL REFERENCES rights_editions(edition_id),
    volume_id      TEXT NOT NULL,
    page_id        TEXT NOT NULL,
    effective_date TEXT NOT NULL,          -- ISO YYYY-MM-DD, inclusive
    account_id     TEXT NOT NULL,
    share_num      INTEGER NOT NULL CHECK (share_num >= 0),
    share_den      INTEGER NOT NULL CHECK (share_den > 0),
    UNIQUE (edition_id, volume_id, page_id, effective_date, account_id)
);
CREATE INDEX IF NOT EXISTS page_right_shares_lookup
    ON page_right_shares (edition_id, volume_id, page_id, effective_date);

CREATE TABLE IF NOT EXISTS account_allocations (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    edition_id     TEXT NOT NULL REFERENCES rights_editions(edition_id),
    account_id     TEXT NOT NULL,
    effective_date TEXT NOT NULL,          -- ISO YYYY-MM-DD, inclusive
    to_account_id  TEXT NOT NULL,
    share_num      INTEGER NOT NULL CHECK (share_num >= 0),
    share_den      INTEGER NOT NULL CHECK (share_den > 0),
    UNIQUE (edition_id, account_id, effective_date, to_account_id)
);
CREATE INDEX IF NOT EXISTS account_allocations_lookup
    ON account_allocations (edition_id, account_id, effective_date);

-- ------------------------------------------------------------------
-- Pricing groups (栏目计价组) and columns (栏目).
-- ------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS pricing_groups (
    group_id        TEXT PRIMARY KEY,
    per_page_cents  INTEGER NOT NULL CHECK (per_page_cents >= 0)
);

CREATE TABLE IF NOT EXISTS columns_table (
    column_id        TEXT PRIMARY KEY,
    pricing_group_id TEXT NOT NULL REFERENCES pricing_groups(group_id)
);

-- ------------------------------------------------------------------
-- Pending-balance ledger: each lot records which issue produced the cents.
-- ------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS pending_lots (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    payee_id  TEXT NOT NULL,
    source    TEXT NOT NULL,              -- an issue_id or "opening"
    cents     INTEGER NOT NULL CHECK (cents >= 0)
);
CREATE INDEX IF NOT EXISTS pending_lots_payee ON pending_lots (payee_id, id);

CREATE TABLE IF NOT EXISTS payee_terms (
    payee_id        TEXT PRIMARY KEY,
    threshold_cents INTEGER NOT NULL CHECK (threshold_cents >= 0)
);

-- ------------------------------------------------------------------
-- Sealed settlement vouchers.  One row per issue; re-closing returns it.
-- ------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS settlements (
    issue_id          TEXT PRIMARY KEY,
    publication_date  TEXT NOT NULL,
    rights_edition_id TEXT NOT NULL,
    catalog_revision  INTEGER NOT NULL,
    voucher_sha256    TEXT NOT NULL,
    voucher_json      TEXT NOT NULL,
    created_at        TEXT NOT NULL
);

-- ------------------------------------------------------------------
-- Issues (刊次) and the citations registered into columns.
--
-- A registration binds an existing citation (the excerpt) to a column and
-- captures the catalog revision it was registered under.  The same citation
-- may be registered in several columns, which is exactly the repeated-archive
-- case the royalty rules price by (group, page).
-- ------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS issues (
    issue_id          TEXT PRIMARY KEY,
    publication_date  TEXT NOT NULL,
    rights_edition_id TEXT NOT NULL REFERENCES rights_editions(edition_id),
    catalog_revision  INTEGER NOT NULL,
    status            TEXT NOT NULL DEFAULT 'open'  -- open | sealed
        CHECK (status IN ('open', 'sealed')),
    created_at        TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS issue_citations (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    issue_id         TEXT NOT NULL REFERENCES issues(issue_id) ON DELETE CASCADE,
    column_id        TEXT NOT NULL REFERENCES columns_table(column_id),
    citation_id      TEXT NOT NULL REFERENCES citations(citation_id),
    position         INTEGER NOT NULL,
    excerpt          TEXT NOT NULL,
    catalog_revision INTEGER NOT NULL,
    UNIQUE (issue_id, column_id, citation_id)
);
CREATE INDEX IF NOT EXISTS issue_citations_issue
    ON issue_citations (issue_id, position);
"""


def connect(path: str | Path) -> sqlite3.Connection:
    """Open a connection in autocommit mode; transactions are explicit."""

    conn = sqlite3.connect(str(path), isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    # WAL lets a read snapshot (export) coexist with a writer.  The export code
    # loads the complete state inside one read transaction and evaluates from
    # that immutable snapshot only.
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    # Concurrent sealing serializes on BEGIN IMMEDIATE; wait rather than fail.
    conn.execute("PRAGMA busy_timeout = 10000")
    return conn


def initialize_schema(conn: sqlite3.Connection) -> None:
    # ``IF NOT EXISTS`` makes this safe repeatedly.  It also migrates an older
    # database by adding tables that did not exist when it was created.
    conn.executescript(SCHEMA)
    if get_meta(conn, "schema_version") != str(SCHEMA_VERSION):
        set_meta(conn, "schema_version", str(SCHEMA_VERSION))


def get_meta(conn: sqlite3.Connection, key: str, default: str | None = None) -> str | None:
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    if row is None:
        return default
    return row["value"]


def set_meta(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO meta (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )


def catalog_revision(conn: sqlite3.Connection) -> int:
    value = get_meta(conn, "catalog_revision", "0")
    return int(value or 0)


def set_catalog_revision(conn: sqlite3.Connection, revision: int) -> None:
    set_meta(conn, "catalog_revision", str(int(revision)))


def bump_catalog_revision(conn: sqlite3.Connection) -> int:
    revision = catalog_revision(conn) + 1
    set_catalog_revision(conn, revision)
    return revision


def next_citation_position(conn: sqlite3.Connection) -> int:
    value = int(get_meta(conn, "citation_position", "0") or 0) + 1
    set_meta(conn, "citation_position", str(value))
    return value


@contextmanager
def write_transaction(conn: sqlite3.Connection) -> Iterator[None]:
    """An immediate write transaction, committed only when the block returns."""

    conn.execute("BEGIN IMMEDIATE")
    try:
        yield
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")


@contextmanager
def read_snapshot_transaction(conn: sqlite3.Connection) -> Iterator[None]:
    """A consistent read transaction (SQLite snapshot isolation in WAL mode)."""

    conn.execute("BEGIN")
    try:
        yield
    finally:
        # Ending a read transaction with ROLLBACK is harmless and never writes.
        conn.execute("ROLLBACK")


def in_transaction(conn: sqlite3.Connection) -> bool:
    return bool(conn.in_transaction)


def clear_all(conn: sqlite3.Connection) -> None:
    """Clear every application table (used only for an explicit reset)."""

    with write_transaction(conn):
        for table in (
            "issue_citations",
            "issues",
            "settlements",
            "pending_lots",
            "payee_terms",
            "columns_table",
            "pricing_groups",
            "account_allocations",
            "page_right_shares",
            "rights_editions",
            "operation_log",
            "pinned_members",
            "range_endpoints",
            "citations",
            "pages",
            "volumes",
            "meta",
        ):
            conn.execute(f"DELETE FROM {table}")
