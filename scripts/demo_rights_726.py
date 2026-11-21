"""End-to-end royalty settlement demonstration for task 726.

Runs entirely on local synthetic data.  It builds a small bound-periodical
scenario matching the task's worked examples:

* the same printed page 3 appears twice (pages ``p`` and ``q``);
* p is split A/B, q belongs to B;
* column one pins p with a fixed excerpt, column two covers p..q with a range;
* pricing groups decide whether the shared page is charged once or twice;
* an account correction reroutes A's share to B;
* thresholds carry balances across two issues;
* a compound intermediate account merges several paths;
* an interior insert changes the range's priced pages in the next issue;
* concurrent seal attempts settle exactly once.

Run ``python3 scripts/demo_rights_726.py``.  Every issue's sealed voucher is
written to ``exports/rights/`` so history can be explained offline.
"""

from __future__ import annotations

import json
import shutil
import sys
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from citation_system import db  # noqa: E402
from citation_system.catalog import Catalog  # noqa: E402
from citation_system.repository import CitationRepository  # noqa: E402
from citation_system.rights import RightsLedger  # noqa: E402
from citation_system.settlement import SettlementLedger  # noqa: E402

WORK = ROOT / "work" / "rights_demo"
EXPORTS = ROOT / "exports" / "rights"


def reset(path: Path) -> None:
    for p in (path, Path(str(path) + "-wal"), Path(str(path) + "-shm")):
        if p.exists():
            p.unlink()


def fresh_catalog(name: str = "rights") -> tuple[Catalog, CitationRepository]:
    WORK.mkdir(parents=True, exist_ok=True)
    path = WORK / f"{name}.db"
    reset(path)
    catalog = Catalog.create(str(path))
    catalog.load_pages_document(
        {
            "format": "volume-pages/1",
            "revision": 1,
            "volumes": [
                {
                    "volume_id": "V1",
                    "pages": [
                        {"page_id": "p1", "section_id": "body", "label": "2", "active": True},
                        {"page_id": "p", "section_id": "body", "label": "3", "active": True},
                        {"page_id": "q", "section_id": "body", "label": "3", "active": True},
                    ],
                }
            ],
        }
    )
    repo = CitationRepository(catalog)
    repo.create_pinned("C_FIXED", "固定摘录 p", "V1", ["p"])
    repo.create_range("C_RANGE", "连续摘录 p..q", "V1", "p", "q")
    return catalog, repo


def base_rights(catalog: Catalog) -> RightsLedger:
    rights = RightsLedger(catalog)
    rights.create_edition("E")
    rights.add_page_shares("E", "V1", "p", "2020-01-01", {"A": "1/2", "B": "1/2"})
    rights.add_page_shares("E", "V1", "q", "2020-01-01", {"B": "1"})
    rights.add_page_shares("E", "V1", "p1", "2020-01-01", {"B": "1"})
    return rights


def summary(voucher: dict) -> dict:
    return {
        "issue": voucher["issue_id"],
        "total_cents": voucher["total_cents"],
        "allotted": voucher["allocation"]["allotted"],
        "payees": {
            p["payee_id"]: {
                "prior": p["prior_balance"],
                "allotment": p["allotment"],
                "threshold": p["threshold"],
                "paid": p["paid"],
                "carried": p["carried"],
            }
            for p in voucher["payees"]
        },
        "sha256": voucher["voucher_sha256"][:16] + "…",
    }


def main() -> int:
    if EXPORTS.exists():
        shutil.rmtree(EXPORTS)
    EXPORTS.mkdir(parents=True, exist_ok=True)
    steps: dict = {}

    # ---- 1. same group: shared page charged once -------------------
    catalog, _ = fresh_catalog()
    base_rights(catalog)
    S = SettlementLedger(catalog)
    S.create_pricing_group("G", 3)
    S.create_column("K1", "G")
    S.create_column("K2", "G")
    S.create_issue("I1", "2021-05-01", "E")
    S.register("I1", "K1", "C_FIXED")
    S.register("I1", "K2", "C_RANGE")
    v = S.settle("I1")
    steps["1_same_group_once"] = summary(v)
    assert v["total_cents"] == 6 and v["allocation"]["allotted"] == {"A": 2, "B": 4}
    catalog.close()

    # ---- 2. column two in a different same-priced group -> 9c ------
    catalog2, _ = fresh_catalog("rights2")
    base_rights(catalog2)
    S2 = SettlementLedger(catalog2)
    S2.create_pricing_group("G", 3)
    S2.create_pricing_group("H", 3)
    S2.create_column("K1", "G")
    S2.create_column("K3", "H")
    S2.create_issue("I1", "2021-05-01", "E")
    S2.register("I1", "K1", "C_FIXED")
    S2.register("I1", "K3", "C_RANGE")
    v2 = S2.settle("I1")
    steps["2_two_groups"] = summary(v2)
    assert v2["total_cents"] == 9 and v2["allocation"]["allotted"] == {"A": 3, "B": 6}
    catalog2.close()

    # ---- 3. correction: A's share goes entirely to B ---------------
    catalog3, _ = fresh_catalog("rights3")
    rights3 = base_rights(catalog3)
    rights3.add_account_allocations("E", "A", "2020-06-01", {"B": "1"})
    S3 = SettlementLedger(catalog3)
    S3.create_pricing_group("G", 3)
    S3.create_column("K1", "G")
    S3.create_column("K2", "G")
    S3.create_issue("I1", "2021-05-01", "E")
    S3.register("I1", "K1", "C_FIXED")
    S3.register("I1", "K2", "C_RANGE")
    v3 = S3.settle("I1")
    steps["3_reroute"] = summary(v3)
    assert v3["allocation"]["allotted"] == {"B": 6}
    catalog3.close()

    # ---- 4. thresholds carry across issues -------------------------
    catalog4, _ = fresh_catalog("rights4")
    base_rights(catalog4)
    S4 = SettlementLedger(catalog4)
    S4.create_pricing_group("G", 3)
    S4.create_column("K1", "G")
    S4.create_column("K2", "G")
    S4.set_threshold("A", 5)
    S4.set_threshold("B", 5)
    for issue, date in (("I1", "2021-05-01"), ("I2", "2021-06-01")):
        S4.create_issue(issue, date, "E")
        S4.register(issue, "K1", "C_FIXED")
        S4.register(issue, "K2", "C_RANGE")
        steps[f"4_threshold_{issue}"] = summary(S4.settle(issue))
    assert S4.pending_balance("A")["balance"] == 4
    assert S4.pending_balance("B")["balance"] == 0

    # ---- 5. compound multi-path entitlement + insert + concurrency --
    catalog5, _ = fresh_catalog("rights5")
    rights5 = base_rights(catalog5)
    # p: A 1/2, B 1/2. A -> M 1/2 + B 1/2; M -> B 1.
    rights5.add_account_allocations("E", "A", "2020-02-01", {"M": "1/2", "B": "1/2"})
    rights5.add_account_allocations("E", "M", "2020-02-01", {"B": "1"})
    ent = rights5.resolve_page_entitlement("E", "V1", "p", "2021-05-01")
    steps["5_compound_entitlement"] = {
        "page": "V1/p",
        "final_shares": ent.shares_as_strings(),
        "channels": [p["channel"] for p in ent.paths],
    }
    assert ent.shares == {"B": 1} and len(ent.paths) == 3

    S5 = SettlementLedger(catalog5)
    S5.create_pricing_group("G", 3)
    S5.create_column("K1", "G")
    S5.create_column("K2", "G")
    S5.create_issue("I1", "2021-05-01", "E")
    S5.register("I1", "K1", "C_FIXED")
    S5.register("I1", "K2", "C_RANGE")
    steps["5_before_insert"] = summary(S5.settle("I1"))
    # An interior insert: next issue prices the new page too.
    catalog5.apply_operations(
        [{"operation": "insert", "volume_id": "V1", "before_page_id": "q",
          "page": {"page_id": "px", "section_id": "body", "label": "2a"}}]
    )
    rights5.add_page_shares("E", "V1", "px", "2020-01-01", {"B": "1"})
    S5.create_issue("I2", "2021-06-01", "E")
    S5.register("I2", "K1", "C_FIXED")
    S5.register("I2", "K2", "C_RANGE")
    steps["5_insert_issue"] = summary(S5.settle("I2"))

    # Concurrent retries on a third issue must settle exactly once.
    S5.create_issue("I3", "2021-07-01", "E")
    S5.register("I3", "K1", "C_FIXED")
    S5.register("I3", "K2", "C_RANGE")
    results: list[dict] = []
    errors: list[BaseException] = []
    db_path = str(WORK / "rights5.db")

    def worker():
        c = Catalog.open(db_path)
        try:
            results.append(SettlementLedger(c).settle("I3"))
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)
        finally:
            c.close()

    threads = [threading.Thread(target=worker) for _ in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    steps["5_concurrent"] = {
        "attempts": len(results) + len(errors),
        "errors": [str(e) for e in errors],
        "distinct_vouchers": len({r["voucher_sha256"] for r in results}),
    }
    assert not errors and len({r["voucher_sha256"] for r in results}) == 1

    # Write each recorded voucher so history is explainable offline.
    for issue in ("I1", "I2", "I3"):
        voucher = S5.get_voucher(issue)
        if voucher is not None:
            (EXPORTS / f"{issue}.voucher.json").write_text(
                json.dumps(voucher, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
    docs = {
        "scenario": "task 726 royalty settlement",
        "steps": steps,
        "vouchers_verified": {i: S5.verify_voucher(i) for i in ("I1", "I2", "I3")},
    }
    (EXPORTS / "index.json").write_text(
        json.dumps(docs, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )

    print(json.dumps(steps, ensure_ascii=False, indent=2))
    print("\nVouchers verified:", docs["vouchers_verified"])
    catalog5.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
