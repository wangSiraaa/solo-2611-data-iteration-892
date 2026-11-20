"""Benchmark the documented operating costs at the task's target scale.

Target: <=20 volumes, <=200 pages each, <=500 citations.

Reports:
  * catalog load
  * citation creation (identity and resolved forms)
  * export from one snapshot
  * direct reverse lookup (indexed PINNED + interval RANGE)
  * dependency index rebuild and query
  * a same-volume move followed by export and index refresh
"""

from __future__ import annotations

import json
import random
import statistics
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from citation_system import dependency, export  # noqa: E402
from citation_system.catalog import Catalog  # noqa: E402
from citation_system.repository import CitationRepository  # noqa: E402

RNG = random.Random(2026)


def timed(fn):
    start = time.perf_counter()
    value = fn()
    return time.perf_counter() - start, value


def main() -> int:
    tmp = Path(tempfile.mkdtemp())
    catalog = Catalog.create(str(tmp / "bench.db"))
    volumes = {
        f"V{v:02d}": [
            {
                "page_id": f"p{i:03d}",
                "section_id": "body" if i < 150 else "appendix",
                "label": str(i if i < 150 else i - 150),
                "active": True,
            }
            for i in range(200)
        ]
        for v in range(1, 21)
    }
    doc = {
        "format": "volume-pages/1",
        "revision": 1,
        "volumes": [{"volume_id": v, "pages": p} for v, p in volumes.items()],
    }
    load_s, _ = timed(lambda: catalog.load_pages_document(doc))
    repo = CitationRepository(catalog)

    references = []
    for n in range(500):
        vid = f"V{RNG.randint(1, 20):02d}"
        if n % 2 == 0:
            a, b = sorted(RNG.sample(range(200), 2))
            references.append(
                {
                    "citation_id": f"C{n:03d}",
                    "text": f"citation {n}",
                    "kind": "PINNED",
                    "volume_id": vid,
                    "page_ids": [f"p{a:03d}", f"p{b:03d}"],
                }
            )
        else:
            a, b = sorted(RNG.sample(range(200), 2))
            references.append(
                {
                    "citation_id": f"C{n:03d}",
                    "text": f"citation {n}",
                    "kind": "RANGE",
                    "volume_id": vid,
                    "start_page_id": f"p{a:03d}",
                    "end_page_id": f"p{b:03d}",
                }
            )
    create_s, _ = timed(lambda: [repo.create_from_reference(r) for r in references])

    export_s, summary = timed(
        lambda: export.write_products(
            catalog.conn, tmp / "u.jsonl", tmp / "a.jsonl"
        )
    )

    samples = [("V01", f"p{RNG.randint(0, 199):03d}") for _ in range(200)]
    direct_samples = []
    for vid, pid in samples:
        dt, _ = timed(lambda: repo.citations_referencing(vid, pid))
        direct_samples.append(dt)

    index_s, index_info = timed(lambda: dependency.rebuild(catalog.conn))
    indexed_samples = []
    for vid, pid in samples:
        dt, _ = timed(lambda: dependency.referrers(catalog.conn, vid, pid))
        indexed_samples.append(dt)

    move_dt, _ = timed(
        lambda: catalog.apply_batch(
            {"operation": "move", "volume_id": "V01", "page_id": "p199",
             "before_page_id": "p000"}
        )
    )
    reexport_s, _ = timed(
        lambda: export.write_products(
            catalog.conn, tmp / "u2.jsonl", tmp / "a2.jsonl"
        )
    )
    rebuild_s, _ = timed(lambda: dependency.rebuild(catalog.conn))

    result = {
        "scale": {"volumes": 20, "pages_per_volume": 200, "citations": 500},
        "catalog_load_s": round(load_s, 4),
        "create_500_citations_s": round(create_s, 4),
        "create_per_citation_ms": round(create_s / 500 * 1000, 3),
        "export_s": round(export_s, 4),
        "export_summary": summary,
        "direct_reverse_lookup_ms": {
            "median": round(statistics.median(direct_samples) * 1000, 4),
            "p95": round(sorted(direct_samples)[int(len(direct_samples) * 0.95)] * 1000, 4),
        },
        "dependency_rebuild_s": round(index_s, 4),
        "dependency_rows": index_info["dependency_rows"],
        "indexed_reverse_lookup_ms": {
            "median": round(statistics.median(indexed_samples) * 1000, 4),
            "p95": round(sorted(indexed_samples)[int(len(indexed_samples) * 0.95)] * 1000, 4),
        },
        "same_volume_move_s": round(move_dt, 4),
        "reexport_after_move_s": round(reexport_s, 4),
        "dependency_rebuild_after_move_s": round(rebuild_s, 4),
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))
    catalog.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
