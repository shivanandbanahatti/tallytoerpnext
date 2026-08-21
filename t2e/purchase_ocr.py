"""Match durable OCR extracts to Tally Purchase vouchers before ERPNext load."""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any

from .config import DATA_DIR

DEFAULT_BUNDLE = DATA_DIR / "reports" / "pi_extract_bundle.json"
AMOUNT_TOLERANCE = Decimal("0.05")
MAX_LINE_NUDGE = Decimal("5.00")


def normalize_bill_no(value: str | None) -> str:
    """Normalize formatting only; do not guess OCR character substitutions."""
    return re.sub(r"[^A-Z0-9]", "", str(value or "").upper())


def normalize_party(value: str | None) -> set[str]:
    noise = {"M", "S", "MS", "PVT", "PRIVATE", "LTD", "LIMITED", "LLP", "THE"}
    return {
        token for token in re.findall(r"[A-Z0-9]+", str(value or "").upper())
        if len(token) > 1 and token not in noise
    }


def _amount(value: Any) -> Decimal:
    return Decimal(str(value or 0)).quantize(Decimal("0.01"))


def _line_total(row: dict) -> Decimal:
    return sum((_amount(line.get("amount")) for line in row.get("lines") or []),
               Decimal("0.00"))


@dataclass(frozen=True)
class OCRMatch:
    bill_no: str
    supplier: str
    lines: tuple[dict, ...]
    source_file: str
    source_page: int


class PurchaseOCRCatalog:
    def __init__(self, rows: list[dict] | None = None) -> None:
        self.by_bill: dict[str, list[dict]] = {}
        for row in rows or []:
            key = normalize_bill_no(row.get("bill_no"))
            if not key or not row.get("lines") or row.get("lines_ok") is False:
                continue
            self.by_bill.setdefault(key, []).append(row)

    @classmethod
    def from_path(cls, path: Path = DEFAULT_BUNDLE) -> "PurchaseOCRCatalog":
        if not path.exists():
            return cls()
        raw = json.loads(path.read_text(encoding="utf-8"))
        rows = raw.get("invoices") if isinstance(raw, dict) else raw
        return cls(rows if isinstance(rows, list) else [])

    def match(self, bill_no: str | None, net_total: Decimal | float,
              supplier: str | None = None, posting_date: str | None = None
              ) -> OCRMatch | None:
        """Return one exact bill-number and amount match; ambiguity is rejected."""
        candidates = list(self.by_bill.get(normalize_bill_no(bill_no), []))
        expected = _amount(net_total)

        wanted_party = normalize_party(supplier)
        if wanted_party:
            party_matches = [
                row for row in candidates
                if wanted_party & normalize_party(row.get("supplier"))
            ]
            if party_matches:
                candidates = party_matches

        candidates = [
            row for row in candidates
            if abs(_line_total(row) - expected) <= MAX_LINE_NUDGE
        ]
        if not candidates:
            return None

        if len(candidates) > 1 and posting_date:
            date_matches = [
                row for row in candidates
                if not row.get("ocr_date") or row.get("ocr_date") == posting_date
            ]
            if date_matches:
                candidates = date_matches

        # Never choose arbitrarily when repeated supplier invoice numbers remain.
        unique = {
            (row.get("pdf_file"), int(row.get("page") or 0)): row
            for row in candidates
        }
        if len(unique) != 1:
            return None
        row = next(iter(unique.values()))
        lines = [dict(line) for line in row["lines"]]
        delta = expected - _line_total(row)
        if abs(delta) > AMOUNT_TOLERANCE:
            # OCR rates commonly drift by a few paise. Keep every extracted
            # quantity/item and reconcile the final line to Tally's net total.
            lines[-1]["amount"] = float(_amount(lines[-1].get("amount")) + delta)
        return OCRMatch(
            bill_no=str(row.get("bill_no") or bill_no or ""),
            supplier=str(row.get("supplier") or ""),
            lines=tuple(lines),
            source_file=str(row.get("pdf_file") or ""),
            source_page=int(row.get("page") or 0),
        )
