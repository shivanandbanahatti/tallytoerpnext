"""GPT-4o vision extraction for scanned Purchase Invoice PDF pages."""
from __future__ import annotations

import base64
import json
import re
from pathlib import Path
from typing import Any

from dotenv import dotenv_values

from .config import ROOT, DATA_DIR
from .pi_ocr import MIGRATION_DIR, REPORTS_DIR, render_page, _norm_bill

VISION_CACHE_DIR = REPORTS_DIR / "pi_vision_cache"
TRIAL_OUT = REPORTS_DIR / "pi_vision_trial.json"

EXTRACT_SCHEMA_HINT = """
Return ONLY valid JSON (no markdown) with this shape:
{
  "ocr_bill_no": "string or null — supplier invoice / bill number",
  "ocr_supplier": "string or null — seller/supplier name",
  "ocr_date": "YYYY-MM-DD or null",
  "ocr_grand_total": number or null — final payable including tax,
  "ocr_net_total": number or null — taxable / net before tax if shown,
  "ocr_lines": [
    {
      "item_name": "string",
      "description": "string",
      "qty": number,
      "uom": "string",
      "rate": number,
      "amount": number,
      "gst_hsn_code": "string"
    }
  ],
  "notes": "string — any uncertainty"
}
Rules:
- Prefer the supplier invoice number, not e-way / IRN / order / DC numbers.
- Read invoice numbers carefully (VI vs VII, 0105 vs 0106).
- Include EVERY goods line; exclude CGST/SGST/IGST/round-off/freight-tax-only rows.
- Line amounts MUST be pre-tax (taxable value). Never put GST-inclusive amounts on lines.
- sum(ocr_lines.amount) must equal ocr_net_total (taxable), NOT ocr_grand_total.
- qty * rate should approximately equal amount.
- Use null when unknown; do not invent lines.
""".strip()


def get_openai_api_key() -> str:
    """Read OPENAI_API_KEY from env or .env.erpnext (unprefixed shared secret)."""
    import os
    key = (os.environ.get("OPENAI_API_KEY") or "").strip()
    if key:
        return key
    raw = dotenv_values(ROOT / ".env.erpnext")
    key = (raw.get("OPENAI_API_KEY") or "").strip()
    if key:
        return key
    raise RuntimeError(
        "OPENAI_API_KEY not set. Add it to .env.erpnext (unprefixed) or export it."
    )


def _png_data_url(png_path: Path) -> str:
    data = png_path.read_bytes()
    b64 = base64.standard_b64encode(data).decode("ascii")
    return f"data:image/png;base64,{b64}"


def _openai_client():
    """OpenAI client; respects OPENAI_INSECURE_SSL=1 for broken corporate CAs."""
    import os
    from openai import OpenAI

    key = get_openai_api_key()
    insecure = (
        os.environ.get("OPENAI_INSECURE_SSL")
        or dotenv_values(ROOT / ".env.erpnext").get("OPENAI_INSECURE_SSL")
        or ""
    ).strip() in ("1", "true", "True", "yes", "YES")
    if not insecure:
        return OpenAI(api_key=key)
    import httpx
    return OpenAI(api_key=key, http_client=httpx.Client(verify=False))


def extract_page_vision(
    pdf_name: str,
    page: int,
    *,
    model: str = "gpt-4o",
    force: bool = False,
    zoom: float = 2.0,
    erp_hints: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Render one PDF page and extract invoice fields via GPT-4o vision."""
    pdf_path = MIGRATION_DIR / pdf_name
    if not pdf_path.exists():
        raise FileNotFoundError(pdf_path)

    VISION_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    stem = re.sub(r"[^\w]+", "_", Path(pdf_name).stem)[:40]
    hint_tag = ""
    if erp_hints and erp_hints.get("net_total") is not None:
        hint_tag = "_hint"
    cache = VISION_CACHE_DIR / f"{stem}_p{page:03d}{hint_tag}.json"
    if cache.exists() and not force and cache.stat().st_size > 0:
        cached = json.loads(cache.read_text(encoding="utf-8"))
        cached["from_cache"] = True
        return cached

    png = render_page(pdf_path, page, zoom=zoom)
    prompt = EXTRACT_SCHEMA_HINT
    if erp_hints:
        prompt += (
            "\n\nERPNext matched this page to a Purchase Invoice — use as ground truth:\n"
            f"- bill_no: {erp_hints.get('bill_no')}\n"
            f"- net_total (pre-tax, line amounts MUST sum to this): {erp_hints.get('net_total')}\n"
            f"- grand_total (incl. tax): {erp_hints.get('grand_total')}\n"
            "Extract every goods line so sum(amount) equals net_total."
        )
    client = _openai_client()
    resp = client.chat.completions.create(
        model=model,
        temperature=0,
        max_tokens=4096,
        messages=[
            {
                "role": "system",
                "content": (
                    "You extract structured data from Indian GST tax invoice images "
                    "for Spaceki Designs LLP (buyer). Be precise with numbers."
                ),
            },
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": _png_data_url(png),
                            "detail": "high",
                        },
                    },
                ],
            },
        ],
    )
    raw = (resp.choices[0].message.content or "").strip()
    parsed = _parse_model_json(raw)
    out = {
        "pdf_file": pdf_name,
        "page": page,
        "png_path": str(png),
        "engine": "openai",
        "model": model,
        "ocr_bill_no": parsed.get("ocr_bill_no"),
        "ocr_supplier": parsed.get("ocr_supplier"),
        "ocr_date": parsed.get("ocr_date"),
        "ocr_grand_total": parsed.get("ocr_grand_total"),
        "ocr_net_total": parsed.get("ocr_net_total"),
        "ocr_lines": parsed.get("ocr_lines") or [],
        "notes": parsed.get("notes") or "",
        "bill_no_norm": _norm_bill(parsed.get("ocr_bill_no")),
        "raw_response": raw[:4000],
        "usage": {
            "prompt_tokens": getattr(resp.usage, "prompt_tokens", None),
            "completion_tokens": getattr(resp.usage, "completion_tokens", None),
        },
    }
    # Normalize line numerics
    lines = []
    for ln in out["ocr_lines"]:
        if not isinstance(ln, dict):
            continue
        try:
            lines.append({
                "item_name": str(ln.get("item_name") or "")[:140],
                "description": str(ln.get("description") or ""),
                "qty": float(ln.get("qty") or 0),
                "uom": str(ln.get("uom") or "Nos"),
                "rate": float(ln.get("rate") or 0),
                "amount": float(ln.get("amount") or 0),
                "gst_hsn_code": str(ln.get("gst_hsn_code") or ""),
            })
        except (TypeError, ValueError):
            continue
    out["ocr_lines"] = lines
    out["lines_sum"] = round(sum(x["amount"] for x in lines), 2)

    cache.write_text(json.dumps(out, indent=2, default=str), encoding="utf-8")
    return out


def _parse_model_json(raw: str) -> dict[str, Any]:
    text = raw.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", text, re.S)
        if not m:
            raise
        blob = m.group(0)
        try:
            return json.loads(blob)
        except json.JSONDecodeError:
            # Common model glitch: unescaped quotes inside item_name strings
            repaired = re.sub(
                r'("(?:item_name|description)"\s*:\s*")(.*?)("\s*,)',
                lambda mo: mo.group(1)
                + mo.group(2).replace('"', "'")
                + mo.group(3),
                blob,
                flags=re.S,
            )
            return json.loads(repaired)


def trial_one_page(
    pdf_name: str = "April 2026_Purchase Invoices_1.pdf",
    page: int = 1,
    *,
    model: str = "gpt-4o",
    force: bool = True,
) -> Path:
    """Extract one page and write data/reports/pi_vision_trial.json."""
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    result = extract_page_vision(pdf_name, page, model=model, force=force)
    TRIAL_OUT.write_text(json.dumps(result, indent=2, default=str), encoding="utf-8")
    return TRIAL_OUT


def _vision_fields(result: dict[str, Any]) -> dict[str, Any]:
    """Fields to merge into a staging row from a vision extract."""
    return {
        "engine": "openai",
        "model": result.get("model"),
        "ocr_bill_no": result.get("ocr_bill_no"),
        "ocr_supplier": result.get("ocr_supplier"),
        "ocr_date": result.get("ocr_date"),
        "ocr_grand_total": result.get("ocr_grand_total"),
        "ocr_net_total": result.get("ocr_net_total"),
        "ocr_lines": result.get("ocr_lines") or [],
        "bill_no_norm": result.get("bill_no_norm") or _norm_bill(result.get("ocr_bill_no")),
        "lines_sum": result.get("lines_sum"),
        "vision_notes": result.get("notes") or "",
        "png_path": result.get("png_path"),
    }


def select_pages_needing_vision(
    staging_rows: list[dict],
    pi_totals: list[float],
    *,
    all_unmatched: bool = False,
) -> list[dict]:
    """Pages for GPT-4o: high without lines_ok, or unmatched (all or filtered)."""
    need = []
    seen = set()
    for r in staging_rows:
        key = (r.get("pdf_file"), r.get("page"))
        if key in seen:
            continue
        status = r.get("match_status")
        if status == "already_remastered":
            continue
        # Always retry high matches with bad/missing lines
        if status == "high" and not r.get("lines_ok") and r.get("still_generic", True):
            need.append(r)
            seen.add(key)
            continue
        if status not in (None, "unmatched", "medium"):
            continue
        if all_unmatched:
            need.append(r)
            seen.add(key)
            continue
        # Skip blank / failed pages
        preview = (r.get("ocr_text_preview") or "")
        tot = r.get("ocr_grand_total")
        if tot is None and len(preview) < 40:
            continue
        # Prefer pages whose OCR total is near some still-generic PI total
        if tot is not None and pi_totals:
            if any(abs(float(tot) - t) <= 2.0 for t in pi_totals):
                need.append(r)
                seen.add(key)
                continue
        # Or pages that already have a bill-looking number
        bill = (r.get("ocr_bill_no") or "").strip()
        if bill and bill.lower() not in ("dated", "date", "e-way", "eway", "e-viay"):
            if len(preview) >= 80 or tot is not None:
                need.append(r)
                seen.add(key)
    return need


def extract_vision_needed(
    *,
    model: str = "gpt-4o",
    force: bool = False,
    limit: int = 0,
    all_unmatched: bool = False,
    progress=None,
) -> dict[str, Any]:
    """Run GPT-4o on staging pages that still need better extraction; merge into staging JSON."""
    from .pi_remaster import (
        BATCH_STAGING_JSON, BATCH_STAGING_CSV, _load_dev_pis,
    )
    import csv

    if not BATCH_STAGING_JSON.exists():
        raise FileNotFoundError(
            f"missing {BATCH_STAGING_JSON} — run pi-remaster extract (tesseract) first"
        )
    rows = json.loads(BATCH_STAGING_JSON.read_text(encoding="utf-8"))
    pis = _load_dev_pis()
    pi_totals = [p["grand_total"] for p in pis if p.get("still_generic")]
    targets = select_pages_needing_vision(rows, pi_totals, all_unmatched=all_unmatched)
    if limit:
        targets = targets[:limit]

    # Index staging rows by pdf+page
    index = {(r.get("pdf_file"), r.get("page")): i for i, r in enumerate(rows)}
    ok = fail = skipped_cache = 0
    for i, target in enumerate(targets, 1):
        pdf = target["pdf_file"]
        page = int(target["page"])
        if progress:
            progress(i, len(targets), pdf, page)
        else:
            print(f"  vision {i}/{len(targets)}  {pdf} p{page}", flush=True)
        try:
            # Re-call with ERPNext net/grand hints when already amount-matched but lines bad
            erp_hints = None
            force_page = force
            if (
                target.get("match_status") == "high"
                and not target.get("lines_ok")
                and target.get("erp_net_total") is not None
            ):
                erp_hints = {
                    "bill_no": target.get("erp_bill_no") or target.get("corrected_bill_no"),
                    "net_total": target.get("erp_net_total"),
                    "grand_total": target.get("erp_grand_total"),
                }
                force_page = True
            result = extract_page_vision(
                pdf, page, model=model, force=force_page, erp_hints=erp_hints,
            )
            if not force_page and result.get("from_cache"):
                skipped_cache += 1
            idx = index.get((pdf, page))
            if idx is None:
                continue
            merged = {**rows[idx], **_vision_fields(result)}
            # Clear prior match so rematch is clean
            for k in (
                "match_status", "erp_pi_name", "lines_ok", "corrected_lines",
                "still_generic", "notes",
            ):
                merged.pop(k, None)
            rows[idx] = merged
            ok += 1
            # Persist incrementally
            if i % 5 == 0 or i == len(targets):
                BATCH_STAGING_JSON.write_text(
                    json.dumps(rows, indent=2, default=str), encoding="utf-8"
                )
        except Exception as exc:
            fail += 1
            print(f"    FAIL {pdf} p{page}: {type(exc).__name__}: {exc}", flush=True)

    BATCH_STAGING_JSON.write_text(json.dumps(rows, indent=2, default=str), encoding="utf-8")
    with BATCH_STAGING_CSV.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(
            fh,
            fieldnames=[
                "pdf_file", "page", "engine", "ocr_bill_no", "ocr_supplier",
                "ocr_date", "ocr_grand_total", "ocr_net_total", "lines_sum",
                "n_lines", "match_status", "erp_pi_name", "notes",
            ],
            extrasaction="ignore",
        )
        w.writeheader()
        for r in rows:
            w.writerow({
                **r,
                "n_lines": len(r.get("ocr_lines") or []),
            })
    return {
        "targeted": len(targets),
        "ok": ok,
        "failed": fail,
        "staging": str(BATCH_STAGING_JSON),
    }


def _amount_text_patterns(amount: float) -> list[str]:
    """String forms of an amount to hunt in OCR text."""
    if amount is None:
        return []
    n = float(amount)
    whole = int(round(n))
    pats = {
        f"{n:.2f}",
        f"{n:.0f}",
        str(whole),
        f"{whole:,}",
        f"{n:,.2f}",
    }
    return [p for p in pats if p]


def _bill_search_tokens(bill: str | None) -> list[str]:
    """Tokens from a bill_no worth searching in OCR text."""
    if not bill:
        return []
    raw = str(bill).strip()
    norm = _norm_bill(raw)
    toks = [raw, norm]
    parts = re.split(r"[/\-_\s]+", norm)
    for p in parts:
        if len(p) < 5:
            continue
        # Skip calendar years and other ultra-common tokens
        if p.isdigit() and 1990 <= int(p) <= 2099:
            continue
        toks.append(p)
    if len(norm) >= 6:
        toks.append(norm)
    seen = set()
    out = []
    for t in toks:
        key = t.upper()
        if key and key not in seen:
            seen.add(key)
            out.append(t)
    return out


def _page_ocr_blob(row: dict[str, Any]) -> str:
    """Combine staging fields + on-disk Tesseract cache for search."""
    from .pi_ocr import OCR_CACHE_DIR

    parts = [
        str(row.get("ocr_text_preview") or ""),
        str(row.get("ocr_bill_no") or ""),
        str(row.get("ocr_supplier") or ""),
        str(row.get("ocr_grand_total") or ""),
        str(row.get("ocr_net_total") or ""),
    ]
    pdf = row.get("pdf_file") or ""
    page = row.get("page")
    if pdf and page:
        stem = re.sub(r"[^\w]+", "_", Path(pdf).stem)[:40]
        cache = OCR_CACHE_DIR / f"{stem}_p{int(page):03d}.txt"
        if cache.exists():
            try:
                parts.append(cache.read_text(encoding="utf-8", errors="ignore"))
            except OSError:
                pass
    return "\n".join(parts).upper()


def locate_pages_for_pis(
    staging_rows: list[dict],
    pis: list[dict],
    *,
    top_k: int = 2,
    min_score: float = 8.0,
) -> list[dict[str, Any]]:
    """For each still-generic PI, pick best staging page(s) by bill/amount/text hit."""
    from .pi_remaster import _bill_close, _supplier_fuzzy

    # Precompute blobs once
    blobs = []
    for r in staging_rows:
        if r.get("match_status") == "already_remastered":
            blobs.append("")
            continue
        blobs.append(_page_ocr_blob(r))

    assignments: list[dict[str, Any]] = []
    used_pages: set[tuple[str, int]] = set()

    # Score all (pi, page) pairs then greedy-assign best first
    scored: list[tuple[float, dict, dict, list[str]]] = []
    for pi in pis:
        if not pi.get("still_generic"):
            continue
        bill = pi.get("bill_no") or ""
        bill_norm = pi.get("bill_no_norm") or _norm_bill(bill)
        gt = float(pi["grand_total"])
        nt = float(pi["net_total"])
        bill_toks = _bill_search_tokens(bill)
        amt_pats = [p.upper() for p in _amount_text_patterns(gt) + _amount_text_patterns(nt)]

        for i, row in enumerate(staging_rows):
            if r_status := row.get("match_status"):
                if r_status == "already_remastered":
                    continue
            blob = blobs[i]
            why: list[str] = []
            score = 0.0

            ocr_bill = _norm_bill(row.get("ocr_bill_no") or "")
            amount_hit = False
            try:
                if ocr_g := row.get("ocr_grand_total"):
                    if abs(float(ocr_g) - gt) <= 1.0:
                        score += 12
                        why.append("grand_exact")
                        amount_hit = True
                    elif abs(float(ocr_g) - gt) <= max(5.0, gt * 0.005):
                        score += 6
                        why.append("grand_near")
                        amount_hit = True
                if ocr_n := row.get("ocr_net_total"):
                    if abs(float(ocr_n) - nt) <= 1.0:
                        score += 10
                        why.append("net_exact")
                        amount_hit = True
                    elif abs(float(ocr_n) - nt) <= max(5.0, nt * 0.005):
                        score += 5
                        why.append("net_near")
                        amount_hit = True
            except (TypeError, ValueError):
                pass

            if bill_norm and ocr_bill:
                if bill_norm == ocr_bill:
                    score += 10
                    why.append("bill_exact")
                elif _bill_close(bill_norm, ocr_bill):
                    # Weak alone — require amount confirmation for series-like bills
                    score += 8 if amount_hit else 2
                    why.append("bill_close")
                elif bill_norm in ocr_bill or ocr_bill in bill_norm:
                    score += 5 if amount_hit else 1
                    why.append("bill_substr")

            if blob:
                bill_in_text = False
                for tok in bill_toks:
                    if len(tok) >= 5 and tok.upper() in blob:
                        score += 4
                        why.append(f"text_bill:{tok}")
                        bill_in_text = True
                        break
                amt_in_text = False
                # Prefer longer/more distinctive amount patterns
                for pat in sorted(amt_pats, key=len, reverse=True):
                    if len(pat) >= 5 and pat in blob:
                        # Small round amounts are ambiguous unless bill also in text
                        digits = re.sub(r"\D", "", pat)
                        if len(digits) <= 4 and not (bill_in_text or amount_hit):
                            continue
                        score += 6 if bill_in_text else 3
                        why.append(f"text_amt:{pat}")
                        amt_in_text = True
                        break
                if bill_in_text and amt_in_text:
                    score += 4
                    why.append("text_bill+amt")

            supp = _supplier_fuzzy(row.get("ocr_supplier"), pi.get("supplier"))
            if supp >= 0.35:
                score += 2 + supp
                why.append(f"supplier={supp:.2f}")

            if score >= min_score and (
                amount_hit
                or "bill_exact" in why
                or "text_bill+amt" in why
                or any(w.startswith("unique_text_amt:") for w in why)
                or any(
                    w.startswith("text_amt:")
                    and len(re.sub(r"\D", "", w)) >= 5
                    for w in why
                )
            ):
                scored.append((score, pi, row, why))

    scored.sort(key=lambda x: (-x[0], x[1]["name"], x[2].get("page") or 0))

    # Second pass: unique distinctive amount-in-text (no ambiguous 4-digit nets)
    claimed_pis = {s[1]["name"] for s in scored}
    for pi in pis:
        if not pi.get("still_generic") or pi["name"] in claimed_pis:
            continue
        gt = float(pi["grand_total"])
        nt = float(pi["net_total"])
        pats = []
        for a in (gt, nt):
            for pat in _amount_text_patterns(a):
                digits = re.sub(r"\D", "", pat)
                if len(digits) >= 5:
                    pats.append(pat.upper())
        if not pats:
            continue
        page_hits: list[tuple[dict, str]] = []
        seen_pages: set[tuple[str, int]] = set()
        for i, row in enumerate(staging_rows):
            if row.get("match_status") == "already_remastered":
                continue
            blob = blobs[i]
            if not blob:
                continue
            key = (row.get("pdf_file"), int(row.get("page") or 0))
            for pat in pats:
                if pat in blob:
                    if key not in seen_pages:
                        page_hits.append((row, pat))
                        seen_pages.add(key)
                    break
        if len(page_hits) == 1:
            row, pat = page_hits[0]
            from .pi_remaster import _supplier_fuzzy
            supp = _supplier_fuzzy(row.get("ocr_supplier"), pi.get("supplier"))
            digits = re.sub(r"\D", "", pat)
            # Require strong supplier match, or 6+ digit distinctive amount
            if len(digits) >= 6 or supp >= 0.5:
                scored.append(
                    (9.0 + supp, pi, row, [f"unique_text_amt:{pat}", f"supplier={supp:.2f}"])
                )

    scored.sort(key=lambda x: (-x[0], x[1]["name"], x[2].get("page") or 0))
    per_pi_count: dict[str, int] = {}
    for score, pi, row, why in scored:
        key = (row.get("pdf_file"), int(row.get("page") or 0))
        if key in used_pages:
            continue
        name = pi["name"]
        if per_pi_count.get(name, 0) >= top_k:
            continue
        used_pages.add(key)
        per_pi_count[name] = per_pi_count.get(name, 0) + 1
        assignments.append({
            "erp_pi_name": name,
            "erp_bill_no": pi.get("bill_no"),
            "erp_grand_total": pi["grand_total"],
            "erp_net_total": pi["net_total"],
            "erp_supplier": pi.get("supplier"),
            "pdf_file": row.get("pdf_file"),
            "page": int(row.get("page") or 0),
            "score": score,
            "why": why,
        })
    return assignments


def extract_vision_for_pis(
    *,
    model: str = "gpt-4o",
    force: bool = True,
    limit: int = 0,
    top_k: int = 1,
    min_score: float = 8.0,
) -> dict[str, Any]:
    """PI-driven: locate best PDF page per still-generic PI, GPT-extract with ERPNext hints."""
    from .pi_remaster import BATCH_STAGING_JSON, BATCH_STAGING_CSV, _load_dev_pis
    import csv

    if not BATCH_STAGING_JSON.exists():
        raise FileNotFoundError(
            f"missing {BATCH_STAGING_JSON} — run pi-remaster extract first"
        )
    rows = json.loads(BATCH_STAGING_JSON.read_text(encoding="utf-8"))
    pis = _load_dev_pis()
    generic = [p for p in pis if p.get("still_generic")]
    if limit:
        generic = generic[:limit]

    assignments = locate_pages_for_pis(
        rows, generic, top_k=top_k, min_score=min_score,
    )
    locate_path = REPORTS_DIR / "pi_vision_pi_locate.json"
    locate_path.write_text(json.dumps(assignments, indent=2, default=str), encoding="utf-8")
    print(f"  located {len(assignments)} page(s) for {len(generic)} generic PIs -> {locate_path}")

    index = {(r.get("pdf_file"), int(r.get("page") or 0)): i for i, r in enumerate(rows)}
    ok = fail = 0
    for i, asg in enumerate(assignments, 1):
        pdf = asg["pdf_file"]
        page = int(asg["page"])
        print(
            f"  pi-vision {i}/{len(assignments)}  {asg['erp_pi_name']} "
            f"bill={asg['erp_bill_no']} → {pdf} p{page}  score={asg['score']:.1f}",
            flush=True,
        )
        try:
            erp_hints = {
                "bill_no": asg["erp_bill_no"],
                "net_total": asg["erp_net_total"],
                "grand_total": asg["erp_grand_total"],
            }
            result = extract_page_vision(
                pdf, page, model=model, force=force, erp_hints=erp_hints,
            )
            idx = index.get((pdf, page))
            if idx is None:
                fail += 1
                continue
            merged = {**rows[idx], **_vision_fields(result)}
            # Seed ERP identity so match prefers this PI; clear stale flags
            for k in ("match_status", "lines_ok", "corrected_lines", "notes"):
                merged.pop(k, None)
            merged["pi_driven"] = True
            merged["pi_driven_target"] = asg["erp_pi_name"]
            merged["pi_driven_score"] = asg["score"]
            merged["target_grand_total"] = asg["erp_grand_total"]
            merged["target_net_total"] = asg["erp_net_total"]
            merged["target_bill_no"] = asg["erp_bill_no"]
            rows[idx] = merged
            ok += 1
            if i % 5 == 0 or i == len(assignments):
                BATCH_STAGING_JSON.write_text(
                    json.dumps(rows, indent=2, default=str), encoding="utf-8"
                )
        except Exception as exc:
            fail += 1
            print(f"    FAIL {pdf} p{page}: {type(exc).__name__}: {exc}", flush=True)

    BATCH_STAGING_JSON.write_text(json.dumps(rows, indent=2, default=str), encoding="utf-8")
    with BATCH_STAGING_CSV.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(
            fh,
            fieldnames=[
                "pdf_file", "page", "engine", "ocr_bill_no", "ocr_supplier",
                "ocr_date", "ocr_grand_total", "ocr_net_total", "lines_sum",
                "n_lines", "match_status", "erp_pi_name", "pi_driven_target", "notes",
            ],
            extrasaction="ignore",
        )
        w.writeheader()
        for r in rows:
            w.writerow({**r, "n_lines": len(r.get("ocr_lines") or [])})

    unlocated = len(generic) - len({a["erp_pi_name"] for a in assignments})
    return {
        "generic_pis": len(generic),
        "located": len(assignments),
        "unlocated": unlocated,
        "ok": ok,
        "failed": fail,
        "locate_report": str(locate_path),
        "staging": str(BATCH_STAGING_JSON),
    }
