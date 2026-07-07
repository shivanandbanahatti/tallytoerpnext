"""Extract Tally masters and vouchers into the SQLite staging store.

Masters are exported as whole collections. Vouchers are exported **month by
month** across the configured date window: each chunk is a small, resilient
request, parsed, staged, and counted -- so a failure in one month never loses
the rest, and per-chunk counts feed the completeness check.
"""
from __future__ import annotations

import datetime as dt
from xml.etree import ElementTree as ET

from .staging import Staging
from .tally_client import TallyClient


def _text(el: ET.Element, tag: str, default: str = "") -> str:
    v = el.findtext(tag)
    return v.strip() if v else default


def _norm(name: str) -> str:
    """Collapse internal/leading/trailing whitespace in a Tally master name."""
    return " ".join((name or "").split())


def _elem_to_dict(el: ET.Element):
    """Recursively convert a Tally element into a JSON-friendly structure.

    * Leaf elements (no children) collapse to their stripped text -- attributes
      such as ``TYPE="Amount"`` are intentionally dropped so values like AMOUNT
      stay plain numeric strings rather than ``{'#text': ...}`` wrappers.
    * Container elements become dicts; repeated child tags (Tally's ``*.LIST``
      members) are gathered into lists so nothing is dropped.
    """
    if len(el) == 0:
        return (el.text or "").strip()
    node: dict = {}
    for child in el:
        cd = _elem_to_dict(child)
        if child.tag in node:
            if not isinstance(node[child.tag], list):
                node[child.tag] = [node[child.tag]]
            node[child.tag].append(cd)
        else:
            node[child.tag] = cd
    return node


# ---------------------------------------------------------------------------
# Master extraction
# ---------------------------------------------------------------------------
_MASTER_SPECS = [
    # (kind, collection type, scalar methods captured for quick columns)
    ("unit",       "Unit",          ["Name", "BaseUnits", "GUID"]),
    ("godown",     "Godown",        ["Name", "Parent", "GUID"]),
    ("costcategory", "CostCategory", ["Name", "GUID"]),
    ("costcentre", "CostCentre",    ["Name", "Parent", "Category", "GUID"]),
    ("group",      "Group",         ["Name", "Parent", "IsRevenue", "IsDeemedPositive", "GUID"]),
    ("stockgroup", "StockGroup",    ["Name", "Parent", "GUID"]),
    ("stockitem",  "StockItem",     ["Name", "Parent", "BaseUnits", "GUID", "OpeningBalance", "OpeningValue"]),
    ("ledger",     "Ledger",        ["Name", "Parent", "OpeningBalance", "GUID",
                                      "GSTRegistrationType", "PartyGSTIN", "LedgerPhone",
                                      "Email", "LedgerContact", "Address", "PinCode",
                                      "CountryName", "LedgerStateName", "BillCreditPeriod"]),
    ("vouchertype", "VoucherType",  ["Name", "Parent", "GUID"]),
]


def extract_masters(client: TallyClient, store: Staging) -> dict[str, int]:
    results: dict[str, int] = {}
    for kind, ctype, methods in _MASTER_SPECS:
        root = client.export_collection(
            f"masters_{kind}", ctype, methods=methods, save_as=f"master_{kind}")
        tag = ctype.upper()
        n = 0
        with store.tx():
            for el in root.findall(f".//{tag}"):
                name = _norm(el.get("NAME") or _text(el, "NAME"))
                if not name:
                    continue
                guid = _text(el, "GUID") or f"{kind}:{name}"  # synth key if no GUID
                payload = _elem_to_dict(el)
                payload["NAME"] = name
                parent = _norm(_text(el, "PARENT")) or None
                store.upsert_master(kind, guid, name, parent, payload)
                n += 1
        results[kind] = n
    return results


# ---------------------------------------------------------------------------
# Voucher extraction (month-chunked)
# ---------------------------------------------------------------------------
def _month_windows(from_yyyymmdd: str, to_yyyymmdd: str):
    start = dt.datetime.strptime(from_yyyymmdd, "%Y%m%d").date()
    end = dt.datetime.strptime(to_yyyymmdd, "%Y%m%d").date()
    # Clamp absurd upper bound to today to avoid thousands of empty months.
    end = min(end, dt.date.today())
    cur = start.replace(day=1)
    while cur <= end:
        nxt = (cur.replace(day=28) + dt.timedelta(days=4)).replace(day=1)
        win_end = min(nxt - dt.timedelta(days=1), end)
        yield cur.strftime("%Y%m%d"), win_end.strftime("%Y%m%d")
        cur = nxt


def _parse_voucher_date(raw: str) -> str | None:
    raw = (raw or "").strip()
    if len(raw) == 8 and raw.isdigit():
        return f"{raw[0:4]}-{raw[4:6]}-{raw[6:8]}"
    return None


def _voucher_total_debit(v: ET.Element) -> float:
    """Sum of absolute debit amounts across ledger entries (= voucher value)."""
    total = 0.0
    for le in v.findall("ALLLEDGERENTRIES.LIST") + v.findall("LEDGERENTRIES.LIST"):
        amt = _text(le, "AMOUNT")
        dpos = _text(le, "ISDEEMEDPOSITIVE")
        try:
            val = float(amt)
        except ValueError:
            continue
        if val < 0:  # negative AMOUNT = debit side
            total += abs(val)
    return round(total, 2)


# Header fields + AllLedgerEntries (which carries amounts, Dr/Cr flag and bill
# allocations). The Voucher *collection* honours SVFROMDATE/SVTODATE (the Day
# Book report does not), so we drive extraction with a per-month collection.
_VCH_FETCH = [
    "Date", "VoucherTypeName", "VoucherNumber", "PartyLedgerName", "PartyName",
    "Narration", "Reference", "ReferenceDate", "GUID", "MasterId", "AlterId",
    "PlaceOfSupply", "StateName", "PartyGSTIN", "ISCANCELLED", "ISDELETED",
    "AllLedgerEntries",
]


def extract_vouchers(client: TallyClient, store: Staging,
                     progress=lambda *a: None) -> dict[str, int]:
    from_d = str(client.from_date) or "20000101"
    to_d = str(client.to_date) or dt.date.today().strftime("%Y%m%d")
    total = 0
    per_type: dict[str, int] = {}
    for win_from, win_to in _month_windows(from_d, to_d):
        client.from_date, client.to_date = win_from, win_to
        root = client.export_collection(
            f"vch_{win_from}", "Voucher", fetch=_VCH_FETCH,
            dated=True, save_as=f"vch_{win_from[:6]}")
        vch = root.findall(".//VOUCHER")
        if not vch:
            progress(win_from, win_to, 0)
            continue
        with store.tx():
            for idx, v in enumerate(vch):
                # Skip cancelled/deleted vouchers; keep optional ones flagged.
                if _text(v, "ISCANCELLED") == "Yes" or _text(v, "ISDELETED") == "Yes":
                    continue
                entries = v.findall("ALLLEDGERENTRIES.LIST") + v.findall("LEDGERENTRIES.LIST")
                guid = _text(v, "GUID")
                # Empty placeholder <VOUCHER> elements (no guid, no postings): skip.
                if not guid and not entries:
                    continue
                vtype = _text(v, "VOUCHERTYPENAME") or v.get("VCHTYPE") or "Unknown"
                vnum = _text(v, "VOUCHERNUMBER")
                if not guid:
                    guid = f"{vtype}:{vnum}:{_text(v,'DATE')}:{win_from}:{idx}"
                payload = _elem_to_dict(v)
                store.upsert_voucher(
                    guid, vtype, vnum,
                    _parse_voucher_date(_text(v, "DATE")),
                    _text(v, "PARTYLEDGERNAME") or _text(v, "PARTYNAME"),
                    _voucher_total_debit(v), payload)
                per_type[vtype] = per_type.get(vtype, 0) + 1
                total += 1
        progress(win_from, win_to, len(vch))
    per_type["_total"] = total
    return per_type
