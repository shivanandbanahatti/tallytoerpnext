"""Load Tally Sales/Purchase vouchers as ERPNext Sales/Purchase Invoices.

Each invoice voucher becomes a real AR/AP document so its outstanding can be
settled (Paid / Partially Paid) by linked payments and journals. Lines are
classified:
  * party  -> debit_to (Sales) / credit_to (Purchase)
  * tax     (CGST/SGST/IGST/CESS) and rounding -> "Actual" tax rows
  * expense/income lines -> item rows on a generic non-stock item, posting to
    the line's own account (so the GL matches Tally exactly)

The party's "New Ref" bill name is recorded in the staging bill_ref index so
payments/journals can allocate against this invoice.

Vouchers that can't be modelled as an invoice (no party, party kind mismatched
to the invoice type, unbalanced) are left pending and handled by the journal
loader instead -- nothing is dropped.
"""
from __future__ import annotations

from .config import get_config
from .erpnext_client import ERPNextClient, ERPNextError
from .lines import is_round_ledger, is_tax_ledger, parse_entries
from .mapping import CompanyDefaults, LedgerResolver
from .staging import Staging

GENERIC_ITEM = "Tally Migration Item"
GENERIC_HSN = "998399"

# Tally voucher -> (ERPNext doctype, party kind, is_return)
INVOICE_SPECS = {
    "Sales":       ("Sales Invoice", "Customer", False),
    "Credit Note": ("Sales Invoice", "Customer", True),
    "Purchase":    ("Purchase Invoice", "Supplier", False),
    "Debit Note":  ("Purchase Invoice", "Supplier", True),
}


def ensure_generic_item(erp: ERPNextClient) -> None:
    if erp.exists("Item", GENERIC_ITEM):
        return
    erp.insert("Item", {
        "item_code": GENERIC_ITEM, "item_name": GENERIC_ITEM,
        "item_group": "All Item Groups", "stock_uom": "Nos",
        "is_stock_item": 0, "is_purchase_item": 1, "is_sales_item": 1,
        "gst_hsn_code": GENERIC_HSN,
    })


class InvoiceLoader:
    def __init__(self, erp: ERPNextClient, store: Staging,
                 defaults: CompanyDefaults, resolver: LedgerResolver):
        self.erp = erp
        self.store = store
        self.d = defaults
        self.r = resolver
        self.field = get_config().idempotency_field
        self.fallback: list[str] = []   # guids that must go to the journal loader

    # ---- build ----------------------------------------------------------
    def _build(self, vrow):
        spec = INVOICE_SPECS[vrow["vtype"]]
        doctype, kind, is_return = spec
        import json
        payload = json.loads(vrow["payload"])
        entries = parse_entries(payload)

        party_line = party_res = None
        items, taxes = [], []
        for e in entries:
            res = self.r.get(e["ledger"])
            if res and res.kind == "party":
                if party_line is not None:
                    return None  # multiple parties -> not a simple invoice
                party_line, party_res = e, res
            elif is_tax_ledger(e["ledger"]) or is_round_ledger(e["ledger"]):
                taxes.append(e)
            else:
                items.append(e)

        if party_res is None or party_res.party_type != kind or not items:
            return None  # can't model as this invoice type
        # sign of return: ERPNext expects negative qty/amount
        sign = -1 if is_return else 1

        item_rows = [{
            "item_code": GENERIC_ITEM, "qty": sign, "rate": round(e["mag"], 2),
            ("income_account" if kind == "Customer" else "expense_account"):
                (self.r.get(e["ledger"]).account if self.r.get(e["ledger"]) else self.d.suspense),
            "cost_center": self.d.cost_center,
        } for e in items]

        tax_rows = [{
            "charge_type": "Actual",
            # Post every tax / rounding line to its own Tally ledger account so
            # the GL matches Tally exactly. Rounding ledgers ("Rounding Off",
            # "Rounded Off") are real Tally ledgers -> resolve them like any
            # other; only truly unresolved lines fall back to the round-off acct.
            "account_head": (self.r.get(e["ledger"]).account if self.r.get(e["ledger"])
                             else self.d.round_off),
            "description": e["ledger"][:140],
            # A tax/rounding line increases the bill when it sits OPPOSITE the
            # party line (i.e. on the items' side): on a Sale the party is a Tally
            # DEBIT and output GST a CREDIT; on a Purchase the party is a CREDIT
            # and input GST a DEBIT -- both additive. Judging against the party's
            # actual Dr/Cr direction (not the doctype) keeps returns correct too:
            # a Credit Note's party is a credit, so its output-GST debit reverses
            # (negative tax_amount). The `sign` then applies ERPNext's return flip.
            "tax_amount": round(
                sign * (e["mag"] if e["debit"] != party_line["debit"]
                        else -e["mag"]), 2),
            "cost_center": self.d.cost_center,
        } for e in taxes]

        # party bill reference (New Ref) for later payment allocation
        billname = next((b["name"] for b in party_line["bills"]
                         if b["type"] in ("New Ref", "Agst Ref")), None) \
            or vrow["vnumber"] or vrow["guid"][:20]

        doc = {
            "doctype": doctype,
            "company": self.d.name,
            "posting_date": vrow["vdate"],
            "set_posting_time": 1,
            "update_stock": 0,
            # Tally keeps exact paise on invoice totals; ERPNext otherwise rounds
            # the grand total to a whole rupee and books the difference to a
            # round-off account (an entry Tally never made). Disable it so the
            # migrated GL ties to Tally to the paise.
            "disable_rounded_total": 1,
            "is_return": 1 if is_return else 0,
            "items": item_rows,
            "taxes": tax_rows,
            self.field: vrow["guid"],
        }
        if kind == "Customer":
            doc["customer"] = party_res.party
            doc["debit_to"] = self.d.receivable
            doc["naming_series"] = "SRET-.YY.-" if is_return else "SINV-.YY.-"
            # This site sets Sales Invoice autoname to "Prompt", so a name must
            # be supplied -- use the real Tally invoice number. India Compliance
            # only allows alphanumerics, dash and slash, so sanitize.
            nm = _gst_safe_name(vrow["vnumber"]) or f"TLY-SINV-{vrow['guid'][:10]}"
            doc["name"] = nm[:140]
        else:
            doc["supplier"] = party_res.party
            doc["credit_to"] = self.d.payable
            doc["bill_no"] = billname
            doc["bill_date"] = vrow["vdate"]
            doc["naming_series"] = "PRET-.YY.-" if is_return else "PINV-.YY.-"
        return doc, party_res.party, doctype, billname

    # ---- run ------------------------------------------------------------
    def run(self, vtype=None, limit=0, progress=lambda *a: None) -> dict[str, int]:
        types = [vtype] if vtype else list(INVOICE_SPECS)
        rows = []
        for vt in types:
            rows.extend(self.store.vouchers(vtype=vt, status="pending"))
        rows.sort(key=lambda r: (r["vdate"] or "", r["vnumber"] or ""))
        if limit:
            rows = rows[:limit]
        stats = {"loaded": 0, "fallback": 0, "error": 0}
        for i, vrow in enumerate(rows, 1):
            try:
                built = self._build(vrow)
                if built is None:
                    self.fallback.append(vrow["guid"])
                    stats["fallback"] += 1
                    continue
                doc, party, doctype, billname = built
                if self.erp.dry_run:
                    stats["loaded"] += 1
                    continue
                res = self.erp.insert_and_submit(doctype, doc)
                name = _name_of(res)
                self.store.mark("voucher", vrow["guid"], "loaded", doctype, name)
                self.store.add_bill_ref(party, billname, doctype, name)
                stats["loaded"] += 1
            except ERPNextError as exc:
                self.store.mark("voucher", vrow["guid"], "error", error=str(exc)[:800])
                stats["error"] += 1
            if i % 50 == 0:
                self.store.conn.commit()
                progress(i, len(rows), stats)
        self.store.conn.commit()
        progress(len(rows), len(rows), stats)
        return stats


def _gst_safe_name(raw: str | None) -> str:
    """GST transaction names allow only alphanumerics, '-' and '/', starting
    with an alphanumeric."""
    import re
    s = re.sub(r"[^A-Za-z0-9/-]+", "-", (raw or "").strip())
    s = re.sub(r"-{2,}", "-", s).strip("-/")
    return s


def _name_of(res):
    if isinstance(res, dict):
        msg = res.get("message")
        if isinstance(msg, dict):
            return msg.get("name")
        if isinstance(res.get("data"), dict):
            return res["data"].get("name")
    return None
