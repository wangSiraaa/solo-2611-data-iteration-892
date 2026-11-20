"""Citation repository: excerpts with PINNED and RANGE references.

Both reference kinds are resolved once at creation time and then store real
page identities.  They are never re-resolved from section/label text on read.

PINNED
    A fixed set of ``(volume_id, page_id)``.  Order is not meaningful.  A
    member that later becomes inactive stays in the set and makes the whole
    citation *needs attention*; members are never silently dropped.

RANGE
    Two endpoint identities in one volume.  At export time the range is the
    currently physical interval between them, filtered to active pages and
    inclusive of both endpoints.  If either endpoint is inactive, or the
    endpoints have reversed in physical order, the citation is *needs
    attention* and the endpoints are listed in their original source order;
    they are never swapped or guessed.

The repository works whether or not any citation exists: the catalog is an
independent object.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from typing import Any, Iterable

from . import db
from .catalog import Catalog, Page
from .errors import (
    CITATION_EXISTS,
    CITATION_NOT_FOUND,
    DUPLICATE_PINNED_MEMBER,
    INVALID_CITATION,
    OperationRejected,
    PAGE_NOT_FOUND,
    ReferenceCreationError,
    VOLUME_NOT_FOUND,
)

PINNED = "PINNED"
RANGE = "RANGE"


@dataclass(frozen=True)
class Citation:
    citation_id: str
    text: str
    kind: str
    volume_id: str
    position: int
    created_revision: int
    origin: dict[str, Any] | None

    def as_dict(self) -> dict[str, Any]:
        return {
            "citation_id": self.citation_id,
            "text": self.text,
            "kind": self.kind,
            "volume_id": self.volume_id,
            "position": self.position,
            "created_revision": self.created_revision,
            "origin": self.origin,
        }


class CitationRepository:
    def __init__(self, catalog: Catalog) -> None:
        self.catalog = catalog
        self.conn = catalog.conn

    # ------------------------------------------------------------------
    # creation
    # ------------------------------------------------------------------
    def create_pinned(
        self,
        citation_id: str,
        text: str,
        volume_id: str,
        page_ids: Iterable[str],
        *,
        origin: dict[str, Any] | None = None,
    ) -> Citation:
        """Pin an explicit set of page identities (already resolved)."""

        page_ids = list(page_ids)
        if not page_ids:
            raise ReferenceCreationError(INVALID_CITATION, "PINNED needs at least one page")
        if len(set(page_ids)) != len(page_ids):
            raise ReferenceCreationError(
                DUPLICATE_PINNED_MEMBER, "PINNED members must be unique"
            )
        self._check_citation_id_free(citation_id)
        if volume_id not in self.catalog.volume_ids():
            raise ReferenceCreationError(
                VOLUME_NOT_FOUND, f"volume {volume_id!r} does not exist",
                details={"volume_id": volume_id},
            )
        resolved: list[Page] = []
        for pid in page_ids:
            page = self.catalog.get_page(volume_id, pid, missing_ok=True)
            if page is None:
                raise ReferenceCreationError(
                    PAGE_NOT_FOUND, f"page {volume_id}/{pid} does not exist",
                    details={"volume_id": volume_id, "page_id": pid},
                )
            resolved.append(page)

        with db.write_transaction(self.conn):
            citation = self._insert_citation(
                citation_id, text, PINNED, volume_id, origin
            )
            for position, page in enumerate(resolved):
                self.conn.execute(
                    "INSERT INTO pinned_members (citation_id, position, volume_id, page_id) "
                    "VALUES (?, ?, ?, ?)",
                    (citation_id, position, page.volume_id, page.page_id),
                )
        return citation

    def create_range(
        self,
        citation_id: str,
        text: str,
        volume_id: str,
        start_page_id: str,
        end_page_id: str,
        *,
        origin: dict[str, Any] | None = None,
    ) -> Citation:
        """Create a RANGE from two explicit endpoint identities."""

        self._check_citation_id_free(citation_id)
        if volume_id not in self.catalog.volume_ids():
            raise ReferenceCreationError(
                VOLUME_NOT_FOUND, f"volume {volume_id!r} does not exist",
                details={"volume_id": volume_id},
            )
        for pid in (start_page_id, end_page_id):
            if self.catalog.get_page(volume_id, pid, missing_ok=True) is None:
                raise ReferenceCreationError(
                    PAGE_NOT_FOUND, f"page {volume_id}/{pid} does not exist",
                    details={"volume_id": volume_id, "page_id": pid},
                )
        with db.write_transaction(self.conn):
            citation = self._insert_citation(
                citation_id, text, RANGE, volume_id, origin
            )
            self.conn.execute(
                "INSERT INTO range_endpoints "
                "(citation_id, volume_id, start_page_id, end_page_id) "
                "VALUES (?, ?, ?, ?)",
                (citation_id, volume_id, start_page_id, end_page_id),
            )
        return citation

    def create_from_reference(
        self,
        reference: dict[str, Any],
    ) -> Citation:
        """Create a citation from a ``references.jsonl``-style record.

        Records that supply ``section_id``/``label`` (or ``items`` for a
        PINNED member list, or ``start_*``/``end_*`` fields for a RANGE) are
        resolved once here; the stored citation keeps the real identities.
        Records that supply ``page_ids`` / ``start_page_id``/``end_page_id``
        are already identities and are stored as-is.  Resolution is never
        repeated on read.  Ambiguity or absence raises and writes nothing.

        Supported inputs::

            {"citation_id","text","kind":"PINNED","volume_id","page_ids":[...]}
            {"citation_id","text","kind":"PINNED","volume_id",
             "items":[{"section_id","label"}, ...]}
            {"citation_id","text","kind":"RANGE","volume_id",
             "start_page_id","end_page_id"}
            {"citation_id","text","kind":"RANGE","volume_id",
             "start_section_id","start_label",
             "end_section_id","end_label"}
        """

        if not isinstance(reference, dict):
            raise ReferenceCreationError(INVALID_CITATION, "reference must be an object")
        cid = reference.get("citation_id")
        text = reference.get("text")
        kind = reference.get("kind")
        volume_id = reference.get("volume_id")
        if not isinstance(cid, str) or not cid:
            raise ReferenceCreationError(INVALID_CITATION, "citation_id is required")
        if not isinstance(text, str):
            raise ReferenceCreationError(INVALID_CITATION, "text must be a string")
        if kind not in (PINNED, RANGE):
            raise ReferenceCreationError(INVALID_CITATION, f"unknown kind {kind!r}")
        if not isinstance(volume_id, str) or not volume_id:
            raise ReferenceCreationError(INVALID_CITATION, "volume_id is required")

        origin = {k: v for k, v in reference.items() if k not in ("text",)}

        if kind == PINNED:
            if "page_ids" in reference:
                page_ids = reference["page_ids"]
                if not isinstance(page_ids, list) or not page_ids:
                    raise ReferenceCreationError(
                        INVALID_CITATION, "page_ids must be a non-empty list"
                    )
                # Explicit identities need no resolution; they are fixed now.
                return self.create_pinned(
                    cid, text, volume_id, page_ids, origin=origin
                )
            items = reference.get("items")
            if not isinstance(items, list) or not items:
                raise ReferenceCreationError(
                    INVALID_CITATION,
                    "PINNED requires page_ids or items with section_id/label",
                )
            page_ids = []
            for item in items:
                if not isinstance(item, dict):
                    raise ReferenceCreationError(INVALID_CITATION, "invalid PINNED item")
                if "page_id" in item:
                    page_ids.append(item["page_id"])
                else:
                    page = self._resolve_for_creation(
                        volume_id,
                        item.get("section_id"),
                        item.get("label"),
                    )
                    page_ids.append(page.page_id)
            return self.create_pinned(
                cid, text, volume_id, page_ids, origin=origin
            )

        # RANGE
        if "start_page_id" in reference and "end_page_id" in reference:
            start_pid = reference["start_page_id"]
            end_pid = reference["end_page_id"]
        else:
            start = self._resolve_for_creation(
                volume_id,
                reference.get("start_section_id"),
                reference.get("start_label"),
            )
            end = self._resolve_for_creation(
                volume_id,
                reference.get("end_section_id"),
                reference.get("end_label"),
            )
            start_pid, end_pid = start.page_id, end.page_id
        if not isinstance(start_pid, str) or not isinstance(end_pid, str):
            raise ReferenceCreationError(INVALID_CITATION, "invalid RANGE endpoints")
        return self.create_range(
            cid, text, volume_id, start_pid, end_pid, origin=origin
        )

    def _resolve_for_creation(
        self, volume_id: str, section_id: Any, label: Any
    ) -> Page:
        if not isinstance(section_id, str) or not isinstance(label, str):
            raise ReferenceCreationError(
                INVALID_CITATION,
                "resolution requires string section_id and label",
                details={"section_id": section_id, "label": label},
            )
        try:
            return self.catalog.resolve(volume_id, section_id, label)
        except OperationRejected as exc:
            raise ReferenceCreationError(
                exc.code, exc.message, details=exc.details
            ) from None

    def _check_citation_id_free(self, citation_id: str) -> None:
        if not isinstance(citation_id, str) or not citation_id:
            raise ReferenceCreationError(INVALID_CITATION, "citation_id is required")
        row = self.conn.execute(
            "SELECT 1 FROM citations WHERE citation_id = ?", (citation_id,)
        ).fetchone()
        if row is not None:
            raise ReferenceCreationError(
                CITATION_EXISTS, f"citation {citation_id!r} already exists",
                details={"citation_id": citation_id},
            )

    def _insert_citation(
        self,
        citation_id: str,
        text: str,
        kind: str,
        volume_id: str,
        origin: dict[str, Any] | None,
    ) -> Citation:
        position = db.next_citation_position(self.conn)
        revision = db.catalog_revision(self.conn)
        self.conn.execute(
            "INSERT INTO citations "
            "(citation_id, text, kind, volume_id, position, created_revision, origin_json) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                citation_id,
                text,
                kind,
                volume_id,
                position,
                revision,
                json.dumps(origin, ensure_ascii=False, sort_keys=True)
                if origin is not None else None,
            ),
        )
        return Citation(
            citation_id=citation_id,
            text=text,
            kind=kind,
            volume_id=volume_id,
            position=position,
            created_revision=revision,
            origin=origin,
        )

    # ------------------------------------------------------------------
    # reading
    # ------------------------------------------------------------------
    def get(self, citation_id: str) -> Citation:
        row = self.conn.execute(
            "SELECT * FROM citations WHERE citation_id = ?", (citation_id,)
        ).fetchone()
        if row is None:
            raise ReferenceCreationError(
                CITATION_NOT_FOUND, f"citation {citation_id!r} not found",
                details={"citation_id": citation_id},
            )
        return self._row_to_citation(row)

    def all(self) -> list[Citation]:
        rows = self.conn.execute(
            "SELECT * FROM citations ORDER BY position"
        ).fetchall()
        return [self._row_to_citation(r) for r in rows]

    def pinned_members(self, citation_id: str) -> list[Page]:
        rows = self.conn.execute(
            "SELECT m.position, p.* FROM pinned_members m "
            "JOIN pages p ON p.volume_id = m.volume_id AND p.page_id = m.page_id "
            "WHERE m.citation_id = ? ORDER BY m.position",
            (citation_id,),
        ).fetchall()
        return [Catalog._row_to_page(r) for r in rows]

    def range_endpoints(self, citation_id: str) -> tuple[Page, Page]:
        row = self.conn.execute(
            "SELECT * FROM range_endpoints WHERE citation_id = ?", (citation_id,)
        ).fetchone()
        if row is None:
            raise ReferenceCreationError(
                INVALID_CITATION, f"citation {citation_id!r} is not a RANGE"
            )
        start = self.catalog.get_page(row["volume_id"], row["start_page_id"])
        end = self.catalog.get_page(row["volume_id"], row["end_page_id"])
        assert start is not None and end is not None
        return start, end

    def citations_referencing(
        self,
        volume_id: str,
        page_id: str,
        *,
        include_pinned: bool = True,
        include_ranges: bool = True,
    ) -> list[dict[str, Any]]:
        """Reverse lookup: citations that reference a real page identity.

        PINNED members are an indexed identity lookup.  RANGE citations are
        found through the endpoint indexes plus a physical interval test, so
        interior pages are reported against the current order.
        """

        found: list[dict[str, Any]] = []
        seen: set[str] = set()
        if include_pinned:
            rows = self.conn.execute(
                "SELECT DISTINCT citation_id FROM pinned_members "
                "WHERE volume_id = ? AND page_id = ?",
                (volume_id, page_id),
            ).fetchall()
            for row in rows:
                cid = row["citation_id"]
                if cid in seen:
                    continue
                seen.add(cid)
                found.append({"citation_id": cid, "kind": PINNED, "role": "member"})
        if include_ranges:
            page = self.catalog.get_page(volume_id, page_id)
            assert page is not None
            eps = self.conn.execute(
                "SELECT * FROM range_endpoints WHERE volume_id = ?",
                (volume_id,),
            ).fetchall()
            for ep in eps:
                if ep["citation_id"] in seen:
                    continue
                start = self.catalog.get_page(volume_id, ep["start_page_id"])
                end = self.catalog.get_page(volume_id, ep["end_page_id"])
                assert start is not None and end is not None
                lo, hi = sorted((start.seq, end.seq))
                if lo <= page.seq <= hi:
                    role = "start" if page.seq == start.seq else (
                        "end" if page.seq == end.seq else "interior"
                    )
                    seen.add(ep["citation_id"])
                    found.append(
                        {"citation_id": ep["citation_id"], "kind": RANGE, "role": role}
                    )
        # Stable order by citation position.
        positions = {
            r["citation_id"]: r["position"]
            for r in self.conn.execute(
                "SELECT citation_id, position FROM citations"
            )
        }
        found.sort(key=lambda f: positions.get(f["citation_id"], 10**9))
        return found

    # ------------------------------------------------------------------
    def _row_to_citation(self, row: sqlite3.Row) -> Citation:
        return Citation(
            citation_id=row["citation_id"],
            text=row["text"],
            kind=row["kind"],
            volume_id=row["volume_id"],
            position=int(row["position"]),
            created_revision=int(row["created_revision"]),
            origin=json.loads(row["origin_json"]) if row["origin_json"] else None,
        )
