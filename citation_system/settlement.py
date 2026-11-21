"""Per-issue royalty settlement for a bound periodical (合刊).

Pricing
-------
A column (栏目) belongs to exactly one pricing group (计价组).  Within one
issue, every distinct ``(group, page)`` pair is charged **once**, at the
group's integer cents-per-page.  The same page reached through several columns
in the same group is paid once; the same page in two different groups is paid
once per group.  Page identity is ``(volume_id, page_id)``.

Exact maths
-----------
For every ``(group, page)`` unit the group's cents are multiplied by the
payee's exact final share of that page (from :mod:`citation_system.rights`).
The per-payee exact amounts over the whole issue are summed, floored, and the
remaining cents are handed out one at a time by descending fractional
remainder, ties broken by ascending payee id.

Pending balance and payout
--------------------------
Each payee keeps a pending balance made of *lots*, each labelled with the
issue that produced it (or ``opening``).  Sealing an issue is one atomic
transaction that:

1. refuses to proceed if **any** registered excerpt is currently
   unresolvable (needs attention) or any page's rights cannot be resolved;
   the whole issue is blocked, never settled on the usable subset;
2. computes this issue's integer allotment per payee;
3. for each payee, ``available = prior_balance + allotment``; if
   ``available >= threshold`` it pays out the full ``available`` and consumes
   the prior lots, otherwise it carries ``available`` forward;
4. records this issue's allotment and the payout in a sealed voucher whose
   canonical JSON is hashed.

Re-closing an already sealed issue returns the stored voucher and consumes no
balance again.  Because sealing runs in ``BEGIN IMMEDIATE`` two concurrent
attempts serialize and only the first can mutate the ledger.
"""

from __future__ import annotations

import datetime
import hashlib
import json
import sqlite3
from dataclasses import dataclass
from fractions import Fraction
from typing import Any, Iterable

from . import db
from .errors import (
    COLUMN_EXISTS,
    COLUMN_NOT_FOUND,
    INVALID_SETTLEMENT,
    ISSUE_EXISTS,
    ISSUE_HAS_NO_CITATIONS,
    ISSUE_NOT_FOUND,
    PRICING_GROUP_EXISTS,
    PRICING_GROUP_NOT_FOUND,
    SETTLEMENT_BLOCKED,
)
from .errors import OperationRejected
from .export import evaluate_snapshot, load_snapshot
from .repository import CitationRepository
from .rights import (
    _frac_str,
    PageEntitlement,
    RightsLedger,
    normalize_date,
)


def _now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


@dataclass(frozen=True)
class Unit:
    group_id: str
    volume_id: str
    page_id: str
    cents: int

    @property
    def key(self) -> tuple[str, str, str]:
        return (self.group_id, self.volume_id, self.page_id)


class SettlementLedger:
    def __init__(self, catalog) -> None:
        self.catalog = catalog
        self.conn = catalog.conn
        self.rights = RightsLedger(catalog)

    # ------------------------------------------------------------------
    # pricing groups and columns
    # ------------------------------------------------------------------
    def create_pricing_group(self, group_id: str, per_page_cents: int) -> None:
        if not isinstance(group_id, str) or not group_id:
            raise OperationRejected(INVALID_SETTLEMENT, "group_id must be a non-empty string")
        if isinstance(per_page_cents, bool) or not isinstance(per_page_cents, int):
            raise OperationRejected(INVALID_SETTLEMENT, "per_page_cents must be an integer")
        if per_page_cents < 0:
            raise OperationRejected(INVALID_SETTLEMENT, "per_page_cents must be non-negative")
        if self._group_row(group_id) is not None:
            raise OperationRejected(
                PRICING_GROUP_EXISTS, f"pricing group {group_id!r} already exists",
                details={"group_id": group_id},
            )
        with db.write_transaction(self.conn):
            self.conn.execute(
                "INSERT INTO pricing_groups (group_id, per_page_cents) VALUES (?, ?)",
                (group_id, per_page_cents),
            )

    def set_pricing_group_rate(self, group_id: str, per_page_cents: int) -> None:
        if isinstance(per_page_cents, bool) or not isinstance(per_page_cents, int) or per_page_cents < 0:
            raise OperationRejected(INVALID_SETTLEMENT, "per_page_cents must be a non-negative integer")
        self.require_group(group_id)
        with db.write_transaction(self.conn):
            self.conn.execute(
                "UPDATE pricing_groups SET per_page_cents = ? WHERE group_id = ?",
                (per_page_cents, group_id),
            )

    def groups(self) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT group_id, per_page_cents FROM pricing_groups ORDER BY group_id"
        ).fetchall()
        return [dict(r) for r in rows]

    def _group_row(self, group_id: str) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM pricing_groups WHERE group_id = ?", (group_id,)
        ).fetchone()

    def require_group(self, group_id: str) -> dict[str, Any]:
        row = self._group_row(group_id)
        if row is None:
            raise OperationRejected(
                PRICING_GROUP_NOT_FOUND,
                f"pricing group {group_id!r} does not exist",
                details={"group_id": group_id},
            )
        return dict(row)

    def create_column(self, column_id: str, pricing_group_id: str) -> None:
        if not isinstance(column_id, str) or not column_id:
            raise OperationRejected(INVALID_SETTLEMENT, "column_id must be a non-empty string")
        if self._column_row(column_id) is not None:
            raise OperationRejected(
                COLUMN_EXISTS, f"column {column_id!r} already exists",
                details={"column_id": column_id},
            )
        self.require_group(pricing_group_id)
        with db.write_transaction(self.conn):
            self.conn.execute(
                "INSERT INTO columns_table (column_id, pricing_group_id) VALUES (?, ?)",
                (column_id, pricing_group_id),
            )

    def reassign_column(self, column_id: str, pricing_group_id: str) -> None:
        if self._column_row(column_id) is None:
            raise OperationRejected(
                COLUMN_NOT_FOUND, f"column {column_id!r} does not exist",
                details={"column_id": column_id},
            )
        self.require_group(pricing_group_id)
        with db.write_transaction(self.conn):
            self.conn.execute(
                "UPDATE columns_table SET pricing_group_id = ? WHERE column_id = ?",
                (pricing_group_id, column_id),
            )

    def columns(self) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT column_id, pricing_group_id FROM columns_table ORDER BY column_id"
        ).fetchall()
        return [dict(r) for r in rows]

    def _column_row(self, column_id: str) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM columns_table WHERE column_id = ?", (column_id,)
        ).fetchone()

    def require_column(self, column_id: str) -> dict[str, Any]:
        row = self._column_row(column_id)
        if row is None:
            raise OperationRejected(
                COLUMN_NOT_FOUND, f"column {column_id!r} does not exist",
                details={"column_id": column_id},
            )
        return dict(row)

    # ------------------------------------------------------------------
    # issues and registrations
    # ------------------------------------------------------------------
    def create_issue(
        self, issue_id: str, publication_date: Any, rights_edition_id: str
    ) -> dict[str, Any]:
        if not isinstance(issue_id, str) or not issue_id:
            raise OperationRejected(INVALID_SETTLEMENT, "issue_id must be a non-empty string")
        date = normalize_date(publication_date)
        self.rights.require_edition(rights_edition_id)
        if self._issue_row(issue_id) is not None:
            raise OperationRejected(
                ISSUE_EXISTS, f"issue {issue_id!r} already exists",
                details={"issue_id": issue_id},
            )
        revision = db.catalog_revision(self.conn)
        with db.write_transaction(self.conn):
            self.conn.execute(
                "INSERT INTO issues (issue_id, publication_date, rights_edition_id, "
                "catalog_revision, status, created_at) VALUES (?,?,?,?, 'open', ?)",
                (issue_id, date, rights_edition_id, revision, _now()),
            )
        return self.get_issue(issue_id)

    def get_issue(self, issue_id: str) -> dict[str, Any]:
        row = self._issue_row(issue_id)
        if row is None:
            raise OperationRejected(
                ISSUE_NOT_FOUND, f"issue {issue_id!r} does not exist",
                details={"issue_id": issue_id},
            )
        return dict(row)

    def _issue_row(self, issue_id: str) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM issues WHERE issue_id = ?", (issue_id,)
        ).fetchone()

    def register(
        self,
        issue_id: str,
        column_id: str,
        citation_id: str,
        *,
        excerpt: str | None = None,
        position: int | None = None,
    ) -> dict[str, Any]:
        return self.register_many(
            issue_id,
            [
                {
                    "column_id": column_id,
                    "citation_id": citation_id,
                    "excerpt": excerpt,
                    "position": position,
                }
            ],
        )[0]

    def register_many(
        self, issue_id: str, registrations: Iterable[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        """Register excerpts to columns for an issue, atomically.

        Each registration records the column, the excerpt (the citation), the
        excerpt text and the catalog revision at registration.  Registering the
        same ``(issue, column, citation)`` again updates that row (a correction)
        rather than duplicating it.
        """

        issue = self.get_issue(issue_id)
        if issue["status"] != "open":
            raise OperationRejected(
                INVALID_SETTLEMENT, f"issue {issue_id!r} is sealed",
                details={"issue_id": issue_id, "status": issue["status"]},
            )
        repo = CitationRepository(self.catalog)
        prepared: list[dict[str, Any]] = []
        for raw in registrations:
            if not isinstance(raw, dict):
                raise OperationRejected(INVALID_SETTLEMENT, "registration must be an object")
            column_id = raw.get("column_id")
            citation_id = raw.get("citation_id")
            if not isinstance(column_id, str) or not column_id:
                raise OperationRejected(INVALID_SETTLEMENT, "column_id is required")
            if not isinstance(citation_id, str) or not citation_id:
                raise OperationRejected(INVALID_SETTLEMENT, "citation_id is required")
            self.require_column(column_id)
            citation = repo.get(citation_id)  # raises if missing
            excerpt = raw.get("excerpt")
            if excerpt is None:
                excerpt = citation.text
            if not isinstance(excerpt, str):
                raise OperationRejected(INVALID_SETTLEMENT, "excerpt must be a string")
            prepared.append(
                {
                    "column_id": column_id,
                    "citation_id": citation_id,
                    "excerpt": excerpt,
                    "position": raw.get("position"),
                }
            )
        revision = db.catalog_revision(self.conn)
        with db.write_transaction(self.conn):
            for item in prepared:
                existing = self.conn.execute(
                    "SELECT id FROM issue_citations WHERE issue_id = ? AND column_id = ? "
                    "AND citation_id = ?",
                    (issue_id, item["column_id"], item["citation_id"]),
                ).fetchone()
                if existing is not None:
                    self.conn.execute(
                        "UPDATE issue_citations SET excerpt = ?, catalog_revision = ? "
                        "WHERE id = ?",
                        (item["excerpt"], revision, existing["id"]),
                    )
                    continue
                pos = item["position"]
                if pos is None:
                    row = self.conn.execute(
                        "SELECT COALESCE(MAX(position), 0) AS m FROM issue_citations "
                        "WHERE issue_id = ?",
                        (issue_id,),
                    ).fetchone()
                    pos = int(row["m"]) + 1
                self.conn.execute(
                    "INSERT INTO issue_citations (issue_id, column_id, citation_id, "
                    "position, excerpt, catalog_revision) VALUES (?,?,?,?,?,?)",
                    (issue_id, item["column_id"], item["citation_id"], int(pos),
                     item["excerpt"], revision),
                )
        return self.registrations(issue_id)

    def registrations(self, issue_id: str) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM issue_citations WHERE issue_id = ? ORDER BY position, id",
            (issue_id,),
        ).fetchall()
        return [dict(r) for r in rows]

    def inspect(self, issue_id: str) -> dict[str, Any]:
        """Report which registrations are ready and which need attention.

        This is a non-mutating pre-flight: it never seals, and it leaves the
        exact column and citation for every unresolved excerpt so the whole
        issue can be fixed before freezing.
        """

        issue = self.get_issue(issue_id)
        registrations = self.registrations(issue_id)
        with db.read_snapshot_transaction(self.conn):
            snapshot = load_snapshot(self.conn)
            usable, attention = evaluate_snapshot(snapshot)
        ready_ids = {p["citation_id"] for p in usable}
        attention_by = {p["citation_id"]: p for p in attention}
        ready: list[dict[str, Any]] = []
        needs_attention: list[dict[str, Any]] = []
        for reg in registrations:
            cid = reg["citation_id"]
            if cid in ready_ids:
                ready.append(
                    {"column_id": reg["column_id"], "citation_id": cid,
                     "position": reg["position"]}
                )
            else:
                product = attention_by.get(cid)
                needs_attention.append(
                    {
                        "column_id": reg["column_id"],
                        "citation_id": cid,
                        "position": reg["position"],
                        "reason": product["reason"] if product else "citation_missing",
                        "detail": product.get("detail", {}) if product else {},
                    }
                )
        return {
            "issue_id": issue_id,
            "status": issue["status"],
            "ready": ready,
            "needs_attention": needs_attention,
            "settleable": not needs_attention and bool(registrations),
        }

    # ------------------------------------------------------------------
    # sealing / settlement
    # ------------------------------------------------------------------
    def settle(self, issue_id: str) -> dict[str, Any]:
        """Seal an issue atomically and return (or replay) its voucher."""

        with db.write_transaction(self.conn):
            existing = self.conn.execute(
                "SELECT * FROM settlements WHERE issue_id = ?", (issue_id,)
            ).fetchone()
            if existing is not None:
                # Idempotent replay: no balance is consumed a second time.
                return json.loads(existing["voucher_json"])

            issue = self._issue_row(issue_id)
            if issue is None:
                raise OperationRejected(
                    ISSUE_NOT_FOUND, f"issue {issue_id!r} does not exist",
                    details={"issue_id": issue_id},
                )
            registrations = self.conn.execute(
                "SELECT * FROM issue_citations WHERE issue_id = ? ORDER BY position, id",
                (issue_id,),
            ).fetchall()
            if not registrations:
                raise OperationRejected(
                    ISSUE_HAS_NO_CITATIONS, f"issue {issue_id!r} has no registered excerpts",
                    details={"issue_id": issue_id},
                )

            snapshot = load_snapshot(self.conn)
            usable, attention = evaluate_snapshot(snapshot)
            by_citation = {p["citation_id"]: p for p in usable}
            unresolved = {p["citation_id"]: p for p in attention}

            # 1) Every registered excerpt must be resolvable; nothing partial.
            pending: list[dict[str, Any]] = []
            pages_by_registration: list[tuple[sqlite3.Row, list[dict[str, Any]]]] = []
            for reg in registrations:
                cid = reg["citation_id"]
                if cid in unresolved:
                    product = unresolved[cid]
                    pending.append(
                        {
                            "column_id": reg["column_id"],
                            "citation_id": cid,
                            "reason": product["reason"],
                            "detail": product.get("detail", {}),
                        }
                    )
                    continue
                product = by_citation.get(cid)
                if product is None:
                    pending.append(
                        {
                            "column_id": reg["column_id"],
                            "citation_id": cid,
                            "reason": "citation_missing",
                        }
                    )
                    continue
                pages_by_registration.append((reg, product["pages"]))
            if pending:
                raise OperationRejected(
                    SETTLEMENT_BLOCKED,
                    "issue cannot be sealed while excerpts are unresolved",
                    details={"issue_id": issue_id, "unresolved": pending},
                )

            # 2) Build the distinct (group, page) units.
            group_rates = {
                r["group_id"]: int(r["per_page_cents"]) for r in self.groups()
            }
            column_group = {
                c["column_id"]: c["pricing_group_id"] for c in self.columns()
            }
            units: dict[tuple[str, str, str], Unit] = {}
            entitlements: dict[tuple[str, str], PageEntitlement] = {}
            rights_problems: list[dict[str, Any]] = []
            for reg, pages in pages_by_registration:
                group_id = column_group.get(reg["column_id"])
                if group_id is None:  # pragma: no cover - schema guards this
                    rights_problems.append(
                        {"column_id": reg["column_id"], "citation_id": reg["citation_id"],
                         "reason": "column_has_no_group"}
                    )
                    continue
                if group_id not in group_rates:
                    rights_problems.append(
                        {"column_id": reg["column_id"], "citation_id": reg["citation_id"],
                         "reason": "pricing_group_missing", "group_id": group_id}
                    )
                    continue
                rate = group_rates[group_id]
                for page in pages:
                    volume_id, page_id = page["volume_id"], page["page_id"]
                    key = (group_id, volume_id, page_id)
                    if key in units:
                        continue
                    units[key] = Unit(group_id, volume_id, page_id, rate)
                    page_key = (volume_id, page_id)
                    if page_key not in entitlements:
                        ent, problem = self.rights.try_resolve_page(
                            issue["rights_edition_id"],
                            volume_id,
                            page_id,
                            issue["publication_date"],
                        )
                        if problem is not None:
                            rights_problems.append(
                                {
                                    "column_id": reg["column_id"],
                                    "citation_id": reg["citation_id"],
                                    "volume_id": volume_id,
                                    "page_id": page_id,
                                    "reason": "rights_needs_review",
                                    "rights_problem": problem,
                                }
                            )
                            continue
                        assert ent is not None
                        entitlements[page_key] = ent
            if rights_problems:
                raise OperationRejected(
                    SETTLEMENT_BLOCKED,
                    "issue cannot be sealed while page rights are unresolved",
                    details={"issue_id": issue_id, "rights": rights_problems},
                )

            # 3) Exact per-payee amounts, then integer allocation.
            allocation = self._allocate(units, entitlements)

            # 4) Pending balance / threshold payouts.
            payee_rows = self._apply_payouts(issue_id, allocation["allotted"])

            voucher = self._build_voucher(
                issue, snapshot.revision, registrations, units, entitlements,
                allocation, payee_rows,
            )
            voucher_json = _canonical_json(voucher)
            digest = hashlib.sha256(voucher_json.encode("utf-8")).hexdigest()
            voucher["voucher_sha256"] = digest
            # Hash is over the body without the digest field; re-serialize with
            # the digest included so on-disk JSON also verifies independently.
            stored_json = _canonical_json(voucher)
            self.conn.execute(
                "INSERT INTO settlements (issue_id, publication_date, rights_edition_id, "
                "catalog_revision, voucher_sha256, voucher_json, created_at) "
                "VALUES (?,?,?,?,?,?,?)",
                (
                    issue_id, issue["publication_date"], issue["rights_edition_id"],
                    snapshot.revision, digest, stored_json, _now(),
                ),
            )
            self.conn.execute(
                "UPDATE issues SET status = 'sealed' WHERE issue_id = ?", (issue_id,)
            )
            return voucher

    # ------------------------------------------------------------------
    def _allocate(
        self,
        units: dict[tuple[str, str, str], Unit],
        entitlements: dict[tuple[str, str], PageEntitlement],
    ) -> dict[str, Any]:
        exact: dict[str, Fraction] = {}
        basis: dict[str, list[dict[str, Any]]] = {}
        for unit in units.values():
            ent = entitlements[(unit.volume_id, unit.page_id)]
            for payee, share in ent.shares.items():
                amount = Fraction(unit.cents) * share
                exact[payee] = exact.get(payee, Fraction(0)) + amount
                basis.setdefault(payee, []).append(
                    {
                        "group_id": unit.group_id,
                        "volume_id": unit.volume_id,
                        "page_id": unit.page_id,
                        "unit_cents": unit.cents,
                        "share": _frac_str(share),
                        "amount": _frac_str(amount),
                    }
                )

        total_cents = sum(u.cents for u in units.values())
        floored = {p: amount.numerator // amount.denominator for p, amount in exact.items()}
        remainder = total_cents - sum(floored.values())
        if remainder < 0:  # pragma: no cover - shares are non-negative
            raise OperationRejected(INVALID_SETTLEMENT, "negative rounding remainder")

        residuals = {p: exact[p] - floored[p] for p in exact}
        order = sorted(residuals, key=lambda p: (-residuals[p], p))
        awarded = {p: 0 for p in exact}
        for payee in order[:remainder]:
            awarded[payee] = 1
        allotted = {p: floored[p] + awarded[p] for p in exact}

        return {
            "total_cents": total_cents,
            "exact": exact,
            "floored": floored,
            "residuals": residuals,
            "remainder_order": order,
            "remainder_cents": remainder,
            "remainder_awarded": awarded,
            "allotted": allotted,
            "basis": basis,
        }

    def _prior_balance(self, payee_id: str) -> int:
        row = self.conn.execute(
            "SELECT COALESCE(SUM(cents), 0) AS c FROM pending_lots WHERE payee_id = ?",
            (payee_id,),
        ).fetchone()
        return int(row["c"])

    def _prior_lots(self, payee_id: str) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT id, source, cents FROM pending_lots WHERE payee_id = ? ORDER BY id",
            (payee_id,),
        ).fetchall()
        return [{"lot_id": int(r["id"]), "source": r["source"], "cents": int(r["cents"])}
                for r in rows]

    def _threshold(self, payee_id: str) -> int:
        row = self.conn.execute(
            "SELECT threshold_cents FROM payee_terms WHERE payee_id = ?", (payee_id,)
        ).fetchone()
        return int(row["threshold_cents"]) if row is not None else 0

    def _apply_payouts(
        self, issue_id: str, allotted: dict[str, int]
    ) -> list[dict[str, Any]]:
        # Only payees who participate in *this* issue are evaluated.  A payee
        # with a carried balance but no share this issue is not triggered; the
        # balance simply stays in its lots.  Payees whose exact share floors to
        # zero still appear in ``allotted`` and are evaluated (they may reach
        # the threshold on the carried amount alone).
        payees = sorted(allotted)
        results: list[dict[str, Any]] = []
        for payee in payees:
            prior = self._prior_balance(payee)
            lots = self._prior_lots(payee)
            allotment = int(allotted.get(payee, 0))
            threshold = self._threshold(payee)
            available = prior + allotment
            if available >= threshold:
                paid = available
                carried = 0
                self.conn.execute(
                    "DELETE FROM pending_lots WHERE payee_id = ?", (payee,)
                )
            else:
                paid = 0
                carried = available
                if allotment:
                    self.conn.execute(
                        "INSERT INTO pending_lots (payee_id, source, cents) VALUES (?, ?, ?)",
                        (payee, issue_id, allotment),
                    )
            results.append(
                {
                    "payee_id": payee,
                    "prior_balance": prior,
                    "prior_lots": lots,
                    "allotment": allotment,
                    "available": available,
                    "threshold": threshold,
                    "paid": paid,
                    "carried": carried,
                }
            )
        return results

    def _build_voucher(
        self,
        issue: sqlite3.Row,
        revision: int,
        registrations: Iterable[sqlite3.Row],
        units: dict[tuple[str, str, str], Unit],
        entitlements: dict[tuple[str, str], PageEntitlement],
        allocation: dict[str, Any],
        payees: list[dict[str, Any]],
    ) -> dict[str, Any]:
        entitlement_rows = []
        for (volume_id, page_id), ent in sorted(entitlements.items()):
            entitlement_rows.append(
                {
                    "volume_id": volume_id,
                    "page_id": page_id,
                    "publication_date": ent.publication_date,
                    "page_share_version": ent.basis["page_share_version"],
                    "shares": ent.shares_as_strings(),
                    "basis": ent.basis,
                }
            )
        basis_rows = {}
        for payee, entries in allocation["basis"].items():
            basis_rows[payee] = entries
        voucher = {
            "format": "royalty-voucher/1",
            "issue_id": issue["issue_id"],
            "publication_date": issue["publication_date"],
            "rights_edition_id": issue["rights_edition_id"],
            "catalog_revision": revision,
            "registrations": [
                {
                    "column_id": r["column_id"],
                    "citation_id": r["citation_id"],
                    "excerpt": r["excerpt"],
                    "registered_catalog_revision": int(r["catalog_revision"]),
                }
                for r in registrations
            ],
            "units": [
                {
                    "group_id": u.group_id,
                    "volume_id": u.volume_id,
                    "page_id": u.page_id,
                    "unit_cents": u.cents,
                }
                for u in sorted(units.values(), key=lambda u: u.key)
            ],
            "total_cents": allocation["total_cents"],
            "entitlements": entitlement_rows,
            "allocation": {
                "exact": {p: _frac_str(v) for p, v in sorted(allocation["exact"].items())},
                "floored": dict(sorted(allocation["floored"].items())),
                "residuals": {
                    p: _frac_str(v) for p, v in sorted(allocation["residuals"].items())
                },
                "remainder_cents": allocation["remainder_cents"],
                "remainder_order": allocation["remainder_order"],
                "remainder_awarded": dict(sorted(allocation["remainder_awarded"].items())),
                "allotted": dict(sorted(allocation["allotted"].items())),
                "basis": basis_rows,
            },
            "payees": payees,
        }
        return voucher

    # ------------------------------------------------------------------
    # reading
    # ------------------------------------------------------------------
    def set_opening_balance(self, payee_id: str, cents: int) -> None:
        if isinstance(cents, bool) or not isinstance(cents, int) or cents < 0:
            raise OperationRejected(INVALID_SETTLEMENT, "opening balance must be a non-negative integer")
        with db.write_transaction(self.conn):
            self.conn.execute(
                "DELETE FROM pending_lots WHERE payee_id = ? AND source = 'opening'",
                (payee_id,),
            )
            if cents:
                self.conn.execute(
                    "INSERT INTO pending_lots (payee_id, source, cents) VALUES (?, 'opening', ?)",
                    (payee_id, cents),
                )

    def set_threshold(self, payee_id: str, cents: int) -> None:
        if isinstance(cents, bool) or not isinstance(cents, int) or cents < 0:
            raise OperationRejected(INVALID_SETTLEMENT, "threshold must be a non-negative integer")
        with db.write_transaction(self.conn):
            self.conn.execute(
                "INSERT INTO payee_terms (payee_id, threshold_cents) VALUES (?, ?) "
                "ON CONFLICT(payee_id) DO UPDATE SET threshold_cents = excluded.threshold_cents",
                (payee_id, cents),
            )

    def pending_balance(self, payee_id: str) -> dict[str, Any]:
        return {
            "payee_id": payee_id,
            "balance": self._prior_balance(payee_id),
            "lots": self._prior_lots(payee_id),
            "threshold": self._threshold(payee_id),
        }

    def get_voucher(self, issue_id: str) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT * FROM settlements WHERE issue_id = ?", (issue_id,)
        ).fetchone()
        return json.loads(row["voucher_json"]) if row is not None else None

    def vouchers(self) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT voucher_json FROM settlements ORDER BY created_at, issue_id"
        ).fetchall()
        return [json.loads(r["voucher_json"]) for r in rows]

    def verify_voucher(self, issue_id: str) -> bool:
        """Recompute the digest over the stored body (without the digest)."""

        row = self.conn.execute(
            "SELECT voucher_json, voucher_sha256 FROM settlements WHERE issue_id = ?",
            (issue_id,),
        ).fetchone()
        if row is None:
            raise OperationRejected(
                ISSUE_NOT_FOUND, f"no settlement for issue {issue_id!r}",
                details={"issue_id": issue_id},
            )
        stored = json.loads(row["voucher_json"])
        digest = stored.pop("voucher_sha256", None)
        recomputed = hashlib.sha256(_canonical_json(stored).encode("utf-8")).hexdigest()
        return digest == recomputed == row["voucher_sha256"]
