"""OCR helpers for Purchase Invoice remaster from Migration PDF scans."""
from __future__ import annotations

import hashlib
import re
from datetime import datetime
from pathlib import Path
from typing import Any

from .config import DATA_DIR

MIGRATION_DIR = DATA_DIR / "Migration"
REPORTS_DIR = DATA_DIR / "reports"
OCR_CACHE_DIR = REPORTS_DIR / "pi_ocr_cache"
PAGES_DIR = REPORTS_DIR / "pi_ocr_pages"

# Common Tesseract install paths on Windows
_TESS_CANDIDATES = [
    r"C:\Program Files\Tesseract-OCR\tesseract.exe",
    r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe",
]


def _find_tesseract() -> str | None:
    import shutil
    found = shutil.which("tesseract")
    if found:
        return found
    for p in _TESS_CANDIDATES:
        if Path(p).exists():
            return p
    return None


def ensure_tesseract() -> str:
    """Return path to tesseract.exe; configure pytesseract. Raises if missing."""
    import pytesseract

    path = _find_tesseract()
    if not path:
        raise RuntimeError(
            "Tesseract OCR not found. Install UB-Mannheim.TesseractOCR "
            "(winget install UB-Mannheim.TesseractOCR) and retry."
        )
    pytesseract.pytesseract.tesseract_cmd = path
    return path


def purchase_pdfs() -> list[Path]:
    return sorted(MIGRATION_DIR.glob("*Purchase*.pdf"))


def _cache_key(pdf_name: str, page: int, zoom: float) -> str:
    raw = f"{pdf_name}|{page}|{zoom}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


def render_page(pdf_path: Path, page: int, zoom: float = 2.0) -> Path:
    """Render 1-based page to PNG; return path."""
    import fitz

    PAGES_DIR.mkdir(parents=True, exist_ok=True)
    stem = re.sub(r"[^\w]+", "_", pdf_path.stem)[:40]
    out = PAGES_DIR / f"{stem}_p{page:03d}.png"
    if out.exists() and out.stat().st_size > 0:
        return out
    doc = fitz.open(pdf_path)
    try:
        i = page - 1
        if i < 0 or i >= doc.page_count:
            raise ValueError(f"page {page} out of range for {pdf_path.name}")
        pix = doc[i].get_pixmap(matrix=fitz.Matrix(zoom, zoom), alpha=False)
        pix.save(str(out))
    finally:
        doc.close()
    return out


def ocr_image(png_path: Path, *, force: bool = False) -> str:
    """OCR a PNG; cache raw text under pi_ocr_cache/."""
    import pytesseract
    from PIL import Image

    ensure_tesseract()
    OCR_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cache = OCR_CACHE_DIR / f"{png_path.stem}.txt"
    if cache.exists() and not force and cache.stat().st_size > 0:
        return cache.read_text(encoding="utf-8", errors="replace")
    img = Image.open(png_path)
    # Improve contrast for scans
    text = pytesseract.image_to_string(img, lang="eng", config="--psm 6")
    cache.write_text(text, encoding="utf-8")
    return text


def _norm_bill(s: str | None) -> str:
    if not s:
        return ""
    s = s.upper().strip()
    s = re.sub(r"\s+", "", s)
    s = s.replace("–", "-").replace("—", "-")
    return s


def _parse_amount(token: str) -> float | None:
    t = token.strip().replace(",", "")
    t = re.sub(r"[^\d.]", "", t)
    if not t or t.count(".") > 1:
        return None
    try:
        return float(t)
    except ValueError:
        return None


def _parse_date(text: str) -> str | None:
    patterns = [
        (r"(\d{1,2}[-/]\d{1,2}[-/]\d{2,4})", ["%d-%m-%Y", "%d/%m/%Y", "%d-%m-%y", "%d/%m/%y"]),
        (r"(\d{1,2}\s+[A-Za-z]{3,9}\s+\d{2,4})", ["%d %B %Y", "%d %b %Y", "%d %B %y", "%d %b %y"]),
    ]
    for pat, fmts in patterns:
        for m in re.finditer(pat, text):
            raw = m.group(1)
            for fmt in fmts:
                try:
                    dt = datetime.strptime(raw.replace("/", "-") if "%" in fmt and "-" in fmt
                                           else raw, fmt.replace("/", "-") if "/" not in raw else fmt)
                    # fix 2-digit year parse side effects
                    if dt.year < 100:
                        dt = dt.replace(year=2000 + dt.year)
                    if 2020 <= dt.year <= 2030:
                        return dt.strftime("%Y-%m-%d")
                except ValueError:
                    continue
    return None


def parse_invoice_text(text: str) -> dict[str, Any]:
    """Heuristic parse of Indian GST tax invoice OCR text."""
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    joined = "\n".join(lines)

    bill_no = None
    bill_patterns = [
        r"(?:Invoice\s*(?:No|Number|#)|Inv\.?\s*No\.?|Bill\s*No\.?|"
        r"Tax\s*Invoice\s*No\.?)\s*[:.\-]?\s*([A-Za-z0-9][A-Za-z0-9/_\-]{2,40})",
        r"(?:INVOICE\s*No\.?)\s*([A-Za-z0-9][A-Za-z0-9/_\-]{2,40})",
    ]
    for pat in bill_patterns:
        m = re.search(pat, joined, re.I)
        if m:
            cand = m.group(1).strip().rstrip(".,")
            if cand.lower() not in ("dated", "date", "no", "number"):
                bill_no = cand
                break

    # Standalone Indian-style bill refs often OCR'd without a label
    if not bill_no:
        ref_pats = [
            r"(?<![A-Za-z0-9])([A-Z$58]{1,8}/\d{2,6}/\d{2}-\d{2})(?![A-Za-z0-9])",
            r"\b([A-Z]{2,6}-\d{4}-\d{2}-\d{1,4})\b",
            r"\b([A-Z]{2,6}/\d{2,4}/\d{3,6})\b",
            r"\b([A-Z]{1,6}-\d{4}-\d{2}-\d{1,5})\b",
        ]
        for pat in ref_pats:
            hits = re.findall(pat, joined, re.I)
            for h in hits:
                if re.search(r"\d", h) and ("/" in h or "-" in h):
                    bill_no = h
                    break
            if bill_no:
                break
    # OCR often reads S as $ or 8 — normalize common SB/ prefix mangling
    if bill_no:
        bill_no = re.sub(r"^\$8/", "SB/", bill_no, flags=re.I)
        bill_no = re.sub(r"^S8/", "SB/", bill_no, flags=re.I)
        bill_no = re.sub(r"^58/", "SB/", bill_no, flags=re.I)
        bill_no = re.sub(r"^\$B/", "SB/", bill_no, flags=re.I)
        bill_no = re.sub(r"^8/", "SB/", bill_no)  # $ stripped by OCR

    # Supplier: first substantial line that isn't "Tax Invoice" / address noise
    supplier = None
    skip_re = re.compile(
        r"tax\s*invoice|original|duplicate|gstin|state\s*code|page\s*\d|"
        r"purchase|buyer|consignee|ship\s*to|bill\s*to|invoice\s*no|"
        r"dated|irn|ack|e-?invoice|mobile|phone|pipe\s*line|road",
        re.I,
    )
    for ln in lines[:25]:
        if len(ln) < 4 or skip_re.search(ln):
            continue
        if re.match(r"^[\d\s/.\-|$]+$", ln):
            continue
        if re.search(r"\d{2}[A-Z]{5}\d{4}[A-Z]", ln, re.I):  # GSTIN-ish
            continue
        supplier = ln[:140]
        break

    ocr_date = _parse_date(joined)

    grand_total = None
    total_patterns = [
        r"(?:Grand\s*Total|Invoice\s*Total|Total\s*Amount|"
        r"Amount\s*Payable|Net\s*Payable|Total\s*Invoice\s*Value)"
        r"[^\d]{0,40}(?:Rs\.?|INR|₹)?\s*([\d,]+\.?\d*)",
        r"(?:TOTAL)\s*[:.\-]?\s*(?:Rs\.?|INR|₹)?\s*([\d,]+\.\d{2})",
        r"Grand\s*Total\s*Rs\.?\s*:?\s*([\d,]+\.?\d*)",
    ]
    for pat in total_patterns:
        for m in re.finditer(pat, joined, re.I):
            amt = _parse_amount(m.group(1))
            if amt is not None and amt > 0:
                grand_total = amt
        if grand_total:
            break
    if grand_total is None:
        amounts = []
        for m in re.finditer(r"([\d,]+\.\d{2})", joined):
            a = _parse_amount(m.group(1))
            if a and a >= 100:
                amounts.append(a)
        if amounts:
            grand_total = max(amounts)

    ocr_lines = _parse_line_items(lines)

    return {
        "ocr_bill_no": bill_no,
        "ocr_supplier": supplier,
        "ocr_date": ocr_date,
        "ocr_grand_total": grand_total,
        "ocr_lines": ocr_lines,
        "ocr_text_preview": joined[:1500],
    }


def _parse_line_items(lines: list[str]) -> list[dict]:
    """Best-effort line items from noisy OCR rows."""
    items: list[dict] = []
    skip_re = re.compile(
        r"cgst|sgst|igst|taxable|round\s*off|gst\s*in|output\s*[cs]|"
        r"freight|packing\s*charges|discount|grand\s*total|"
        r"description of|quantity|invoice|gstin|state\s*name",
        re.I,
    )
    header_re = re.compile(
        r"^(sl\.?|sno|hsn|description|qty|rate|amount|particular)\b",
        re.I,
    )

    def _add(name, qty, rate, amount, uom="", hsn=""):
        if qty is None or rate is None or amount is None:
            return
        if qty <= 0 or amount <= 0 or rate <= 0:
            return
        # Prefer integer qty from amount/rate when OCR amount drifted
        ratio = amount / rate
        if abs(ratio - round(ratio)) < 0.2 and round(ratio) >= 1:
            qty = float(round(ratio))
            amount = qty * rate
        elif abs(qty * rate - amount) > max(5.0, amount * 0.08):
            alt = round(amount / rate, 3)
            if abs(alt * rate - amount) <= max(1.0, amount * 0.02) and alt > 0:
                qty = alt
            else:
                return
        name = re.sub(r"\s+", " ", str(name)).strip(" .-|\t>[]=")
        name = re.sub(r"^\d+\s*", "", name)
        name = re.sub(r"\s+\d+@\s*$", "", name).strip()
        if len(name) < 3 or skip_re.search(name):
            return
        if re.search(r"total|output\s*[cs]gst|^\d+\.\d+\s*$", name, re.I):
            return
        if amount < 50:
            return
        if qty < 0.5:
            return
        items.append({
            "item_name": name[:140],
            "description": "",
            "qty": float(qty),
            "uom": (uom or "Nos").strip(" .") or "Nos",
            "rate": float(rate),
            "amount": float(amount),
            "gst_hsn_code": (hsn or "")[:8],
        })

    for ln in lines:
        if header_re.search(ln) or len(ln) < 10 or skip_re.search(ln):
            continue
        clean = ln.replace(")", " ").replace("(", " ").replace(",", "")
        clean = re.sub(r"[|]+", " ", clean)
        clean = re.sub(r"\s+", " ", clean).strip()

        hsn = ""
        hm = re.search(r"\b(\d{6,8})\b", clean)
        if hm:
            hsn = hm.group(1)

        money = [(m.start(), float(m.group(1))) for m in re.finditer(r"(\d+\.\d{2})\b", clean)]
        if len(money) < 2:
            continue
        amount = money[-1][1]
        rate = money[-2][1]
        before_rate = clean[: money[-2][0]]
        qty_matches = list(re.finditer(
            r"(\d+(?:\.\d+)?)\s*([A-Za-z.]{0,10})\s*$", before_rate.rstrip()
        ))
        qty, uom = None, "Nos"
        if qty_matches:
            qty = float(qty_matches[-1].group(1))
            uom = qty_matches[-1].group(2) or "Nos"
        else:
            nums = list(re.finditer(r"\b(\d+(?:\.\d+)?)\b", before_rate))
            for nm in reversed(nums):
                if hsn and nm.group(1) == hsn:
                    continue
                val = float(nm.group(1))
                if val == rate:
                    continue
                qty = val
                break
        if qty is None:
            continue

        name_end = hm.start() if hm else (
            qty_matches[-1].start() if qty_matches else money[-2][0]
        )
        name = clean[:name_end].strip(" -|/.")
        name = re.sub(
            r"\b(Rolls?|Nos|Mtrs?|Meters?|Kg|pcs)\b\s*$", "", name, flags=re.I
        ).strip(" -|/")
        _add(name, qty, rate, amount, uom, hsn)

    seen = set()
    uniq = []
    for it in items:
        key = (it["item_name"][:40].lower(), it["qty"], round(it["amount"], 2))
        if key in seen:
            continue
        seen.add(key)
        uniq.append(it)
    return uniq


def extract_page(pdf_path: Path, page: int, *, force: bool = False, zoom: float = 2.0) -> dict[str, Any]:
    png = render_page(pdf_path, page, zoom=zoom)
    text = ocr_image(png, force=force)
    parsed = parse_invoice_text(text)
    return {
        "pdf_file": pdf_path.name,
        "page": page,
        "png_path": str(png),
        "ocr_bill_no": parsed["ocr_bill_no"],
        "ocr_supplier": parsed["ocr_supplier"],
        "ocr_date": parsed["ocr_date"],
        "ocr_grand_total": parsed["ocr_grand_total"],
        "ocr_lines": parsed["ocr_lines"],
        "ocr_text_preview": parsed["ocr_text_preview"],
        "bill_no_norm": _norm_bill(parsed["ocr_bill_no"]),
    }


def extract_all_purchase_pdfs(
    *, force: bool = False, limit_pages: int = 0, progress=None
) -> list[dict]:
    """OCR every page of every *Purchase*.pdf under Migration."""
    ensure_tesseract()
    rows: list[dict] = []
    pdfs = purchase_pdfs()
    # Pre-count pages
    import fitz
    jobs: list[tuple[Path, int]] = []
    for pdf in pdfs:
        doc = fitz.open(pdf)
        n = doc.page_count
        doc.close()
        for p in range(1, n + 1):
            jobs.append((pdf, p))
            if limit_pages and len(jobs) >= limit_pages:
                break
        if limit_pages and len(jobs) >= limit_pages:
            break

    total = len(jobs)
    for i, (pdf, page) in enumerate(jobs, 1):
        try:
            row = extract_page(pdf, page, force=force)
            rows.append(row)
        except Exception as exc:
            rows.append({
                "pdf_file": pdf.name,
                "page": page,
                "error": f"{type(exc).__name__}: {exc}",
                "ocr_lines": [],
            })
        if progress:
            progress(i, total, pdf.name, page)
    return rows
