"""Integration tests against the real, unmodified task materials."""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from citation_system import export
from citation_system.catalog import Catalog
from citation_system.repository import CitationRepository

ROOT = Path(__file__).resolve().parents[1]
MATERIALS = ROOT / "materials" / "726"


def read_jsonl(path):
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


class MaterialsIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.cat = Catalog.create(str(self.tmp / "cat.db"))
        self.cat.load_pages_document(
            json.loads((MATERIALS / "pages.json").read_text(encoding="utf-8"))
        )
        self.repo = CitationRepository(self.cat)
        for reference in read_jsonl(MATERIALS / "references.jsonl"):
            self.repo.create_from_reference(reference)

    def tearDown(self):
        self.cat.close()
        shutil.rmtree(self.tmp)

    def client(self, usable, attention):
        out = subprocess.run(
            [sys.executable, str(MATERIALS / "citation_client.py"), str(usable), str(attention)],
            capture_output=True,
            text=True,
            check=True,
        )
        return out.stdout.splitlines()

    def test_initial_export_matches_handwritten_example_exactly(self):
        usable_path = self.tmp / "u.jsonl"
        attention_path = self.tmp / "a.jsonl"
        export.write_products(self.cat.conn, usable_path, attention_path)
        self.assertEqual(
            read_jsonl(usable_path),
            read_jsonl(MATERIALS / "example_usable.jsonl"),
        )
        self.assertEqual(
            usable_path.read_text(encoding="utf-8"),
            (MATERIALS / "example_usable.jsonl").read_text(encoding="utf-8"),
        )
        self.assertEqual(read_jsonl(attention_path), [])

    def test_material_operations_and_client_end_state(self):
        ops = read_jsonl(MATERIALS / "operations.jsonl")
        results = self.cat.apply_operations(ops)
        self.assertEqual(
            [r["status"] for r in results],
            ["applied", "applied", "applied", "rejected"],
        )
        self.assertEqual(results[-1]["error_code"], "cross_volume_move")
        # Cross-volume move left every page exactly where it was.
        self.assertEqual(
            [p.page_id for p in self.cat.pages("V1")], ["p1", "p4", "p2", "px", "p3"]
        )
        self.assertEqual([p.page_id for p in self.cat.pages("V2")], ["p1"])

        usable_path = self.tmp / "u.jsonl"
        attention_path = self.tmp / "a.jsonl"
        export.write_products(self.cat.conn, usable_path, attention_path)
        self.assertEqual(
            self.client(usable_path, attention_path),
            [
                "C_PIN needs_attention V1/p2,V1/p3(inactive) pinned_inactive_member",
                "C_RANGE needs_attention V1/p2,V1/p4 range_endpoints_reversed",
            ],
        )

    def test_old_and_new_exports_both_consumable(self):
        usable0 = self.tmp / "old_u.jsonl"
        attention0 = self.tmp / "old_a.jsonl"
        export.write_products(self.cat.conn, usable0, attention0)
        old_lines = self.client(usable0, attention0)

        self.cat.apply_batch(
            {
                "operation": "insert",
                "volume_id": "V1",
                "before_page_id": "p3",
                "page": {"page_id": "px", "section_id": "body", "label": "2a", "active": True},
            }
        )
        usable1 = self.tmp / "new_u.jsonl"
        attention1 = self.tmp / "new_a.jsonl"
        export.write_products(self.cat.conn, usable1, attention1)
        new_lines = self.client(usable1, attention1)

        # Old files are untouched and still parse.
        self.assertEqual(self.client(usable0, attention0), old_lines)
        self.assertNotEqual(old_lines, new_lines)
        self.assertIn("C_RANGE usable V1/p2,V1/px,V1/p3,V1/p4 -", new_lines)


if __name__ == "__main__":
    unittest.main()
