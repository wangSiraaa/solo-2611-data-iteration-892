"""Rights editions (权属版) for page identities.

A *rights edition* is a named set of declarations.  Both page shares and
account allocations are **versioned by effective date**:

* A page's declaration for ``(edition, volume, page)`` is the newest set of
  rows whose ``effective_date`` is on or before the issue's publication date.
* An account's allocation is resolved the same way.

This is what lets an old issue be re-settled with the rights that were in
force on its publication date: a correction entered with a later effective
date never back-fills an older issue.  Querying an older publication date
therefore re-reads the older version ("旧版重查"); adding a new dated version
is a batch correction ("批量更正").

Shares are exact :class:`fractions.Fraction` values.  Every declaration level
(page accounts, and each account's outgoing allocation) must be non-negative
and sum to exactly ``1``.  A page-level account with no allocation rows is a
final payee.  An account whose allocations exist only for *later* dates does
not cover the requested publication date and makes the page *needs review*.
A cycle reachable from a page's accounts likewise makes the page needs review;
the settlement layer must then refuse to settle the whole issue.

The final entitlement of a page is the merged per-payee total over every
reachable path, together with a layer-by-layer basis that names the page-level
accounts, the allocation version used at each account, and each path.
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass
from fractions import Fraction
from typing import Any, Iterable, Mapping

from . import db
from .errors import (
    INVALID_RIGHTS,
    RIGHTS_EDITION_EXISTS,
    RIGHTS_EDITION_NOT_FOUND,
    RIGHTS_NEEDS_REVIEW,
    RIGHTS_VERSION_EXISTS,
    CitationSystemError,
)
from .errors import OperationRejected


class RightsResolutionError(CitationSystemError):
    """A page's rights cannot be resolved for a given edition/date."""


@dataclass(frozen=True)
class PageEntitlement:
    edition_id: str
    volume_id: str
    page_id: str
    publication_date: str
    shares: dict[str, Fraction]      # merged final payee -> fraction
    paths: tuple[dict[str, Any], ...]
    basis: dict[str, Any]

    def shares_as_strings(self) -> dict[str, str]:
        return {k: _frac_str(v) for k, v in self.shares.items()}


# ----------------------------------------------------------------------
# helpers
# ----------------------------------------------------------------------
def normalize_date(value: Any) -> str:
    if isinstance(value, datetime.date):
        return value.isoformat()
    if isinstance(value, str):
        try:
            datetime.date.fromisoformat(value)
        except ValueError as exc:
            raise OperationRejected(
                INVALID_RIGHTS, f"invalid ISO date {value!r}",
                details={"date": value},
            ) from exc
        return value
    raise OperationRejected(
        INVALID_RIGHTS, "date must be an ISO string or datetime.date",
        details={"date": value},
    )


def parse_share(value: Any) -> Fraction:
    """Parse a non-negative rational exactly (never through float)."""

    if isinstance(value, Fraction):
        result = value
    elif isinstance(value, bool):
        raise OperationRejected(INVALID_RIGHTS, "share must be rational", details={"share": value})
    elif isinstance(value, int):
        result = Fraction(value, 1)
    elif isinstance(value, str):
        try:
            result = Fraction(value)
        except (ValueError, ZeroDivisionError) as exc:
            raise OperationRejected(
                INVALID_RIGHTS, f"invalid rational share {value!r}",
                details={"share": value},
            ) from exc
    else:
        raise OperationRejected(
            INVALID_RIGHTS, "share must be a Fraction, int or 'num/den' string",
            details={"share": value},
        )
    if result < 0:
        raise OperationRejected(
            INVALID_RIGHTS, "shares must be non-negative", details={"share": str(value)}
        )
    return result


def parse_shares(pairs: Any) -> list[tuple[str, Fraction]]:
    """Accept a mapping or a list of ``{"account_id","share"}`` records."""

    out: list[tuple[str, Fraction]] = []
    if isinstance(pairs, Mapping):
        items: Iterable[tuple[Any, Any]] = pairs.items()
        for account_id, share in items:
            if not isinstance(account_id, str) or not account_id:
                raise OperationRejected(INVALID_RIGHTS, "account id must be a non-empty string")
            out.append((account_id, parse_share(share)))
    elif isinstance(pairs, list):
        for item in pairs:
            if not isinstance(item, dict):
                raise OperationRejected(INVALID_RIGHTS, "each share must be an object")
            account_id = item.get("account_id")
            if not isinstance(account_id, str) or not account_id:
                raise OperationRejected(INVALID_RIGHTS, "account_id is required")
            share = item.get("share")
            if share is None and "num" in item and "den" in item:
                share = f"{int(item['num'])}/{int(item['den'])}"
            out.append((account_id, parse_share(share)))
    else:
        raise OperationRejected(INVALID_RIGHTS, "shares must be a mapping or a list")
    _validate_level(out)
    return out


def _validate_level(pairs: list[tuple[str, Fraction]]) -> None:
    if not pairs:
        raise OperationRejected(INVALID_RIGHTS, "a rights level needs at least one share")
    accounts = [a for a, _ in pairs]
    if len(set(accounts)) != len(accounts):
        raise OperationRejected(
            INVALID_RIGHTS, "duplicate account in one rights level",
            details={"accounts": sorted(accounts)},
        )
    total = sum((s for _, s in pairs), Fraction(0))
    if total != 1:
        raise OperationRejected(
            INVALID_RIGHTS, "shares in one level must sum to exactly 1",
            details={"sum": _frac_str(total), "shares": _share_pairs_json(pairs)},
        )


def _frac_str(value: Fraction) -> str:
    return f"{value.numerator}/{value.denominator}"


def _share_pairs_json(pairs: Iterable[tuple[str, Fraction]]) -> list[dict[str, str]]:
    return [{"account_id": a, "share": _frac_str(s)} for a, s in pairs]


def _frac_json(value: Fraction) -> dict[str, int]:
    return {"num": value.numerator, "den": value.denominator}


# ----------------------------------------------------------------------
class RightsLedger:
    def __init__(self, catalog) -> None:
        self.catalog = catalog
        self.conn = catalog.conn

    # ---------------------------- editions ----------------------------
    def create_edition(self, edition_id: str, description: str | None = None) -> None:
        if not isinstance(edition_id, str) or not edition_id:
            raise OperationRejected(INVALID_RIGHTS, "edition_id must be a non-empty string")
        row = self.conn.execute(
            "SELECT 1 FROM rights_editions WHERE edition_id = ?", (edition_id,)
        ).fetchone()
        if row is not None:
            raise OperationRejected(
                RIGHTS_EDITION_EXISTS, f"rights edition {edition_id!r} already exists",
                details={"edition_id": edition_id},
            )
        with db.write_transaction(self.conn):
            self.conn.execute(
                "INSERT INTO rights_editions (edition_id, description) VALUES (?, ?)",
                (edition_id, description),
            )

    def editions(self) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT edition_id, description FROM rights_editions ORDER BY edition_id"
        ).fetchall()
        return [dict(r) for r in rows]

    def require_edition(self, edition_id: str) -> None:
        row = self.conn.execute(
            "SELECT 1 FROM rights_editions WHERE edition_id = ?", (edition_id,)
        ).fetchone()
        if row is None:
            raise OperationRejected(
                RIGHTS_EDITION_NOT_FOUND,
                f"rights edition {edition_id!r} does not exist",
                details={"edition_id": edition_id},
            )

    # ------------------------ declaring versions ----------------------
    def add_page_shares(
        self,
        edition_id: str,
        volume_id: str,
        page_id: str,
        effective_date: Any,
        shares: Any,
        *,
        replace: bool = False,
    ) -> None:
        """Declare one dated version of a page's direct account shares."""

        self.apply_batch(
            [
                {
                    "kind": "page_shares",
                    "edition_id": edition_id,
                    "volume_id": volume_id,
                    "page_id": page_id,
                    "effective_date": effective_date,
                    "shares": shares,
                    "replace": replace,
                }
            ]
        )

    def add_account_allocations(
        self,
        edition_id: str,
        account_id: str,
        effective_date: Any,
        allocations: Any,
        *,
        replace: bool = False,
    ) -> None:
        """Declare one dated version of an account's outgoing allocation."""

        self.apply_batch(
            [
                {
                    "kind": "account_allocations",
                    "edition_id": edition_id,
                    "account_id": account_id,
                    "effective_date": effective_date,
                    "shares": allocations,
                    "replace": replace,
                }
            ]
        )

    def apply_batch(self, declarations: Iterable[dict[str, Any]]) -> dict[str, int]:
        """Apply a batch of dated declarations atomically.

        Every declaration is validated *before* any write.  A single invalid
        member rejects the whole batch, which makes bulk corrections safe.
        """

        normalized: list[dict[str, Any]] = []
        for raw in declarations:
            normalized.append(self._validate_declaration(raw))

        counts = {"page_shares": 0, "account_allocations": 0}
        with db.write_transaction(self.conn):
            for decl in normalized:
                if decl["kind"] == "page_shares":
                    self._write_page_shares(decl)
                    counts["page_shares"] += 1
                else:
                    self._write_allocations(decl)
                    counts["account_allocations"] += 1
        return counts

    def _validate_declaration(self, raw: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(raw, dict):
            raise OperationRejected(INVALID_RIGHTS, "declaration must be an object")
        kind = raw.get("kind")
        if kind not in ("page_shares", "account_allocations"):
            raise OperationRejected(
                INVALID_RIGHTS, f"unknown declaration kind {kind!r}",
                details={"kind": kind},
            )
        edition_id = raw.get("edition_id")
        if not isinstance(edition_id, str) or not edition_id:
            raise OperationRejected(INVALID_RIGHTS, "edition_id is required")
        effective = normalize_date(raw.get("effective_date"))
        pairs = parse_shares(raw.get("shares"))
        out: dict[str, Any] = {
            "kind": kind,
            "edition_id": edition_id,
            "effective_date": effective,
            "shares": pairs,
            "replace": bool(raw.get("replace", False)),
        }
        if kind == "page_shares":
            volume_id = raw.get("volume_id")
            page_id = raw.get("page_id")
            if not isinstance(volume_id, str) or not volume_id:
                raise OperationRejected(INVALID_RIGHTS, "volume_id is required")
            if not isinstance(page_id, str) or not page_id:
                raise OperationRejected(INVALID_RIGHTS, "page_id is required")
            # A rights edition keys on the full page identity and may be
            # prepared before the page appears in a catalog, or reused across
            # issues; existence in the *catalog* is therefore not required.
            out["volume_id"] = volume_id
            out["page_id"] = page_id
        else:
            account_id = raw.get("account_id")
            if not isinstance(account_id, str) or not account_id:
                raise OperationRejected(INVALID_RIGHTS, "account_id is required")
            out["account_id"] = account_id
        return out

    def _write_page_shares(self, decl: dict[str, Any]) -> None:
        self._require_edition_in_txn(decl["edition_id"])
        exists = self.conn.execute(
            "SELECT 1 FROM page_right_shares WHERE edition_id = ? AND volume_id = ? "
            "AND page_id = ? AND effective_date = ? LIMIT 1",
            (decl["edition_id"], decl["volume_id"], decl["page_id"], decl["effective_date"]),
        ).fetchone()
        if exists is not None:
            if not decl["replace"]:
                raise OperationRejected(
                    RIGHTS_VERSION_EXISTS,
                    "a page-share version already exists for that date; use a later "
                    "effective date or replace=True",
                    details={
                        "edition_id": decl["edition_id"],
                        "volume_id": decl["volume_id"],
                        "page_id": decl["page_id"],
                        "effective_date": decl["effective_date"],
                    },
                )
            self.conn.execute(
                "DELETE FROM page_right_shares WHERE edition_id = ? AND volume_id = ? "
                "AND page_id = ? AND effective_date = ?",
                (decl["edition_id"], decl["volume_id"], decl["page_id"], decl["effective_date"]),
            )
        for account_id, share in decl["shares"]:
            self.conn.execute(
                "INSERT INTO page_right_shares (edition_id, volume_id, page_id, "
                "effective_date, account_id, share_num, share_den) VALUES (?,?,?,?,?,?,?)",
                (
                    decl["edition_id"], decl["volume_id"], decl["page_id"],
                    decl["effective_date"], account_id,
                    share.numerator, share.denominator,
                ),
            )

    def _write_allocations(self, decl: dict[str, Any]) -> None:
        self._require_edition_in_txn(decl["edition_id"])
        exists = self.conn.execute(
            "SELECT 1 FROM account_allocations WHERE edition_id = ? AND account_id = ? "
            "AND effective_date = ? LIMIT 1",
            (decl["edition_id"], decl["account_id"], decl["effective_date"]),
        ).fetchone()
        if exists is not None:
            if not decl["replace"]:
                raise OperationRejected(
                    RIGHTS_VERSION_EXISTS,
                    "an allocation version already exists for that date; use a later "
                    "effective date or replace=True",
                    details={
                        "edition_id": decl["edition_id"],
                        "account_id": decl["account_id"],
                        "effective_date": decl["effective_date"],
                    },
                )
            self.conn.execute(
                "DELETE FROM account_allocations WHERE edition_id = ? AND account_id = ? "
                "AND effective_date = ?",
                (decl["edition_id"], decl["account_id"], decl["effective_date"]),
            )
        for to_account, share in decl["shares"]:
            self.conn.execute(
                "INSERT INTO account_allocations (edition_id, account_id, effective_date, "
                "to_account_id, share_num, share_den) VALUES (?,?,?,?,?,?)",
                (
                    decl["edition_id"], decl["account_id"], decl["effective_date"],
                    to_account, share.numerator, share.denominator,
                ),
            )

    def _require_edition_in_txn(self, edition_id: str) -> None:
        row = self.conn.execute(
            "SELECT 1 FROM rights_editions WHERE edition_id = ?", (edition_id,)
        ).fetchone()
        if row is None:
            raise OperationRejected(
                RIGHTS_EDITION_NOT_FOUND,
                f"rights edition {edition_id!r} does not exist",
                details={"edition_id": edition_id},
            )

    # ---------------------------- reading -----------------------------
    def page_share_versions(
        self, edition_id: str, volume_id: str, page_id: str
    ) -> list[str]:
        rows = self.conn.execute(
            "SELECT DISTINCT effective_date FROM page_right_shares "
            "WHERE edition_id = ? AND volume_id = ? AND page_id = ? "
            "ORDER BY effective_date",
            (edition_id, volume_id, page_id),
        ).fetchall()
        return [r["effective_date"] for r in rows]

    def account_allocation_versions(self, edition_id: str, account_id: str) -> list[str]:
        rows = self.conn.execute(
            "SELECT DISTINCT effective_date FROM account_allocations "
            "WHERE edition_id = ? AND account_id = ? ORDER BY effective_date",
            (edition_id, account_id),
        ).fetchall()
        return [r["effective_date"] for r in rows]

    def _page_version_rows(
        self, edition_id: str, volume_id: str, page_id: str, publication_date: str
    ) -> tuple[str, list[tuple[str, Fraction]]]:
        """Newest page version effective on/before ``publication_date``."""

        versions = self.page_share_versions(edition_id, volume_id, page_id)
        eligible = [v for v in versions if v <= publication_date]
        if not eligible:
            raise RightsResolutionError(
                RIGHTS_NEEDS_REVIEW,
                "no page-share version covers the publication date",
                details={
                    "edition_id": edition_id,
                    "volume_id": volume_id,
                    "page_id": page_id,
                    "publication_date": publication_date,
                    "available_versions": versions,
                    "reason": "declaration_not_effective",
                },
            )
        effective = max(eligible)
        rows = self.conn.execute(
            "SELECT account_id, share_num, share_den FROM page_right_shares "
            "WHERE edition_id = ? AND volume_id = ? AND page_id = ? "
            "AND effective_date = ? ORDER BY account_id",
            (edition_id, volume_id, page_id, effective),
        ).fetchall()
        pairs = [
            (r["account_id"], Fraction(int(r["share_num"]), int(r["share_den"])))
            for r in rows
        ]
        _validate_resolved_level(pairs, edition_id, "page", effective)
        return effective, pairs

    def _allocation_version(
        self, edition_id: str, account_id: str, publication_date: str
    ) -> tuple[str | None, list[tuple[str, Fraction]] | None, list[str]]:
        versions = self.account_allocation_versions(edition_id, account_id)
        if not versions:
            return None, None, versions
        eligible = [v for v in versions if v <= publication_date]
        if not eligible:
            raise RightsResolutionError(
                RIGHTS_NEEDS_REVIEW,
                "account allocations do not cover the publication date",
                details={
                    "edition_id": edition_id,
                    "account_id": account_id,
                    "publication_date": publication_date,
                    "available_versions": versions,
                    "reason": "allocation_not_effective",
                },
            )
        effective = max(eligible)
        rows = self.conn.execute(
            "SELECT to_account_id, share_num, share_den FROM account_allocations "
            "WHERE edition_id = ? AND account_id = ? AND effective_date = ? "
            "ORDER BY to_account_id",
            (edition_id, account_id, effective),
        ).fetchall()
        pairs = [
            (r["to_account_id"], Fraction(int(r["share_num"]), int(r["share_den"])))
            for r in rows
        ]
        _validate_resolved_level(pairs, edition_id, "allocation", effective)
        return effective, pairs, versions

    def page_shares_at(
        self, edition_id: str, volume_id: str, page_id: str, publication_date: Any
    ) -> list[dict[str, str]]:
        """Direct account shares for a page, resolved at a publication date."""

        date = normalize_date(publication_date)
        _, pairs = self._page_version_rows(edition_id, volume_id, page_id, date)
        return _share_pairs_json(pairs)

    def resolve_page_entitlement(
        self, edition_id: str, volume_id: str, page_id: str, publication_date: Any
    ) -> PageEntitlement:
        """Final merged payee shares plus the layer-by-layer basis."""

        date = normalize_date(publication_date)
        effective, page_accounts = self._page_version_rows(
            edition_id, volume_id, page_id, date
        )

        payee_shares: dict[str, Fraction] = {}
        paths: list[dict[str, Any]] = []
        allocations_used: dict[str, dict[str, Any]] = {}
        stack: list[str] = []

        def walk(account: str, amount: Fraction, chain: list[str]) -> None:
            if account in chain:
                raise RightsResolutionError(
                    RIGHTS_NEEDS_REVIEW,
                    "cycle in account allocations",
                    details={
                        "edition_id": edition_id,
                        "volume_id": volume_id,
                        "page_id": page_id,
                        "publication_date": date,
                        "cycle": chain[chain.index(account):] + [account],
                        "reason": "allocation_cycle",
                    },
                )
            alloc_effective, destinations, _versions = self._allocation_version(
                edition_id, account, date
            )
            if destinations is None:
                payee_shares[account] = payee_shares.get(account, Fraction(0)) + amount
                paths.append(
                    {
                        "payee_id": account,
                        "share": _frac_str(amount),
                        "channel": chain + [account],
                    }
                )
                return
            allocations_used[account] = {
                "effective_date": alloc_effective,
                "destinations": _share_pairs_json(destinations),
            }
            for dest, share in destinations:
                walk(dest, amount * share, chain + [account])

        for account, share in page_accounts:
            walk(account, share, [])

        total = sum(payee_shares.values(), Fraction(0))
        if total != 1:
            raise RightsResolutionError(
                RIGHTS_NEEDS_REVIEW,
                "resolved entitlements do not sum to 1",
                details={
                    "edition_id": edition_id,
                    "volume_id": volume_id,
                    "page_id": page_id,
                    "publication_date": date,
                    "sum": _frac_str(total),
                    "reason": "entitlement_not_normalized",
                },
            )

        basis = {
            "page_share_version": effective,
            "page_accounts": _share_pairs_json(page_accounts),
            "allocation_versions": allocations_used,
            "paths": paths,
        }
        return PageEntitlement(
            edition_id=edition_id,
            volume_id=volume_id,
            page_id=page_id,
            publication_date=date,
            shares=payee_shares,
            paths=tuple(paths),
            basis=basis,
        )

    def try_resolve_page(
        self, edition_id: str, volume_id: str, page_id: str, publication_date: Any
    ) -> tuple[PageEntitlement | None, dict[str, Any] | None]:
        """Resolve without raising; return ``(entitlement, problem)``."""

        try:
            return self.resolve_page_entitlement(
                edition_id, volume_id, page_id, publication_date
            ), None
        except RightsResolutionError as exc:
            problem = {"code": exc.code, "message": exc.message, **exc.details}
            return None, problem


def _validate_resolved_level(
    pairs: list[tuple[str, Fraction]], edition_id: str, level: str, effective: str
) -> None:
    if not pairs:
        raise RightsResolutionError(
            RIGHTS_NEEDS_REVIEW,
            f"{level} version {effective} has no shares",
            details={"edition_id": edition_id, "level": level, "effective_date": effective,
                     "reason": "empty_version"},
        )
    total = sum((s for _, s in pairs), Fraction(0))
    if total != 1:
        raise RightsResolutionError(
            RIGHTS_NEEDS_REVIEW,
            f"{level} version {effective} does not sum to 1",
            details={"edition_id": edition_id, "level": level, "effective_date": effective,
                     "sum": _frac_str(total), "reason": "level_not_normalized"},
        )
