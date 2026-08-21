"""Load Tally Sales/Purchase vouchers as ERPNext Sales/Purchase Invoices.

Each invoice voucher becomes a real AR/AP document so its outstanding can be
settled (Paid / Partially Paid) by linked payments and journals. Lines are
classified:
  * party  -> debit_to (Sales) / credit_to (Purchase)
  * tax     (CGST/SGST/IGST/CESS) and rounding -> "Actual" tax rows
  * expense/income lines -> item rows on a generic stock item; Purchase
    Invoices use update_stock=1 into the company default warehouse

The party's "New Ref" bill name is recorded in the staging bill_ref index so
payments/journals can allocate against this invoice.

Vouchers that can't be modelled as an invoice (no party, party kind mismatched
to the invoice type, unbalanced) are left pending and handled by the journal
loader instead -- nothing is dropped.
"""
from __future__ import annotations

import re
from decimal import Decimal, ROUND_HALF_UP

from .config import get_config
from .erpnext_client import ERPNextClient, ERPNextError
from .gst_setup import (
    ensure_item_tax_template,
    item_tax_template_name,
    select_gst,
)
from .lines import is_round_ledger, is_tax_ledger, parse_entries
from .load_masters import _GST_STATE
from .mapping import CompanyDefaults, LedgerResolver
from .purchase_ocr import OCRMatch, PurchaseOCRCatalog
from .staging import Staging

GENERIC_ITEM = "Tally Migration Item"
GENERIC_HSN = "998399"
PERCENT_RATE_RE = re.compile(r"(?<![\d.])(\d+(?:\.\d+)?|\.\d+)\s*%")
MEANINGFUL_CHARGE_RE = re.compile(
    r"\b(TRANSPORT(?:ATION)?|FREIGHT|CARTAGE|FORWARDING|DELIVERY|"
    r"COURIER|PACKING|LOADING|UNLOADING|HANDLING)\b",
    re.IGNORECASE,
)
# Curated broad SAC classifications from the GST service-code hierarchy.
# These avoid guessing a transport mode or more specific service than the Tally
# ledger states. An explicit GSTHSNSACCODE from Tally always takes precedence.
CURATED_CHARGE_SAC = (
    (re.compile(
        r"\b(TRANSPORT(?:ATION)?|FREIGHT|CARTAGE|DELIVERY|COURIER)\b",
        re.IGNORECASE,
    ), "996511"),
    (re.compile(r"\bPACKING\b", re.IGNORECASE), "998540"),
    (re.compile(
        r"\b(LOADING|UNLOADING|HANDLING|FORWARDING)\b", re.IGNORECASE
    ), "996719"),
)
INVOICE_VALIDATION_FALLBACKS = (
    "GST amounts do not match the calculated values",
    "Company GSTIN and Party GSTIN are same",
    "Supplier without GSTIN",
    "Cannot charge IGST for intra-state supplies",
    "Charge Type is set to Actual",
    "Debit and Credit not equal for",
    # Purchase lines that resolve to a group (e.g. Stock In Hand) cannot post as
    # PI expense accounts; preserve exact GL via Journal Entry instead.
    "You selected the account group",
)


def _tax_rate_from_ledger(ledger: str | None) -> float:
    """Return the explicit percentage in a Tally tax ledger name.

    Actual charge rows keep Tally's posted amount authoritative; ``rate`` is
    populated separately so ERPNext displays the source rate.  A missing or
    invalid percentage deliberately stays zero rather than deriving a possibly
    wrong rate from an invoice containing mixed tax bases.
    """
    match = PERCENT_RATE_RE.search(ledger or "")
    if not match:
        return 0.0
    rate = Decimal(match.group(1))
    return float(rate) if Decimal("0") <= rate <= Decimal("100") else 0.0


def _is_gst_ledger(ledger: str | None) -> bool:
    name = (ledger or "").upper()
    return any(token in name for token in ("CGST", "SGST", "IGST", "UTGST"))


def _is_meaningful_charge_ledger(ledger: str | None) -> bool:
    """True for ancillary allocations that must remain visible invoice lines."""
    return bool(MEANINGFUL_CHARGE_RE.search(ledger or ""))


def _semantic_item_code(ledger: str) -> str:
    return re.sub(r"\s+", " ", ledger).strip()[:140]


def _semantic_charge_hsn(entry: dict) -> str:
    source = str(entry.get("gst_hsn_code") or "").strip()
    if source:
        return source
    return next(
        (code for pattern, code in CURATED_CHARGE_SAC
         if pattern.search(entry.get("ledger") or "")),
        "",
    )


def _gst_item_code(kind: str, taxes: list[dict]) -> str:
    """Return the rate-specific fallback item requested for GST invoices."""
    rates = [
        Decimal(str(_tax_rate_from_ledger(entry["ledger"])))
        for entry in taxes if _is_gst_ledger(entry.get("ledger"))
    ]
    total = sum((rate for rate in rates if rate > 0), Decimal("0"))
    if total <= 0:
        return GENERIC_ITEM
    rate_text = format(total.normalize(), "f")
    side = "Sales" if kind == "Customer" else "Purchase"
    return f"GST {side} at {rate_text}%"


def _gst_rate(taxes: list[dict]) -> float:
    """Return the combined GST rate represented by the voucher tax ledgers."""
    return float(sum(
        (Decimal(str(_tax_rate_from_ledger(entry["ledger"])))
         for entry in taxes if _is_gst_ledger(entry.get("ledger"))),
        Decimal("0"),
    ))


def _standard_gst_account(kind: str, ledger: str, abbr: str) -> str | None:
    """Map Tally GST ledgers to accounts used by ERPNext GST templates."""
    from .lines import standard_gst_account
    return standard_gst_account(ledger, abbr, kind=kind)


def _rate_from_item_template(name: str) -> float:
    match = re.search(r"GST\s+(\d+(?:\.\d+)?)%", name, re.IGNORECASE)
    if not match:
        raise ValueError(f"invalid GST Item Tax Template name: {name}")
    return float(match.group(1))


def _party_gst_address(party: str) -> tuple[str, str]:
    title = f"{party[:110]} - Tally GST"
    return title, f"{title}-Billing"


def _payload_text(value) -> str:
    """Return Tally XML scalar text without leaking parser containers."""
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, dict):
        return str(value.get("#text") or "").strip()
    if isinstance(value, list):
        return _payload_text(value[0]) if value else ""
    return str(value).strip()


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
                 defaults: CompanyDefaults, resolver: LedgerResolver,
                 ocr_catalog: PurchaseOCRCatalog | None = None):
        self.erp = erp
        self.store = store
        self.d = defaults
        self.r = resolver
        self.field = get_config().idempotency_field
        self.fallback: list[str] = []   # guids that must go to the journal loader
        self.ocr = ocr_catalog if ocr_catalog is not None \
            else PurchaseOCRCatalog.from_path()

    # ---- build ----------------------------------------------------------
    def _build(self, vrow):
        spec = INVOICE_SPECS[vrow["vtype"]]
        doctype, kind, is_return = spec
        import json
        payload = json.loads(vrow["payload"])
        entries = parse_entries(payload)

        party_line = party_res = None
        items, taxes, roundings = [], [], []
        for e in entries:
            res = self.r.get(e["ledger"])
            if res and res.kind == "party":
                if party_line is not None:
                    return None  # multiple parties -> not a simple invoice
                party_line, party_res = e, res
            elif is_round_ledger(e["ledger"]):
                roundings.append(e)
            elif is_tax_ledger(e["ledger"]):
                taxes.append(e)
            else:
                items.append(e)

        if party_res is None or party_res.party_type != kind or not items:
            return None  # can't model as this invoice type
        # Non-tax ledgers on the party side are discounts/adjustments, not goods.
        # Model them as negative Actual charges so item quantities stay valid.
        adjustments = [
            entry for entry in items
            if entry["debit"] == party_line["debit"]
        ]
        items = [
            entry for entry in items
            if entry["debit"] != party_line["debit"]
        ]
        if not items:
            return None
        # sign of return: ERPNext expects negative qty/amount
        sign = -1 if is_return else 1
        fallback_item = _gst_item_code(kind, taxes)
        gst_rate = _gst_rate(taxes)
        item_tax_template = (
            item_tax_template_name(gst_rate, self.d.abbr) if gst_rate > 0 else None
        )

        # Party bill reference (New Ref) is the supplier's invoice number on a
        # Purchase. Tally's own Purchase voucher sequence repeats every year.
        billname = next((b["name"] for b in party_line["bills"]
                         if b["type"] in ("New Ref", "Agst Ref")), None) \
            or vrow["vnumber"] or vrow["guid"][:20]

        semantic_entries = [
            entry for entry in items
            if _is_meaningful_charge_ledger(entry["ledger"])
        ]
        semantic_codes = {
            _semantic_item_code(entry["ledger"]) for entry in semantic_entries
        }
        base_items = [
            entry for entry in items
            if not _is_meaningful_charge_ledger(entry["ledger"])
        ]

        def ledger_item_row(entry: dict) -> dict:
            semantic = _is_meaningful_charge_ledger(entry["ledger"])
            resolved = self.r.get(entry["ledger"])
            row = {
                "item_code": (
                    _semantic_item_code(entry["ledger"])
                    if semantic else fallback_item
                ),
                "qty": sign,
                "rate": round(entry["mag"], 2),
                ("income_account" if kind == "Customer" else "expense_account"):
                    (resolved.account if resolved else self.d.suspense),
                "cost_center": self.d.cost_center,
            }
            source_hsn = (
                _semantic_charge_hsn(entry)
                if semantic else entry.get("gst_hsn_code")
            )
            if source_hsn:
                row["gst_hsn_code"] = source_hsn
            elif not semantic:
                row["gst_hsn_code"] = GENERIC_HSN
            return row

        item_rows = [ledger_item_row(entry) for entry in items]
        semantic_definitions = [{
            "item_code": _semantic_item_code(entry["ledger"]),
            "item_name": _semantic_item_code(entry["ledger"]),
            "gst_hsn_code": _semantic_charge_hsn(entry),
            "kind": kind,
        } for entry in semantic_entries]
        ocr_match: OCRMatch | None = None
        ocr_definitions: list[dict] = []
        # Preserve the Tally GL account: OCR expansion is safe when the voucher
        # has one base purchase ledger and extracted lines tie to that amount.
        # Legitimate ancillary lines (freight/transport/etc.) remain separate
        # taxable service items and are never replaced or duplicated by OCR.
        if kind == "Supplier" and not is_return and len(base_items) == 1:
            ocr_match = self.ocr.match(
                billname, Decimal(str(base_items[0]["mag"])),
                supplier=party_res.party, posting_date=vrow["vdate"],
            )
            if ocr_match:
                expense = self.r.get(base_items[0]["ledger"])
                ocr_rows, ocr_definitions = self._ocr_item_rows(
                    ocr_match, expense.account if expense else self.d.suspense
                )
                if ocr_rows:
                    item_rows = ocr_rows + [
                        ledger_item_row(entry) for entry in semantic_entries
                    ]
                else:
                    ocr_match = None
                    ocr_definitions = []

        if item_tax_template:
            for row in item_rows:
                row["item_tax_template"] = item_tax_template
                if row["item_code"] not in semantic_codes:
                    row["gst_hsn_code"] = row.get("gst_hsn_code") or GENERIC_HSN

        tax_rows = [{
            # Standard GST rows must be percentage-based so ERPNext displays
            # the real CGST/SGST/IGST rate rather than "0%". Non-GST statutory
            # rows remain Actual because they may use a different tax base.
            "charge_type": (
                "On Net Total"
                if _is_gst_ledger(e["ledger"]) and _tax_rate_from_ledger(e["ledger"]) > 0
                else "Actual"
            ),
            "rate": _tax_rate_from_ledger(e["ledger"]),
            # Post every tax / rounding line to its own Tally ledger account so
            # the GL matches Tally exactly. Rounding ledgers ("Rounding Off",
            # "Rounded Off") are real Tally ledgers -> resolve them like any
            # other; only truly unresolved lines fall back to the round-off acct.
            "account_head": (
                _standard_gst_account(kind, e["ledger"], self.d.abbr)
                or (self.r.get(e["ledger"]).account if self.r.get(e["ledger"])
                    else self.d.round_off)
            ),
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
        tax_rows.extend({
            "charge_type": "Actual",
            "account_head": (
                self.r.get(e["ledger"]).account if self.r.get(e["ledger"])
                else self.d.suspense
            ),
            "description": e["ledger"][:140],
            "tax_amount": round(-sign * e["mag"], 2),
            "cost_center": self.d.cost_center,
        } for e in adjustments)
        calculated = sum(
            (Decimal(str(row["qty"])) * Decimal(str(row["rate"]))
             for row in item_rows), Decimal("0")
        ) + sum(
            (Decimal(str(row["tax_amount"])) for row in tax_rows), Decimal("0")
        )
        tally_rounded_total = Decimal(sign) * Decimal(str(party_line["mag"]))
        rounding_adjustment = tally_rounded_total - calculated

        doc = {
            "doctype": doctype,
            "company": self.d.name,
            "posting_date": vrow["vdate"],
            "set_posting_time": 1,
            # Only real OCR goods are received into stock. Generic ledger lines
            # remain accounting-only and must not create artificial inventory.
            "update_stock": 1 if ocr_match else 0,
            # ERPNext owns the rounding adjustment. The explicit Tally ROUNDING
            # OFF ledger is intentionally not copied into the taxes child table.
            "disable_rounded_total": 0,
            "is_return": 1 if is_return else 0,
            "items": item_rows,
            "taxes": tax_rows,
            # The REST client compares ERPNext's calculated draft against this
            # source total before submit. Material tax-recomputation drift is
            # posted through native round-off fields, never as a tax row.
            # Keep the sign: Credit/Debit Notes have a negative total, and
            # abs() here used to force a positive rounded_total on submit,
            # which inverted the Debtors/Creditors GL (and parked 2x on
            # Rounded Off).
            "_tally_total_target": float(tally_rounded_total),
            self.field: vrow["guid"],
        }
        narration = _payload_text(payload.get("NARRATION"))
        if narration:
            doc["remarks"] = narration[:1000]
        party_gstin = str(payload.get("PARTYGSTIN") or "").strip().upper()
        if re.fullmatch(r"\d{2}[A-Z0-9]{10}[A-Z0-9][A-Z][A-Z0-9]", party_gstin):
            # Voucher XML often contains the GSTIN even when Tally's ledger
            # master export omits it. Use that authoritative value both on the
            # transaction and to repair the ERPNext party Tax tab.
            doc["gst_category"] = "Registered Regular"
            gstin_field = "supplier_gstin" if kind == "Supplier" else "customer_gstin"
            address_field = "supplier_address" if kind == "Supplier" else "customer_address"
            doc[gstin_field] = party_gstin
            address_title, address_name = _party_gst_address(party_res.party)
            doc[address_field] = address_name
            doc["_party_tax_update"] = {
                "doctype": kind,
                "name": party_res.party,
                "values": {
                    "gstin": party_gstin,
                    "pan": party_gstin[2:12],
                    "gst_category": "Registered Regular",
                },
                "address_title": address_title,
                "address_name": address_name,
                "state_code": party_gstin[:2],
            }
        if roundings and abs(rounding_adjustment) > Decimal("0.001"):
            # Do not represent Tally ROUNDING OFF as a tax.  The REST client
            # supplies these native ERPNext fields during the submit request so
            # ERPNext posts the difference to the company's round-off account.
            doc["_tally_rounding_override"] = {
                "rounded_total": float(tally_rounded_total),
                "rounding_adjustment": float(rounding_adjustment),
            }
        gst = select_gst(kind, [e["ledger"] for e in taxes], self.d.abbr)
        if gst:
            doc["tax_category"] = gst.tax_category
            doc["taxes_and_charges"] = gst.template
            tally_pos = str(payload.get("PLACEOFSUPPLY") or "").strip()
            pos = next(
                (
                    f"{code}-{state}"
                    for code, state in _GST_STATE.items()
                    if state.casefold() == tally_pos.casefold()
                ),
                None,
            )
            if pos:
                # On purchases India Compliance compares place of supply with
                # the supplier GSTIN's state. Preserve Tally's destination
                # state; using the supplier state here would misclassify IGST.
                doc["place_of_supply"] = pos
        if ocr_match:
            doc["_ocr_item_definitions"] = ocr_definitions
            provenance = (
                f"Purchase items extracted from {ocr_match.source_file} "
                f"page {ocr_match.source_page}."
            )
            doc["remarks"] = (
                f"{doc['remarks']}\n\n{provenance}"
                if doc.get("remarks") else provenance
            )[:1000]
        if semantic_definitions:
            doc["_semantic_item_definitions"] = semantic_definitions
        doc["_migration_meta"] = {
            "tally_party_total": float(Decimal(str(party_line["mag"]))),
            "erp_rounded_total": float(abs(tally_rounded_total)),
            "tally_rounding_rows": len(roundings),
            "rounding_override": float(rounding_adjustment),
            "ocr_matched": bool(ocr_match),
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

    def _ocr_item_rows(self, match: OCRMatch, expense_account: str
                       ) -> tuple[list[dict], list[dict]]:
        rows, definitions = [], []
        aliases = {
            "ROLLS": "Roll", "MTRS": "Meter", "MTR": "Meter",
            "METERS": "Meter", "NOS.": "Nos", "NOS": "Nos",
        }
        for line in match.lines:
            name = re.sub(r"\s+", " ", str(line.get("item_name") or "")).strip()
            qty = Decimal(str(line.get("qty") or 0))
            amount = Decimal(str(line.get("amount") or 0)).quantize(Decimal("0.01"))
            if not name or qty <= 0 or amount <= 0:
                continue
            # Amount is the accounting truth. ERPNext stores invoice currency
            # rates at two decimals, so a source amount such as 32736.36 / 53
            # cannot be reproduced by keeping both qty=53 and a rounded rate.
            # For those lines use one accounting unit and retain the physical
            # OCR quantity/rate in the description. This is preferable to a
            # generic adjustment item or a tax-table adjustment and preserves
            # the exact invoice/tax base.
            source_qty = qty
            source_rate = Decimal(str(line.get("rate") or 0))
            code = name[:140]
            raw_uom = str(line.get("uom") or "Nos").strip()
            uom = aliases.get(raw_uom.upper(), raw_uom) or "Nos"
            if uom in {"Nos", "Roll", "Box", "Packet", "Set"} \
                    and qty != qty.to_integral_value():
                qty = max(Decimal("1"), qty.quantize(
                    Decimal("1"), rounding=ROUND_HALF_UP
                ))
            rate = (amount / qty).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
            collapsed = (qty * rate).quantize(
                Decimal("0.01"), rounding=ROUND_HALF_UP
            ) != amount
            if collapsed:
                qty = Decimal("1")
                rate = amount
            hsn = re.sub(r"\D", "", str(line.get("gst_hsn_code") or ""))[:8]
            row = {
                "item_code": code,
                "item_name": name[:140],
                "description": (
                    (
                        f"{str(line.get('description') or name)} "
                        f"[OCR quantity: {source_qty} {uom}; "
                        f"unit rate: {source_rate}]"
                    )
                    if collapsed else str(line.get("description") or name)
                ),
                "qty": float(qty),
                "rate": float(rate),
                "uom": uom,
                "expense_account": expense_account,
                "cost_center": self.d.cost_center,
            }
            if self.d.default_warehouse:
                row["warehouse"] = self.d.default_warehouse
            if hsn:
                row["gst_hsn_code"] = hsn
            rows.append(row)
            definitions.append({
                "item_code": code, "item_name": name[:140], "uom": uom,
                "description": row["description"], "gst_hsn_code": hsn,
            })
        return rows, definitions

    def _ensure_ocr_items(self, doc: dict) -> None:
        definitions = doc.pop("_ocr_item_definitions", [])
        item_rows = {row["item_code"]: row for row in doc.get("items") or []}
        for definition in definitions:
            code = definition["item_code"]
            row = item_rows[code]
            uom = definition["uom"]
            if not self.erp.exists("UOM", uom):
                self.erp.insert("UOM", {"uom_name": uom})

            hsn = definition.get("gst_hsn_code") or ""
            if hsn and not self.erp.exists("GST HSN Code", hsn):
                definition["gst_hsn_code"] = GENERIC_HSN
                row["gst_hsn_code"] = GENERIC_HSN
                hsn = GENERIC_HSN

            if self.erp.exists("Item", code):
                if not self.erp.dry_run:
                    existing = self.erp._request(
                        "GET", f"/api/resource/Item/{_quote(code)}"
                    )["data"]
                    existing_uom = existing.get("stock_uom") or uom
                    row["uom"] = existing_uom
                    values = {
                        "is_stock_item": 1, "is_purchase_item": 1,
                        "gst_hsn_code": hsn or GENERIC_HSN,
                    }
                    if row.get("item_tax_template"):
                        values["taxes"] = [{
                            "item_tax_template": row["item_tax_template"],
                        }]
                    self.erp.update("Item", code, values)
                continue
            item_doc = {
                "item_code": code,
                "item_name": definition["item_name"],
                "description": definition["description"],
                "item_group": "All Item Groups",
                "stock_uom": uom,
                "is_stock_item": 1,
                "is_purchase_item": 1,
                "is_sales_item": 0,
                "gst_hsn_code": hsn or GENERIC_HSN,
            }
            if row.get("item_tax_template"):
                item_doc["taxes"] = [{
                    "item_tax_template": row["item_tax_template"],
                }]
            self.erp.insert("Item", item_doc)

    def _ensure_semantic_items(self, doc: dict) -> None:
        """Create non-stock service items for meaningful ancillary ledgers."""
        definitions = doc.pop("_semantic_item_definitions", [])
        item_rows = {row["item_code"]: row for row in doc.get("items") or []}
        for definition in definitions:
            code = definition["item_code"]
            row = item_rows[code]
            hsn = definition.get("gst_hsn_code") or ""
            if hsn and not self.erp.exists("GST HSN Code", hsn):
                # A source code unknown to ERPNext is not replaced with a
                # guessed service code. Keep it blank for explicit review.
                hsn = ""
                row.pop("gst_hsn_code", None)
            values = {
                "is_purchase_item": 1,
                "is_sales_item": 1,
            }
            if hsn:
                values["gst_hsn_code"] = hsn
            template = row.get("item_tax_template")
            if template:
                values["taxes"] = [{"item_tax_template": template}]
            if self.erp.exists("Item", code):
                if not self.erp.dry_run:
                    self.erp.update("Item", code, values)
                continue
            item_doc = {
                "item_code": code,
                "item_name": definition["item_name"],
                "description": (
                    f"Migrated Tally ancillary ledger: {definition['item_name']}"
                ),
                "item_group": "All Item Groups",
                "stock_uom": "Nos",
                "is_stock_item": 0,
                **values,
            }
            self.erp.insert("Item", item_doc)

    def _ensure_fallback_items(self, doc: dict) -> None:
        """Create rate-specific accounting items and keep HSN on every row."""
        for row in doc.get("items") or []:
            code = row.get("item_code")
            if not code or (code != GENERIC_ITEM and not code.startswith("GST ")):
                continue
            row["gst_hsn_code"] = row.get("gst_hsn_code") or GENERIC_HSN
            template = row.get("item_tax_template")
            if template:
                rate = _rate_from_item_template(template)
                ensure_item_tax_template(self.erp, self.d, rate)
            item_taxes = (
                [{"item_tax_template": template}] if template else []
            )
            if self.erp.exists("Item", code):
                if not self.erp.dry_run:
                    self.erp.update("Item", code, {
                        "gst_hsn_code": GENERIC_HSN,
                        "is_purchase_item": 1,
                        "is_sales_item": 1,
                        "taxes": item_taxes,
                    })
                continue
            self.erp.insert("Item", {
                "item_code": code,
                "item_name": code,
                "item_group": "All Item Groups",
                "stock_uom": "Nos",
                "is_stock_item": 0,
                "is_purchase_item": 1,
                "is_sales_item": 1,
                "gst_hsn_code": GENERIC_HSN,
                "taxes": item_taxes,
            })

    def preflight(self) -> dict:
        """Build every staged invoice without writes and report unsafe rows."""
        rows = []
        for voucher_type in INVOICE_SPECS:
            rows.extend(self.store.vouchers(vtype=voucher_type))
        stats = {
            "total": len(rows), "buildable": 0, "fallback": 0,
            "rounding_mismatch": 0, "ocr_items": 0,
            "sales_name_duplicates": 0, "nonstandard_rounding": 0,
        }
        issues: list[dict] = []
        sales_names: dict[str, list[str]] = {}
        for row in rows:
            built = self._build(row)
            if built is None:
                stats["fallback"] += 1
                continue
            stats["buildable"] += 1
            doc = built[0]
            meta = doc["_migration_meta"]
            if meta["ocr_matched"]:
                stats["ocr_items"] += 1
            delta = abs(meta["tally_party_total"] - meta["erp_rounded_total"])
            # With rounded totals enabled, a sub-rupee difference is expected.
            # More than 50 paise means the invoice structure itself does not tie.
            if delta > 0.50:
                stats["rounding_mismatch"] += 1
                if len(issues) < 50:
                    issues.append({
                        "guid": row["guid"], "type": row["vtype"],
                        "number": row["vnumber"], "issue": "rounded_total_mismatch",
                        "tally_total": meta["tally_party_total"],
                        "erp_rounded_total": meta["erp_rounded_total"],
                    })
            if built[2] == "Sales Invoice":
                sales_names.setdefault(doc["name"], []).append(row["guid"])
        duplicates = {name: guids for name, guids in sales_names.items()
                      if len(guids) > 1}
        stats["sales_name_duplicates"] = len(duplicates)
        for name, guids in list(duplicates.items())[:20]:
            issues.append({
                "issue": "duplicate_sales_invoice_name",
                "name": name, "guids": guids,
            })
        return {"stats": stats, "issues": issues}

    # ---- run ------------------------------------------------------------
    def run(self, vtype=None, limit=0, latest=False,
            progress=lambda *a: None) -> dict[str, int]:
        types = [vtype] if vtype else list(INVOICE_SPECS)
        rows = []
        for vt in types:
            rows.extend(self.store.vouchers(vtype=vt, status="pending"))
        rows.sort(key=lambda r: (r["vdate"] or "", r["vnumber"] or ""))
        if limit:
            rows = rows[-limit:] if latest else rows[:limit]
        stats = {"loaded": 0, "fallback": 0, "error": 0, "ocr_items": 0}
        for i, vrow in enumerate(rows, 1):
            try:
                built = self._build(vrow)
                if built is None:
                    self.fallback.append(vrow["guid"])
                    stats["fallback"] += 1
                    continue
                doc, party, doctype, billname = built
                party_tax_update = doc.pop("_party_tax_update", None)
                if self.erp.dry_run:
                    if doc.get("_ocr_item_definitions"):
                        stats["ocr_items"] += 1
                    stats["loaded"] += 1
                    continue
                if party_tax_update:
                    self.erp.update(
                        party_tax_update["doctype"],
                        party_tax_update["name"],
                        party_tax_update["values"],
                    )
                    self._ensure_party_gst_address(party_tax_update)
                existing = self.erp.find_by_field(
                    doctype, self.field, vrow["guid"]
                )
                if existing:
                    current = self.erp._request(
                        "GET", f"/api/resource/{_quote(doctype)}/{_quote(existing)}"
                    )["data"]
                    if int(current.get("docstatus") or 0) == 2:
                        raise ERPNextError(
                            f"{doctype} {existing} for {self.field}="
                            f"{vrow['guid']} is cancelled"
                        )
                    if int(current.get("docstatus") or 0) == 0:
                        requested = str(doc.get("name") or "").strip()
                        if requested and requested != existing:
                            if self.erp.exists(doctype, requested):
                                raise ERPNextError(
                                    f"cannot restore Tally invoice name {requested}: "
                                    "name already exists"
                                )
                            self.erp.rename(doctype, existing, requested)
                            existing = requested
                        self.erp.submit_existing(doctype, existing)
                    elif doctype == "Sales Invoice" and int(
                        current.get("is_consolidated") or 0
                    ):
                        # Recover cleanly if a prior run submitted the invoice
                        # but was interrupted before clearing the temporary
                        # rounding calculation guard.
                        self.erp._restore_sales_consolidation_flag(existing)
                    self.store.mark(
                        "voucher", vrow["guid"], "loaded", doctype, existing
                    )
                    self.store.add_bill_ref(
                        party, billname, doctype, existing
                    )
                    stats["loaded"] += 1
                    continue
                if doc.get("_ocr_item_definitions"):
                    self._ensure_ocr_items(doc)
                    stats["ocr_items"] += 1
                if doc.get("_semantic_item_definitions"):
                    self._ensure_semantic_items(doc)
                self._ensure_fallback_items(doc)
                doc.pop("_migration_meta", None)
                res = self.erp.insert_and_submit(doctype, doc)
                name = _name_of(res)
                self.store.mark("voucher", vrow["guid"], "loaded", doctype, name)
                self.store.add_bill_ref(party, billname, doctype, name)
                stats["loaded"] += 1
            except ERPNextError as exc:
                detail = exc.body or str(exc)
                if any(message in detail for message in INVOICE_VALIDATION_FALLBACKS):
                    # The source cannot satisfy ERPNext/India Compliance invoice
                    # validation without changing GST identity, rates or posted
                    # values. Preserve it through VoucherLoader's exact GL Journal
                    # Entry path instead of silently dropping or fabricating data.
                    existing = self.erp.find_by_field(
                        doctype, self.field, vrow["guid"]
                    )
                    if existing:
                        current = self.erp._request(
                            "GET",
                            f"/api/resource/{_quote(doctype)}/{_quote(existing)}",
                        )["data"]
                        if int(current.get("docstatus") or 0) == 0:
                            self.erp.delete(doctype, existing)
                    self.store.mark("voucher", vrow["guid"], "pending", error=None)
                    self.fallback.append(vrow["guid"])
                    stats["fallback"] += 1
                else:
                    self.store.mark(
                        "voucher", vrow["guid"], "error", error=detail[:800]
                    )
                    stats["error"] += 1
            if i % 50 == 0:
                self.store.conn.commit()
                progress(i, len(rows), stats)
        self.store.conn.commit()
        progress(len(rows), len(rows), stats)
        return stats

    def _ensure_party_gst_address(self, update: dict) -> None:
        state = _GST_STATE.get(update["state_code"]) or ""
        values = {
            "address_title": update["address_title"],
            "address_type": "Billing",
            "address_line1": state or "India",
            "city": state or "India",
            "state": state,
            "country": "India",
            "gstin": update["values"]["gstin"],
            "gst_category": "Registered Regular",
            "links": [{
                "link_doctype": update["doctype"],
                "link_name": update["name"],
            }],
        }
        if self.erp.exists("Address", update["address_name"]):
            self.erp.update("Address", update["address_name"], values)
        else:
            self.erp.insert("Address", values)


def _gst_safe_name(raw: str | None) -> str:
    """GST transaction names allow only alphanumerics, '-' and '/', starting
    with an alphanumeric."""
    import re
    s = re.sub(r"[^A-Za-z0-9/-]+", "-", (raw or "").strip())
    s = re.sub(r"-{2,}", "-", s).strip("-/")
    return s


def _quote(value: str) -> str:
    import urllib.parse
    return urllib.parse.quote(str(value), safe="")


def _name_of(res):
    if isinstance(res, dict):
        msg = res.get("message")
        if isinstance(msg, dict):
            return msg.get("name")
        if isinstance(res.get("data"), dict):
            return res["data"].get("name")
    return None
