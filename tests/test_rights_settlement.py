"""Tests for rights editions (权属版) and per-issue royalty settlement.

These exercise the synthetic contract from the task:

* page shares and account allocations are dated versions; a lookup uses the
  version effective on the publication date, never today's owner;
* multi-path compound entitlements merge to final payees;
* same-group cross-column reuse pays a page once, different groups pay each;
* exact per-payee amounts are floored and cents are distributed by descending
  remainder then ascending payee id;
* pending balances, thresholds, atomic sealing, retry idempotency, and
  independently verifiable historical vouchers.
"""

from __future__ import annotations

import json
import shutil
import tempfile
import threading
import unittest
from fractions import Fraction
from pathlib import Path

from citation_system.catalog import Catalog
from citation_system.errors import OperationRejected
from citation_system.repository import CitationRepository
from citation_system.rights import RightsLedger
from citation_system.settlement import SettlementLedger


def load_worked_pair(cat: Catalog, rates=None):
    """The task's worked example: p,q printed as page 3; p split A/B, q all B."""

    cat.load_pages_document(
        {
            "format": "volume-pages/1",
            "revision": 1,
            "volumes": [
                {
                    "volume_id": "V1",
                    "pages": [
                        {"page_id": "p", "section_id": "body", "label": "3", "active": True},
                        {"page_id": "q", "section_id": "body", "label": "3", "active": True},
                    ],
                }
            ],
        }
    )
    repo = CitationRepository(cat)
    # Column one cites fixed p; column two cites the range p..q.
    repo.create_pinned("CP", "pin p", "V1", ["p"])
    repo.create_range("CR", "range p-q", "V1", "p", "q")
    rights = RightsLedger(cat)
    rights.create_edition("E")
    rights.add_page_shares("E", "V1", "p", "2020-01-01", {"A": "1/2", "B": "1/2"})
    rights.add_page_shares("E", "V1", "q", "2020-01-01", {"B": "1"})
    return repo, rights


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.db_path = self.tmp / "cat.db"
        self.cat = Catalog.create(str(self.db_path))

    def tearDown(self):
        self.cat.close()
        shutil.rmtree(self.tmp)

    def settle(self, issue_id):
        return SettlementLedger(self.cat).settle(issue_id)


class WorkedExampleTests(Base):
    def test_same_group_pays_each_page_once(self):
        _, _ = load_worked_pair(self.cat)
        S = SettlementLedger(self.cat)
        S.create_pricing_group("G", 3)
        S.create_column("K1", "G")
        S.create_column("K2", "G")
        S.create_issue("I1", "2021-05-01", "E")
        S.register("I1", "K1", "CP")
        S.register("I1", "K2", "CR")
        voucher = S.settle("I1")
        # p (3c) and q (3c) each once -> 6c total; A=1.5, B=4.5.
        self.assertEqual(voucher["total_cents"], 6)
        self.assertEqual(voucher["allocation"]["allotted"], {"A": 2, "B": 4})
        payees = {p["payee_id"]: p for p in voucher["payees"]}
        self.assertEqual(payees["A"]["paid"], 2)
        self.assertEqual(payees["B"]["paid"], 4)

    def test_different_groups_price_the_page_twice(self):
        load_worked_pair(self.cat)
        S = SettlementLedger(self.cat)
        S.create_pricing_group("G", 3)
        S.create_pricing_group("H", 3)
        S.create_column("K1", "G")
        S.create_column("K2", "H")
        S.create_issue("I1", "2021-05-01", "E")
        S.register("I1", "K1", "CP")
        S.register("I1", "K2", "CR")
        voucher = S.settle("I1")
        # G: p (3c). H: p + q (6c). Total 9c.
        # exact A = 1.5 (p in G) + 1.5 (p in H) = 3; B = 1.5 + 3 + 1.5 = 6.
        self.assertEqual(voucher["total_cents"], 9)
        self.assertEqual(voucher["allocation"]["allotted"], {"A": 3, "B": 6})

    def test_column_reassignment_moves_the_charge_to_the_new_group(self):
        # The prompt's "move column two into another same-priced group" gives
        # a total of 9c; here that is p in G (3c) + p,q in H (6c) = 9c.
        load_worked_pair(self.cat)
        S = SettlementLedger(self.cat)
        S.create_pricing_group("G", 3)
        S.create_pricing_group("H", 3)
        S.create_column("K1", "G")
        S.create_column("K2", "G")
        S.create_issue("I1", "2021-05-01", "E")
        S.register("I1", "K1", "CP")
        S.register("I1", "K2", "CR")
        S.reassign_column("K2", "H")
        voucher = S.settle("I1")
        # G: p only (3c). H: p + q (6c). Total 9c; A=3, B=6.
        self.assertEqual(voucher["total_cents"], 9)
        self.assertEqual(voucher["allocation"]["allotted"], {"A": 3, "B": 6})

    def test_correcting_account_allocation_sends_share_to_another_payee(self):
        _, rights = load_worked_pair(self.cat)
        rights.add_account_allocations("E", "A", "2020-06-01", {"B": "1"})
        S = SettlementLedger(self.cat)
        S.create_pricing_group("G", 3)
        S.create_column("K1", "G")
        S.create_column("K2", "G")
        S.create_issue("I1", "2021-05-01", "E")
        S.register("I1", "K1", "CP")
        S.register("I1", "K2", "CR")
        voucher = S.settle("I1")
        self.assertEqual(voucher["total_cents"], 6)
        self.assertEqual(voucher["allocation"]["allotted"], {"B": 6})
        self.assertNotIn("A", voucher["allocation"]["allotted"])

    def test_thresholds_carry_then_pay(self):
        load_worked_pair(self.cat)
        S = SettlementLedger(self.cat)
        S.create_pricing_group("G", 3)
        S.create_column("K1", "G")
        S.create_column("K2", "G")
        S.set_threshold("A", 5)
        S.set_threshold("B", 5)
        S.create_issue("I1", "2021-05-01", "E")
        S.register("I1", "K1", "CP")
        S.register("I1", "K2", "CR")
        v1 = self.settle("I1")
        p1 = {p["payee_id"]: p for p in v1["payees"]}
        # Both below 5 on issue one.
        self.assertEqual((p1["A"]["paid"], p1["A"]["carried"]), (0, 2))
        self.assertEqual((p1["B"]["paid"], p1["B"]["carried"]), (0, 4))

        S.create_issue("I2", "2021-06-01", "E")
        S.register("I2", "K1", "CP")
        S.register("I2", "K2", "CR")
        v2 = self.settle("I2")
        p2 = {p["payee_id"]: p for p in v2["payees"]}
        # A: 2+2=4 still below 5 -> carry. B: 4+4=8 meets 5 -> pay 8.
        self.assertEqual((p2["A"]["available"], p2["A"]["paid"], p2["A"]["carried"]), (4, 0, 4))
        self.assertEqual((p2["B"]["available"], p2["B"]["paid"], p2["B"]["carried"]), (8, 8, 0))
        self.assertEqual(S.pending_balance("A")["balance"], 4)
        self.assertEqual(S.pending_balance("B")["balance"], 0)


class RightsVersioningTests(Base):
    def setUp(self):
        super().setUp()
        load_worked_pair(self.cat)

    def test_old_issue_uses_old_version_not_today(self):
        rights = RightsLedger(self.cat)
        # A correction effective 2022 sends A's share to B, but 2021 stays.
        rights.add_page_shares(
            "E", "V1", "p", "2022-01-01", {"B": "1"}
        )
        old = rights.resolve_page_entitlement("E", "V1", "p", "2021-05-01")
        new = rights.resolve_page_entitlement("E", "V1", "p", "2023-05-01")
        self.assertEqual(old.shares, {"A": Fraction(1, 2), "B": Fraction(1, 2)})
        self.assertEqual(new.shares, {"B": Fraction(1)})

    def test_no_version_effective_is_needs_review(self):
        rights = RightsLedger(self.cat)
        ent, problem = rights.try_resolve_page("E", "V1", "p", "2019-01-01")
        self.assertIsNone(ent)
        self.assertEqual(problem["reason"], "declaration_not_effective")

    def test_duplicate_version_rejected_unless_replace(self):
        rights = RightsLedger(self.cat)
        with self.assertRaises(OperationRejected) as ctx:
            rights.add_page_shares("E", "V1", "p", "2020-01-01", {"A": "1"})
        self.assertEqual(ctx.exception.code, "rights_version_exists")
        rights.add_page_shares("E", "V1", "p", "2020-01-01", {"A": "1"}, replace=True)
        self.assertEqual(
            rights.page_shares_at("E", "V1", "p", "2021-01-01"),
            [{"account_id": "A", "share": "1/1"}],
        )

    def test_shares_must_sum_to_one_and_be_non_negative(self):
        rights = RightsLedger(self.cat)
        with self.assertRaises(OperationRejected):
            rights.add_page_shares("E", "V1", "p", "2023-01-01", {"A": "1/2", "B": "1/4"})
        with self.assertRaises(OperationRejected):
            rights.add_page_shares("E", "V1", "p", "2023-01-01", {"A": "-1", "B": "2"})

    def test_bulk_correction_is_atomic(self):
        rights = RightsLedger(self.cat)
        with self.assertRaises(OperationRejected):
            rights.apply_batch(
                [
                    {
                        "kind": "page_shares", "edition_id": "E", "volume_id": "V1",
                        "page_id": "p", "effective_date": "2023-01-01", "shares": {"A": "1"},
                    },
                    {
                        "kind": "page_shares", "edition_id": "E", "volume_id": "V1",
                        "page_id": "q", "effective_date": "2023-01-01", "shares": {"B": "1/2"},
                    },
                ]
            )
        # Nothing from the failed batch was written.
        self.assertEqual(rights.page_share_versions("E", "V1", "q"), ["2020-01-01"])


class CompoundPathTests(Base):
    def setUp(self):
        super().setUp()
        load_worked_pair(self.cat)
        self.rights = RightsLedger(self.cat)

    def test_intermediate_accounts_merge_and_keep_basis(self):
        rights = self.rights
        # p: A 1/2, B 1/2. A pays half to M and half to B (compound path).
        # M pays all to B. So A 1/2 -> M 1/4 + B 1/4; M 1/4 -> B 1/4.
        # Final: B = 1/2 (direct) + 1/4 (via A) + 1/4 (via M) = 1.
        rights.add_account_allocations("E", "A", "2020-02-01", {"M": "1/2", "B": "1/2"})
        rights.add_account_allocations("E", "M", "2020-02-01", {"B": "1"})
        ent = rights.resolve_page_entitlement("E", "V1", "p", "2021-01-01")
        self.assertEqual(ent.shares, {"B": Fraction(1)})
        # Three distinct channels reach B.
        channels = sorted(tuple(p["channel"]) for p in ent.paths)
        self.assertIn(("B",), channels)
        self.assertIn(("A", "B"), channels)
        self.assertIn(("A", "M", "B"), channels)
        self.assertEqual(ent.basis["allocation_versions"]["A"]["effective_date"], "2020-02-01")

    def test_cycle_is_needs_review(self):
        self.rights.add_account_allocations("E", "A", "2020-02-01", {"B": "1"})
        self.rights.add_account_allocations("E", "B", "2020-02-01", {"A": "1"})
        ent, problem = self.rights.try_resolve_page("E", "V1", "p", "2021-01-01")
        self.assertIsNone(ent)
        self.assertEqual(problem["reason"], "allocation_cycle")

    def test_allocation_not_effective_is_needs_review(self):
        self.rights.add_account_allocations("E", "A", "2022-01-01", {"B": "1"})
        ent, problem = self.rights.try_resolve_page("E", "V1", "p", "2021-01-01")
        self.assertIsNone(ent)
        self.assertEqual(problem["reason"], "allocation_not_effective")


class AllocationRoundingTests(Base):
    def test_floor_then_largest_remainder_then_payee_id(self):
        # Three equal payees over 1 cent: all tie at 1/3 remainder, payout the
        # single cent to the smallest payee id.
        self.cat.load_pages_document(
            {
                "format": "volume-pages/1",
                "revision": 1,
                "volumes": [
                    {"volume_id": "V1", "pages": [
                        {"page_id": "p", "section_id": "body", "label": "1", "active": True},
                    ]},
                ],
            }
        )
        repo = CitationRepository(self.cat)
        repo.create_pinned("C", "one", "V1", ["p"])
        rights = RightsLedger(self.cat)
        rights.create_edition("E")
        rights.add_page_shares(
            "E", "V1", "p", "2020-01-01", {"B": "1/3", "A": "1/3", "C": "1/3"}
        )
        S = SettlementLedger(self.cat)
        S.create_pricing_group("G", 1)
        S.create_column("K", "G")
        S.create_issue("I", "2021-01-01", "E")
        S.register("I", "K", "C")
        v = S.settle("I")
        self.assertEqual(v["total_cents"], 1)
        self.assertEqual(v["allocation"]["allotted"], {"A": 1, "B": 0, "C": 0})

    def test_remainder_goes_to_larger_fraction_first(self):
        self.cat.load_pages_document(
            {
                "format": "volume-pages/1",
                "revision": 1,
                "volumes": [
                    {"volume_id": "V1", "pages": [
                        {"page_id": "p", "section_id": "body", "label": "1", "active": True},
                    ]},
                ],
            }
        )
        repo = CitationRepository(self.cat)
        repo.create_pinned("C", "one", "V1", ["p"])
        rights = RightsLedger(self.cat)
        rights.create_edition("E")
        # 1/2, 1/3, 1/6 over 1 cent: remainders 1/2, 1/3, 1/6 -> A gets the cent.
        rights.add_page_shares(
            "E", "V1", "p", "2020-01-01", {"A": "1/2", "B": "1/3", "C": "1/6"}
        )
        S = SettlementLedger(self.cat)
        S.create_pricing_group("G", 1)
        S.create_column("K", "G")
        S.create_issue("I", "2021-01-01", "E")
        S.register("I", "K", "C")
        v = S.settle("I")
        self.assertEqual(v["allocation"]["allotted"], {"A": 1, "B": 0, "C": 0})


class BlockingTests(Base):
    def test_inactive_excerpt_blocks_whole_issue(self):
        load_worked_pair(self.cat)
        S = SettlementLedger(self.cat)
        S.create_pricing_group("G", 3)
        S.create_column("K1", "G")
        S.create_column("K2", "G")
        S.create_issue("I1", "2021-05-01", "E")
        S.register("I1", "K1", "CP")
        S.register("I1", "K2", "CR")
        self.cat.apply_operations(
            [{"operation": "deactivate", "volume_id": "V1", "page_id": "q"}]
        )
        with self.assertRaises(OperationRejected) as ctx:
            S.settle("I1")
        self.assertEqual(ctx.exception.code, "settlement_blocked")
        unresolved = ctx.exception.details["unresolved"]
        self.assertEqual(unresolved[0]["citation_id"], "CR")
        self.assertEqual(unresolved[0]["column_id"], "K2")
        self.assertEqual(unresolved[0]["reason"], "range_endpoint_inactive")
        # Nothing sealed, no payouts.
        self.assertIsNone(S.get_voucher("I1"))
        self.assertEqual(S.pending_balance("A")["balance"], 0)

    def test_inspect_reports_exact_column_and_citation(self):
        load_worked_pair(self.cat)
        S = SettlementLedger(self.cat)
        S.create_pricing_group("G", 3)
        S.create_column("K1", "G")
        S.create_column("K2", "G")
        S.create_issue("I1", "2021-05-01", "E")
        S.register("I1", "K1", "CP")
        S.register("I1", "K2", "CR")
        self.cat.apply_operations(
            [{"operation": "deactivate", "volume_id": "V1", "page_id": "q"}]
        )
        report = S.inspect("I1")
        self.assertFalse(report["settleable"])
        self.assertEqual([r["citation_id"] for r in report["ready"]], ["CP"])
        bad = report["needs_attention"][0]
        self.assertEqual((bad["column_id"], bad["citation_id"]), ("K2", "CR"))
        self.assertEqual(bad["reason"], "range_endpoint_inactive")

    def test_unresolved_rights_blocks_whole_issue(self):
        load_worked_pair(self.cat)
        S = SettlementLedger(self.cat)
        S.create_pricing_group("G", 3)
        S.create_column("K1", "G")
        S.create_column("K2", "G")
        S.create_issue("I1", "2019-01-01", "E")  # before any rights version
        S.register("I1", "K1", "CP")
        S.register("I1", "K2", "CR")
        with self.assertRaises(OperationRejected) as ctx:
            S.settle("I1")
        self.assertEqual(ctx.exception.code, "settlement_blocked")
        self.assertEqual(ctx.exception.details["rights"][0]["reason"], "rights_needs_review")

    def test_issue_with_no_excerpts_cannot_settle(self):
        load_worked_pair(self.cat)
        S = SettlementLedger(self.cat)
        S.create_pricing_group("G", 3)
        S.create_column("K", "G")
        S.create_issue("I1", "2021-05-01", "E")
        with self.assertRaises(OperationRejected) as ctx:
            S.settle("I1")
        self.assertEqual(ctx.exception.code, "issue_has_no_citations")


class AtomicityAndRetryTests(Base):
    def setUp(self):
        super().setUp()
        load_worked_pair(self.cat)
        self.S = SettlementLedger(self.cat)
        self.S.create_pricing_group("G", 3)
        self.S.create_column("K1", "G")
        self.S.create_column("K2", "G")
        self.S.set_threshold("A", 5)
        self.S.set_threshold("B", 5)

    def _issue(self, iid, date):
        self.S.create_issue(iid, date, "E")
        self.S.register(iid, "K1", "CP")
        self.S.register(iid, "K2", "CR")

    def test_retry_does_not_consume_balance_twice(self):
        self._issue("I1", "2021-05-01")
        v1 = self.S.settle("I1")
        balance_after_first = self.S.pending_balance("B")["balance"]
        self.assertEqual(balance_after_first, 4)
        v1_retry = self.S.settle("I1")
        self.assertEqual(v1, v1_retry)
        self.assertEqual(self.S.pending_balance("B")["balance"], balance_after_first)

    def test_second_issue_continues_the_ledger(self):
        self._issue("I1", "2021-05-01")
        self.S.settle("I1")  # B carries 4
        self._issue("I2", "2021-06-01")
        v2 = self.S.settle("I2")
        b = {p["payee_id"]: p for p in v2["payees"]}["B"]
        self.assertEqual((b["available"], b["paid"]), (8, 8))
        self.assertEqual(self.S.pending_balance("B")["balance"], 0)

    def test_voucher_is_independently_verifiable_and_explains_history(self):
        self._issue("I1", "2021-05-01")
        self.S.settle("I1")
        self._issue("I2", "2021-06-01")
        v2 = self.S.settle("I2")
        self.assertTrue(self.S.verify_voucher("I1"))
        self.assertTrue(self.S.verify_voucher("I2"))
        # The voucher names prior lots with their source issue.
        b = {p["payee_id"]: p for p in v2["payees"]}["B"]
        self.assertEqual(b["prior_balance"], 4)
        self.assertEqual(
            [(lot["source"], lot["cents"]) for lot in b["prior_lots"]],
            [("I1", 4)],
        )
        # Every unit names its page, group and price; a basis row traces cents.
        self.assertEqual(len(v2["units"]), 2)
        basis = v2["allocation"]["basis"]["B"]
        # p (1/2 of 3c = 3/2) + q (3c) = 9/2 exact cents.
        self.assertEqual(sum(Fraction(x["amount"]) for x in basis), Fraction(9, 2))

    def test_tampering_breaks_verification(self):
        self._issue("I1", "2021-05-01")
        self.S.settle("I1")
        self.S.conn.execute(
            "UPDATE settlements SET voucher_json = replace(voucher_json, '\"total_cents\":6', "
            "'\"total_cents\":7') WHERE issue_id = 'I1'"
        )
        self.assertFalse(self.S.verify_voucher("I1"))

    def test_sealed_issue_rejects_more_registrations(self):
        self._issue("I1", "2021-05-01")
        self.S.settle("I1")
        with self.assertRaises(OperationRejected) as ctx:
            self.S.register("I1", "K1", "CP")
        self.assertEqual(ctx.exception.code, "invalid_settlement")

    def test_concurrent_settle_only_pays_once(self):
        self._issue("I1", "2021-05-01")
        results = []
        errors = []

        def worker():
            # SQLite connections are per-thread; each worker opens its own.
            cat = Catalog.open(str(self.db_path))
            try:
                results.append(SettlementLedger(cat).settle("I1"))
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)
            finally:
                cat.close()

        threads = [threading.Thread(target=worker) for _ in range(6)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertFalse(errors, errors)
        # All replays return the same voucher, and the ledger was only touched
        # once: B's carried balance is exactly one issue's 4 cents.
        self.assertEqual(len({v["voucher_sha256"] for v in results}), 1)
        S = SettlementLedger(self.cat)
        self.assertEqual(S.pending_balance("B")["balance"], 4)
        rows = self.cat.conn.execute(
            "SELECT COUNT(*) AS c FROM settlements WHERE issue_id = 'I1'"
        ).fetchone()
        self.assertEqual(rows["c"], 1)


class PricingCompositionTests(Base):
    def test_same_page_in_two_columns_same_group_pays_once(self):
        self.cat.load_pages_document(
            {
                "format": "volume-pages/1",
                "revision": 1,
                "volumes": [
                    {"volume_id": "V1", "pages": [
                        {"page_id": "p", "section_id": "body", "label": "1", "active": True},
                    ]},
                ],
            }
        )
        repo = CitationRepository(self.cat)
        repo.create_pinned("C1", "a", "V1", ["p"])
        repo.create_pinned("C2", "b", "V1", ["p"])
        rights = RightsLedger(self.cat)
        rights.create_edition("E")
        rights.add_page_shares("E", "V1", "p", "2020-01-01", {"A": "1"})
        S = SettlementLedger(self.cat)
        S.create_pricing_group("G", 7)
        S.create_column("K1", "G")
        S.create_column("K2", "G")
        S.create_issue("I", "2021-01-01", "E")
        S.register("I", "K1", "C1")
        S.register("I", "K2", "C2")
        v = S.settle("I")
        self.assertEqual(v["total_cents"], 7)

    def test_same_page_different_groups_pays_per_group(self):
        self.cat.load_pages_document(
            {
                "format": "volume-pages/1",
                "revision": 1,
                "volumes": [
                    {"volume_id": "V1", "pages": [
                        {"page_id": "p", "section_id": "body", "label": "1", "active": True},
                    ]},
                ],
            }
        )
        repo = CitationRepository(self.cat)
        repo.create_pinned("C1", "a", "V1", ["p"])
        repo.create_pinned("C2", "b", "V1", ["p"])
        rights = RightsLedger(self.cat)
        rights.create_edition("E")
        rights.add_page_shares("E", "V1", "p", "2020-01-01", {"A": "1"})
        S = SettlementLedger(self.cat)
        S.create_pricing_group("G", 7)
        S.create_pricing_group("H", 2)
        S.create_column("K1", "G")
        S.create_column("K2", "H")
        S.create_issue("I", "2021-01-01", "E")
        S.register("I", "K1", "C1")
        S.register("I", "K2", "C2")
        v = S.settle("I")
        self.assertEqual(v["total_cents"], 9)


class InsertPageAcrossIssueTests(Base):
    def test_inserted_interior_page_is_priced_in_the_new_issue_only(self):
        self.cat.load_pages_document(
            {
                "format": "volume-pages/1",
                "revision": 1,
                "volumes": [
                    {"volume_id": "V1", "pages": [
                        {"page_id": "p1", "section_id": "body", "label": "1", "active": True},
                        {"page_id": "p2", "section_id": "body", "label": "2", "active": True},
                        {"page_id": "p3", "section_id": "body", "label": "3", "active": True},
                    ]},
                ],
            }
        )
        repo = CitationRepository(self.cat)
        repo.create_range("CR", "r", "V1", "p1", "p3")
        rights = RightsLedger(self.cat)
        rights.create_edition("E")
        for pid in ("p1", "p2", "p3"):
            rights.add_page_shares("E", "V1", pid, "2020-01-01", {"A": "1"})
        S = SettlementLedger(self.cat)
        S.create_pricing_group("G", 10)
        S.create_column("K", "G")
        S.create_issue("I1", "2021-01-01", "E")
        S.register("I1", "K", "CR")
        v1 = S.settle("I1")
        self.assertEqual(v1["total_cents"], 30)

        self.cat.apply_operations(
            [{"operation": "insert", "volume_id": "V1", "before_page_id": "p2",
              "page": {"page_id": "px", "section_id": "body", "label": "1a"}}]
        )
        rights.add_page_shares("E", "V1", "px", "2020-01-01", {"A": "1"})
        S.create_issue("I2", "2021-02-01", "E")
        S.register("I2", "K", "CR")
        v2 = S.settle("I2")
        # The stored RANGE now covers p1, px, p2, p3 under the current order.
        self.assertEqual(v2["total_cents"], 40)
        self.assertIn("px", {u["page_id"] for u in v2["units"]})


class HistoricalReplayTests(Base):
    def test_voucher_can_be_recomputed_from_stored_basis(self):
        """A sealed voucher is self-contained: its units x entitlements = exact."""

        cat = self.cat
        load_worked_pair(cat)
        S = SettlementLedger(cat)
        S.create_pricing_group("G", 3)
        S.create_column("K1", "G")
        S.create_column("K2", "G")
        S.create_issue("I1", "2021-05-01", "E")
        S.register("I1", "K1", "CP")
        S.register("I1", "K2", "CR")
        v = S.settle("I1")
        # Recompute exact payee totals from the voucher alone.
        shares = {
            (e["volume_id"], e["page_id"]): {
                p: Fraction(s) for p, s in e["shares"].items()
            }
            for e in v["entitlements"]
        }
        recomputed: dict[str, Fraction] = {}
        for unit in v["units"]:
            for payee, share in shares[(unit["volume_id"], unit["page_id"])].items():
                recomputed[payee] = (
                    recomputed.get(payee, Fraction(0)) + unit["unit_cents"] * share
                )
        self.assertEqual(
            {p: f"{f.numerator}/{f.denominator}" for p, f in recomputed.items()},
            v["allocation"]["exact"],
        )


class OpeningBalanceTests(Base):
    def test_opening_balance_counts_toward_threshold_and_is_consumed_once(self):
        load_worked_pair(self.cat)
        S = SettlementLedger(self.cat)
        S.create_pricing_group("G", 3)
        S.create_column("K1", "G")
        S.create_column("K2", "G")
        S.set_threshold("A", 5)
        S.set_threshold("B", 5)
        # A starts with a carried-over 3 cents; one issue (2c) reaches 5.
        S.set_opening_balance("A", 3)
        S.set_opening_balance("B", 0)
        S.create_issue("I1", "2021-05-01", "E")
        S.register("I1", "K1", "CP")
        S.register("I1", "K2", "CR")
        v = S.settle("I1")
        payees = {p["payee_id"]: p for p in v["payees"]}
        self.assertEqual((payees["A"]["available"], payees["A"]["paid"]), (5, 5))
        self.assertEqual(payees["A"]["prior_lots"],
                         [{"lot_id": payees["A"]["prior_lots"][0]["lot_id"],
                           "source": "opening", "cents": 3}])
        self.assertEqual(S.pending_balance("A")["balance"], 0)
        # B: 4c still below 5 -> carries.
        self.assertEqual((payees["B"]["paid"], payees["B"]["carried"]), (0, 4))


class MultiPayeeRoundingSettlementTests(Base):
    def test_three_payees_over_two_page_units(self):
        self.cat.load_pages_document(
            {
                "format": "volume-pages/1",
                "revision": 1,
                "volumes": [
                    {"volume_id": "V1", "pages": [
                        {"page_id": "p1", "section_id": "body", "label": "1", "active": True},
                        {"page_id": "p2", "section_id": "body", "label": "2", "active": True},
                    ]},
                ],
            }
        )
        repo = CitationRepository(self.cat)
        repo.create_range("C", "both", "V1", "p1", "p2")
        rights = RightsLedger(self.cat)
        rights.create_edition("E")
        rights.add_page_shares(
            "E", "V1", "p1", "2020-01-01", {"A": "1/2", "B": "1/3", "C": "1/6"}
        )
        rights.add_page_shares("E", "V1", "p2", "2020-01-01", {"A": "1/2", "B": "1/3", "C": "1/6"})
        S = SettlementLedger(self.cat)
        S.create_pricing_group("G", 1)
        S.create_column("K", "G")
        S.create_issue("I", "2021-01-01", "E")
        S.register("I", "K", "C")
        v = S.settle("I")
        # total 2c; exact A=1, B=2/3, C=1/3. Floor A=1,B=0,C=0; remainder 1
        # to largest remainder B (2/3) -> A=1,B=1,C=0.
        self.assertEqual(v["total_cents"], 2)
        self.assertEqual(v["allocation"]["allotted"], {"A": 1, "B": 1, "C": 0})
        self.assertEqual(v["allocation"]["residuals"]["B"], "2/3")


if __name__ == "__main__":
    unittest.main()
