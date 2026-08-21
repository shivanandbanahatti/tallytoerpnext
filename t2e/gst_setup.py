"""Idempotent GST transaction categories and tax templates for ERPNext."""
from __future__ import annotations

from dataclasses import dataclass

from .erpnext_client import ERPNextClient
from .mapping import CompanyDefaults, acc_name


@dataclass(frozen=True)
class GSTSelection:
    tax_category: str
    template: str


def gst_rate_text(rate: float) -> str:
    """Format a GST percentage for stable ERPNext document names."""
    return f"{rate:g}"


def item_tax_template_name(rate: float, abbr: str) -> str:
    return f"GST {gst_rate_text(rate)}% - {abbr}"


def ensure_item_tax_template(
    erp: ERPNextClient, defaults: CompanyDefaults, rate: float
) -> str:
    """Ensure a taxable, rate-specific India Compliance item tax template."""
    name = item_tax_template_name(rate, defaults.abbr)
    if erp.exists("Item Tax Template", name):
        return name

    half = rate / 2
    rows = [
        ("Output Tax SGST", half),
        ("Output Tax CGST", half),
        ("Output Tax IGST", rate),
        ("Input Tax SGST", half),
        ("Input Tax CGST", half),
        ("Input Tax IGST", rate),
    ]
    erp.insert("Item Tax Template", {
        "title": f"GST {gst_rate_text(rate)}%",
        "company": defaults.name,
        "gst_treatment": "Taxable",
        "gst_rate": rate,
        "disabled": 0,
        "taxes": [{
            "tax_type": acc_name(account, defaults.abbr),
            "tax_rate": tax_rate,
        } for account, tax_rate in rows],
    })
    return name


def select_gst(kind: str, ledger_names: list[str],
               abbr: str) -> GSTSelection | None:
    joined = " ".join(ledger_names).upper()
    if not any(token in joined for token in ("CGST", "SGST", "IGST", "UTGST")):
        return None
    out_state = "IGST" in joined and not any(
        token in joined for token in ("CGST", "SGST", "UTGST")
    )
    category = "Out-State" if out_state else "In-State"
    prefix = "Output GST" if kind == "Customer" else "Input GST"
    geography = "Out-state" if out_state else "In-state"
    return GSTSelection(category, f"{prefix} {geography} - {abbr}")


def ensure_gst_setup(erp: ERPNextClient, defaults: CompanyDefaults) -> dict[str, str]:
    """Ensure the four standard non-RCM templates used by this migration."""
    result: dict[str, str] = {}
    for title in ("In-State", "Out-State"):
        if erp.exists("Tax Category", title):
            result[f"Tax Category/{title}"] = "exists"
        elif erp.dry_run:
            result[f"Tax Category/{title}"] = "would-create"
        else:
            erp.insert("Tax Category", {"title": title, "disabled": 0})
            result[f"Tax Category/{title}"] = "created"

    specs = [
        ("Purchase Taxes and Charges Template", "Input GST In-state", "In-State", [
            ("Input Tax CGST", "CGST", 9),
            ("Input Tax SGST", "SGST", 9),
        ]),
        ("Purchase Taxes and Charges Template", "Input GST Out-state", "Out-State", [
            ("Input Tax IGST", "IGST", 18),
        ]),
        ("Sales Taxes and Charges Template", "Output GST In-state", "In-State", [
            ("Output Tax SGST", "SGST", 9),
            ("Output Tax CGST", "CGST", 9),
        ]),
        ("Sales Taxes and Charges Template", "Output GST Out-state", "Out-State", [
            ("Output Tax IGST", "IGST", 18),
        ]),
    ]
    for doctype, title, category, rows in specs:
        full_name = f"{title} - {defaults.abbr}"
        key = f"{doctype}/{full_name}"
        if erp.exists(doctype, full_name):
            result[key] = "exists"
            continue
        if erp.dry_run:
            result[key] = "would-create"
            continue
        erp.insert(doctype, {
            "title": title,
            "company": defaults.name,
            "tax_category": category,
            "disabled": 0,
            "taxes": [{
                "charge_type": "On Net Total",
                "account_head": acc_name(account, defaults.abbr),
                "description": description,
                "rate": rate,
                "cost_center": defaults.cost_center,
            } for account, description, rate in rows],
        })
        result[key] = "created"
    return result
