"""Command line interface for the citation catalog / repository.

Typical use::

    python -m citation_system init --db work/catalog.db \\
        --pages materials/726/pages.json

    python -m citation_system add-references --db work/catalog.db \\
        --references materials/726/references.jsonl

    python -m citation_system apply-operations --db work/catalog.db \\
        --operations materials/726/operations.jsonl

    python -m citation_system export --db work/catalog.db \\
        --usable exports/current_usable.jsonl \\
        --needs-attention exports/current_needs_attention.jsonl
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from . import export
from .catalog import Catalog
from .repository import CitationRepository
from .rights import RightsLedger
from .settlement import SettlementLedger


def _read_json(path: str | Path) -> Any:
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def _read_jsonl(path: str | Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with open(path, encoding="utf-8") as fh:
        for number, line in enumerate(fh, 1):
            if not line.strip():
                continue
            rows.append(json.loads(line))
    return rows


def _open_existing(path: str) -> Catalog:
    p = Path(path)
    if not p.exists():
        raise SystemExit(f"database {path} does not exist; run 'init' first")
    return Catalog.open(path)


def cmd_init(args: argparse.Namespace) -> int:
    path = Path(args.db)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and not args.force:
        raise SystemExit(f"{path} exists; use --force to reset it")
    if path.exists():
        path.unlink()
    for suffix in ("-wal", "-shm"):
        extra = Path(str(path) + suffix)
        if extra.exists():
            extra.unlink()
    catalog = Catalog.create(str(path))
    document = _read_json(args.pages)
    catalog.load_pages_document(document)
    print(
        json.dumps(
            {
                "db": str(path),
                "catalog_revision": catalog.revision(),
                "volumes": len(catalog.volume_ids()),
                "pages": sum(len(catalog.pages(v)) for v in catalog.volume_ids()),
            },
            ensure_ascii=False,
        )
    )
    catalog.close()
    return 0


def cmd_add_references(args: argparse.Namespace) -> int:
    catalog = _open_existing(args.db)
    repo = CitationRepository(catalog)
    created: list[str] = []
    failures: list[dict[str, Any]] = []
    for reference in _read_jsonl(args.references):
        try:
            citation = repo.create_from_reference(reference)
        except Exception as exc:  # noqa: BLE001 - report, then fail the run
            failures.append(
                {
                    "citation_id": (reference or {}).get("citation_id"),
                    "error": _error_dict(exc),
                }
            )
            continue
        created.append(citation.citation_id)
    catalog.close()
    print(
        json.dumps(
            {"created": created, "failures": failures},
            ensure_ascii=False,
        )
    )
    return 1 if failures else 0


def cmd_apply_operations(args: argparse.Namespace) -> int:
    catalog = _open_existing(args.db)
    operations = _read_jsonl(args.operations)
    try:
        results = catalog.apply_operations(operations)
        log = catalog.operation_log()
        final_revision = catalog.revision()
    finally:
        catalog.close()
    print(
        json.dumps(
            {
                "results": [
                    {
                        "request_index": r.get("request_index"),
                        "status": r.get("status"),
                        "error_code": r.get("error_code"),
                        "catalog_revision": r.get("catalog_revision"),
                    }
                    for r in results
                ],
                "operation_log": [
                    {
                        "request_index": row["request_index"],
                        "status": row["status"],
                        "error_code": row["error_code"],
                        "catalog_revision": row["catalog_revision"],
                        "expectation": row["expectation"],
                        "expectation_met": row["expectation_met"],
                    }
                    for row in log
                ],
                "final_catalog_revision": final_revision,
            },
            ensure_ascii=False,
        )
    )
    return 0


def cmd_export(args: argparse.Namespace) -> int:
    catalog = _open_existing(args.db)
    try:
        summary = export.write_products(
            catalog.conn, args.usable, args.needs_attention
        )
    finally:
        catalog.close()
    print(json.dumps(summary, ensure_ascii=False))
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    catalog = _open_existing(args.db)
    try:
        repo = CitationRepository(catalog)
        data = {
            "catalog_revision": catalog.revision(),
            "volumes": {
                v: [
                    {
                        "page_id": p.page_id,
                        "section_id": p.section_id,
                        "label": p.label,
                        "active": p.active,
                    }
                    for p in catalog.pages(v)
                ]
                for v in catalog.volume_ids()
            },
            "citations": [
                {
                    "citation_id": c.citation_id,
                    "kind": c.kind,
                    "volume_id": c.volume_id,
                    "created_revision": c.created_revision,
                }
                for c in repo.all()
            ],
        }
    finally:
        catalog.close()
    print(json.dumps(data, ensure_ascii=False, indent=2))
    return 0


def cmd_show_citation(args: argparse.Namespace) -> int:
    catalog = _open_existing(args.db)
    try:
        repo = CitationRepository(catalog)
        citation = repo.get(args.citation_id)
        out: dict[str, Any] = citation.as_dict()
        if citation.kind == "PINNED":
            out["members"] = [
                p.as_dict() for p in repo.pinned_members(args.citation_id)
            ]
        else:
            start, end = repo.range_endpoints(args.citation_id)
            out["start"] = start.as_dict()
            out["end"] = end.as_dict()
    finally:
        catalog.close()
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0


def cmd_lookup(args: argparse.Namespace) -> int:
    """Resolve a (volume, section, label) to its single active page."""

    catalog = _open_existing(args.db)
    try:
        page = catalog.resolve(args.volume_id, args.section_id, args.label)
        out = {"page": page.as_dict(include_seq=True)}
    except Exception as exc:  # noqa: BLE001 - structural errors are the output
        out = {"error": _error_dict(exc)}
    finally:
        catalog.close()
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0 if "page" in out else 2


def cmd_referrers(args: argparse.Namespace) -> int:
    catalog = _open_existing(args.db)
    try:
        repo = CitationRepository(catalog)
        found = repo.citations_referencing(args.volume_id, args.page_id)
    finally:
        catalog.close()
    print(json.dumps(found, ensure_ascii=False, indent=2))
    return 0


def cmd_rights_declare(args: argparse.Namespace) -> int:
    """Apply dated rights declarations from JSONL atomically."""

    catalog = _open_existing(args.db)
    try:
        ledger = RightsLedger(catalog)
        declarations = _read_jsonl(args.declarations)
        counts = ledger.apply_batch(declarations)
    finally:
        catalog.close()
    print(json.dumps({"applied": counts}, ensure_ascii=False))
    return 0


def cmd_rights_edition(args: argparse.Namespace) -> int:
    catalog = _open_existing(args.db)
    try:
        ledger = RightsLedger(catalog)
        if args.list:
            out: Any = {"editions": ledger.editions()}
        else:
            ledger.create_edition(args.edition_id, args.description)
            out = {"created": args.edition_id}
    finally:
        catalog.close()
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0


def cmd_rights_page(args: argparse.Namespace) -> int:
    """Show a page's final entitlement and layered basis at a publication date."""

    catalog = _open_existing(args.db)
    try:
        ledger = RightsLedger(catalog)
        ent = ledger.resolve_page_entitlement(
            args.edition_id, args.volume_id, args.page_id, args.date
        )
        out = {
            "edition_id": ent.edition_id,
            "volume_id": ent.volume_id,
            "page_id": ent.page_id,
            "publication_date": ent.publication_date,
            "shares": ent.shares_as_strings(),
            "paths": list(ent.paths),
            "basis": ent.basis,
        }
    except Exception as exc:  # noqa: BLE001 - structural errors are the output
        out = {"error": _error_dict(exc)}
    finally:
        catalog.close()
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0 if "error" not in out else 2


def cmd_pricing_group(args: argparse.Namespace) -> int:
    catalog = _open_existing(args.db)
    try:
        ledger = SettlementLedger(catalog)
        if args.list:
            out: Any = {"groups": ledger.groups(), "columns": ledger.columns()}
        else:
            ledger.create_pricing_group(args.group_id, args.per_page_cents)
            out = {"created": args.group_id, "per_page_cents": args.per_page_cents}
    finally:
        catalog.close()
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0


def cmd_column(args: argparse.Namespace) -> int:
    catalog = _open_existing(args.db)
    try:
        ledger = SettlementLedger(catalog)
        if args.list:
            out: Any = {"columns": ledger.columns()}
        elif args.reassign:
            ledger.reassign_column(args.column_id, args.group_id)
            out = {"reassigned": args.column_id, "group_id": args.group_id}
        else:
            ledger.create_column(args.column_id, args.group_id)
            out = {"created": args.column_id, "group_id": args.group_id}
    finally:
        catalog.close()
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0


def cmd_issue(args: argparse.Namespace) -> int:
    catalog = _open_existing(args.db)
    try:
        ledger = SettlementLedger(catalog)
        out: Any = ledger.create_issue(
            args.issue_id, args.publication_date, args.edition_id
        )
    finally:
        catalog.close()
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0


def cmd_register(args: argparse.Namespace) -> int:
    catalog = _open_existing(args.db)
    try:
        ledger = SettlementLedger(catalog)
        if args.registrations:
            regs = _read_jsonl(args.registrations)
        else:
            regs = [
                {
                    "column_id": args.column_id,
                    "citation_id": args.citation_id,
                    "excerpt": args.excerpt,
                }
            ]
        rows = ledger.register_many(args.issue_id, regs)
    finally:
        catalog.close()
    print(json.dumps({"issue_id": args.issue_id, "registrations": rows},
                     ensure_ascii=False, indent=2))
    return 0


def cmd_settle(args: argparse.Namespace) -> int:
    catalog = _open_existing(args.db)
    try:
        ledger = SettlementLedger(catalog)
        try:
            voucher = ledger.settle(args.issue_id)
        except Exception as exc:  # noqa: BLE001 - structured rejection
            print(json.dumps({"error": _error_dict(exc)}, ensure_ascii=False, indent=2))
            return 2
    finally:
        catalog.close()
    if args.out:
        Path(args.out).write_text(
            json.dumps(voucher, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
    print(json.dumps(
        {
            "issue_id": voucher["issue_id"],
            "total_cents": voucher["total_cents"],
            "allotted": voucher["allocation"]["allotted"],
            "payees": [
                {
                    "payee_id": p["payee_id"],
                    "allotment": p["allotment"],
                    "prior_balance": p["prior_balance"],
                    "threshold": p["threshold"],
                    "paid": p["paid"],
                    "carried": p["carried"],
                }
                for p in voucher["payees"]
            ],
            "voucher_sha256": voucher["voucher_sha256"],
        },
        ensure_ascii=False,
        indent=2,
    ))
    return 0


def cmd_voucher(args: argparse.Namespace) -> int:
    catalog = _open_existing(args.db)
    try:
        ledger = SettlementLedger(catalog)
        if args.verify:
            out: Any = {
                "issue_id": args.issue_id,
                "verified": ledger.verify_voucher(args.issue_id),
            }
        else:
            voucher = ledger.get_voucher(args.issue_id)
            out = voucher if voucher is not None else {"error": _error_dict(
                ValueError(f"no settlement for issue {args.issue_id!r}")
            )}
    except Exception as exc:  # noqa: BLE001
        out = {"error": _error_dict(exc)}
    finally:
        catalog.close()
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0 if "error" not in out else 2


def cmd_balance(args: argparse.Namespace) -> int:
    catalog = _open_existing(args.db)
    try:
        ledger = SettlementLedger(catalog)
        out = ledger.pending_balance(args.payee_id)
    finally:
        catalog.close()
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0


def cmd_payee_terms(args: argparse.Namespace) -> int:
    catalog = _open_existing(args.db)
    try:
        ledger = SettlementLedger(catalog)
        ledger.set_threshold(args.payee_id, args.threshold_cents)
        if args.opening_cents is not None:
            ledger.set_opening_balance(args.payee_id, args.opening_cents)
        out = ledger.pending_balance(args.payee_id)
    finally:
        catalog.close()
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0


def cmd_inspect_issue(args: argparse.Namespace) -> int:
    """Pre-flight: which registrations are ready, which need attention."""

    catalog = _open_existing(args.db)
    try:
        ledger = SettlementLedger(catalog)
        out: Any = ledger.inspect(args.issue_id)
    except Exception as exc:  # noqa: BLE001
        out = {"error": _error_dict(exc)}
    finally:
        catalog.close()
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0 if "error" not in out else 2


def _error_dict(exc: BaseException) -> dict[str, Any]:
    if hasattr(exc, "as_dict"):
        return exc.as_dict()  # type: ignore[no-any-return]
    return {"code": exc.__class__.__name__, "message": str(exc)}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="citation_system")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("init", help="create a catalog from volumes pages JSON")
    p.add_argument("--db", required=True)
    p.add_argument("--pages", required=True)
    p.add_argument("--force", action="store_true")
    p.set_defaults(func=cmd_init)

    p = sub.add_parser("add-references", help="create citations from JSONL")
    p.add_argument("--db", required=True)
    p.add_argument("--references", required=True)
    p.set_defaults(func=cmd_add_references)

    p = sub.add_parser("apply-operations", help="apply operation JSONL batches")
    p.add_argument("--db", required=True)
    p.add_argument("--operations", required=True)
    p.set_defaults(func=cmd_apply_operations)

    p = sub.add_parser("export", help="write citation-product/1 JSONL files")
    p.add_argument("--db", required=True)
    p.add_argument("--usable", required=True)
    p.add_argument("--needs-attention", required=True)
    p.set_defaults(func=cmd_export)

    p = sub.add_parser("status", help="dump catalog and citation summary")
    p.add_argument("--db", required=True)
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("show-citation", help="show a stored citation")
    p.add_argument("--db", required=True)
    p.add_argument("citation_id")
    p.set_defaults(func=cmd_show_citation)

    p = sub.add_parser("lookup", help="resolve volume/section/label")
    p.add_argument("--db", required=True)
    p.add_argument("--volume", dest="volume_id", required=True)
    p.add_argument("--section", dest="section_id", required=True)
    p.add_argument("--label", required=True)
    p.set_defaults(func=cmd_lookup)

    p = sub.add_parser("referrers", help="citations referencing a real page")
    p.add_argument("--db", required=True)
    p.add_argument("--volume", dest="volume_id", required=True)
    p.add_argument("--page", dest="page_id", required=True)
    p.set_defaults(func=cmd_referrers)

    # ---- rights ------------------------------------------------------
    p = sub.add_parser("rights-edition", help="create or list rights editions")
    p.add_argument("--db", required=True)
    p.add_argument("--edition", dest="edition_id")
    p.add_argument("--description")
    p.add_argument("--list", action="store_true")
    p.set_defaults(func=cmd_rights_edition)

    p = sub.add_parser("rights-declare", help="apply dated declarations JSONL")
    p.add_argument("--db", required=True)
    p.add_argument("--declarations", required=True)
    p.set_defaults(func=cmd_rights_declare)

    p = sub.add_parser("rights-page", help="final entitlement + basis for a page")
    p.add_argument("--db", required=True)
    p.add_argument("--edition", dest="edition_id", required=True)
    p.add_argument("--volume", dest="volume_id", required=True)
    p.add_argument("--page", dest="page_id", required=True)
    p.add_argument("--date", required=True)
    p.set_defaults(func=cmd_rights_page)

    # ---- settlement --------------------------------------------------
    p = sub.add_parser("pricing-group", help="create or list pricing groups")
    p.add_argument("--db", required=True)
    p.add_argument("--group", dest="group_id")
    p.add_argument("--cents", dest="per_page_cents", type=int)
    p.add_argument("--list", action="store_true")
    p.set_defaults(func=cmd_pricing_group)

    p = sub.add_parser("column", help="create/reassign/list columns")
    p.add_argument("--db", required=True)
    p.add_argument("--column", dest="column_id")
    p.add_argument("--group", dest="group_id")
    p.add_argument("--reassign", action="store_true")
    p.add_argument("--list", action="store_true")
    p.set_defaults(func=cmd_column)

    p = sub.add_parser("issue", help="create a bound issue")
    p.add_argument("--db", required=True)
    p.add_argument("--issue", dest="issue_id", required=True)
    p.add_argument("--date", dest="publication_date", required=True)
    p.add_argument("--edition", dest="edition_id", required=True)
    p.set_defaults(func=cmd_issue)

    p = sub.add_parser("register", help="register excerpt(s) into issue columns")
    p.add_argument("--db", required=True)
    p.add_argument("--issue", dest="issue_id", required=True)
    p.add_argument("--column", dest="column_id")
    p.add_argument("--citation", dest="citation_id")
    p.add_argument("--excerpt")
    p.add_argument("--registrations")
    p.set_defaults(func=cmd_register)

    p = sub.add_parser("inspect-issue", help="pre-flight ready/needs-attention report")
    p.add_argument("--db", required=True)
    p.add_argument("--issue", dest="issue_id", required=True)
    p.set_defaults(func=cmd_inspect_issue)

    p = sub.add_parser("settle", help="seal an issue atomically")    p.add_argument("--db", required=True)
    p.add_argument("--issue", dest="issue_id", required=True)
    p.add_argument("--out")
    p.set_defaults(func=cmd_settle)

    p = sub.add_parser("voucher", help="show or verify a sealed voucher")
    p.add_argument("--db", required=True)
    p.add_argument("--issue", dest="issue_id", required=True)
    p.add_argument("--verify", action="store_true")
    p.set_defaults(func=cmd_voucher)

    p = sub.add_parser("balance", help="show a payee's pending balance")
    p.add_argument("--db", required=True)
    p.add_argument("--payee", dest="payee_id", required=True)
    p.set_defaults(func=cmd_balance)

    p = sub.add_parser("payee-terms", help="set threshold and optional opening balance")
    p.add_argument("--db", required=True)
    p.add_argument("--payee", dest="payee_id", required=True)
    p.add_argument("--threshold", dest="threshold_cents", type=int, required=True)
    p.add_argument("--opening", dest="opening_cents", type=int)
    p.set_defaults(func=cmd_payee_terms)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except Exception as exc:  # noqa: BLE001 - single, structured CLI failure
        print(json.dumps({"error": _error_dict(exc)}, ensure_ascii=False),
              file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
