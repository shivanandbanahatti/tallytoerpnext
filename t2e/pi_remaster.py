"""Purchase Invoice remaster: replace Tally Migration Item lines with real items.

Full Migration batch (DEV):
  python -m t2e --env dev pi-remaster extract
  python -m t2e --env dev pi-remaster match
  python -m t2e --env dev pi-remaster apply            # dry-run high+lines_ok
  python -m t2e --env dev pi-remaster apply --confirm
  python -m t2e --env dev pi-remaster verify
  python -m t2e --env dev pi-remaster export-extract   # durable CSVs for remigration

Pilot seed (legacy):
  python -m t2e --env dev pi-remaster staging

Path: cancel settlement JE/PE → cancel PI → recreate with real stock items
(update_stock=1 into Stores) → recreate settlements → purge cancelled originals.
"""
from __future__ import annotations

import csv
import json
import re
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pymysql

from .config import DATA_DIR, get_config
from .erpnext_client import ERPNextClient, ERPNextError

MIGRATION_DIR = DATA_DIR / "Migration"
REPORTS_DIR = DATA_DIR / "reports"
PAGES_DIR = REPORTS_DIR / "pi_pilot_pages"
STAGING_JSON = REPORTS_DIR / "pi_remaster_pilot.json"
STAGING_CSV = REPORTS_DIR / "pi_remaster_pilot.csv"
APPLY_LOG = REPORTS_DIR / "pi_remaster_pilot_apply.json"

# Full-batch artefacts (OCR → match → apply)
BATCH_STAGING_JSON = REPORTS_DIR / "pi_remaster_staging.json"
BATCH_STAGING_CSV = REPORTS_DIR / "pi_remaster_staging.csv"
BATCH_APPLY_LOG = REPORTS_DIR / "pi_remaster_apply.json"
BATCH_SUMMARY = REPORTS_DIR / "pi_remaster_summary.json"
EXTRACT_PAGES_CSV = REPORTS_DIR / "pi_extract_pages.csv"
EXTRACT_LINES_CSV = REPORTS_DIR / "pi_extract_lines.csv"
EXTRACT_BUNDLE_JSON = REPORTS_DIR / "pi_extract_bundle.json"

GENERIC_ITEM = "Tally Migration Item"
TOTAL_TOL = Decimal("0.05")          # remaster recreate / line sum vs ERPNext net
MATCH_TOTAL_TOL = Decimal("1.00")    # OCR/vision total vs ERPNext grand (ERPNext is truth)
MATCH_BILL_TOTAL_TOL = Decimal("5.00")  # looser when bill_no also matches
NUDGE_MAX = Decimal("1000.00")       # max last-line nudge toward ERPNext net
PI_DATE_FROM = "2026-04-01"
PI_DATE_TO = "2026-06-30"


def _f(v) -> float:
    try:
        return float(v or 0)
    except (TypeError, ValueError):
        return 0.0


def _dec(v) -> Decimal:
    return Decimal(str(v or 0)).quantize(Decimal("0.01"))


def _connect():
    return pymysql.connect(
        **get_config().db_params,
        cursorclass=pymysql.cursors.DictCursor,
        connect_timeout=30,
    )


# ---------------------------------------------------------------------------
# Pilot seed: confirmed from PDF page 1 + ERPNext PINV-26-01093
# Second pilot: PINV-26-01095 (same JE payment) — lines TBD / single-line hold
# until its PDF page is reviewed; apply skips rows without corrected_lines.
# ---------------------------------------------------------------------------
PILOT_SEED: list[dict] = [
    {
        "pdf_file": "April 2026_Purchase Invoices_1.pdf",
        "page": 1,
        "ocr_bill_no": "106",  # OCR misread
        "corrected_bill_no": "SB/0105/26-27",
        "ocr_supplier": "S.B. INDUSTRIES",
        "ocr_date": "2026-04-08",
        "ocr_grand_total": 47318.0,
        "erp_pi_name": "PINV-26-01093-1",
        "match_status": "matched",
        "corrected_lines": [
            {
                "item_name": "SPT 1250 mm * 100 Mtr - TRP",
                "description": "",
                "qty": 5.0,
                "uom": "Rolls",
                "rate": 1750.0,
                "amount": 8750.0,
                "gst_hsn_code": "39199090",
            },
            {
                "item_name": "Self Adhesive SPT Roll",
                "description": '2"*100mtr = Clear',
                "qty": 30.0,
                "uom": "Rolls",
                "rate": 70.0,
                "amount": 2100.0,
                "gst_hsn_code": "39199090",
            },
            {
                # Invoice prints "Backer Rod"; user said "Black Rod" — use invoice text
                "item_name": "Backer Rod - 8mm",
                "description": "",
                "qty": 3000.0,
                "uom": "Mtrs",
                "rate": 3.0,
                "amount": 9000.0,
                "gst_hsn_code": "39161010",
            },
            {
                "item_name": "Self Adhesive Masking Tape in Roll",
                "description": '1 1/2" * 50mtr = IND',
                "qty": 300.0,
                "uom": "Rolls",
                "rate": 67.5,
                "amount": 20250.0,
                "gst_hsn_code": "39199090",
            },
        ],
    },
    {
        # Second pilot: Overdue Globalink PI (no PE/JE refs) — lines from PDF TBD.
        # Seeded with a single corrected line matching net_total so the recreate
        # path can be tested without payment unlink; replace when PDF page found.
        "pdf_file": "",
        "page": 0,
        "ocr_bill_no": "GLE-2026-27-7",
        "corrected_bill_no": "GLE-2026-27-7",
        "ocr_supplier": "Globalink Enterprises",
        "ocr_date": "2026-04-02",
        "ocr_grand_total": 46374.0,
        "erp_pi_name": "PINV-26-01079",
        "match_status": "matched",
        "notes": "Pilot B: overdue, no payment links. Single line = net_total pending real PDF split.",
        "corrected_lines": [
            {
                "item_name": "Aluminium / Facade Materials",
                "description": "Pilot placeholder — replace with PDF line items",
                "qty": 1.0,
                "uom": "Nos",
                "rate": 39300.0,
                "amount": 39300.0,
                "gst_hsn_code": "76042100",
            },
        ],
    },
]


def write_pilot_staging() -> Path:
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    # Enrich from DB
    conn = _connect()
    cur = conn.cursor()
    rows = []
    for seed in PILOT_SEED:
        name = seed["erp_pi_name"]
        cur.execute(
            """SELECT name, supplier, bill_no, bill_date, posting_date,
                      grand_total, net_total, outstanding_amount, status,
                      tally_guid, company, credit_to
               FROM `tabPurchase Invoice` WHERE name=%s""",
            (name,),
        )
        erp = cur.fetchone()
        if not erp:
            seed = {**seed, "match_status": "erp_missing"}
            rows.append(seed)
            continue
        seed = {
            **seed,
            "erp_supplier": erp["supplier"],
            "erp_bill_no": erp["bill_no"],
            "erp_grand_total": float(erp["grand_total"]),
            "erp_net_total": float(erp["net_total"]),
            "erp_outstanding": float(erp["outstanding_amount"]),
            "erp_status": erp["status"],
            "erp_posting_date": str(erp["posting_date"]),
            "erp_bill_date": str(erp["bill_date"]) if erp["bill_date"] else None,
            "tally_guid": erp["tally_guid"],
            "company": erp["company"],
            "credit_to": erp["credit_to"],
        }
        # Validate line amounts vs net_total
        line_sum = sum(_f(x["amount"]) for x in seed.get("corrected_lines") or [])
        seed["lines_sum"] = round(line_sum, 2)
        seed["lines_ok"] = abs(line_sum - float(erp["net_total"])) <= 0.05
        rows.append(seed)
    conn.close()

    STAGING_JSON.write_text(json.dumps(rows, indent=2, default=str), encoding="utf-8")
    with STAGING_CSV.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(
            fh,
            fieldnames=[
                "erp_pi_name", "corrected_bill_no", "erp_bill_no", "erp_supplier",
                "erp_grand_total", "erp_status", "match_status", "lines_ok",
                "lines_sum", "pdf_file", "page", "notes",
            ],
            extrasaction="ignore",
        )
        w.writeheader()
        for r in rows:
            w.writerow(r)
    return STAGING_JSON


def render_pdf_pages(pdf_name: str, pages: list[int], zoom: float = 2.0) -> list[Path]:
    """Render 1-based page numbers from a Migration PDF to PNG."""
    import fitz

    PAGES_DIR.mkdir(parents=True, exist_ok=True)
    pdf = MIGRATION_DIR / pdf_name
    doc = fitz.open(pdf)
    out_paths = []
    for pno in pages:
        i = pno - 1
        if i < 0 or i >= doc.page_count:
            continue
        pix = doc[i].get_pixmap(matrix=fitz.Matrix(zoom, zoom), alpha=False)
        path = PAGES_DIR / f"{Path(pdf_name).stem[:20].replace(' ', '_')}_p{pno:03d}.png"
        pix.save(str(path))
        out_paths.append(path)
    doc.close()
    return out_paths


# ---------------------------------------------------------------------------
# Apply remaster
# ---------------------------------------------------------------------------
def _load_pi(cur, name: str) -> dict:
    cur.execute("SELECT * FROM `tabPurchase Invoice` WHERE name=%s", (name,))
    header = cur.fetchone()
    cur.execute(
        """SELECT * FROM `tabPurchase Invoice Item`
           WHERE parent=%s ORDER BY idx""",
        (name,),
    )
    items = cur.fetchall()
    cur.execute(
        """SELECT * FROM `tabPurchase Taxes and Charges`
           WHERE parent=%s ORDER BY idx""",
        (name,),
    )
    taxes = cur.fetchall()
    return {"header": header, "items": items, "taxes": taxes}


def _pe_refs(cur, pi_name: str) -> list[dict]:
    cur.execute(
        """SELECT parent AS pe_name, reference_doctype, reference_name,
                  allocated_amount, total_amount, outstanding_amount, idx
           FROM `tabPayment Entry Reference`
           WHERE reference_doctype='Purchase Invoice' AND reference_name=%s
           ORDER BY parent, idx""",
        (pi_name,),
    )
    return list(cur.fetchall())


def _je_refs(cur, pi_name: str) -> list[dict]:
    cur.execute(
        """SELECT parent AS je_name, name AS row_name, idx, account, party_type,
                  party, debit, credit, reference_type, reference_name
           FROM `tabJournal Entry Account`
           WHERE reference_type='Purchase Invoice' AND reference_name=%s
           ORDER BY parent, idx""",
        (pi_name,),
    )
    return list(cur.fetchall())


def _ensure_uom(erp: ERPNextClient, uom: str) -> str:
    uom = (uom or "Nos").strip()
    # normalize common plurals to ERPNext-ish names
    aliases = {
        "Rolls": "Roll",
        "Mtrs": "Meter",
        "Mtrs.": "Meter",
        "Mtr": "Meter",
        "Mtr.": "Meter",
        "NOS": "Nos",
        "Nos.": "Nos",
        "Kg": "Kg",
        "ROL": "Roll",
    }
    uom = aliases.get(uom, uom)
    if erp.dry_run or erp.exists("UOM", uom):
        return uom
    try:
        erp.insert("UOM", {"uom_name": uom})
    except ERPNextError:
        if not erp.exists("UOM", uom):
            return "Nos"
    return uom


def _default_warehouse(company: str | None = None) -> str:
    """Resolve leaf warehouse for update_stock=1 (prefer Stores - <abbr>)."""
    conn = _connect()
    cur = conn.cursor()
    try:
        if not company:
            company = get_config().erpnext["company"]
        cur.execute("SELECT abbr FROM tabCompany WHERE name=%s", (company,))
        row = cur.fetchone()
        abbr = (row or {}).get("abbr") or "SDL"
        preferred = f"Stores - {abbr}"
        cur.execute(
            "SELECT name FROM tabWarehouse WHERE name=%s AND is_group=0 AND disabled=0",
            (preferred,),
        )
        if cur.fetchone():
            return preferred
        cur.execute(
            "SELECT name FROM tabWarehouse "
            "WHERE company=%s AND is_group=0 AND disabled=0 ORDER BY name LIMIT 1",
            (company,),
        )
        hit = cur.fetchone()
        return hit["name"] if hit else preferred
    finally:
        conn.close()


def _ensure_item(erp: ERPNextClient, line: dict) -> str:
    """Create/promote stock purchase Item; return item_code."""
    name = (line["item_name"] or "").strip()
    if not name:
        raise ValueError("item_name required")
    code = re.sub(r"\s+", " ", name)[:140]
    uom = _ensure_uom(erp, line.get("uom") or "Nos")
    hsn = (line.get("gst_hsn_code") or "").strip()
    if hsn and not _hsn_exists(hsn):
        hsn = ""
    if erp.exists("Item", code):
        if not erp.dry_run:
            patch = {"is_stock_item": 1, "is_purchase_item": 1, "stock_uom": uom}
            if hsn:
                patch["gst_hsn_code"] = hsn
            try:
                erp.update("Item", code, patch)
            except ERPNextError:
                pass
        return code
    doc = {
        "item_code": code,
        "item_name": code,
        "item_group": "All Item Groups",
        "stock_uom": uom,
        "is_stock_item": 1,
        "is_purchase_item": 1,
        "is_sales_item": 0,
        "description": line.get("description") or code,
    }
    if hsn:
        doc["gst_hsn_code"] = hsn
    if erp.dry_run:
        return code
    try:
        erp.insert("Item", doc)
    except ERPNextError:
        # Retry without HSN if link validation failed
        if "gst_hsn_code" in doc:
            doc.pop("gst_hsn_code", None)
            try:
                erp.insert("Item", doc)
                return code
            except ERPNextError:
                pass
        if not erp.exists("Item", code):
            raise
    return code


def _uom_must_be_int(uom: str) -> bool:
    conn = _connect()
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT must_be_whole_number FROM tabUOM WHERE name=%s", (uom,)
        )
        row = cur.fetchone()
        return bool(row and row.get("must_be_whole_number"))
    except Exception:
        return uom in ("Nos", "Roll")
    finally:
        conn.close()


def _hsn_exists(hsn: str) -> bool:
    conn = _connect()
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT name FROM `tabGST HSN Code` WHERE name=%s LIMIT 1", (hsn,)
        )
        return bool(cur.fetchone())
    except Exception:
        return False
    finally:
        conn.close()


def _unlink_payments(erp: ERPNextClient, pe_refs: list[dict]) -> list[str]:
    """Remove PI from each PE's references child table via full-doc update."""
    touched = []
    by_pe: dict[str, list[dict]] = {}
    for r in pe_refs:
        by_pe.setdefault(r["pe_name"], []).append(r)
    for pe_name, refs in by_pe.items():
        drop = {r["reference_name"] for r in refs}
        if erp.dry_run:
            touched.append(pe_name)
            continue
        pe = erp._request(
            "GET",
            f"/api/resource/Payment%20Entry/{urllib_quote(pe_name)}",
        )["data"]
        new_refs = [
            row for row in (pe.get("references") or [])
            if not (
                row.get("reference_doctype") == "Purchase Invoice"
                and row.get("reference_name") in drop
            )
        ]
        # Clearing all refs may require unpaid-to-something; send update
        erp.update("Payment Entry", pe_name, {"references": new_refs})
        touched.append(pe_name)
    return touched


def urllib_quote(s: str) -> str:
    import urllib.parse
    return urllib.parse.quote(str(s), safe="")


# Parent doctype -> child table names (without "tab" prefix)
_PURGE_CHILDREN: dict[str, list[str]] = {
    "Purchase Invoice": [
        "Purchase Invoice Item",
        "Purchase Taxes and Charges",
        "Purchase Invoice Advance",
        "Payment Schedule",
    ],
    "Journal Entry": ["Journal Entry Account"],
    "Payment Entry": [
        "Payment Entry Reference",
        "Payment Entry Deduction",
        "Advance Taxes and Charges",
    ],
}


def _purge_cancelled(doctype: str, name: str) -> dict[str, int]:
    """Hard-delete a cancelled transaction and its ledger orphans (MariaDB).

    REST delete fails when Payment Ledger Entry / amended_from still link the
    cancelled voucher. Scoped SQL matches db_wipe's approach, but only for one
    named cancelled doc (docstatus must be 2).
    """
    counts: dict[str, int] = {}
    conn = _connect()
    try:
        cur = conn.cursor()
        cur.execute(
            f"SELECT docstatus FROM `tab{doctype}` WHERE name=%s", (name,)
        )
        row = cur.fetchone()
        if not row:
            counts["missing"] = 1
            return counts
        if int(row["docstatus"]) != 2:
            raise ValueError(
                f"refuse purge {doctype} {name}: docstatus={row['docstatus']} (need 2)"
            )

        # Break amendment links pointing at this cancelled doc
        for child_dt in ("Purchase Invoice", "Journal Entry", "Payment Entry",
                         "Sales Invoice"):
            try:
                cur.execute(
                    f"UPDATE `tab{child_dt}` SET amended_from=NULL "
                    f"WHERE amended_from=%s",
                    (name,),
                )
                if cur.rowcount:
                    counts[f"clear_amended_from:{child_dt}"] = cur.rowcount
            except Exception:
                pass

        for tbl, cols in (
            ("GL Entry", ("voucher_no",)),
            ("Stock Ledger Entry", ("voucher_no",)),
            ("Payment Ledger Entry", ("voucher_no", "against_voucher_no")),
        ):
            for col in cols:
                try:
                    cur.execute(
                        f"DELETE FROM `tab{tbl}` WHERE `{col}`=%s", (name,)
                    )
                    if cur.rowcount:
                        counts[f"{tbl}:{col}"] = cur.rowcount
                except Exception:
                    pass

        for child in _PURGE_CHILDREN.get(doctype, []):
            try:
                cur.execute(
                    f"DELETE FROM `tab{child}` WHERE parent=%s AND parenttype=%s",
                    (name, doctype),
                )
                if cur.rowcount:
                    counts[child] = cur.rowcount
            except Exception:
                pass

        cur.execute(f"DELETE FROM `tab{doctype}` WHERE name=%s AND docstatus=2", (name,))
        counts["parent"] = cur.rowcount
        conn.commit()
        return counts
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _cancel_jes(erp: ERPNextClient, je_names: list[str]) -> list[str]:
    done = []
    for je_name in sorted(set(je_names)):
        if erp.dry_run:
            done.append(je_name)
            continue
        erp.cancel("Journal Entry", je_name)
        done.append(je_name)
    return done


def _resubmit_je_with_pi_rename(
    erp: ERPNextClient, je_name: str, old_pi: str, new_pi: str
) -> str:
    """Recreate a cancelled JE with old_pi → new_pi, then purge the cancelled JE.

    Does not use amended_from so the cancelled original can be deleted.
    """
    if erp.dry_run:
        return je_name
    old = erp._request(
        "GET", f"/api/resource/Journal%20Entry/{urllib_quote(je_name)}"
    )["data"]
    if int(old.get("docstatus") or 0) == 1:
        erp.cancel("Journal Entry", je_name)
        old = erp._request(
            "GET", f"/api/resource/Journal%20Entry/{urllib_quote(je_name)}"
        )["data"]
    accounts = []
    for row in old.get("accounts") or []:
        row = _child_clean(row)
        if (
            row.get("reference_type") == "Purchase Invoice"
            and row.get("reference_name") == old_pi
        ):
            row["reference_name"] = new_pi
        accounts.append(row)
    field = get_config().idempotency_field
    guid = old.get(field) or old.get("tally_guid")
    if guid:
        try:
            erp.update("Journal Entry", je_name, {field: f"{guid}-cancelled"})
        except ERPNextError:
            pass
    doc = {
        "company": old["company"],
        "posting_date": old["posting_date"],
        "voucher_type": old.get("voucher_type") or "Journal Entry",
        "user_remark": old.get("user_remark") or old.get("remark"),
        "accounts": accounts,
    }
    if guid:
        doc[field] = guid
    res = erp.insert_and_submit("Journal Entry", doc)
    new_name = res["data"]["name"]
    _purge_cancelled("Journal Entry", je_name)
    return new_name


def _child_clean(row: dict, drop: set[str] | None = None) -> dict:
    drop = drop or set()
    skip = {
        "name", "creation", "modified", "owner", "modified_by",
        "parent", "parenttype", "parentfield", "docstatus",
    } | drop
    return {k: v for k, v in row.items() if k not in skip and v not in (None, "")}


def _snapshot_pe(erp: ERPNextClient, pe_name: str) -> dict:
    return erp._request(
        "GET", f"/api/resource/Payment%20Entry/{urllib_quote(pe_name)}"
    )["data"]


def _resubmit_pe_with_pi_rename(
    erp: ERPNextClient, pe_doc: dict, old_pi: str, new_pi: str
) -> str:
    """Recreate cancelled PE with renamed PI ref, then purge the cancelled PE."""
    pe_name = pe_doc["name"]
    if erp.dry_run:
        return pe_name
    # Refresh snapshot in case we only had a stub
    if int(pe_doc.get("docstatus") or 0) != 2:
        pe_doc = _snapshot_pe(erp, pe_name)
    refs = []
    for row in pe_doc.get("references") or []:
        row = _child_clean(row)
        if (
            row.get("reference_doctype") == "Purchase Invoice"
            and row.get("reference_name") == old_pi
        ):
            row["reference_name"] = new_pi
        refs.append(row)
    field = get_config().idempotency_field
    keep = {
        "payment_type", "party_type", "party", "company", "posting_date",
        "mode_of_payment", "paid_from", "paid_to", "paid_amount",
        "received_amount", "source_exchange_rate", "target_exchange_rate",
        "reference_no", "reference_date", "remarks", "cost_center",
        field,
    }
    doc = {k: pe_doc[k] for k in keep if pe_doc.get(k) not in (None, "")}
    doc["references"] = refs
    guid = pe_doc.get(field)
    if guid:
        try:
            erp.update("Payment Entry", pe_name, {field: f"{guid}-cancelled"})
        except ERPNextError:
            pass
        doc[field] = guid
    res = erp.insert_and_submit("Payment Entry", doc)
    new_name = res["data"]["name"]
    _purge_cancelled("Payment Entry", pe_name)
    return new_name


def _build_new_pi(old: dict, staging: dict, item_codes: list[str]) -> dict:
    h = old["header"]
    lines = staging["corrected_lines"]
    expense = None
    cost_center = None
    for it in old["items"]:
        expense = expense or it.get("expense_account")
        cost_center = cost_center or it.get("cost_center")
    for tax in old["taxes"]:
        cost_center = cost_center or tax.get("cost_center")

    warehouse = staging.get("warehouse") or _default_warehouse(h.get("company"))

    item_rows = []
    for code, line in zip(item_codes, lines):
        uom = line.get("_uom_resolved") or line.get("uom") or "Nos"
        qty = _f(line["qty"])
        rate = _f(line["rate"])
        # Nos/Roll/etc require whole numbers on this site
        if uom in ("Nos", "Roll", "Box", "Packet", "Set") or _uom_must_be_int(uom):
            if abs(qty - round(qty)) > 0.001:
                # keep amount: adjust rate after rounding qty
                amount = round(qty * rate, 2)
                qty = float(max(1, round(qty)))
                rate = round(amount / qty, 4) if qty else rate
            else:
                qty = float(round(qty))
        row = {
            "item_code": code,
            "qty": qty,
            "rate": rate,
            "uom": uom,
            "warehouse": warehouse,
            "expense_account": expense,
        }
        if cost_center:
            row["cost_center"] = cost_center
        desc = (line.get("description") or "").strip()
        if desc:
            row["description"] = desc
        hsn = (line.get("gst_hsn_code") or "").strip()
        if hsn:
            row["gst_hsn_code"] = hsn
        item_rows.append(row)

    tax_rows = []
    for t in old["taxes"]:
        if "ROUND" in (
            f"{t.get('description') or ''} {t.get('account_head') or ''}"
        ).upper():
            continue
        tax_rows.append({
            "charge_type": t.get("charge_type") or "Actual",
            "account_head": t["account_head"],
            "description": t.get("description") or t["account_head"],
            "tax_amount": _f(t["tax_amount"]),
            "rate": _f(t.get("rate")),
            "cost_center": t.get("cost_center") or cost_center,
        })

    field = get_config().idempotency_field
    return {
        "company": h["company"],
        "supplier": h["supplier"],
        "posting_date": str(h["posting_date"]),
        "set_posting_time": 1,
        # Receive stock on the PI for this migration; PR+PI split can come later.
        "update_stock": 1,
        "disable_rounded_total": 0,
        "is_return": int(h.get("is_return") or 0),
        "credit_to": h["credit_to"],
        "bill_no": staging.get("corrected_bill_no") or h["bill_no"],
        "bill_date": str(h["bill_date"] or h["posting_date"]),
        "naming_series": h.get("naming_series") or "PINV-.YY.-",
        "tax_category": h.get("tax_category") or "",
        "taxes_and_charges": h.get("taxes_and_charges") or "",
        "items": item_rows,
        "taxes": tax_rows,
        field: h.get(field) or h.get("tally_guid"),
    }


def remaster_one(
    erp: ERPNextClient, staging: dict, *, skip_if_lines_bad: bool = True
) -> dict:
    """Cancel settlements → cancel PI → recreate with real items → purge cancelled.

    Cancelled originals are hard-deleted via scoped MariaDB purge (REST delete
    fails on Payment Ledger / amendment links). Resumes if PI/JE already cancelled.
    """
    name = staging["erp_pi_name"]
    result: dict[str, Any] = {
        "erp_pi_name": name,
        "ok": False,
        "dry_run": erp.dry_run,
    }
    lines = staging.get("corrected_lines") or []
    if not lines:
        result["error"] = "no corrected_lines"
        return result
    if skip_if_lines_bad and staging.get("lines_ok") is False:
        result["error"] = (
            f"lines_sum {staging.get('lines_sum')} != net_total "
            f"{staging.get('erp_net_total')}"
        )
        return result

    conn = _connect()
    cur = conn.cursor()
    try:
        old = _load_pi(cur, name)
        if not old["header"]:
            result["error"] = "erp_missing"
            return result
        ds = int(old["header"]["docstatus"] or 0)
        if ds not in (1, 2):
            result["error"] = f"docstatus={ds} (need submitted or cancelled)"
            return result

        # Settlement refs: live query + any JE names already known from a prior attempt
        pe_refs = _pe_refs(cur, name)
        je_refs = _je_refs(cur, name)
        # Also find cancelled JEs still pointing at this PI
        cur.execute(
            """SELECT DISTINCT parent AS je_name FROM `tabJournal Entry Account`
               WHERE reference_type='Purchase Invoice' AND reference_name=%s""",
            (name,),
        )
        for r in cur.fetchall():
            if r["je_name"] not in {x["je_name"] for x in je_refs}:
                je_refs.append({"je_name": r["je_name"]})
        result["pe_refs"] = [r["pe_name"] for r in pe_refs]
        result["je_refs"] = sorted({r["je_name"] for r in je_refs})

        old_gt = _dec(old["header"]["grand_total"])
        old_net = _dec(old["header"]["net_total"])
        line_sum = _dec(sum(_f(x["amount"]) for x in lines))
        if abs(line_sum - old_net) > TOTAL_TOL:
            result["error"] = f"line sum {line_sum} vs net {old_net}"
            return result

        item_codes = []
        for line in lines:
            uom = _ensure_uom(erp, line.get("uom") or "Nos")
            line["_uom_resolved"] = uom
            item_codes.append(_ensure_item(erp, {**line, "uom": uom}))
        result["item_codes"] = item_codes

        pe_docs = []
        for pe_name in sorted({r["pe_name"] for r in pe_refs}):
            if erp.dry_run:
                pe_docs.append({"name": pe_name})
            else:
                pe_docs.append(_snapshot_pe(erp, pe_name))

        if erp.dry_run:
            result["ok"] = True
            result["would_cancel_jes"] = result["je_refs"]
            result["would_cancel_pes"] = [p["name"] for p in pe_docs]
            result["new_pi_preview"] = _build_new_pi(old, staging, item_codes)
            result["expected_grand_total"] = float(old_gt)
            result["resume_cancelled_pi"] = ds == 2
            result["would_purge_cancelled"] = name
            return result

        # 1) Cancel JEs (skip if already cancelled)
        for je_name in result["je_refs"]:
            cur.execute(
                "SELECT docstatus FROM `tabJournal Entry` WHERE name=%s", (je_name,)
            )
            row = cur.fetchone()
            if row and int(row["docstatus"]) == 1:
                erp.cancel("Journal Entry", je_name)

        # 2) Cancel PEs
        for pe in pe_docs:
            if int(pe.get("docstatus") or 0) == 1:
                erp.cancel("Payment Entry", pe["name"])

        # 3) Cancel PI if still submitted
        if ds == 1:
            erp.cancel("Purchase Invoice", name)
            ds = 2

        # 4) Free tally_guid on cancelled PI, recreate (no amended_from)
        field = get_config().idempotency_field
        guid = old["header"].get(field) or old["header"].get("tally_guid")
        if guid:
            try:
                erp.update("Purchase Invoice", name, {field: f"{guid}-cancelled"})
            except ERPNextError as exc:
                result["guid_rename_warn"] = str(exc)[:300]

        doc = _build_new_pi(old, staging, item_codes)
        if guid:
            doc[field] = guid
        res = erp.insert_and_submit("Purchase Invoice", doc)
        new_name = res["data"]["name"]
        result["new_pi_name"] = new_name

        # 5) Recreate JEs with renamed PI ref (purges cancelled JE)
        new_jes = []
        for je_name in result["je_refs"]:
            new_jes.append(
                _resubmit_je_with_pi_rename(erp, je_name, name, new_name)
            )
        result["new_je_names"] = new_jes

        # 6) Recreate PEs with renamed PI ref (purges cancelled PE)
        new_pes = []
        for pe in pe_docs:
            new_pes.append(
                _resubmit_pe_with_pi_rename(erp, pe, name, new_name)
            )
        result["new_pe_names"] = new_pes

        # 7) Purge cancelled original PI (and ledger orphans)
        result["purged"] = _purge_cancelled("Purchase Invoice", name)
        cur.execute(
            "SELECT name, grand_total, net_total, status, outstanding_amount, "
            "bill_no FROM `tabPurchase Invoice` WHERE name=%s",
            (new_name,),
        )
        verify = cur.fetchone()
        result["verify"] = {
            k: (float(v) if hasattr(v, "as_tuple") else v)
            for k, v in (verify or {}).items()
        }
        if verify and abs(_dec(verify["grand_total"]) - old_gt) > TOTAL_TOL:
            result["error"] = (
                f"grand_total drift: new {verify['grand_total']} vs old {old_gt}"
            )
            return result
        result["ok"] = True
        return result
    except Exception as exc:
        result["error"] = f"{type(exc).__name__}: {exc}"
        if getattr(exc, "body", None):
            result["error_body"] = str(exc.body)[:1500]
        return result
    finally:
        conn.close()


def _norm_bill(s: str | None) -> str:
    if not s:
        return ""
    s = str(s).upper().strip()
    s = re.sub(r"\s+", "", s)
    s = s.replace("–", "-").replace("—", "-")
    # Common OCR mangling of SB/
    s = re.sub(r"^\$8/", "SB/", s)
    s = re.sub(r"^S8/", "SB/", s)
    s = re.sub(r"^58/", "SB/", s)
    return s


def _bill_close(a: str, b: str) -> bool:
    """Exact or small OCR drift (1 edit), or 2 edits when lengths are close."""
    if not a or not b:
        return False
    if a == b:
        return True
    if abs(len(a) - len(b)) > 2:
        return False
    # Levenshtein
    la, lb = len(a), len(b)
    prev = list(range(lb + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            ins = cur[j - 1] + 1
            delete = prev[j] + 1
            sub = prev[j - 1] + (ca != cb)
            cur.append(min(ins, delete, sub))
        prev = cur
    dist = prev[lb]
    if dist <= 1:
        return True
    # Allow 2 edits only when the shared prefix is long (avoid GLE-…-6 vs GLE-…-99)
    if dist == 2:
        common = 0
        for x, y in zip(a, b):
            if x != y:
                break
            common += 1
        return common >= max(6, int(0.6 * min(la, lb)))
    return False


def _amount_variants(amount: float) -> list[float]:
    """OCR often drops/adds a trailing zero; try common digit-shift variants."""
    if not amount:
        return []
    out = [amount]
    for f in (10.0, 0.1, 100.0, 0.01):
        v = round(amount * f, 2)
        if v > 0 and v not in out:
            out.append(v)
    return out


def _supplier_tokens(s: str | None) -> set[str]:
    if not s:
        return set()
    parts = re.findall(r"[A-Za-z0-9]+", s.upper())
    stop = {"PVT", "LTD", "LLP", "PRIVATE", "LIMITED", "THE", "AND", "CO", "COMPANY"}
    return {p for p in parts if len(p) > 2 and p not in stop}


def _supplier_fuzzy(a: str | None, b: str | None) -> float:
    ta, tb = _supplier_tokens(a), _supplier_tokens(b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / max(len(ta), len(tb))


def extract_migration(*, force: bool = False, limit_pages: int = 0) -> Path:
    """OCR all Migration *Purchase*.pdf pages → batch staging JSON (pre-match)."""
    from . import pi_ocr

    REPORTS_DIR.mkdir(parents=True, exist_ok=True)

    def prog(i, total, pdf_name, page):
        if i == 1 or i % 10 == 0 or i == total:
            print(f"  extract {i}/{total}  {pdf_name} p{page}", flush=True)

    rows = pi_ocr.extract_all_purchase_pdfs(
        force=force, limit_pages=limit_pages, progress=prog
    )
    BATCH_STAGING_JSON.write_text(
        json.dumps(rows, indent=2, default=str), encoding="utf-8"
    )
    with BATCH_STAGING_CSV.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(
            fh,
            fieldnames=[
                "pdf_file", "page", "ocr_bill_no", "ocr_supplier", "ocr_date",
                "ocr_grand_total", "n_lines", "error",
            ],
            extrasaction="ignore",
        )
        w.writeheader()
        for r in rows:
            w.writerow({
                **r,
                "n_lines": len(r.get("ocr_lines") or []),
            })
    print(f"  wrote {BATCH_STAGING_JSON} ({len(rows)} pages)")
    return BATCH_STAGING_JSON


def _load_dev_pis(from_date: str = PI_DATE_FROM, to_date: str = PI_DATE_TO) -> list[dict]:
    conn = _connect()
    cur = conn.cursor()
    cur.execute(
        """SELECT p.name, p.supplier, p.bill_no, p.bill_date, p.posting_date,
                  p.grand_total, p.net_total, p.outstanding_amount, p.status,
                  p.tally_guid, p.company, p.credit_to,
                  EXISTS(
                    SELECT 1 FROM `tabPurchase Invoice Item` i
                    WHERE i.parent=p.name AND i.item_code=%s
                  ) AS still_generic
           FROM `tabPurchase Invoice` p
           WHERE p.docstatus=1 AND p.posting_date BETWEEN %s AND %s
           ORDER BY p.posting_date, p.name""",
        (GENERIC_ITEM, from_date, to_date),
    )
    rows = list(cur.fetchall())
    conn.close()
    for r in rows:
        r["bill_no_norm"] = _norm_bill(r.get("bill_no"))
        r["still_generic"] = bool(r.get("still_generic"))
        r["grand_total"] = float(r["grand_total"] or 0)
        r["net_total"] = float(r["net_total"] or 0)
    return rows


def _correct_lines_to_net(
    lines: list[dict], net: float, grand: float, notes: str = "",
    *, force_scale: bool = False,
) -> tuple[list[dict], float, bool, str]:
    """Scale/nudge OCR lines so sum == ERPNext net_total (source of truth)."""
    lines = list(lines or [])
    line_sum = round(sum(_f(x.get("amount")) for x in lines), 2)
    lines_ok = bool(lines) and abs(line_sum - net) <= float(TOTAL_TOL)
    # OCR sometimes drops/adds zeros on all line amounts
    if lines and not lines_ok and line_sum > 0:
        for factor in (10.0, 0.1, 100.0, 0.01):
            if abs(line_sum * factor - net) <= float(NUDGE_MAX):
                scaled = []
                for ln in lines:
                    amt = round(_f(ln.get("amount")) * factor, 2)
                    q = _f(ln.get("qty")) or 1.0
                    scaled.append({
                        **ln,
                        "amount": amt,
                        "rate": round(amt / q, 4) if q else _f(ln.get("rate")),
                    })
                lines = scaled
                line_sum = round(sum(_f(x.get("amount")) for x in lines), 2)
                lines_ok = abs(line_sum - net) <= float(TOTAL_TOL)
                notes = (notes + "; " if notes else "") + f"scale_digit_shift={factor:g}"
                break
    if (
        lines and not lines_ok
        and line_sum > 0
        and abs(line_sum - grand) <= float(MATCH_TOTAL_TOL)
        and abs(net - grand) > float(MATCH_TOTAL_TOL)
    ):
        factor = net / line_sum
        scaled = []
        for ln in lines:
            amt = round(_f(ln.get("amount")) * factor, 2)
            q = _f(ln.get("qty")) or 1.0
            scaled.append({
                **ln,
                "amount": amt,
                "rate": round(amt / q, 4) if q else _f(ln.get("rate")),
            })
        lines = scaled
        line_sum = round(sum(_f(x.get("amount")) for x in lines), 2)
        if abs(line_sum - net) > float(TOTAL_TOL) and abs(line_sum - net) <= 1.0:
            diff = round(net - line_sum, 2)
            last = dict(lines[-1])
            last["amount"] = round(_f(last.get("amount")) + diff, 2)
            q = _f(last.get("qty")) or 1.0
            last["rate"] = round(last["amount"] / q, 4)
            lines = list(lines[:-1]) + [last]
            line_sum = round(sum(_f(x.get("amount")) for x in lines), 2)
        lines_ok = abs(line_sum - net) <= float(TOTAL_TOL)
        notes = (notes + "; " if notes else "") + f"scale_tax_incl_to_net={factor:.6f}"
    # Invoice identity confirmed (bill+totals) but line OCR incomplete — scale to ERP net
    if lines and not lines_ok and line_sum > 0 and force_scale:
        factor = net / line_sum
        scaled = []
        for ln in lines:
            amt = round(_f(ln.get("amount")) * factor, 2)
            q = _f(ln.get("qty")) or 1.0
            scaled.append({
                **ln,
                "amount": amt,
                "rate": round(amt / q, 4) if q else _f(ln.get("rate")),
            })
        lines = scaled
        line_sum = round(sum(_f(x.get("amount")) for x in lines), 2)
        diff = round(net - line_sum, 2)
        if abs(diff) > float(TOTAL_TOL):
            last = dict(lines[-1])
            last["amount"] = round(_f(last.get("amount")) + diff, 2)
            q = _f(last.get("qty")) or 1.0
            last["rate"] = round(last["amount"] / q, 4)
            lines = list(lines[:-1]) + [last]
            line_sum = round(sum(_f(x.get("amount")) for x in lines), 2)
        lines_ok = abs(line_sum - net) <= float(TOTAL_TOL)
        notes = (notes + "; " if notes else "") + f"force_scale_to_net={factor:.6f}"
    if lines and not lines_ok and abs(line_sum - net) <= float(NUDGE_MAX):
        diff = round(net - line_sum, 2)
        last = dict(lines[-1])
        last["amount"] = round(_f(last.get("amount")) + diff, 2)
        q = _f(last.get("qty")) or 1.0
        last["rate"] = round(last["amount"] / q, 4)
        lines = list(lines[:-1]) + [last]
        line_sum = round(sum(_f(x.get("amount")) for x in lines), 2)
        lines_ok = abs(line_sum - net) <= float(TOTAL_TOL)
        notes = (notes + "; " if notes else "") + f"nudge_last_line={diff}"
    return lines, line_sum, lines_ok, notes


def _find_pi_candidate(
    row: dict,
    pis: list[dict],
    by_bill: dict[str, list[dict]],
    used_pis: set[str],
) -> tuple[dict | None, str, str]:
    """Return (candidate, match_status, notes) for one OCR/vision page."""
    ocr_bill = _norm_bill(row.get("ocr_bill_no") or row.get("bill_no_norm"))
    ocr_total = _f(row.get("ocr_grand_total"))
    ocr_net = _f(row.get("ocr_net_total"))
    # PI-driven locate may stash ERPNext targets when OCR totals drifted
    if row.get("target_grand_total") is not None and (
        ocr_total is None
        or abs(ocr_total - _f(row.get("target_grand_total"))) > float(MATCH_BILL_TOTAL_TOL)
    ):
        # Prefer target for matching when model missed the total
        if row.get("pi_driven"):
            ocr_total = _f(row.get("target_grand_total"))
    if row.get("target_net_total") is not None and row.get("pi_driven"):
        if ocr_net is None or abs(ocr_net - _f(row.get("target_net_total"))) > float(MATCH_BILL_TOTAL_TOL):
            ocr_net = _f(row.get("target_net_total"))
    if row.get("target_bill_no") and row.get("pi_driven"):
        ocr_bill = _norm_bill(row.get("target_bill_no")) or ocr_bill

    ocr_date = row.get("ocr_date")
    ocr_supplier = row.get("ocr_supplier") or ""
    notes = ""

    def unused(c: dict) -> bool:
        return c["name"] not in used_pis

    # PI-driven: hard-prefer the located target when still available
    target_name = row.get("pi_driven_target")
    if target_name and row.get("pi_driven"):
        for cand in pis:
            if cand["name"] == target_name and unused(cand):
                return cand, "high", "pi_driven"

    # High: bill + grand within loose tol (incl. ×10 OCR digit-shift when bill matches)
    if ocr_total or ocr_bill:
        totals = _amount_variants(ocr_total) if ocr_total else []
        for cand in pis:
            if not unused(cand):
                continue
            if not (ocr_bill and _bill_close(ocr_bill, cand["bill_no_norm"])):
                continue
            if ocr_total:
                if any(
                    abs(cand["grand_total"] - v) <= float(MATCH_BILL_TOTAL_TOL)
                    for v in totals
                ):
                    return cand, "high", notes
            if ocr_net and abs(float(cand["net_total"]) - ocr_net) <= float(MATCH_BILL_TOTAL_TOL):
                return cand, "high", "bill_plus_net"
        if ocr_bill and ocr_bill in by_bill:
            for cand in by_bill[ocr_bill]:
                if not unused(cand):
                    continue
                if ocr_total and any(
                    abs(cand["grand_total"] - v) <= float(MATCH_BILL_TOTAL_TOL)
                    for v in totals
                ):
                    return cand, "high", notes
        # Unique grand_total among unused (exact OCR total only — no digit-shift)
        if ocr_total:
            hits = [
                c for c in pis
                if unused(c) and abs(c["grand_total"] - ocr_total) <= float(MATCH_TOTAL_TOL)
            ]
            if len(hits) == 1:
                return hits[0], "high", "unique_amount"

    # High: unique OCR net ≈ ERPNext net (exact only)
    if ocr_net:
        hits = [
            c for c in pis
            if unused(c) and abs(float(c["net_total"]) - ocr_net) <= float(MATCH_TOTAL_TOL)
        ]
        if len(hits) == 1:
            return hits[0], "high", "unique_net"
        if not ocr_total:
            hits = [
                c for c in pis
                if unused(c) and abs(c["grand_total"] - ocr_net) <= float(MATCH_TOTAL_TOL)
            ]
            if len(hits) == 1:
                return hits[0], "high", "unique_amount_from_net"

    # Exact bill only when unique among unused AND amount is in the ballpark
    if ocr_bill and ocr_bill in by_bill:
        hits = [c for c in by_bill[ocr_bill] if unused(c)]
        if len(hits) == 1:
            cand = hits[0]
            if ocr_total and abs(cand["grand_total"] - ocr_total) <= max(
                float(MATCH_BILL_TOTAL_TOL) * 20, cand["grand_total"] * 0.05 + 1
            ):
                return cand, "high", "unique_bill"
            if ocr_net and abs(float(cand["net_total"]) - ocr_net) <= float(MATCH_BILL_TOTAL_TOL):
                return cand, "high", "unique_bill_net"

    # Medium: amount + supplier fuzzy + date window
    if ocr_total:
        best_score = 0.0
        best = None
        for cand in pis:
            if not unused(cand):
                continue
            if abs(cand["grand_total"] - ocr_total) > float(MATCH_BILL_TOTAL_TOL):
                continue
            score = _supplier_fuzzy(ocr_supplier, cand["supplier"])
            if ocr_date and cand.get("posting_date"):
                try:
                    d1 = datetime.strptime(str(ocr_date)[:10], "%Y-%m-%d").date()
                    d2 = cand["posting_date"]
                    if isinstance(d2, str):
                        d2 = datetime.strptime(str(d2)[:10], "%Y-%m-%d").date()
                    if abs((d1 - d2).days) > 3:
                        continue
                    score += 0.3
                except Exception:
                    pass
            if score > best_score and score >= 0.35:
                best_score = score
                best = cand
        if best:
            return best, "medium", f"fuzzy_score={best_score:.2f}"

    return None, "unmatched", notes


def match_staging(
    *, from_date: str = PI_DATE_FROM, to_date: str = PI_DATE_TO
) -> Path:
    """Score OCR rows against DEV PIs; write enriched batch staging.

    Two-pass: score every page, then assign PIs preferring rows whose lines
    already reconcile to ERPNext net_total.
    """
    if not BATCH_STAGING_JSON.exists():
        raise FileNotFoundError(
            f"missing {BATCH_STAGING_JSON} — run pi-remaster extract first"
        )
    pages = json.loads(BATCH_STAGING_JSON.read_text(encoding="utf-8"))
    pis = _load_dev_pis(from_date, to_date)
    by_bill: dict[str, list[dict]] = {}
    for p in pis:
        key = p["bill_no_norm"]
        if key:
            by_bill.setdefault(key, []).append(p)

    # Pass 1: provisional candidates with empty used set (may collide)
    provisional: list[tuple[int, dict | None, str, str, bool]] = []
    for idx, row in enumerate(pages):
        if row.get("error"):
            provisional.append((idx, None, "unmatched", row["error"], False))
            continue
        cand, status, notes = _find_pi_candidate(row, pis, by_bill, used_pis=set())
        preview_ok = False
        if cand is not None:
            ocr_g = _f(row.get("ocr_grand_total"))
            ocr_n = _f(row.get("ocr_net_total"))
            force = bool(row.get("pi_driven")) or (
                (ocr_n is not None and abs(ocr_n - float(cand["net_total"])) <= float(MATCH_TOTAL_TOL))
                or (ocr_g is not None and abs(ocr_g - float(cand["grand_total"])) <= float(MATCH_TOTAL_TOL))
            )
            lines, _, preview_ok, _ = _correct_lines_to_net(
                list(row.get("ocr_lines") or []),
                float(cand["net_total"]),
                float(cand["grand_total"]),
                force_scale=force,
            )
            preview_ok = preview_ok and bool(lines)
        provisional.append((idx, cand, status, notes, preview_ok))

    # Pass 2: assign greedily — lines_ok first, then high, then medium
    def _prio(item: tuple[int, dict | None, str, str, bool]) -> tuple:
        idx, cand, status, notes, preview_ok = item
        if cand is None:
            return (9, idx)
        rank = 0 if preview_ok else 1
        if status == "high":
            rank += 0
        elif status == "medium":
            rank += 2
        else:
            rank += 5
        return (rank, idx)

    used_pis: set[str] = set()
    assigned: dict[int, tuple[dict, str, str]] = {}
    for idx, cand, status, notes, _preview_ok in sorted(provisional, key=_prio):
        if cand is None or status == "unmatched":
            continue
        if cand["name"] in used_pis:
            # Re-resolve against remaining PIs
            cand2, status2, notes2 = _find_pi_candidate(
                pages[idx], pis, by_bill, used_pis
            )
            if cand2 is None:
                continue
            cand, status, notes = cand2, status2, notes2
        used_pis.add(cand["name"])
        assigned[idx] = (cand, status, notes)

    out: list[dict] = []
    counts = {"high": 0, "medium": 0, "unmatched": 0, "already_remastered": 0,
              "lines_ok": 0, "pages": len(pages)}

    for idx, row in enumerate(pages):
        if idx not in assigned:
            notes = provisional[idx][3] if provisional[idx][0] == idx else ""
            counts["unmatched"] += 1
            out.append({**row, "match_status": "unmatched", "notes": notes})
            continue

        candidate, match_status, notes = assigned[idx]
        ocr_g = _f(row.get("ocr_grand_total"))
        ocr_n = _f(row.get("ocr_net_total"))
        force = bool(row.get("pi_driven")) or (
            (ocr_n is not None and abs(ocr_n - float(candidate["net_total"])) <= float(MATCH_TOTAL_TOL))
            or (ocr_g is not None and abs(ocr_g - float(candidate["grand_total"])) <= float(MATCH_TOTAL_TOL))
        )
        lines, line_sum, lines_ok, notes = _correct_lines_to_net(
            list(row.get("ocr_lines") or []),
            float(candidate["net_total"]),
            float(candidate["grand_total"]),
            notes,
            force_scale=force,
        )

        enriched = {
            **row,
            "match_status": match_status,
            "erp_pi_name": candidate["name"],
            "erp_supplier": candidate["supplier"],
            "erp_bill_no": candidate["bill_no"],
            "corrected_bill_no": candidate["bill_no"],
            "erp_grand_total": candidate["grand_total"],
            "erp_net_total": candidate["net_total"],
            "erp_outstanding": float(candidate["outstanding_amount"] or 0),
            "erp_status": candidate["status"],
            "erp_posting_date": str(candidate["posting_date"]),
            "erp_bill_date": str(candidate["bill_date"]) if candidate.get("bill_date") else None,
            "tally_guid": candidate.get("tally_guid"),
            "company": candidate.get("company"),
            "credit_to": candidate.get("credit_to"),
            "still_generic": candidate["still_generic"],
            "corrected_lines": lines if lines_ok else [],
            "lines_sum": line_sum,
            "lines_ok": lines_ok,
            "notes": notes,
        }
        if not candidate["still_generic"]:
            enriched["match_status"] = "already_remastered"
            counts["already_remastered"] += 1
        elif match_status == "high":
            counts["high"] += 1
        else:
            counts["medium"] += 1
        if lines_ok:
            counts["lines_ok"] += 1
        out.append(enriched)

    BATCH_STAGING_JSON.write_text(json.dumps(out, indent=2, default=str), encoding="utf-8")
    with BATCH_STAGING_CSV.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(
            fh,
            fieldnames=[
                "pdf_file", "page", "ocr_bill_no", "corrected_bill_no", "erp_pi_name",
                "erp_supplier", "erp_grand_total", "erp_net_total", "match_status",
                "lines_ok", "lines_sum", "still_generic", "notes",
            ],
            extrasaction="ignore",
        )
        w.writeheader()
        for r in out:
            w.writerow(r)

    applicable = sum(
        1 for r in out
        if r.get("match_status") == "high" and r.get("lines_ok") and r.get("still_generic")
    )
    summary = {**counts, "applicable_high_lines_ok": applicable}
    BATCH_SUMMARY.write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(f"  match summary: {summary}")
    print(f"  wrote {BATCH_STAGING_JSON}")
    return BATCH_STAGING_JSON


def apply_pilot(*, confirm: bool = False, limit: int = 0,
                names: list[str] | None = None,
                use_batch: bool = True) -> list[dict]:
    """Apply remaster. Prefers batch staging; only high + lines_ok + still_generic."""
    staging_path = BATCH_STAGING_JSON if use_batch and BATCH_STAGING_JSON.exists() else STAGING_JSON
    apply_log = BATCH_APPLY_LOG if staging_path == BATCH_STAGING_JSON else APPLY_LOG

    if not staging_path.exists():
        if staging_path == STAGING_JSON:
            write_pilot_staging()
        else:
            raise FileNotFoundError(
                f"missing {staging_path} — run extract + match first"
            )
    rows = json.loads(staging_path.read_text(encoding="utf-8"))
    if names:
        want = set(names)
        rows = [r for r in rows if r.get("erp_pi_name") in want]
    else:
        # Batch default filter
        if staging_path == BATCH_STAGING_JSON:
            rows = [
                r for r in rows
                if r.get("match_status") == "high"
                and r.get("lines_ok")
                and r.get("still_generic")
                and (r.get("corrected_lines") or r.get("ocr_lines"))
            ]
            for r in rows:
                if not r.get("corrected_lines") and r.get("ocr_lines"):
                    r["corrected_lines"] = r["ocr_lines"]
        else:
            rows = [r for r in rows if r.get("match_status") in ("matched", "high")]
    if limit:
        rows = rows[:limit]

    erp = ERPNextClient(dry_run=not confirm)
    results = []
    for row in rows:
        print(f"  remaster {row.get('erp_pi_name')} "
              f"({'LIVE' if confirm else 'dry-run'}) ...", flush=True)
        results.append(remaster_one(erp, row))
        r = results[-1]
        if r.get("ok"):
            print(f"    ok -> {r.get('new_pi_name') or '(preview)'}")
        else:
            print(f"    FAIL: {r.get('error')}")

    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    apply_log.write_text(json.dumps(results, indent=2, default=str), encoding="utf-8")

    summary = {
        "staging": str(staging_path),
        "attempted": len(results),
        "ok": sum(1 for r in results if r.get("ok")),
        "failed": sum(1 for r in results if not r.get("ok")),
        "confirm": confirm,
    }
    if BATCH_SUMMARY.exists():
        try:
            prev = json.loads(BATCH_SUMMARY.read_text(encoding="utf-8"))
            prev["apply"] = summary
            BATCH_SUMMARY.write_text(json.dumps(prev, indent=2), encoding="utf-8")
        except Exception:
            pass
    print(f"  apply summary: {summary}")
    print(f"  log={apply_log}")
    return results


def verify_pilot(names: list[str] | None = None) -> list[dict]:
    """Re-read remastered PIs: item codes, totals, PE/JE links, stock bins."""
    log_path = BATCH_APPLY_LOG if BATCH_APPLY_LOG.exists() else APPLY_LOG
    if not log_path.exists():
        return [{"error": f"missing {log_path}"}]
    log = json.loads(log_path.read_text(encoding="utf-8"))
    conn = _connect()
    cur = conn.cursor()
    out = []
    for entry in log:
        if names and entry.get("erp_pi_name") not in names and entry.get("new_pi_name") not in names:
            continue
        pi = entry.get("new_pi_name") or entry.get("erp_pi_name")
        if not pi or not entry.get("ok"):
            out.append(entry)
            continue
        cur.execute(
            "SELECT name, bill_no, grand_total, net_total, status, "
            "outstanding_amount, tally_guid FROM `tabPurchase Invoice` WHERE name=%s",
            (pi,),
        )
        hdr = cur.fetchone()
        cur.execute(
            "SELECT item_code, qty, rate, amount, uom, expense_account, warehouse "
            "FROM `tabPurchase Invoice Item` WHERE parent=%s ORDER BY idx",
            (pi,),
        )
        items = cur.fetchall()
        pe = _pe_refs(cur, pi)
        je = _je_refs(cur, pi)
        bins = []
        for it in items:
            cur.execute(
                "SELECT item_code, warehouse, actual_qty FROM tabBin "
                "WHERE item_code=%s AND actual_qty!=0 LIMIT 5",
                (it["item_code"],),
            )
            bins.extend(cur.fetchall())
        out.append({
            "pi": pi,
            "header": hdr,
            "items": items,
            "has_generic_item": any(
                i["item_code"] == GENERIC_ITEM for i in items
            ),
            "pe_count": len(pe),
            "je_count": len(je),
            "je_names": sorted({r["je_name"] for r in je}),
            "stock_bins": bins,
        })
    conn.close()
    return out


def _vision_cache_lookup(pdf_file: str, page: int) -> dict[str, Any] | None:
    """Load cached GPT extract for a pdf/page if present."""
    from .pi_vision import VISION_CACHE_DIR

    stem = re.sub(r"[^\w]+", "_", Path(pdf_file).stem)[:40]
    for tag in ("", "_hint"):
        path = VISION_CACHE_DIR / f"{stem}_p{int(page):03d}{tag}.json"
        if path.exists() and path.stat().st_size > 0:
            try:
                return json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
    return None


def export_extract_csvs(
    *,
    staging_path: Path | None = None,
) -> dict[str, Any]:
    """Write durable page + line CSVs (and a JSON bundle) for remigration without re-OCR.

    Prefer corrected_lines (ERPNext-reconciled) when present, else ocr_lines,
    else lines from pi_vision_cache.
    """
    src = staging_path or BATCH_STAGING_JSON
    if not src.exists():
        raise FileNotFoundError(f"missing {src}")
    pages = json.loads(src.read_text(encoding="utf-8"))

    page_rows: list[dict[str, Any]] = []
    line_rows: list[dict[str, Any]] = []
    bundle_pages: list[dict[str, Any]] = []

    for row in pages:
        pdf = row.get("pdf_file") or ""
        page = int(row.get("page") or 0)
        cache = None
        lines = list(row.get("corrected_lines") or [])
        line_source = "corrected_lines" if lines else ""
        if not lines:
            lines = list(row.get("ocr_lines") or [])
            line_source = "ocr_lines" if lines else ""
        if not lines:
            cache = _vision_cache_lookup(pdf, page)
            if cache:
                lines = list(cache.get("ocr_lines") or [])
                line_source = "vision_cache" if lines else ""

        bill = (
            row.get("corrected_bill_no")
            or row.get("erp_bill_no")
            or row.get("ocr_bill_no")
            or (cache or {}).get("ocr_bill_no")
        )
        supplier = (
            row.get("erp_supplier")
            or row.get("ocr_supplier")
            or (cache or {}).get("ocr_supplier")
        )
        grand = row.get("erp_grand_total")
        if grand is None:
            grand = row.get("ocr_grand_total")
            if grand is None and cache:
                grand = cache.get("ocr_grand_total")
        net = row.get("erp_net_total")
        if net is None:
            net = row.get("ocr_net_total")
            if net is None and cache:
                net = cache.get("ocr_net_total")

        lines_sum = round(sum(_f(x.get("amount")) for x in lines), 2) if lines else None
        page_rec = {
            "pdf_file": pdf,
            "page": page,
            "engine": row.get("engine") or (cache or {}).get("engine") or "",
            "model": row.get("model") or (cache or {}).get("model") or "",
            "ocr_bill_no": row.get("ocr_bill_no") or (cache or {}).get("ocr_bill_no"),
            "corrected_bill_no": row.get("corrected_bill_no") or row.get("erp_bill_no"),
            "bill_no": bill,
            "ocr_supplier": row.get("ocr_supplier") or (cache or {}).get("ocr_supplier"),
            "erp_supplier": row.get("erp_supplier"),
            "supplier": supplier,
            "ocr_date": row.get("ocr_date") or (cache or {}).get("ocr_date"),
            "ocr_grand_total": row.get("ocr_grand_total") or (cache or {}).get("ocr_grand_total"),
            "ocr_net_total": row.get("ocr_net_total") or (cache or {}).get("ocr_net_total"),
            "erp_grand_total": row.get("erp_grand_total"),
            "erp_net_total": row.get("erp_net_total"),
            "grand_total": grand,
            "net_total": net,
            "erp_pi_name": row.get("erp_pi_name"),
            "match_status": row.get("match_status"),
            "lines_ok": row.get("lines_ok"),
            "still_generic": row.get("still_generic"),
            "line_source": line_source,
            "n_lines": len(lines),
            "lines_sum": lines_sum,
            "notes": row.get("notes") or "",
            "pi_driven": bool(row.get("pi_driven")),
        }
        page_rows.append(page_rec)

        bundle_lines = []
        for idx, ln in enumerate(lines, 1):
            if not isinstance(ln, dict):
                continue
            line_rec = {
                "pdf_file": pdf,
                "page": page,
                "line_no": idx,
                "bill_no": bill,
                "supplier": supplier,
                "erp_pi_name": row.get("erp_pi_name"),
                "match_status": row.get("match_status"),
                "line_source": line_source,
                "item_name": ln.get("item_name") or "",
                "description": ln.get("description") or "",
                "qty": ln.get("qty"),
                "uom": ln.get("uom") or "Nos",
                "rate": ln.get("rate"),
                "amount": ln.get("amount"),
                "gst_hsn_code": ln.get("gst_hsn_code") or "",
                "erp_grand_total": row.get("erp_grand_total"),
                "erp_net_total": row.get("erp_net_total"),
            }
            line_rows.append(line_rec)
            bundle_lines.append({
                "item_name": line_rec["item_name"],
                "description": line_rec["description"],
                "qty": line_rec["qty"],
                "uom": line_rec["uom"],
                "rate": line_rec["rate"],
                "amount": line_rec["amount"],
                "gst_hsn_code": line_rec["gst_hsn_code"],
            })

        bundle_pages.append({
            **{k: page_rec[k] for k in (
                "pdf_file", "page", "bill_no", "supplier", "ocr_date",
                "grand_total", "net_total", "erp_pi_name", "match_status",
                "lines_ok", "line_source", "engine", "model",
            )},
            "lines": bundle_lines,
        })

    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    page_fields = list(page_rows[0].keys()) if page_rows else [
        "pdf_file", "page", "bill_no", "n_lines", "match_status",
    ]
    with EXTRACT_PAGES_CSV.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=page_fields, extrasaction="ignore")
        w.writeheader()
        w.writerows(page_rows)

    line_fields = list(line_rows[0].keys()) if line_rows else [
        "pdf_file", "page", "line_no", "item_name", "qty", "rate", "amount",
    ]
    with EXTRACT_LINES_CSV.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=line_fields, extrasaction="ignore")
        w.writeheader()
        w.writerows(line_rows)

    bundle = {
        "exported_at": datetime.now().isoformat(timespec="seconds"),
        "source_staging": str(src),
        "pages": len(bundle_pages),
        "lines": len(line_rows),
        "pages_with_lines": sum(1 for p in page_rows if (p.get("n_lines") or 0) > 0),
        "invoices": bundle_pages,
    }
    EXTRACT_BUNDLE_JSON.write_text(
        json.dumps(bundle, indent=2, default=str), encoding="utf-8"
    )

    summary = {
        "pages_csv": str(EXTRACT_PAGES_CSV),
        "lines_csv": str(EXTRACT_LINES_CSV),
        "bundle_json": str(EXTRACT_BUNDLE_JSON),
        "pages": len(page_rows),
        "lines": len(line_rows),
        "pages_with_lines": bundle["pages_with_lines"],
        "by_line_source": {},
    }
    from collections import Counter
    summary["by_line_source"] = dict(Counter(p.get("line_source") or "(none)" for p in page_rows))
    print(f"  export: {summary}")
    return summary
