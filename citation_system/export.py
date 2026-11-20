"""Citation product export (``citation-product/1``).

Every export is evaluated from **one** read transaction.  Within that
transaction we take a complete snapshot of the catalog revision, all pages,
all citations, all pinned members and all range endpoints.  Products are then
built purely from that snapshot, so a citation's page list can never mix two
catalog states and a revision is never half-applied.

Two JSONL files are produced:

* ``usable.jsonl``          -- complete, currently usable citations
* ``needs_attention.jsonl`` -- citations with inactive endpoints/members or
                               reversed RANGE endpoints

Each citation appears in exactly one of the two files.
"""

from __future__ import annotations

import json
import os
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from . import db
from .catalog import Page
from .repository import PINNED, RANGE

FORMAT = "citation-product/1"

# Stable machine-readable reasons.
REASON_PINNED_INACTIVE = "pinned_inactive_member"
REASON_RANGE_ENDPOINT_INACTIVE = "range_endpoint_inactive"
REASON_RANGE_REVERSED = "range_endpoints_reversed"


@dataclass(frozen=True)
class Snapshot:
    revision: int
    pages: dict[tuple[str, str], Page]
    volumes: dict[str, list[Page]]  # physical order, active and inactive
    citations: list[dict[str, Any]]
    pinned: dict[str, list[tuple[str, str]]]
    ranges: dict[str, tuple[str, str, str]]  # cid -> (volume, start, end)


def load_snapshot(conn: sqlite3.Connection) -> Snapshot:
    """Read one consistent committed state.  Must be called inside a read txn."""

    revision = db.catalog_revision(conn)
    pages: dict[tuple[str, str], Page] = {}
    volumes: dict[str, list[Page]] = {}
    for row in conn.execute("SELECT * FROM pages ORDER BY volume_id, seq"):
        page = Page(
            volume_id=row["volume_id"],
            page_id=row["page_id"],
            section_id=row["section_id"],
            label=str(row["label"]),
            active=bool(row["active"]),
            seq=int(row["seq"]),
        )
        pages[(page.volume_id, page.page_id)] = page
        volumes.setdefault(page.volume_id, []).append(page)

    citations: list[dict[str, Any]] = []
    for row in conn.execute("SELECT * FROM citations ORDER BY position"):
        citations.append(
            {
                "citation_id": row["citation_id"],
                "text": row["text"],
                "kind": row["kind"],
                "volume_id": row["volume_id"],
                "position": int(row["position"]),
                "created_revision": int(row["created_revision"]),
            }
        )

    pinned: dict[str, list[tuple[str, str]]] = {}
    for row in conn.execute(
        "SELECT citation_id, volume_id, page_id FROM pinned_members "
        "ORDER BY citation_id, position"
    ):
        pinned.setdefault(row["citation_id"], []).append(
            (row["volume_id"], row["page_id"])
        )

    ranges: dict[str, tuple[str, str, str]] = {}
    for row in conn.execute("SELECT * FROM range_endpoints"):
        ranges[row["citation_id"]] = (
            row["volume_id"],
            row["start_page_id"],
            row["end_page_id"],
        )

    return Snapshot(
        revision=revision,
        pages=pages,
        volumes=volumes,
        citations=citations,
        pinned=pinned,
        ranges=ranges,
    )


def _page_fields(page: Page) -> dict[str, Any]:
    return {
        "volume_id": page.volume_id,
        "page_id": page.page_id,
        "section_id": page.section_id,
        "label": page.label,
        "active": page.active,
    }


def _evaluate_pinned(snap: Snapshot, citation: dict[str, Any]) -> dict[str, Any]:
    cid = citation["citation_id"]
    member_keys = snap.pinned.get(cid, [])
    members = [snap.pages[key] for key in member_keys]
    inactive = [p for p in members if not p.active]
    usable = not inactive
    product: dict[str, Any] = {
        "format": FORMAT,
        "catalog_revision": snap.revision,
        "citation_id": cid,
        "text": citation["text"],
        "kind": PINNED,
        "status": "usable" if usable else "needs_attention",
        "source": {
            "volume_id": citation["volume_id"],
            "page_ids": [key[1] for key in member_keys],
        },
        # The full fixed set, in its fixed order, including inactive members.
        "pages": [_page_fields(p) for p in members],
        "reason": None if usable else REASON_PINNED_INACTIVE,
    }
    if not usable:
        product["detail"] = {
            "inactive_page_ids": [p.page_id for p in inactive],
            "member_count": len(members),
            "active_member_count": len(members) - len(inactive),
        }
    return product


def _evaluate_range(snap: Snapshot, citation: dict[str, Any]) -> dict[str, Any]:
    cid = citation["citation_id"]
    volume_id, start_id, end_id = snap.ranges[cid]
    start = snap.pages[(volume_id, start_id)]
    end = snap.pages[(volume_id, end_id)]
    start_active, end_active = start.active, end.active
    reversed_order = start.seq > end.seq

    source = {
        "volume_id": volume_id,
        "start_page_id": start_id,
        "end_page_id": end_id,
    }

    if not start_active or not end_active or reversed_order:
        if not start_active or not end_active:
            reason = REASON_RANGE_ENDPOINT_INACTIVE
        else:
            reason = REASON_RANGE_REVERSED
        return {
            "format": FORMAT,
            "catalog_revision": snap.revision,
            "citation_id": cid,
            "text": citation["text"],
            "kind": RANGE,
            "status": "needs_attention",
            "source": source,
            # Endpoints in their original source order -- never swapped.
            "pages": [_page_fields(start), _page_fields(end)],
            "reason": reason,
            "detail": {
                "start_active": start_active,
                "end_active": end_active,
                "start_position": start.seq,
                "end_position": end.seq,
                "reversed": reversed_order,
            },
        }

    lo, hi = sorted((start.seq, end.seq))
    ordered = snap.volumes[volume_id][lo : hi + 1]
    # A usable range contains all *active* pages between the endpoints,
    # inclusive; inactive pages remain in the physical order but are not
    # cited content.
    active_pages = [p for p in ordered if p.active]
    return {
        "format": FORMAT,
        "catalog_revision": snap.revision,
        "citation_id": cid,
        "text": citation["text"],
        "kind": RANGE,
        "status": "usable",
        "source": source,
        "pages": [_page_fields(p) for p in active_pages],
        "reason": None,
    }


def evaluate_snapshot(snap: Snapshot) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    usable: list[dict[str, Any]] = []
    attention: list[dict[str, Any]] = []
    for citation in snap.citations:
        if citation["kind"] == PINNED:
            product = _evaluate_pinned(snap, citation)
        elif citation["kind"] == RANGE:
            product = _evaluate_range(snap, citation)
        else:  # pragma: no cover - schema constrains this
            raise ValueError(f"unknown citation kind {citation['kind']!r}")
        (usable if product["status"] == "usable" else attention).append(product)
    return usable, attention


def collect_products(conn: sqlite3.Connection) -> tuple[int, list[dict[str, Any]], list[dict[str, Any]]]:
    """Collect both product lists from a single read snapshot."""

    with db.read_snapshot_transaction(conn):
        snap = load_snapshot(conn)
        usable, attention = evaluate_snapshot(snap)
        return snap.revision, usable, attention


def _write_jsonl_atomic(path: Path, rows: Iterable[dict[str, Any]]) -> int:
    """Write one JSONL file to a sibling temp file (fsynced, not yet replaced)."""

    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    count = 0
    with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
        for row in rows:
            fh.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")))
            fh.write("\n")
            count += 1
        fh.flush()
        os.fsync(fh.fileno())
    return count


def write_products(
    conn: sqlite3.Connection,
    usable_path: str | Path,
    attention_path: str | Path,
) -> dict[str, Any]:
    """Export both JSONL files from one committed catalog state.

    The two product lists are produced together from a single read snapshot
    and validated before anything is replaced on disk.  Both files are then
    staged to temp files and renamed.  Each individual file is therefore
    never visible half-written.  (The two renames are sequential; if the
    process dies between them, the second file is the previous export.  A
    consumer that needs the *pair* to switch together should ingest a whole
    export directory, e.g. via a symlink swap, rather than two loose paths.)
    """

    usable_path = Path(usable_path)
    attention_path = Path(attention_path)
    revision, usable, attention = collect_products(conn)

    problems = validate_products(usable + attention)
    if problems:
        raise ValueError("citation product contract violated: " + "; ".join(problems))

    usable_count = _write_jsonl_atomic(usable_path, usable)
    attention_count = _write_jsonl_atomic(attention_path, attention)
    os.replace(usable_path.with_name(usable_path.name + ".tmp"), usable_path)
    os.replace(attention_path.with_name(attention_path.name + ".tmp"), attention_path)
    return {
        "catalog_revision": revision,
        "usable": usable_count,
        "needs_attention": attention_count,
        "total": usable_count + attention_count,
        "usable_path": str(usable_path),
        "needs_attention_path": str(attention_path),
    }


def validate_products(rows: list[dict[str, Any]]) -> list[str]:
    """Return a list of contract problems (empty when valid).

    This mirrors the public field contract from the task README; it is used by
    the CLI and tests so a malformed export cannot be shipped silently.
    """

    problems: list[str] = []
    required = [
        "format", "catalog_revision", "citation_id", "text", "kind",
        "status", "source", "pages", "reason",
    ]
    for i, row in enumerate(rows, 1):
        missing = [k for k in required if k not in row]
        if missing:
            problems.append(f"row {i}: missing {missing}")
            continue
        if row["format"] != FORMAT:
            problems.append(f"row {i}: bad format {row['format']!r}")
        if row["kind"] not in (PINNED, RANGE):
            problems.append(f"row {i}: bad kind {row['kind']!r}")
        if row["status"] not in ("usable", "needs_attention"):
            problems.append(f"row {i}: bad status {row['status']!r}")
        if row["status"] == "usable":
            if row["reason"] is not None:
                problems.append(f"row {i}: usable citation has a reason")
            if any(not p.get("active") for p in row["pages"]):
                problems.append(f"row {i}: usable citation has inactive page")
        else:
            if not isinstance(row["reason"], str) or not row["reason"]:
                problems.append(f"row {i}: needs_attention needs a reason")
        for p in row["pages"]:
            for key in ("volume_id", "page_id", "section_id", "label", "active"):
                if key not in p:
                    problems.append(f"row {i}: page missing {key}")
            if not isinstance(p.get("label"), str):
                problems.append(f"row {i}: page label must be a string")
            if not isinstance(p.get("active"), bool):
                problems.append(f"row {i}: page active must be boolean")
        if row["kind"] == PINNED:
            if "page_ids" not in row["source"]:
                problems.append(f"row {i}: PINNED source needs page_ids")
        else:
            if "start_page_id" not in row["source"] or "end_page_id" not in row["source"]:
                problems.append(f"row {i}: RANGE source needs endpoints")
    return problems
