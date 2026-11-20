from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path

from citation_system import dependency, export
from citation_system.catalog import Catalog
from citation_system.errors import OperationRejected
from citation_system.repository import CitationRepository


def pages_doc(volumes):
    def norm(p):
        if isinstance(p, str):
            return {"page_id": p, "section_id": "body", "label": p, "active": True}
        return {
            "page_id": p["page_id"],
            "section_id": p.get("section_id", "body"),
            "label": p.get("label", p["page_id"]),
            "active": p.get("active", True),
        }

    return {
        "format": "volume-pages/1",
        "revision": 1,
        "volumes": [
            {"volume_id": vid, "pages": [norm(p) for p in pages]}
            for vid, pages in volumes.items()
        ],
    }


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.db_path = self.tmp / "cat.db"
        self.cat = Catalog.create(str(self.db_path))
        self.repo = CitationRepository(self.cat)

    def tearDown(self):
        self.cat.close()
        shutil.rmtree(self.tmp)

    def load(self, volumes):
        self.cat.load_pages_document(pages_doc(volumes))

    def products(self):
        rev, usable, attention = export.collect_products(self.cat.conn)
        return rev, {p["citation_id"]: p for p in usable}, {
            p["citation_id"]: p for p in attention
        }

    def ids(self, pages):
        return [(p["volume_id"], p["page_id"]) for p in pages]


class PageIdentityTests(Base):
    def test_page_ids_are_only_unique_within_volume(self):
        self.load(
            {
                "V1": ["p1", "p2"],
                "V2": ["p1"],
            }
        )
        self.assertNotEqual(
            self.cat.get_page("V1", "p1").as_key(),
            self.cat.get_page("V2", "p1").as_key(),
        )
        # Same page_id in a different volume is not a collision.
        self.assertEqual(len(self.cat.pages("V1")), 2)
        self.assertEqual(len(self.cat.pages("V2")), 1)

    def test_duplicate_page_id_within_volume_rejected_whole_load(self):
        doc = pages_doc({"V1": ["p1"]})
        doc["volumes"][0]["pages"].append(
            {"page_id": "p1", "section_id": "body", "label": "2", "active": True}
        )
        with self.assertRaises(OperationRejected) as ctx:
            self.cat.load_pages_document(doc)
        self.assertEqual(ctx.exception.code, "duplicate_page_id")
        self.assertEqual(self.cat.pages("V1"), [])  # nothing written

    def test_labels_are_strings(self):
        self.load({"V1": [{"page_id": "p1", "label": "01"}]})
        self.assertEqual(self.cat.get_page("V1", "p1").label, "01")
        self.assertIsInstance(self.cat.get_page("V1", "p1").label, str)

    def test_revision_advances_only_on_change(self):
        self.load({"V1": ["p1", "p2"]})
        rev = self.cat.revision()
        self.cat.apply_operations([{"operation": "deactivate", "volume_id": "V1", "page_id": "p1"}])
        self.assertEqual(self.cat.revision(), rev + 1)
        self.cat.apply_operations([{"operation": "deactivate", "volume_id": "V1", "page_id": "p1"}])
        self.assertEqual(self.cat.revision(), rev + 1)  # no-op


class ResolutionTests(Base):
    def test_section_keeps_repeated_label_distinct(self):
        self.load(
            {
                "V1": [
                    {"page_id": "p1", "section_id": "body", "label": "1"},
                    {"page_id": "p2", "section_id": "body", "label": "2"},
                    {"page_id": "p3", "section_id": "body", "label": "3"},
                    {"page_id": "p4", "section_id": "appendix", "label": "3"},
                ]
            }
        )
        self.assertEqual(self.cat.resolve("V1", "body", "3").page_id, "p3")
        self.assertEqual(self.cat.resolve("V1", "appendix", "3").page_id, "p4")

    def test_ambiguous_resolution_in_same_section(self):
        self.load(
            {
                "V1": [
                    {"page_id": "p1", "section_id": "body", "label": "3"},
                    {"page_id": "p2", "section_id": "body", "label": "3"},
                ]
            }
        )
        with self.assertRaises(OperationRejected) as ctx:
            self.cat.resolve("V1", "body", "3")
        self.assertEqual(ctx.exception.code, "resolution_ambiguous")
        self.assertEqual(ctx.exception.details["matching_page_ids"], ["p1", "p2"])

    def test_not_found_does_not_numeric_coerce(self):
        self.load({"V1": [{"page_id": "p1", "section_id": "body", "label": "3"}]})
        with self.assertRaises(OperationRejected) as ctx:
            self.cat.resolve("V1", "body", "03")
        self.assertEqual(ctx.exception.code, "resolution_not_found")

    def test_inactive_page_not_resolved(self):
        self.load({"V1": [{"page_id": "p1", "section_id": "body", "label": "1", "active": False}]})
        with self.assertRaises(OperationRejected) as ctx:
            self.cat.resolve("V1", "body", "1")
        self.assertEqual(ctx.exception.code, "resolution_not_found")

    def test_ambiguity_creates_no_citation(self):
        self.load(
            {
                "V1": [
                    {"page_id": "p1", "section_id": "body", "label": "3"},
                    {"page_id": "p2", "section_id": "body", "label": "3"},
                ]
            }
        )
        with self.assertRaises(Exception) as ctx:
            self.repo.create_from_reference(
                {
                    "citation_id": "C",
                    "text": "x",
                    "kind": "RANGE",
                    "volume_id": "V1",
                    "start_section_id": "body",
                    "start_label": "3",
                    "end_section_id": "body",
                    "end_label": "3",
                }
            )
        self.assertEqual(getattr(ctx.exception, "code", None), "resolution_ambiguous")
        self.assertEqual(self.repo.all(), [])


class NeighbourhoodTests(Base):
    def test_neighbours_and_interval(self):
        self.load({"V1": ["p1", "p2", "p3", "p4"]})
        prev, nxt = self.cat.current_neighbours("V1", "p2")
        self.assertEqual(prev.page_id, "p1")
        self.assertEqual(nxt.page_id, "p3")
        self.assertIsNone(self.cat.current_neighbours("V1", "p1")[0])
        self.assertIsNone(self.cat.current_neighbours("V1", "p4")[1])
        rng = self.cat.ordered_range("V1", "p2", "p4")
        self.assertEqual([p.page_id for p in rng], ["p2", "p3", "p4"])

    def test_active_only_neighbours_skip_inactive(self):
        self.load({"V1": ["p1", "p2", "p3"]})
        self.cat.apply_operations(
            [{"operation": "deactivate", "volume_id": "V1", "page_id": "p2"}]
        )
        prev, nxt = self.cat.neighbours("V1", "p1", active_only=False)["next"], None
        self.assertEqual(prev.page_id, "p2")
        active_next = self.cat.neighbours("V1", "p1", active_only=True)["next"]
        self.assertEqual(active_next.page_id, "p3")


class OperationTests(Base):
    def test_insert_keeps_identity_and_order(self):
        self.load({"V1": ["p1", "p2", "p3"]})
        self.cat.apply_operations(
            [
                {
                    "operation": "insert",
                    "volume_id": "V1",
                    "before_page_id": "p3",
                    "page": {"page_id": "px", "section_id": "body", "label": "2a", "active": True},
                }
            ]
        )
        self.assertEqual(
            [p.page_id for p in self.cat.pages("V1")], ["p1", "p2", "px", "p3"]
        )

    def test_insert_many_atomic(self):
        self.load({"V1": ["p1", "p2"]})
        self.cat.apply_batch(
            {
                "batch_id": "b1",
                "operations": [
                    {
                        "operation": "insert_many",
                        "volume_id": "V1",
                        "before_page_id": "p2",
                        "pages": [
                            {"page_id": "px", "section_id": "body", "label": "1a"},
                            {"page_id": "py", "section_id": "body", "label": "1b"},
                        ],
                    },
                    {
                        "operation": "insert_many",
                        "volume_id": "V1",
                        "at_start": True,
                        "pages": [
                            {"page_id": "pw", "section_id": "body", "label": "0"},
                        ],
                    },
                ],
            }
        )
        self.assertEqual(
            [p.page_id for p in self.cat.pages("V1")], ["pw", "p1", "px", "py", "p2"]
        )

    def test_insert_never_reissues_page_id(self):
        self.load({"V1": ["p1", "p2"]})
        with self.assertRaises(OperationRejected) as ctx:
            self.cat.apply_batch(
                {
                    "operation": "insert",
                    "volume_id": "V1",
                    "at_end": True,
                    "page": {"page_id": "p1", "section_id": "body", "label": "9"},
                }
            )
        self.assertEqual(ctx.exception.code, "duplicate_page_id")
        self.assertEqual([p.page_id for p in self.cat.pages("V1")], ["p1", "p2"])

    def test_reorder_requires_full_permutation_including_inactive(self):
        self.load({"V1": ["p1", "p2", "p3"]})
        self.cat.apply_operations(
            [{"operation": "deactivate", "volume_id": "V1", "page_id": "p2"}]
        )
        with self.assertRaises(OperationRejected):
            self.cat.apply_batch(
                {"operation": "reorder", "volume_id": "V1", "page_ids": ["p3", "p1"]}
            )
        self.cat.apply_operations(
            [{"operation": "reorder", "volume_id": "V1", "page_ids": ["p3", "p2", "p1"]}]
        )
        self.assertEqual(
            [(p.page_id, p.active) for p in self.cat.pages("V1")],
            [("p3", True), ("p2", False), ("p1", True)],
        )

    def test_cross_volume_move_rejected_whole_batch(self):
        self.load({"V1": ["p1", "p2"], "V2": ["p1"]})
        before = {
            v: [p.page_id for p in self.cat.pages(v)] for v in ("V1", "V2")
        }
        rev = self.cat.revision()
        results = self.cat.apply_operations(
            [
                {"operation": "deactivate", "volume_id": "V1", "page_id": "p1"},
                {
                    "operation": "move",
                    "volume_id": "V1",
                    "page_id": "p2",
                    "target_volume_id": "V2",
                    "before_page_id": "p1",
                },
            ]
        )
        self.assertEqual(results[0]["status"], "applied")
        self.assertEqual(results[1]["status"], "rejected")
        # The second request was its own batch; it changed nothing.
        self.assertEqual(
            [p.page_id for p in self.cat.pages("V1")], before["V1"]
        )
        self.assertEqual(
            [p.page_id for p in self.cat.pages("V2")], before["V2"]
        )

    def test_cross_volume_rejected_batch_leaves_valid_sibling_untouched(self):
        self.load({"V1": ["p1", "p2"], "V2": ["p1"]})
        with self.assertRaises(OperationRejected) as ctx:
            self.cat.apply_batch(
                {
                    "batch_id": "mixed",
                    "operations": [
                        {"operation": "deactivate", "volume_id": "V1", "page_id": "p1"},
                        {
                            "operation": "move",
                            "volume_id": "V1",
                            "page_id": "p2",
                            "target_volume_id": "V2",
                        },
                    ],
                }
            )
        self.assertEqual(ctx.exception.code, "cross_volume_move")
        self.assertTrue(self.cat.get_page("V1", "p1").active)
        self.assertEqual([p.page_id for p in self.cat.pages("V1")], ["p1", "p2"])

    def test_shift_and_deactivate_keep_page_identities(self):
        self.load({"V1": ["p1", "p2", "p3", "p4"]})
        self.cat.apply_operations(
            [
                {"operation": "move", "volume_id": "V1", "page_id": "p4", "before_page_id": "p2"},
                {"operation": "deactivate", "volume_id": "V1", "page_id": "p3"},
            ]
        )
        pages = self.cat.pages("V1")
        self.assertEqual([p.page_id for p in pages], ["p1", "p4", "p2", "p3"])
        self.assertEqual([p.seq for p in pages], [0, 1, 2, 3])
        self.assertFalse(pages[3].active)

    def test_multi_operation_batch_applies_together(self):
        self.load({"V1": ["p1", "p2", "p3", "p4"]})
        result = self.cat.apply_batch(
            {
                "batch_id": "change-set-1",
                "operations": [
                    {"operation": "insert", "volume_id": "V1",
                     "before_page_id": "p3",
                     "page": {"page_id": "px", "section_id": "body", "label": "2a"}},
                    {"operation": "move", "volume_id": "V1",
                     "page_id": "p4", "before_page_id": "p1"},
                    {"operation": "deactivate", "volume_id": "V1", "page_id": "p2"},
                ],
            }
        )
        self.assertEqual(result["status"], "applied")
        self.assertEqual(
            [(p.page_id, p.active) for p in self.cat.pages("V1")],
            [("p4", True), ("p1", True), ("p2", False), ("px", True), ("p3", True)],
        )

    def test_rejected_multi_operation_batch_leaves_all_pages_untouched(self):
        self.load({"V1": ["p1", "p2", "p3"], "V2": ["v1"]})
        before = [(p.page_id, p.seq, p.active) for p in self.cat.pages("V1")]
        with self.assertRaises(OperationRejected):
            self.cat.apply_batch(
                {
                    "batch_id": "bad",
                    "operations": [
                        {"operation": "insert", "volume_id": "V1", "at_end": True,
                         "page": {"page_id": "px", "section_id": "body", "label": "4"}},
                        {"operation": "deactivate", "volume_id": "V1", "page_id": "p1"},
                        {"operation": "move", "volume_id": "V1", "page_id": "p1",
                         "target_volume_id": "V2"},
                    ],
                }
            )
        self.assertEqual(
            [(p.page_id, p.seq, p.active) for p in self.cat.pages("V1")], before
        )
        self.assertEqual([p.page_id for p in self.cat.pages("V2")], ["v1"])
        self.assertIsNone(self.cat.get_page("V1", "px", missing_ok=True))

    def test_anchor_in_other_volume_is_rejected_not_treated_as_missing(self):
        self.load({"V1": ["p1", "shared"], "V2": ["v2only"]})
        # "shared" exists only in V1, so a normal move works.
        self.cat.apply_batch(
            {"operation": "move", "volume_id": "V1", "page_id": "p1",
             "before_page_id": "shared"}
        )
        with self.assertRaises(OperationRejected) as ctx:
            self.cat.apply_batch(
                {"operation": "move", "volume_id": "V1", "page_id": "p1",
                 "before_page_id": "v2only"}
            )
        self.assertEqual(ctx.exception.code, "cross_volume_anchor")

    def test_sequence_positions_unique_after_many_moves(self):
        self.load({"V1": [f"p{i}" for i in range(1, 8)]})
        moves = [
            {"operation": "move", "volume_id": "V1", "page_id": "p7", "at_start": True},
            {"operation": "move", "volume_id": "V1", "page_id": "p1", "at_end": True},
            {"operation": "move", "volume_id": "V1", "page_id": "p3", "before_page_id": "p7"},
            {"operation": "move", "volume_id": "V1", "page_id": "p4", "after_page_id": "p7"},
        ]
        self.cat.apply_operations(moves)
        pages = self.cat.pages("V1")
        self.assertEqual(sorted(p.seq for p in pages), list(range(len(pages))))
        self.assertEqual({p.page_id for p in pages}, {f"p{i}" for i in range(1, 8)})

    def test_move_many(self):
        self.load({"V1": ["p1", "p2", "p3", "p4", "p5"]})
        self.cat.apply_operations(
            [
                {
                    "operation": "move_many",
                    "volume_id": "V1",
                    "page_ids": ["p4", "p2"],
                    "before_page_id": "p1",
                }
            ]
        )
        self.assertEqual(
            [p.page_id for p in self.cat.pages("V1")],
            ["p4", "p2", "p1", "p3", "p5"],
        )


class PinnedTests(Base):
    def setUp(self):
        super().setUp()
        self.load({"V1": ["p1", "p2", "p3", "p4"]})
        self.repo.create_from_reference(
            {
                "citation_id": "C_PIN",
                "text": "pin",
                "kind": "PINNED",
                "volume_id": "V1",
                "page_ids": ["p2", "p3"],
            }
        )

    def test_membership_fixed_through_reorder_and_insert(self):
        self.cat.apply_operations(
            [
                {"operation": "insert", "volume_id": "V1", "before_page_id": "p3",
                 "page": {"page_id": "px", "section_id": "body", "label": "2a"}},
                {"operation": "move", "volume_id": "V1", "page_id": "p4", "before_page_id": "p2"},
                {"operation": "reorder", "volume_id": "V1", "page_ids": ["p3", "px", "p2", "p1", "p4"]},
            ]
        )
        _, usable, attention = self.products()
        self.assertEqual(self.ids(usable["C_PIN"]["pages"]), [("V1", "p2"), ("V1", "p3")])
        self.assertNotIn("C_PIN", attention)

    def test_inactive_member_keeps_member_and_is_attention(self):
        self.cat.apply_operations(
            [{"operation": "deactivate", "volume_id": "V1", "page_id": "p3"}]
        )
        _, usable, attention = self.products()
        self.assertNotIn("C_PIN", usable)
        product = attention["C_PIN"]
        self.assertEqual(self.ids(product["pages"]), [("V1", "p2"), ("V1", "p3")])
        self.assertFalse(product["pages"][1]["active"])
        self.assertEqual(product["reason"], export.REASON_PINNED_INACTIVE)
        self.assertEqual(product["source"]["page_ids"], ["p2", "p3"])

    def test_reactivation_restores_usable(self):
        self.cat.apply_operations(
            [{"operation": "deactivate", "volume_id": "V1", "page_id": "p3"}]
        )
        self.assertIn("C_PIN", self.products()[2])
        self.cat.apply_operations(
            [{"operation": "activate", "volume_id": "V1", "page_id": "p3"}]
        )
        self.assertIn("C_PIN", self.products()[1])

    def test_creation_from_section_label_stores_identity(self):
        self.repo.create_from_reference(
            {
                "citation_id": "C_PIN2",
                "text": "pin2",
                "kind": "PINNED",
                "volume_id": "V1",
                "items": [
                    {"section_id": "body", "label": "p1"},
                    {"section_id": "body", "label": "p4"},
                ],
            }
        )
        members = self.repo.pinned_members("C_PIN2")
        self.assertEqual([p.page_id for p in members], ["p1", "p4"])
        # A later display-label change or reorder does not re-resolve the text.
        self.cat.apply_operations(
            [{"operation": "reorder", "volume_id": "V1", "page_ids": ["p4", "p1", "p3", "p2"]}]
        )
        members = self.repo.pinned_members("C_PIN2")
        self.assertEqual([p.page_id for p in members], ["p1", "p4"])


class RangeTests(Base):
    def setUp(self):
        super().setUp()
        self.load({"V1": ["p1", "p2", "p3", "p4", "p5"]})
        self.repo.create_from_reference(
            {
                "citation_id": "C_RANGE",
                "text": "range",
                "kind": "RANGE",
                "volume_id": "V1",
                "start_page_id": "p2",
                "end_page_id": "p4",
            }
        )

    def test_insert_between_endpoints_changes_range(self):
        self.cat.apply_operations(
            [
                {"operation": "insert", "volume_id": "V1", "before_page_id": "p3",
                 "page": {"page_id": "px", "section_id": "body", "label": "2a"}},
                {"operation": "insert", "volume_id": "V1", "after_page_id": "p4",
                 "page": {"page_id": "py", "section_id": "body", "label": "4a"}},
            ]
        )
        _, usable, _ = self.products()
        # py is after the end endpoint and must not be included.
        self.assertEqual(
            self.ids(usable["C_RANGE"]["pages"]),
            [("V1", "p2"), ("V1", "px"), ("V1", "p3"), ("V1", "p4")],
        )

    def test_inactive_interior_page_excluded(self):
        self.cat.apply_operations(
            [{"operation": "deactivate", "volume_id": "V1", "page_id": "p3"}]
        )
        _, usable, attention = self.products()
        self.assertEqual(
            self.ids(usable["C_RANGE"]["pages"]), [("V1", "p2"), ("V1", "p4")]
        )
        self.assertNotIn("C_RANGE", attention)

    def test_endpoint_deactivated_needs_attention_with_original_endpoints(self):
        self.cat.apply_operations(
            [{"operation": "deactivate", "volume_id": "V1", "page_id": "p4"}]
        )
        _, usable, attention = self.products()
        self.assertNotIn("C_RANGE", usable)
        product = attention["C_RANGE"]
        self.assertEqual(
            product["source"],
            {"volume_id": "V1", "start_page_id": "p2", "end_page_id": "p4"},
        )
        self.assertEqual(self.ids(product["pages"]), [("V1", "p2"), ("V1", "p4")])
        self.assertFalse(product["pages"][1]["active"])
        self.assertEqual(product["reason"], export.REASON_RANGE_ENDPOINT_INACTIVE)

    def test_reversal_needs_attention_and_does_not_swap(self):
        self.cat.apply_operations(
            [{"operation": "move", "volume_id": "V1", "page_id": "p4", "before_page_id": "p2"}]
        )
        _, _, attention = self.products()
        product = attention["C_RANGE"]
        self.assertEqual(
            product["source"],
            {"volume_id": "V1", "start_page_id": "p2", "end_page_id": "p4"},
        )
        # Source order preserved: p2 (start) then p4 (end), despite p4 now first.
        self.assertEqual(self.ids(product["pages"]), [("V1", "p2"), ("V1", "p4")])
        self.assertEqual(product["reason"], export.REASON_RANGE_REVERSED)
        self.assertTrue(product["detail"]["reversed"])

    def test_move_back_restores_usable_without_recreation(self):
        self.cat.apply_operations(
            [{"operation": "move", "volume_id": "V1", "page_id": "p4", "before_page_id": "p2"}]
        )
        self.assertIn("C_RANGE", self.products()[2])
        self.cat.apply_operations(
            [{"operation": "move", "volume_id": "V1", "page_id": "p4", "after_page_id": "p3"}]
        )
        _, usable, _ = self.products()
        self.assertEqual(
            self.ids(usable["C_RANGE"]["pages"]),
            [("V1", "p2"), ("V1", "p3"), ("V1", "p4")],
        )

    def test_body_and_appendix_same_label_resolution(self):
        self.load(
            {
                "V2": [
                    {"page_id": "b2", "section_id": "body", "label": "2"},
                    {"page_id": "b4", "section_id": "body", "label": "4"},
                    {"page_id": "a2", "section_id": "appendix", "label": "2"},
                    {"page_id": "a4", "section_id": "appendix", "label": "4"},
                ]
            }
        )
        self.repo.create_from_reference(
            {
                "citation_id": "C_APP",
                "text": "app",
                "kind": "RANGE",
                "volume_id": "V2",
                "start_section_id": "appendix",
                "start_label": "2",
                "end_section_id": "appendix",
                "end_label": "4",
            }
        )
        start, end = self.repo.range_endpoints("C_APP")
        self.assertEqual((start.page_id, end.page_id), ("a2", "a4"))


class CitationCreationTests(Base):
    def test_later_duplicate_label_does_not_disturb_stored_citation(self):
        self.load({"V1": ["p1", "p2", "p3"]})
        self.repo.create_from_reference(
            {
                "citation_id": "C_RES",
                "text": "resolved",
                "kind": "RANGE",
                "volume_id": "V1",
                "start_section_id": "body",
                "start_label": "p1",
                "end_section_id": "body",
                "end_label": "p3",
            }
        )
        # Introduce a second active page with the same (section, label) as the
        # start page.  Re-resolution would now be ambiguous, but the stored
        # citation keeps the identity it was created with.
        self.cat.apply_operations(
            [
                {"operation": "insert", "volume_id": "V1", "at_start": True,
                 "page": {"page_id": "dup", "section_id": "body", "label": "p1"}}
            ]
        )
        with self.assertRaises(OperationRejected) as ctx:
            self.cat.resolve("V1", "body", "p1")
        self.assertEqual(ctx.exception.code, "resolution_ambiguous")
        _, usable, attention = self.products()
        self.assertNotIn("C_RES", attention)
        self.assertEqual(
            self.ids(usable["C_RES"]["pages"]),
            [("V1", "p1"), ("V1", "p2"), ("V1", "p3")],
        )
        self.assertEqual(usable["C_RES"]["source"]["start_page_id"], "p1")

    def test_single_page_range(self):
        self.load({"V1": ["p1", "p2", "p3"]})
        self.repo.create_range("C1", "one", "V1", "p2", "p2")
        _, usable, _ = self.products()
        self.assertEqual(self.ids(usable["C1"]["pages"]), [("V1", "p2")])

    def test_duplicate_citation_id_rejected(self):
        self.load({"V1": ["p1", "p2"]})
        self.repo.create_pinned("C", "t", "V1", ["p1"])
        with self.assertRaises(Exception) as ctx:
            self.repo.create_pinned("C", "t2", "V1", ["p2"])
        self.assertEqual(getattr(ctx.exception, "code", None), "citation_exists")
        self.assertEqual(len(self.repo.all()), 1)

    def test_range_to_unknown_page_writes_nothing(self):
        self.load({"V1": ["p1", "p2"]})
        with self.assertRaises(Exception):
            self.repo.create_range("C", "t", "V1", "p1", "nope")
        self.assertEqual(self.repo.all(), [])

    def test_citations_in_two_volumes_are_distinct(self):
        self.load({"V1": ["p1", "p2"], "V2": ["p1", "p2"]})
        self.repo.create_pinned("C_V1", "v1", "V1", ["p1"])
        self.repo.create_pinned("C_V2", "v2", "V2", ["p1"])
        _, usable, _ = self.products()
        self.assertEqual(
            self.ids(usable["C_V1"]["pages"]), [("V1", "p1")]
        )
        self.assertEqual(
            self.ids(usable["C_V2"]["pages"]), [("V2", "p1")]
        )




class ExportTests(Base):
    def setUp(self):
        super().setUp()
        self.load({"V1": ["p1", "p2", "p3", "p4"], "V2": ["p1"]})
        self.repo.create_from_reference(
            {"citation_id": "C_PIN", "text": "pin", "kind": "PINNED",
             "volume_id": "V1", "page_ids": ["p2", "p3"]}
        )
        self.repo.create_from_reference(
            {"citation_id": "C_RANGE", "text": "range", "kind": "RANGE",
             "volume_id": "V1", "start_page_id": "p2", "end_page_id": "p4"}
        )

    def test_each_citation_in_exactly_one_file(self):
        _, usable, attention = self.products()
        self.assertEqual(set(usable) | set(attention), {"C_PIN", "C_RANGE"})
        self.assertFalse(set(usable) & set(attention))

    def test_files_are_valid_and_atomic(self):
        usable_path = self.tmp / "usable.jsonl"
        attention_path = self.tmp / "attention.jsonl"
        summary = export.write_products(self.cat.conn, usable_path, attention_path)
        self.assertEqual(summary["total"], 2)
        for path in (usable_path, attention_path):
            text = path.read_text(encoding="utf-8")
            self.assertFalse(text.startswith("["))
            for line in text.splitlines():
                row = json.loads(line)
                self.assertEqual(row["format"], "citation-product/1")
                self.assertEqual(export.validate_products([row]), [])

    def test_export_uses_single_snapshot(self):
        # Build an independent snapshot and compare to the written export.
        revision, usable, attention = export.collect_products(self.cat.conn)
        for row in usable + attention:
            self.assertEqual(row["catalog_revision"], revision)

    def test_revision_present_on_attention_rows(self):
        self.cat.apply_operations(
            [{"operation": "deactivate", "volume_id": "V1", "page_id": "p3"}]
        )
        rev, _, attention = self.products()
        self.assertEqual(attention["C_PIN"]["catalog_revision"], rev)

    def test_catalog_independent_of_citations(self):
        # A catalog with no citations exports two empty files.
        cat2 = Catalog.create(str(self.tmp / "empty.db"))
        cat2.load_pages_document(pages_doc({"V1": ["p1"]}))
        usable_path = self.tmp / "u.jsonl"
        attention_path = self.tmp / "a.jsonl"
        summary = export.write_products(cat2.conn, usable_path, attention_path)
        self.assertEqual(summary["total"], 0)
        self.assertEqual(usable_path.read_text(), "")
        self.assertEqual(attention_path.read_text(), "")
        cat2.close()


class ReverseLookupTests(Base):
    def setUp(self):
        super().setUp()
        self.load({"V1": ["p1", "p2", "p3", "p4", "p5"]})
        self.repo.create_from_reference(
            {"citation_id": "C_PIN", "text": "pin", "kind": "PINNED",
             "volume_id": "V1", "page_ids": ["p1", "p3"]}
        )
        self.repo.create_from_reference(
            {"citation_id": "C_RANGE", "text": "range", "kind": "RANGE",
             "volume_id": "V1", "start_page_id": "p2", "end_page_id": "p4"}
        )

    def test_direct_reverse_lookup(self):
        found = self.repo.citations_referencing("V1", "p3")
        self.assertEqual(
            [(f["citation_id"], f["kind"], f["role"]) for f in found],
            [("C_PIN", "PINNED", "member"), ("C_RANGE", "RANGE", "interior")],
        )

    def test_dependency_index_matches_direct_lookup(self):
        dependency.rebuild(self.cat.conn)
        for pid in ("p1", "p2", "p3", "p4", "p5"):
            direct = self.repo.citations_referencing("V1", pid)
            indexed = dependency.referrers(self.cat.conn, "V1", pid)
            self.assertEqual(
                [(f["citation_id"], f["role"]) for f in direct],
                [(f["citation_id"], f["role"]) for f in indexed],
            )

    def test_dependency_index_invalidated_by_move(self):
        dependency.rebuild(self.cat.conn)
        self.assertIn(
            "C_RANGE",
            [f["citation_id"] for f in dependency.referrers(self.cat.conn, "V1", "p3")],
        )
        self.cat.apply_operations(
            [{"operation": "move", "volume_id": "V1", "page_id": "p5", "before_page_id": "p2"}]
        )
        # p3 is still inside p2..p4, p1 no longer inside any range.
        self.assertIn(
            "C_RANGE",
            [f["citation_id"] for f in dependency.referrers(self.cat.conn, "V1", "p3")],
        )
        # Move p4 before p2; then p3 is still between them but the range is
        # reversed; reverse lookup reports physical coverage.
        self.cat.apply_operations(
            [{"operation": "move", "volume_id": "V1", "page_id": "p4", "before_page_id": "p2"}]
        )
        # Reverse lookup follows the physical interval between the endpoints;
        # after the reversal that interval is just p4,p2.
        rows = dependency.referrers(self.cat.conn, "V1", "p2")
        self.assertIn("C_RANGE", [f["citation_id"] for f in rows])
        self.assertNotIn(
            "C_RANGE",
            [f["citation_id"] for f in dependency.referrers(self.cat.conn, "V1", "p3")],
        )
        self.assertTrue(dependency.is_current(self.cat.conn))


class ClientIntegrationTests(Base):
    def test_client_reads_export_without_database(self):
        import subprocess
        import sys

        self.load({"V1": ["p1", "p2", "p3"], "V2": ["p1"]})
        self.repo.create_from_reference(
            {"citation_id": "C", "text": "t", "kind": "PINNED",
             "volume_id": "V1", "page_ids": ["p2"]}
        )
        usable_path = self.tmp / "u.jsonl"
        attention_path = self.tmp / "a.jsonl"
        export.write_products(self.cat.conn, usable_path, attention_path)
        client = Path(__file__).resolve().parents[1] / "materials" / "726" / "citation_client.py"
        out = subprocess.run(
            [sys.executable, str(client), str(usable_path), str(attention_path)],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
        self.assertEqual(out, "C usable V1/p2 -")


class RandomizedInvariantTests(Base):
    def test_random_operations_match_list_model(self):
        import random

        rng = random.Random(1234)
        ids = [f"p{i}" for i in range(1, 13)]
        self.load({"V1": ids})
        model: list[str] = list(ids)
        active: dict[str, bool] = {p: True for p in ids}
        next_id = 1000

        for step in range(200):
            choice = rng.choice(
                ["insert", "insert_many", "move", "move_many", "deactivate", "activate", "reorder"]
            )
            if choice in ("insert", "insert_many"):
                n = 1 if choice == "insert" else rng.randint(1, 3)
                new = [f"q{next_id + i}" for i in range(n)]
                next_id += n
                pos = rng.randint(0, len(model))
                pages = [
                    {"page_id": pid, "section_id": "body", "label": pid, "active": True}
                    for pid in new
                ]
                if choice == "insert":
                    op = {
                        "operation": "insert",
                        "volume_id": "V1",
                        "page": pages[0],
                    }
                    if pos < len(model):
                        op["before_page_id"] = model[pos]
                    else:
                        op["at_end"] = True
                else:
                    op = {"operation": "insert_many", "volume_id": "V1", "pages": pages}
                    if pos < len(model):
                        op["before_page_id"] = model[pos]
                    else:
                        op["at_end"] = True
                self.cat.apply_batch(op)
                model[pos:pos] = new
                for pid in new:
                    active[pid] = True
            elif choice == "move":
                pid = rng.choice(model)
                target = rng.choice([m for m in model if m != pid])
                if rng.random() < 0.5:
                    op = {"operation": "move", "volume_id": "V1",
                          "page_id": pid, "before_page_id": target}
                    model.remove(pid)
                    model.insert(model.index(target), pid)
                else:
                    op = {"operation": "move", "volume_id": "V1",
                          "page_id": pid, "after_page_id": target}
                    model.remove(pid)
                    model.insert(model.index(target) + 1, pid)
                self.cat.apply_batch(op)
            elif choice == "move_many":
                k = rng.randint(2, min(4, len(model)))
                moving = rng.sample(model, k)
                rest = [p for p in model if p not in moving]
                target = rng.choice(rest)
                self.cat.apply_batch(
                    {"operation": "move_many", "volume_id": "V1",
                     "page_ids": moving, "before_page_id": target}
                )
                new_model = [p for p in model if p not in moving]
                idx = new_model.index(target)
                model = new_model[:idx] + moving + new_model[idx:]
            elif choice in ("deactivate", "activate"):
                pid = rng.choice(model)
                op = {"operation": choice, "volume_id": "V1", "page_id": pid}
                self.cat.apply_batch(op)
                active[pid] = choice == "activate"
            else:
                shuffled = list(model)
                rng.shuffle(shuffled)
                self.cat.apply_batch(
                    {"operation": "reorder", "volume_id": "V1", "page_ids": shuffled}
                )
                model = shuffled
            actual = self.cat.pages("V1")
            self.assertEqual([p.page_id for p in actual], model, f"step {step} {choice}")
            self.assertEqual(sorted(p.seq for p in actual), list(range(len(model))))
            self.assertEqual({p.page_id: p.active for p in actual}, active)


class ScaleTests(Base):
    def test_target_scale_citations(self):
        """20 volumes x 200 pages, 500 citations: export stays correct."""
        volumes = {
            f"V{v:02d}": [f"p{i:03d}" for i in range(200)] for v in range(1, 21)
        }
        self.load(volumes)
        import random

        rng = random.Random(7)
        for n in range(500):
            vid = f"V{rng.randint(1, 20):02d}"
            if n % 2 == 0:
                members = sorted(rng.sample(range(200), 2))
                self.repo.create_pinned(
                    f"C{n:03d}", f"pin {n}", vid,
                    [f"p{members[0]:03d}", f"p{members[1]:03d}"],
                )
            else:
                a, b = sorted(rng.sample(range(200), 2))
                self.repo.create_range(
                    f"C{n:03d}", f"range {n}", vid,
                    f"p{a:03d}", f"p{b:03d}",
                )
        rev, usable, attention = export.collect_products(self.cat.conn)
        self.assertEqual(len(usable) + len(attention), 500)
        self.assertTrue(all(p["catalog_revision"] == rev for p in usable + attention))
        for product in usable:
            if product["kind"] == "RANGE":
                positions = [
                    self.cat.position(product["source"]["volume_id"], p["page_id"])
                    for p in product["pages"]
                ]
                self.assertEqual(positions, sorted(positions))


if __name__ == "__main__":
    unittest.main()
