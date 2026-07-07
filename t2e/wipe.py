"""Clean-slate reset of migrated transactions.

The user chose "wipe & re-migrate clean". To stay safe we only ever cancel +
delete the regenerable *transactional* doctypes (Journal Entry, Payment Entry,
and Sales/Purchase Invoice if any) for the target company. The standard chart
of accounts and party masters are NOT mass-deleted by default -- master loading
is idempotent by name, so leftovers never cause duplication. Pass
``with_masters=True`` to also remove migration-created masters (those carrying
the tally_guid field).

Every deletion is gated behind the client's dry-run flag (only off with --confirm).
"""
from __future__ import annotations

from .config import get_config
from .erpnext_client import ERPNextClient, ERPNextError

TXN_DOCTYPES = ["Journal Entry", "Payment Entry", "Sales Invoice", "Purchase Invoice"]
MASTER_DOCTYPES = ["Customer", "Supplier", "Item", "Cost Center", "Account"]


def _delete_all(erp: ERPNextClient, doctype: str, company: str | None,
                only_migrated: bool, field: str, progress) -> int:
    filters = []
    if company and doctype in (TXN_DOCTYPES + ["Cost Center", "Account"]):
        filters.append(["company", "=", company])
    if only_migrated:
        filters.append([field, "is", "set"])
    rows = erp.get_list(doctype, fields=["name", "docstatus"],
                        filters=filters or None, limit=0)
    n = 0
    for r in rows:
        name = r["name"]
        try:
            if r.get("docstatus") == 1:
                erp.cancel(doctype, name)
            erp.delete(doctype, name)
            n += 1
        except ERPNextError as exc:
            progress(f"  ! {doctype} {name}: {str(exc)[:120]}")
    return n


def wipe(erp: ERPNextClient, with_masters: bool = False,
         progress=print) -> dict[str, int]:
    cfg = get_config()
    company = cfg.erpnext["company"]
    field = cfg.idempotency_field
    result: dict[str, int] = {}

    # Transactions first (they reference masters). Cancel before delete.
    for dt in TXN_DOCTYPES:
        result[dt] = _delete_all(erp, dt, company, only_migrated=False,
                                 field=field, progress=progress)
        progress(f"  wiped {result[dt]} {dt}")

    if with_masters:
        # Only migration-created masters (carry tally_guid). Accounts last.
        for dt in MASTER_DOCTYPES:
            result[dt] = _delete_all(erp, dt, company, only_migrated=True,
                                     field=field, progress=progress)
            progress(f"  wiped {result[dt]} {dt} (migrated only)")
    return result
