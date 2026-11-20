"""SQLite connection, schema and transaction helpers."""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

SCHEMA_VERSION = 1

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
    return conn


def initialize_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
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
            "operation_log",
            "pinned_members",
            "range_endpoints",
            "citations",
            "pages",
            "volumes",
            "meta",
        ):
            conn.execute(f"DELETE FROM {table}")
