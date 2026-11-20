"""The physical page catalog for all volumes.

Key invariants
--------------
``(volume_id, page_id)`` is the complete page identity.  A ``page_id`` is
unique inside its volume only; ``V1/p1`` and ``V2/p1`` are different pages.

Every page (active or inactive) keeps its position in the physical order.
Inactive pages are not removed: they remain between pages and are never
renumbered or re-identified.  Positions are contiguous ``0..n-1`` for each
volume and identical across active and inactive pages.

Operation batches are atomic: every operation in a batch runs inside one
SQLite transaction, and a rejected member rolls the whole batch back.  A
rejected batch changes no page (not even one that was otherwise valid), and a
rejected request that declared ``expect: reject_entire_batch`` also leaves no
trace if it unexpectedly succeeds.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Any, Iterable, Iterator

from . import db
from .errors import (
    ANCHOR_NOT_FOUND,
    CROSS_VOLUME_ANCHOR,
    CROSS_VOLUME_MOVE,
    DUPLICATE_PAGE_ID,
    INVALID_OPERATION,
    INVALID_PAGE,
    OperationRejected,
    PAGE_NOT_FOUND,
    RESOLUTION_AMBIGUOUS,
    RESOLUTION_NOT_FOUND,
    UNSUPPORTED_OPERATION,
    VOLUME_NOT_FOUND,
)

KINDS = ("insert", "insert_many", "deactivate", "activate", "move", "move_many", "reorder")


@dataclass(frozen=True)
class Page:
    volume_id: str
    page_id: str
    section_id: str
    label: str
    active: bool
    seq: int

    def as_key(self) -> tuple[str, str]:
        return (self.volume_id, self.page_id)

    def as_dict(self, *, include_seq: bool = False) -> dict[str, Any]:
        out: dict[str, Any] = {
            "volume_id": self.volume_id,
            "page_id": self.page_id,
            "section_id": self.section_id,
            "label": self.label,
            "active": self.active,
        }
        if include_seq:
            out["seq"] = self.seq
        return out


class Catalog:
    """Read/write access to the catalog stored in SQLite."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    # ------------------------------------------------------------------
    # loading
    # ------------------------------------------------------------------
    @classmethod
    def open(cls, path: str) -> "Catalog":
        return cls(db.connect(path))

    @classmethod
    def create(cls, path: str) -> "Catalog":
        cat = cls.open(path)
        db.initialize_schema(cat.conn)
        return cat

    def close(self) -> None:
        self.conn.close()

    def add_volume(self, volume_id: str, title: str | None = None) -> None:
        self.conn.execute(
            "INSERT INTO volumes (volume_id, title) VALUES (?, ?) "
            "ON CONFLICT(volume_id) DO NOTHING",
            (volume_id, title),
        )

    # ------------------------------------------------------------------
    # reading
    # ------------------------------------------------------------------
    def revision(self) -> int:
        return db.catalog_revision(self.conn)

    def volume_ids(self) -> list[str]:
        rows = self.conn.execute(
            "SELECT volume_id FROM volumes ORDER BY volume_id"
        ).fetchall()
        return [r["volume_id"] for r in rows]

    def require_volume(self, volume_id: str) -> None:
        row = self.conn.execute(
            "SELECT 1 FROM volumes WHERE volume_id = ?", (volume_id,)
        ).fetchone()
        if row is None:
            raise OperationRejected(
                VOLUME_NOT_FOUND, f"volume {volume_id!r} does not exist",
                details={"volume_id": volume_id},
            )

    def pages(self, volume_id: str, *, include_inactive: bool = True) -> list[Page]:
        """All pages of a volume in physical order."""

        sql = "SELECT * FROM pages WHERE volume_id = ?"
        params: list[Any] = [volume_id]
        if not include_inactive:
            sql += " AND active = 1"
        sql += " ORDER BY seq"
        return [self._row_to_page(r) for r in self.conn.execute(sql, params)]

    def iter_all_pages(self) -> Iterator[Page]:
        for row in self.conn.execute(
            "SELECT * FROM pages ORDER BY volume_id, seq"
        ):
            yield self._row_to_page(row)

    def get_page(
        self, volume_id: str, page_id: str, *, missing_ok: bool = False
    ) -> Page | None:
        row = self.conn.execute(
            "SELECT * FROM pages WHERE volume_id = ? AND page_id = ?",
            (volume_id, page_id),
        ).fetchone()
        if row is None:
            if missing_ok:
                return None
            raise OperationRejected(
                PAGE_NOT_FOUND,
                f"page {volume_id}/{page_id} does not exist",
                details={"volume_id": volume_id, "page_id": page_id},
            )
        return self._row_to_page(row)

    def position(self, volume_id: str, page_id: str) -> int:
        page = self.get_page(volume_id, page_id)
        assert page is not None
        return page.seq

    def neighbours(
        self, volume_id: str, page_id: str, *, active_only: bool = False
    ) -> dict[str, Page | None]:
        """Previous/next page in physical order.

        ``active_only=True`` skips inactive pages in the physical order; the
        identity of the returned neighbour is still the actual page.  The
        queried page itself may be active or inactive; only the neighbours are
        filtered.
        """

        all_pages = self.pages(volume_id, include_inactive=True)
        ids = [p.page_id for p in all_pages]
        try:
            idx = ids.index(page_id)
        except ValueError:
            self.get_page(volume_id, page_id)  # raises page_not_found
            raise AssertionError("unreachable")

        def step(offset: int) -> Page | None:
            j = idx + offset
            while 0 <= j < len(all_pages):
                candidate = all_pages[j]
                if candidate.active or not active_only:
                    return candidate
                j += offset
            return None

        return {"previous": step(-1), "next": step(1)}

    def current_neighbours(self, volume_id: str, page_id: str) -> tuple[Page | None, Page | None]:
        n = self.neighbours(volume_id, page_id)
        return n["previous"], n["next"]

    def ordered_range(
        self,
        volume_id: str,
        start_page_id: str,
        end_page_id: str,
        *,
        include_inactive: bool = True,
    ) -> list[Page]:
        """All pages between two endpoints, in current physical order.

        The range is inclusive.  Endpoints may be given in either order; the
        physical order decides what lies between.  Callers that need to detect
        a reversed range must compare positions themselves (see
        ``range_positions``).
        """

        start = self.get_page(volume_id, start_page_id)
        end = self.get_page(volume_id, end_page_id)
        assert start is not None and end is not None
        lo, hi = sorted((start.seq, end.seq))
        sql = "SELECT * FROM pages WHERE volume_id = ? AND seq BETWEEN ? AND ?"
        if not include_inactive:
            sql += " AND active = 1"
        sql += " ORDER BY seq"
        return [
            self._row_to_page(r)
            for r in self.conn.execute(sql, (volume_id, lo, hi))
        ]

    def range_positions(
        self, volume_id: str, start_page_id: str, end_page_id: str
    ) -> tuple[int, int]:
        return (
            self.position(volume_id, start_page_id),
            self.position(volume_id, end_page_id),
        )

    def resolve(self, volume_id: str, section_id: str, label: str) -> Page:
        """Resolve ``(volume, section, display label)`` to exactly one active page.

        Display labels are strings, so ``"3"`` of ``body`` and ``"3"`` of
        ``appendix`` are distinct.  No numeric coercion, no prefix matching.
        """

        rows = self.conn.execute(
            "SELECT * FROM pages WHERE volume_id = ? AND section_id = ? "
            "AND label = ? AND active = 1 ORDER BY seq",
            (volume_id, str(section_id), str(label)),
        ).fetchall()
        if not rows:
            raise OperationRejected(
                RESOLUTION_NOT_FOUND,
                f"no active page for {volume_id}/{section_id}/{label}",
                details={
                    "volume_id": volume_id,
                    "section_id": section_id,
                    "label": label,
                },
            )
        if len(rows) > 1:
            raise OperationRejected(
                RESOLUTION_AMBIGUOUS,
                f"{len(rows)} active pages match {volume_id}/{section_id}/{label}",
                details={
                    "volume_id": volume_id,
                    "section_id": section_id,
                    "label": label,
                    "matching_page_ids": [r["page_id"] for r in rows],
                },
            )
        return self._row_to_page(rows[0])

    def resolve_or_none(self, volume_id: str, section_id: str, label: str) -> Page | None:
        try:
            return self.resolve(volume_id, section_id, label)
        except OperationRejected:
            return None

    # ------------------------------------------------------------------
    # mutation: initial load and operations
    # ------------------------------------------------------------------
    def load_pages_document(self, document: dict[str, Any]) -> None:
        """Load a ``volume-pages/1`` document atomically.

        The document is validated completely before any write.  Revision from
        the document is only stored on a fresh catalog.
        """

        if document.get("format") != "volume-pages/1":
            raise OperationRejected(
                INVALID_PAGE, "unsupported pages document format",
                details={"format": document.get("format")},
            )
        volumes = document.get("volumes")
        if not isinstance(volumes, list):
            raise OperationRejected(INVALID_PAGE, "volumes must be a list")

        seen_volumes: set[str] = set()
        for vol in volumes:
            vid = vol.get("volume_id")
            if not isinstance(vid, str) or not vid:
                raise OperationRejected(INVALID_PAGE, "volume_id must be a non-empty string")
            if vid in seen_volumes:
                raise OperationRejected(
                    INVALID_PAGE, f"duplicate volume {vid}",
                    details={"volume_id": vid},
                )
            seen_volumes.add(vid)
            page_ids: set[str] = set()
            for page in vol.get("pages", []):
                pid = page.get("page_id")
                if not isinstance(pid, str) or not pid:
                    raise OperationRejected(INVALID_PAGE, "page_id must be a non-empty string")
                if pid in page_ids:
                    raise OperationRejected(
                        DUPLICATE_PAGE_ID,
                        f"duplicate page_id {pid} in volume {vid}",
                        details={"volume_id": vid, "page_id": pid},
                    )
                page_ids.add(pid)
                if not isinstance(page.get("section_id"), str) or not page.get("section_id"):
                    raise OperationRejected(
                        INVALID_PAGE, f"page {vid}/{pid} has no section_id",
                        details={"volume_id": vid, "page_id": pid},
                    )
                if not isinstance(page.get("label"), str):
                    raise OperationRejected(
                        INVALID_PAGE, f"page {vid}/{pid} label must be a string",
                        details={"volume_id": vid, "page_id": pid},
                    )
                if not isinstance(page.get("active"), bool):
                    raise OperationRejected(
                        INVALID_PAGE, f"page {vid}/{pid} active must be boolean",
                        details={"volume_id": vid, "page_id": pid},
                    )

        with db.write_transaction(self.conn):
            for vol in volumes:
                vid = vol["volume_id"]
                self.conn.execute(
                    "INSERT INTO volumes (volume_id) VALUES (?) "
                    "ON CONFLICT(volume_id) DO NOTHING",
                    (vid,),
                )
                for seq, page in enumerate(vol.get("pages", [])):
                    self.conn.execute(
                        "INSERT INTO pages "
                        "(volume_id, page_id, section_id, label, active, seq) "
                        "VALUES (?, ?, ?, ?, ?, ?)",
                        (
                            vid,
                            page["page_id"],
                            page["section_id"],
                            page["label"],
                            1 if page["active"] else 0,
                            seq,
                        ),
                    )
            db.set_catalog_revision(self.conn, int(document.get("revision", 0)))
            db.set_meta(self.conn, "loaded_from", "pages.json")

    # ---- operations ---------------------------------------------------
    def apply_operations(
        self,
        operations: Iterable[dict[str, Any]],
        *,
        stop_on_rejected_batch: bool = False,
    ) -> list[dict[str, Any]]:
        """Apply operations, each batch atomically.

        Individual operation objects are their own batch unless a grouping
        key is supplied.  An explicit ``batch_id`` (or a list of operations
        passed as a single ``{"operations": [...]}`` request) makes the whole
        group atomic: if any member is rejected the whole batch is rejected.

        A rejected batch is logged and leaves no partial effect.  By default
        later, independent batches are still processed; pass
        ``stop_on_rejected_batch=True`` to halt instead.
        """

        results: list[dict[str, Any]] = []
        for index, request in enumerate(operations, 1):
            try:
                result = self.apply_batch(request, index=index)
            except OperationRejected as exc:
                result = {
                    "request_index": index,
                    "request": request,
                    "status": "rejected",
                    "error_code": exc.code,
                    "error_detail": exc.details,
                    "catalog_revision": self.revision(),
                    "expectation": (request or {}).get("expect"),
                }
                self._log_request(index, request, result)
                results.append(result)
                if stop_on_rejected_batch:
                    break
                continue
            self._log_request(index, request, result)
            results.append(result)
        return results

    def apply_batch(self, request: dict[str, Any], *, index: int | None = None) -> dict[str, Any]:
        """Apply one atomic batch and return its result descriptor.

        The request is either a single operation dict or
        ``{"operations": [...], "batch_id": ...}``.
        """

        if not isinstance(request, dict):
            raise OperationRejected(INVALID_OPERATION, "operation must be an object")
        batch_id = request.get("batch_id")
        if "operations" in request:
            ops = request["operations"]
            if not isinstance(ops, list) or not ops:
                raise OperationRejected(INVALID_OPERATION, "operations must be a non-empty list")
        else:
            ops = [request]

        expectation = request.get("expect")

        with db.write_transaction(self.conn):
            mutated = False
            for op in ops:
                changed = self._apply_one(op)
                mutated = mutated or changed
            if expectation == "reject_entire_batch":
                # The request itself says it must not take effect.  Raising
                # here rolls the transaction back, so a batch that (wrongly)
                # succeeded leaves no trace either.
                raise OperationRejected(
                    INVALID_OPERATION,
                    "request expected to be rejected but was applied",
                    details={"expect": expectation, "batch_id": batch_id},
                )
            new_revision = db.catalog_revision(self.conn)
            if mutated:
                new_revision = db.bump_catalog_revision(self.conn)

        return {
            "request_index": index,
            "batch_id": batch_id,
            "request": request,
            "status": "applied" if mutated else "noop",
            "catalog_revision": new_revision,
        }

    def _apply_one(self, op: dict[str, Any]) -> bool:
        if not isinstance(op, dict):
            raise OperationRejected(INVALID_OPERATION, "operation must be an object")
        kind = op.get("operation")
        if kind not in KINDS:
            raise OperationRejected(
                UNSUPPORTED_OPERATION, f"unsupported operation {kind!r}",
                details={"operation": kind},
            )
        if kind == "insert":
            return self._op_insert(op)
        if kind == "insert_many":
            return self._op_insert_many(op)
        if kind == "deactivate":
            return self._op_set_active(op, False)
        if kind == "activate":
            return self._op_set_active(op, True)
        if kind == "move":
            return self._op_move(op)
        if kind == "move_many":
            return self._op_move_many(op)
        if kind == "reorder":
            return self._op_reorder(op)
        raise AssertionError(kind)  # pragma: no cover

    # ------------------------------------------------------------------
    # operation helpers
    # ------------------------------------------------------------------
    def _op_insert(self, op: dict[str, Any]) -> bool:
        volume_id = self._require_str(op, "volume_id")
        self.require_volume(volume_id)
        page = op.get("page")
        if not isinstance(page, dict):
            raise OperationRejected(INVALID_OPERATION, "insert requires a page object")
        pid = page.get("page_id")
        if not isinstance(pid, str) or not pid:
            raise OperationRejected(INVALID_PAGE, "page_id must be a non-empty string")
        if self.get_page(volume_id, pid, missing_ok=True) is not None:
            raise OperationRejected(
                DUPLICATE_PAGE_ID,
                f"page {volume_id}/{pid} already exists; page_id must not be reissued",
                details={"volume_id": volume_id, "page_id": pid},
            )
        section_id = page.get("section_id")
        label = page.get("label")
        active = page.get("active", True)
        if not isinstance(section_id, str) or not section_id:
            raise OperationRejected(INVALID_PAGE, "section_id must be a non-empty string")
        if not isinstance(label, str):
            raise OperationRejected(INVALID_PAGE, "label must be a string")
        if not isinstance(active, bool):
            raise OperationRejected(INVALID_PAGE, "active must be boolean")

        position = self._resolve_anchor(volume_id, op, allow_absent=True)
        self._shift_right(volume_id, position)
        self.conn.execute(
            "INSERT INTO pages (volume_id, page_id, section_id, label, active, seq) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (volume_id, pid, section_id, label, 1 if active else 0, position),
        )
        return True

    def _op_insert_many(self, op: dict[str, Any]) -> bool:
        """Insert several new pages at one position, keeping their given order."""

        volume_id = self._require_str(op, "volume_id")
        self.require_volume(volume_id)
        pages = op.get("pages")
        if not isinstance(pages, list) or not pages:
            raise OperationRejected(INVALID_OPERATION, "insert_many requires a non-empty pages list")
        ids: set[str] = set()
        for page in pages:
            if not isinstance(page, dict):
                raise OperationRejected(INVALID_PAGE, "each inserted page must be an object")
            pid = page.get("page_id")
            if not isinstance(pid, str) or not pid:
                raise OperationRejected(INVALID_PAGE, "page_id must be a non-empty string")
            if pid in ids or self.get_page(volume_id, pid, missing_ok=True) is not None:
                raise OperationRejected(
                    DUPLICATE_PAGE_ID,
                    f"page {volume_id}/{pid} already exists; page_id must not be reissued",
                    details={"volume_id": volume_id, "page_id": pid},
                )
            ids.add(pid)
            if not isinstance(page.get("section_id"), str) or not page.get("section_id"):
                raise OperationRejected(INVALID_PAGE, "section_id must be a non-empty string")
            if not isinstance(page.get("label"), str):
                raise OperationRejected(INVALID_PAGE, "label must be a string")
            if not isinstance(page.get("active", True), bool):
                raise OperationRejected(INVALID_PAGE, "active must be boolean")

        position = self._resolve_anchor(volume_id, op, allow_absent=True)
        self._shift_right(volume_id, position, amount=len(pages))
        for offset, page in enumerate(pages):
            self.conn.execute(
                "INSERT INTO pages (volume_id, page_id, section_id, label, active, seq) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    volume_id,
                    page["page_id"],
                    page["section_id"],
                    page["label"],
                    1 if page.get("active", True) else 0,
                    position + offset,
                ),
            )
        return True

    def _op_set_active(self, op: dict[str, Any], active: bool) -> bool:
        volume_id = self._require_str(op, "volume_id")
        self.require_volume(volume_id)
        pid = self._require_str(op, "page_id")
        page = self.get_page(volume_id, pid)
        assert page is not None
        if page.active == active:
            return False
        self.conn.execute(
            "UPDATE pages SET active = ? WHERE volume_id = ? AND page_id = ?",
            (1 if active else 0, volume_id, pid),
        )
        return True

    def _op_move(self, op: dict[str, Any]) -> bool:
        volume_id = self._require_str(op, "volume_id")
        self.require_volume(volume_id)
        pid = self._require_str(op, "page_id")
        target_volume = op.get("target_volume_id")
        if target_volume is not None and target_volume != volume_id:
            raise OperationRejected(
                CROSS_VOLUME_MOVE,
                f"cannot move {volume_id}/{pid} to another volume {target_volume!r}",
                details={
                    "volume_id": volume_id,
                    "page_id": pid,
                    "target_volume_id": target_volume,
                },
            )
        page = self.get_page(volume_id, pid)
        assert page is not None
        current = page.seq
        position = self._resolve_anchor(volume_id, op, allow_absent=False)
        # Position is interpreted after removing the moved page.
        if position > current:
            position -= 1
        if position == current:
            return False
        self._move_page(volume_id, current, position)
        return True

    def _op_move_many(self, op: dict[str, Any]) -> bool:
        """Move several existing pages so they sit, in the given order, before
        ``before_page_id`` (or at the end when absent)."""

        volume_id = self._require_str(op, "volume_id")
        self.require_volume(volume_id)
        target_volume = op.get("target_volume_id")
        if target_volume is not None and target_volume != volume_id:
            raise OperationRejected(
                CROSS_VOLUME_MOVE,
                f"cannot move pages out of volume {volume_id}",
                details={"volume_id": volume_id, "target_volume_id": target_volume},
            )
        page_ids = op.get("page_ids")
        if not isinstance(page_ids, list) or not page_ids:
            raise OperationRejected(INVALID_OPERATION, "move_many requires page_ids")
        if len(set(page_ids)) != len(page_ids):
            raise OperationRejected(INVALID_OPERATION, "move_many page_ids must be unique")
        moving = [self.get_page(volume_id, pid) for pid in page_ids]
        if any(p is None for p in moving):
            raise OperationRejected(PAGE_NOT_FOUND, "move_many references an unknown page")
        anchor = op.get("before_page_id")
        if anchor is not None and anchor in page_ids:
            raise OperationRejected(
                INVALID_OPERATION, "cannot move pages before one of the moved pages"
            )
        # Remove every moved page, then insert the whole block at the anchor,
        # preserving the order given in ``page_ids``.
        ordered_pages = self.pages(volume_id)
        moved_set = set(page_ids)
        remaining = [p for p in ordered_pages if p.page_id not in moved_set]
        anchor_index = (
            len(remaining) if anchor is None else self._index_of(remaining, anchor)
        )
        by_id = {p.page_id: p for p in ordered_pages}
        moved_in_request = [by_id[pid] for pid in page_ids]
        new_order = (
            remaining[:anchor_index] + moved_in_request + remaining[anchor_index:]
        )
        return self._write_order(volume_id, new_order)

    def _op_reorder(self, op: dict[str, Any]) -> bool:
        """Set the complete physical order of a volume.

        The supplied ``page_ids`` must be a permutation of every page, active
        or inactive.  No page may be dropped or invented.
        """

        volume_id = self._require_str(op, "volume_id")
        self.require_volume(volume_id)
        page_ids = op.get("page_ids")
        if not isinstance(page_ids, list):
            raise OperationRejected(INVALID_OPERATION, "reorder requires a page_ids list")
        existing = [p.page_id for p in self.pages(volume_id)]
        if len(set(page_ids)) != len(page_ids) or set(page_ids) != set(existing):
            raise OperationRejected(
                INVALID_OPERATION,
                "reorder must list every page of the volume exactly once",
                details={
                    "missing": sorted(set(existing) - set(page_ids)),
                    "unknown": sorted(set(page_ids) - set(existing)),
                },
            )
        by_id = {p.page_id: p for p in self.pages(volume_id)}
        return self._write_order(volume_id, [by_id[pid] for pid in page_ids])

    # ------------------------------------------------------------------
    # anchor / position helpers
    # ------------------------------------------------------------------
    def _resolve_anchor(
        self, volume_id: str, op: dict[str, Any], *, allow_absent: bool
    ) -> int:
        """Return a target position from before/after/at-start/at-end fields."""

        before = op.get("before_page_id")
        after = op.get("after_page_id")
        at_start = bool(op.get("at_start"))
        at_end = bool(op.get("at_end"))
        present = sum(x is not None and x is not False for x in (before, after, at_start, at_end))
        if present == 0:
            if allow_absent:
                return self._page_count(volume_id)  # append
            raise OperationRejected(
                INVALID_OPERATION,
                "operation requires one of before_page_id, after_page_id, at_start, at_end",
            )
        if present > 1:
            raise OperationRejected(
                INVALID_OPERATION, "anchors are mutually exclusive"
            )
        if at_start:
            return 0
        if at_end:
            return self._page_count(volume_id)
        if before is not None:
            return self._anchor_position(volume_id, before)
        return self._anchor_position(volume_id, after) + 1

    def _anchor_position(self, volume_id: str, page_id: str) -> int:
        target = self.get_page(volume_id, page_id, missing_ok=True)
        if target is not None:
            return target.seq
        # Distinguish "no such page anywhere" from "wrong volume": an anchor
        # that exists in another volume is a cross-volume request and must be
        # rejected before any write.
        elsewhere = self.conn.execute(
            "SELECT DISTINCT volume_id FROM pages WHERE page_id = ?", (page_id,)
        ).fetchall()
        if elsewhere:
            raise OperationRejected(
                CROSS_VOLUME_ANCHOR,
                f"anchor page {page_id!r} belongs to another volume",
                details={
                    "volume_id": volume_id,
                    "page_id": page_id,
                    "anchor_volume_ids": [r["volume_id"] for r in elsewhere],
                },
            )
        raise OperationRejected(
            ANCHOR_NOT_FOUND,
            f"anchor page {page_id!r} does not exist",
            details={"volume_id": volume_id, "page_id": page_id},
        )

    def _page_count(self, volume_id: str) -> int:
        row = self.conn.execute(
            "SELECT COUNT(*) AS c FROM pages WHERE volume_id = ?", (volume_id,)
        ).fetchone()
        return int(row["c"])

    def _shift_right(self, volume_id: str, position: int, *, amount: int = 1) -> None:
        """Open a gap at ``position`` without ever violating UNIQUE(volume, seq).

        We walk from the end backwards in single steps inside the transaction.
        Because only one row moves per statement and the target slot is always
        free at that moment, the unique constraint holds throughout.
        """

        # Move rows at positions >= position one step right, highest first.
        rows = self.conn.execute(
            "SELECT page_id, seq FROM pages WHERE volume_id = ? AND seq >= ? "
            "ORDER BY seq DESC",
            (volume_id, position),
        ).fetchall()
        for row in rows:
            self.conn.execute(
                "UPDATE pages SET seq = seq + ? WHERE volume_id = ? AND page_id = ?",
                (amount, volume_id, row["page_id"]),
            )

    def _move_page(self, volume_id: str, current: int, target: int) -> None:
        # Detach with a distinct, currently unused temporary position.
        tmp = self._page_count(volume_id) + 1_000_000
        self.conn.execute(
            "UPDATE pages SET seq = ? WHERE volume_id = ? AND seq = ?",
            (tmp, volume_id, current),
        )
        if target < current:
            # Shift [target, current) right by one, highest first.
            rows = self.conn.execute(
                "SELECT page_id, seq FROM pages WHERE volume_id = ? AND seq >= ? AND seq < ? "
                "ORDER BY seq DESC",
                (volume_id, target, current),
            ).fetchall()
            for row in rows:
                self.conn.execute(
                    "UPDATE pages SET seq = seq + 1 WHERE volume_id = ? AND page_id = ?",
                    (volume_id, row["page_id"]),
                )
        else:
            # Shift (current, target] left by one, lowest first.
            rows = self.conn.execute(
                "SELECT page_id, seq FROM pages WHERE volume_id = ? AND seq > ? AND seq <= ? "
                "ORDER BY seq ASC",
                (volume_id, current, target),
            ).fetchall()
            for row in rows:
                self.conn.execute(
                    "UPDATE pages SET seq = seq - 1 WHERE volume_id = ? AND page_id = ?",
                    (volume_id, row["page_id"]),
                )
        self.conn.execute(
            "UPDATE pages SET seq = ? WHERE volume_id = ? AND seq = ?",
            (target, volume_id, tmp),
        )

    def _write_order(self, volume_id: str, pages: list[Page]) -> bool:
        current = [p.page_id for p in self.pages(volume_id)]
        new = [p.page_id for p in pages]
        if current == new:
            return False
        # Park all rows on temporary positions first, then renumber 0..n-1.
        offset = len(current) + 1_000_000
        for i, pid in enumerate(current):
            self.conn.execute(
                "UPDATE pages SET seq = ? WHERE volume_id = ? AND page_id = ?",
                (offset + i, volume_id, pid),
            )
        for i, pid in enumerate(new):
            self.conn.execute(
                "UPDATE pages SET seq = ? WHERE volume_id = ? AND page_id = ?",
                (i, volume_id, pid),
            )
        return True

    def _index_of(self, pages: list[Page], page_id: str) -> int:
        for i, p in enumerate(pages):
            if p.page_id == page_id:
                return i
        raise OperationRejected(
            ANCHOR_NOT_FOUND, f"anchor page {page_id!r} does not exist",
            details={"page_id": page_id},
        )

    # ------------------------------------------------------------------
    # logging helpers
    # ------------------------------------------------------------------
    def _log_request(self, index: int, request: dict[str, Any], result: dict[str, Any]) -> None:
        import datetime
        import json

        expectation = (request or {}).get("expect")
        status = result.get("status")
        error_code = result.get("error_code")
        expectation_met = None
        if expectation == "reject_entire_batch":
            expectation_met = 1 if status == "rejected" else 0
        with db.write_transaction(self.conn):
            self.conn.execute(
                "INSERT INTO operation_log "
                "(request_index, batch_id, request_json, status, catalog_revision, "
                " error_code, error_detail, expectation, expectation_met, recorded_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    index,
                    result.get("batch_id") or (request or {}).get("batch_id"),
                    json.dumps(request, ensure_ascii=False, sort_keys=True),
                    status,
                    result.get("catalog_revision", self.revision()),
                    error_code,
                    json.dumps(result.get("error_detail", {}), ensure_ascii=False)
                    if error_code else None,
                    expectation,
                    expectation_met,
                    datetime.datetime.now(datetime.timezone.utc).isoformat(),
                ),
            )

    def operation_log(self) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM operation_log ORDER BY request_index, id"
        ).fetchall()
        return [dict(r) for r in rows]

    # ------------------------------------------------------------------
    @staticmethod
    def _require_str(op: dict[str, Any], key: str) -> str:
        value = op.get(key)
        if not isinstance(value, str) or not value:
            raise OperationRejected(
                INVALID_OPERATION, f"{key} must be a non-empty string",
                details={"field": key},
            )
        return value

    @staticmethod
    def _row_to_page(row: sqlite3.Row) -> Page:
        return Page(
            volume_id=row["volume_id"],
            page_id=row["page_id"],
            section_id=row["section_id"],
            label=str(row["label"]),
            active=bool(row["active"]),
            seq=int(row["seq"]),
        )
