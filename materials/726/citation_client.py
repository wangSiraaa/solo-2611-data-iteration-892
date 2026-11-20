#!/usr/bin/env python3
"""Consume exported citation JSONL only, without a catalog or database."""
import argparse
import json

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("products", nargs="+")
    args = ap.parse_args()
    seen = set()
    for path in args.products:
        with open(path, encoding="utf-8") as fh:
            for number, line in enumerate(fh, 1):
                if not line.strip():
                    raise ValueError(f"{path}:{number}: blank JSONL record")
                row = json.loads(line)
                if row["format"] != "citation-product/1":
                    raise ValueError("unsupported product")
                key = row["citation_id"]
                if key in seen:
                    raise ValueError("citation appears more than once")
                seen.add(key)
                pages = [f'{p["volume_id"]}/{p["page_id"]}'
                         + ("" if p["active"] else "(inactive)")
                         for p in row["pages"]]
                print(key, row["status"], ",".join(pages),
                      row.get("reason") or "-")
if __name__ == "__main__":
    main()
