"""End-to-end demonstration for task 726.

Runs the real catalog + repository + export pipeline, writes one immutable
export directory per step, and invokes the *unmodified* read-only
``materials/726/citation_client.py`` against the actual JSONL files.

Nothing here parses PDFs or images.  Every export directory is kept so the
same client can be run against old and new products at any time.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from citation_system import dependency, export  # noqa: E402
from citation_system.catalog import Catalog  # noqa: E402
from citation_system.repository import CitationRepository  # noqa: E402

MATERIALS = ROOT / "materials" / "726"
WORK = ROOT / "work" / "demo"
EXPORTS = ROOT / "exports" / "scenario"
CLIENT = MATERIALS / "citation_client.py"
STEP_INDEX: list[dict] = []


def reset(path: Path) -> None:
    if path.exists():
        path.unlink()
    for suffix in ("-wal", "-shm"):
        extra = Path(str(path) + suffix)
        if extra.exists():
            extra.unlink()


def write_step(
    catalog: Catalog,
    name: str,
    title: str,
    *,
    run_client: bool = True,
) -> dict:
    out_dir = EXPORTS / name
    out_dir.mkdir(parents=True, exist_ok=True)
    util = out_dir / "usable.jsonl"
    attn = out_dir / "needs_attention.jsonl"
    summary = export.write_products(catalog.conn, util, attn)
    step = {
        "step": name,
        "title": title,
        "catalog_revision": summary["catalog_revision"],
        "usable": summary["usable"],
        "needs_attention": summary["needs_attention"],
        "client_output": None,
    }
    if run_client:
        out = subprocess.run(
            [sys.executable, str(CLIENT), str(util), str(attn)],
            capture_output=True, text=True, check=True,
        )
        step["client_output"] = out.stdout.splitlines()
        print(f"\n=== {name}: {title} (revision {summary['catalog_revision']}) ===")
        for line in step["client_output"]:
            print("   ", line)
    STEP_INDEX.append(step)
    return step


def apply(catalog: Catalog, request: dict, label: str) -> dict:
    try:
        result = catalog.apply_batch(request)
        desc = {"label": label, "status": result["status"],
                "catalog_revision": result["catalog_revision"]}
    except Exception as exc:  # noqa: BLE001 - the rejection is the observation
        desc = {
            "label": label,
            "status": "rejected",
            "error_code": getattr(exc, "code", exc.__class__.__name__),
            "error_detail": getattr(exc, "details", {}),
        }
    print(f"  operation {label}: {json.dumps(desc, ensure_ascii=False)}")
    return desc


def main() -> int:
    if EXPORTS.exists():
        shutil.rmtree(EXPORTS)
    EXPORTS.mkdir(parents=True, exist_ok=True)
    WORK.mkdir(parents=True, exist_ok=True)
    db_path = WORK / "demo.db"
    reset(db_path)

    catalog = Catalog.create(str(db_path))
    pages_doc = json.loads((MATERIALS / "pages.json").read_text(encoding="utf-8"))
    catalog.load_pages_document(pages_doc)
    repo = CitationRepository(catalog)
    for line in (MATERIALS / "references.jsonl").read_text(encoding="utf-8").splitlines():
        if line.strip():
            repo.create_from_reference(json.loads(line))
    dependency.rebuild(catalog.conn)

    print("Initial pages:", {
        v: [(p.page_id, p.section_id, p.label, p.active) for p in catalog.pages(v)]
        for v in catalog.volume_ids()
    })

    # 0. Initial state, exactly the hand-worked example.
    write_step(catalog, "00_initial", "initial catalog and references")

    # 1. Insert an interior page: RANGE grows, PINNED unchanged.
    apply(
        catalog,
        {
            "operation": "insert",
            "volume_id": "V1",
            "before_page_id": "p3",
            "page": {"page_id": "px", "section_id": "body", "label": "2a", "active": True},
        },
        "insert px between p2 and p3",
    )
    write_step(catalog, "01_interior_insert", "interior insert widens RANGE only")

    # 2. Same-volume move reverses the RANGE endpoints.
    apply(
        catalog,
        {"operation": "move", "volume_id": "V1", "page_id": "p4", "before_page_id": "p2"},
        "move p4 before p2",
    )
    write_step(catalog, "02_range_reversed", "same-volume move reverses RANGE; PINNED intact")

    # 3. Deactivate a PINNED member: PINNED goes to attention, members kept.
    apply(
        catalog,
        {"operation": "deactivate", "volume_id": "V1", "page_id": "p3"},
        "deactivate pinned p3",
    )
    write_step(catalog, "03_pinned_inactive", "fixed member inactive; not dropped")

    # 4. Reactivate p3 and restore p4 to its place: both citations reopen.
    apply(
        catalog,
        {"operation": "activate", "volume_id": "V1", "page_id": "p3"},
        "reactivate p3",
    )
    apply(
        catalog,
        {"operation": "move", "volume_id": "V1", "page_id": "p4", "after_page_id": "px"},
        "move p4 back after px",
    )
    write_step(catalog, "04_reopened", "reactivation + move back reopen both citations")

    # 5. RANGE endpoint deactivated.
    apply(
        catalog,
        {"operation": "deactivate", "volume_id": "V1", "page_id": "p4"},
        "deactivate RANGE endpoint p4",
    )
    write_step(catalog, "05_endpoint_inactive", "RANGE endpoint inactive -> attention")

    # 6. Endpoint reactivated: usable again without recreating the citation.
    apply(
        catalog,
        {"operation": "activate", "volume_id": "V1", "page_id": "p4"},
        "reactivate endpoint p4",
    )
    write_step(catalog, "06_endpoint_reopened", "endpoint reactivation reopens RANGE")

    # 7. Cross-volume move is rejected; nothing changes, export is identical
    #    *content* but the same revision (no write happened).
    rev_before = catalog.revision()
    apply(
        catalog,
        {
            "operation": "move",
            "volume_id": "V1",
            "target_volume_id": "V2",
            "page_id": "p2",
            "before_page_id": "p1",
        },
        "cross-volume move",
    )
    assert catalog.revision() == rev_before, "rejected batch must not bump the revision"
    V1_before = [(p.page_id, p.active) for p in catalog.pages("V1")]
    V2_before = [(p.page_id, p.active) for p in catalog.pages("V2")]
    assert [(p.page_id, p.active) for p in catalog.pages("V1")] == V1_before
    assert [(p.page_id, p.active) for p in catalog.pages("V2")] == V2_before
    write_step(catalog, "07_cross_volume_rejected", "cross-volume move rejected, no change")

    # Dependency index is current and agrees with direct reverse lookup.
    dep = dependency.rebuild(catalog.conn)
    print("\nDependency index:", dep)
    for pid in ("p1", "p2", "px", "p3", "p4"):
        direct = repo.citations_referencing("V1", pid)
        indexed = dependency.referrers(catalog.conn, "V1", pid)
        assert [(d["citation_id"], d["role"]) for d in direct] == [
            (i["citation_id"], i["role"]) for i in indexed
        ], f"reverse lookup mismatch for {pid}"
    print("Reverse lookup (direct == indexed) verified for V1/p1,p2,px,p3,p4")

    catalog.close()

    # --------------------------------------------------------------
    # Separate catalog for resolution edge cases, so the main scenario
    # export stays clean.
    # --------------------------------------------------------------
    edge_db = WORK / "edge_cases.db"
    reset(edge_db)
    edge = Catalog.create(str(edge_db))
    edge.load_pages_document(
        {
            "format": "volume-pages/1",
            "revision": 1,
            "volumes": [
                {
                    "volume_id": "E1",
                    "pages": [
                        {"page_id": "e1", "section_id": "body", "label": "3", "active": True},
                        {"page_id": "e2", "section_id": "body", "label": "3", "active": True},
                        {"page_id": "e3", "section_id": "appendix", "label": "3", "active": True},
                        {"page_id": "e4", "section_id": "body", "label": "04", "active": True},
                        {"page_id": "e5", "section_id": "body", "label": "4", "active": True},
                    ],
                }
            ],
        }
    )
    edge_repo = CitationRepository(edge)
    edge_cases: dict = {}

    def capture(key, fn):
        try:
            edge_cases[key] = {"ok": True, "value": fn()}
        except Exception as exc:  # noqa: BLE001
            edge_cases[key] = {"ok": False, "error": getattr(exc, "code", type(exc).__name__),
                               "details": getattr(exc, "details", {})}

    capture("same_section_same_label", lambda: edge.resolve("E1", "body", "3"))
    capture("other_section_same_label", lambda: edge.resolve("E1", "appendix", "3").page_id)
    capture("different_string_labels", lambda: (
        edge.resolve("E1", "body", "04").page_id,
        edge.resolve("E1", "body", "4").page_id,
    ))
    capture("leading_zero_not_found", lambda: edge.resolve("E1", "body", "03"))
    # A citation created by resolution against an ambiguous section writes
    # nothing at all.
    agg = {"ok": True}
    try:
        edge_repo.create_from_reference(
            {
                "citation_id": "C_EDGE",
                "text": "ambiguous",
                "kind": "RANGE",
                "volume_id": "E1",
                "start_section_id": "body",
                "start_label": "3",
                "end_section_id": "body",
                "end_label": "4",
            }
        )
    except Exception as exc:  # noqa: BLE001
        agg = {"ok": False, "error": getattr(exc, "code", type(exc).__name__)}
    edge_cases["ambiguous_create_writes_nothing"] = {
        **agg,
        "citations_after": len(edge_repo.all()),
    }
    print("\nResolution edge cases:")
    print(json.dumps(edge_cases, ensure_ascii=False, indent=2))
    edge.close()

    index = {
        "exports_root": str(EXPORTS.relative_to(ROOT)),
        "client": str(CLIENT.relative_to(ROOT)),
        "steps": STEP_INDEX,
        "edge_cases": edge_cases,
    }
    (EXPORTS / "index.json").write_text(
        json.dumps(index, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"\nWrote {EXPORTS / 'index.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
