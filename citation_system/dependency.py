"""Optional materialized page -> citation dependency index.

Why it is optional
------------------
PINNED reverse lookup is already a cheap indexed identity lookup on
``pinned_members(volume_id, page_id)``.  RANGE membership, however, depends on
the *current physical order*: a page is covered when it lies between the two
endpoints and is active.  Answering "which citations cover this page?" for
ranges without extra state costs O(R_v) per query (scan the range endpoints of
that volume and compare positions), where R_v is the number of RANGE
citations in the volume.  At the target size (<=20 volumes, <=200 pages each,
<=500 citations) that is negligible for ad-hoc queries.

For repeated queries this module maintains an explicit dependency table:

    page_citation_dependency(volume_id, page_id, citation_id, role)

The index is **never** used to build exports -- exports always read a full
snapshot.  The index is a query accelerator only, so staleness can never
corrupt a citation product.  It is stamped with the catalog revision it was
built from; any catalog mutation bumps the revision, which invalidates the
index lazily.  A same-volume move therefore invalidates and rebuilds the
range-interior rows the next time the index is consulted; PINNED rows are
unaffected by order but are rebuilt together for simplicity.

Costs
-----
* Build/rebuild: one pass over all citations; for each RANGE, one physical
  slice of its volume.  O(total covered pages + citations).  Worst case with
  500 citations each spanning a 200-page volume: ~100k row inserts.
* Storage: one row per (covered page, citation).
* Incremental maintenance after a move would touch only RANGE citations in the
  moved volume; we use whole-index rebuild because it is simpler to reason
  about and cheap at this scale.  The trade-off is documented, not hidden.
"""

from __future__ import annotations

import sqlite3

from . import db
from .repository import PINNED, RANGE

SCHEMA = """
CREATE TABLE IF NOT EXISTS page_citation_dependency (
    volume_id   TEXT NOT NULL,
    page_id     TEXT NOT NULL,
    citation_id TEXT NOT NULL,
    role        TEXT NOT NULL,   -- member | start | end | interior
    PRIMARY KEY (volume_id, page_id, citation_id)
);
CREATE INDEX IF NOT EXISTS page_citation_dependency_page
    ON page_citation_dependency (volume_id, page_id);
CREATE INDEX IF NOT EXISTS page_citation_dependency_citation
    ON page_citation_dependency (citation_id);
"""

INDEX_REVISION_KEY = "dependency_index_revision"


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)


def index_revision(conn: sqlite3.Connection) -> int | None:
    value = db.get_meta(conn, INDEX_REVISION_KEY)
    return int(value) if value is not None else None


def is_current(conn: sqlite3.Connection) -> bool:
    return index_revision(conn) == db.catalog_revision(conn)


def rebuild(conn: sqlite3.Connection) -> dict[str, int]:
    """Rebuild the whole index from the current committed catalog state.

    Reads and writes in one transaction, so the index corresponds to exactly
    one catalog revision.
    """

    ensure_schema(conn)
    rows = 0
    with db.write_transaction(conn):
        conn.execute("DELETE FROM page_citation_dependency")
        revision = db.catalog_revision(conn)

        for row in conn.execute(
            "SELECT citation_id, volume_id FROM citations WHERE kind = ?", (PINNED,)
        ).fetchall():
            cid, vid = row["citation_id"], row["volume_id"]
            members = conn.execute(
                "SELECT volume_id, page_id FROM pinned_members "
                "WHERE citation_id = ? ORDER BY position",
                (cid,),
            ).fetchall()
            for m in members:
                conn.execute(
                    "INSERT OR REPLACE INTO page_citation_dependency "
                    "(volume_id, page_id, citation_id, role) VALUES (?, ?, ?, ?)",
                    (m["volume_id"], m["page_id"], cid, "member"),
                )
                rows += 1

        for row in conn.execute(
            "SELECT * FROM range_endpoints"
        ).fetchall():
            cid, vid = row["citation_id"], row["volume_id"]
            start = conn.execute(
                "SELECT seq FROM pages WHERE volume_id = ? AND page_id = ?",
                (vid, row["start_page_id"]),
            ).fetchone()
            end = conn.execute(
                "SELECT seq FROM pages WHERE volume_id = ? AND page_id = ?",
                (vid, row["end_page_id"]),
            ).fetchone()
            if start is None or end is None:  # pragma: no cover - FK protects us
                continue
            lo, hi = sorted((start["seq"], end["seq"]))
            covered = conn.execute(
                "SELECT page_id, seq, active FROM pages "
                "WHERE volume_id = ? AND seq BETWEEN ? AND ? "
                "ORDER BY seq",
                (vid, lo, hi),
            ).fetchall()
            for page in covered:
                if not page["active"]:
                    continue
                if page["seq"] == start["seq"]:
                    role = "start"
                elif page["seq"] == end["seq"]:
                    role = "end"
                else:
                    role = "interior"
                conn.execute(
                    "INSERT OR REPLACE INTO page_citation_dependency "
                    "(volume_id, page_id, citation_id, role) VALUES (?, ?, ?, ?)",
                    (vid, page["page_id"], cid, role),
                )
                rows += 1

        db.set_meta(conn, INDEX_REVISION_KEY, str(revision))
    return {"catalog_revision": revision, "dependency_rows": rows}


def referrers(
    conn: sqlite3.Connection,
    volume_id: str,
    page_id: str,
    *,
    auto_rebuild: bool = True,
) -> list[dict[str, object]]:
    """Indexed reverse lookup.  Rebuilds lazily when the catalog changed."""

    ensure_schema(conn)
    if not is_current(conn):
        if not auto_rebuild:
            return []
        rebuild(conn)
    found = [
        {"citation_id": r["citation_id"], "kind": _kind(conn, r["citation_id"]), "role": r["role"]}
        for r in conn.execute(
            "SELECT citation_id, role FROM page_citation_dependency "
            "WHERE volume_id = ? AND page_id = ?",
            (volume_id, page_id),
        )
    ]
    positions = {
        r["citation_id"]: r["position"]
        for r in conn.execute("SELECT citation_id, position FROM citations")
    }
    found.sort(key=lambda f: positions.get(str(f["citation_id"]), 10**9))
    return found


def _kind(conn: sqlite3.Connection, citation_id: str) -> str:
    row = conn.execute(
        "SELECT kind FROM citations WHERE citation_id = ?", (citation_id,)
    ).fetchone()
    return str(row["kind"]) if row else "?"
