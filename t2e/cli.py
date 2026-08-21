"""Command-line entry point for the Tally -> ERPNext migration.

    python -m t2e extract                 # pull Tally -> staging (read-only)
    python -m t2e load-masters [--confirm]
    python -m t2e load-vouchers [--confirm] [--type Payment] [--limit N]
    python -m t2e wipe [--confirm] [--with-masters]
    python -m t2e reconcile
    python -m t2e run-all [--confirm]     # wipe -> masters -> vouchers -> reconcile

All write operations are DRY-RUN unless --confirm is passed.
"""
from __future__ import annotations

import argparse
import sys

from .config import get_config
from .erpnext_client import ERPNextClient
from .staging import Staging
from .tally_client import TallyClient


def _banner(dry_run: bool) -> None:
    cfg = get_config()
    mode = "DRY-RUN (no writes)" if dry_run else "LIVE (writing to ERPNext)"
    print(f"=== Tally -> ERPNext migration | env: {cfg.env_name} "
          f"({cfg.erp_url}) | mode: {mode} ===")


def cmd_extract(args) -> int:
    from . import tally_export as tx
    c, s = TallyClient(), Staging()
    print("Extracting masters from Tally...")
    print("  masters:", tx.extract_masters(c, s))

    def prog(f, t, n):
        if n:
            print(f"  vouchers {f[:6]}: {n}")
    print("Extracting vouchers (month-chunked)...")
    print("  vouchers:", tx.extract_vouchers(c, s, progress=prog))
    s.close()
    return 0


def _masters(erp: ERPNextClient, s: Staging):
    from .load_masters import (MasterLoader, ensure_company_address,
                               ensure_idempotency_field, fetch_company_defaults)
    if not erp.dry_run:
        ensure_idempotency_field(erp)
        print("  company GST address:",
              ensure_company_address(erp, get_config().erpnext["company"], dry_run=False))
    defaults = fetch_company_defaults(erp)
    from .gst_setup import ensure_gst_setup
    gst_setup = ensure_gst_setup(erp, defaults)
    print("  GST setup:", {
        status: sum(1 for value in gst_setup.values() if value == status)
        for status in sorted(set(gst_setup.values()))
    })
    ml = MasterLoader(erp, s, defaults)
    ml.ensure_suspense()
    print("  UOMs:", ml.load_uoms())
    print("  account groups:", ml.load_account_groups())
    print("  ledger accounts:", ml.load_ledger_accounts())
    nc, ns = ml.load_parties()
    print(f"  customers: {nc}  suppliers: {ns}")
    print("  cost centers:", ml.load_cost_centers())
    print("  items:", ml.load_items())
    return defaults


def cmd_load_masters(args) -> int:
    erp, s = ERPNextClient(dry_run=not args.confirm), Staging()
    _banner(erp.dry_run)
    _masters(erp, s)
    s.close()
    return 0


def cmd_load_invoices(args) -> int:
    from .load_invoices import InvoiceLoader, ensure_generic_item
    from .load_masters import fetch_company_defaults
    from .mapping import LedgerResolver
    erp, s = ERPNextClient(dry_run=not args.confirm), Staging()
    _banner(erp.dry_run)
    defaults = fetch_company_defaults(erp)
    from .gst_setup import ensure_gst_setup
    ensure_gst_setup(erp, defaults)
    if not erp.dry_run:
        ensure_generic_item(erp)
    resolver = LedgerResolver(s, defaults)
    il = InvoiceLoader(erp, s, defaults, resolver)

    def prog(i, total, stats):
        print(f"  {i}/{total}  loaded={stats['loaded']} fallback={stats['fallback']} "
              f"error={stats['error']}")
    if not erp.dry_run:
        erp.set_doctype_property("Sales Invoice", "allow_rename", "1", "Check")
    try:
        print("  invoices:", il.run(
            vtype=args.type, limit=args.limit, latest=args.latest, progress=prog
        ))
    finally:
        if not erp.dry_run:
            erp.set_doctype_property("Sales Invoice", "allow_rename", "0", "Check")
    print(f"  ({len(il.fallback)} vouchers fall back to Journal Entry)")
    s.close()
    return 0


def cmd_preflight_invoices(args) -> int:
    import json
    from .load_invoices import InvoiceLoader
    from .load_masters import fetch_company_defaults
    from .mapping import LedgerResolver
    erp, s = ERPNextClient(dry_run=True), Staging()
    _banner(True)
    defaults = fetch_company_defaults(erp)
    report = InvoiceLoader(
        erp, s, defaults, LedgerResolver(s, defaults)
    ).preflight()
    print(json.dumps(report, indent=2))
    s.close()
    stats = report["stats"]
    return 1 if stats["rounding_mismatch"] or stats["sales_name_duplicates"] else 0


def cmd_load_vouchers(args) -> int:
    from .load_masters import fetch_company_defaults
    from .load_vouchers import VoucherLoader
    from .mapping import LedgerResolver
    erp, s = ERPNextClient(dry_run=not args.confirm), Staging()
    _banner(erp.dry_run)
    defaults = fetch_company_defaults(erp)
    resolver = LedgerResolver(s, defaults)
    vl = VoucherLoader(erp, s, defaults, resolver)

    def prog(i, total, stats):
        print(f"  {i}/{total}  loaded={stats['loaded']} error={stats['error']}")
    stats = vl.run(vtype=args.type, limit=args.limit, progress=prog)
    print("  voucher load:", stats)
    if vl.unresolved:
        print(f"  ! {len(vl.unresolved)} unresolved ledgers (first 10):",
              list(vl.unresolved)[:10])
    s.close()
    return 0


def _reset_staging_after_wipe(with_masters: bool) -> None:
    """Wiped ERPNext docs no longer exist, so mark their staged source rows
    pending again -- otherwise the loader would skip them on reload."""
    s = Staging()
    s.conn.execute("UPDATE voucher SET load_status='pending', erp_name=NULL, error=NULL")
    if with_masters:
        s.conn.execute("UPDATE master SET load_status='pending', erp_name=NULL, error=NULL")
    s.conn.commit()
    s.close()


def cmd_wipe(args) -> int:
    from .wipe import wipe
    erp = ERPNextClient(dry_run=not args.confirm)
    _banner(erp.dry_run)
    if erp.dry_run:
        print("  (dry-run: would cancel+delete transactions; pass --confirm to execute)")
    print("  wiped:", wipe(erp, with_masters=args.with_masters))
    if not erp.dry_run:
        _reset_staging_after_wipe(args.with_masters)
    return 0


def cmd_wipe_db(args) -> int:
    from .db_wipe import db_wipe, reset_staging
    cfg = get_config()
    dry = not args.confirm
    _banner(dry)
    print("  DB-level wipe of transactions for", cfg.erpnext["company"])
    res = db_wipe(cfg.erpnext["company"], dry_run=dry)
    for k, v in res.items():
        print(f"    {k}: {v}")
    if not dry:
        reset_staging()
        Staging().clear_bill_refs()
        print("  staging voucher status reset to pending; bill refs cleared")
    return 0


def cmd_reconcile(args) -> int:
    from .reconcile import build_report, print_summary
    erp, s = ERPNextClient(dry_run=True), Staging()
    print_summary(build_report(erp, s))
    s.close()
    return 0


def cmd_reconcile_payments(args) -> int:
    from .load_masters import fetch_company_defaults
    from .reconcile_payments import PaymentReconciler
    erp = ERPNextClient(dry_run=not args.confirm)
    _banner(erp.dry_run)
    if erp.dry_run:
        print("  (dry-run: computing planned FIFO allocations only; "
              "pass --confirm to reconcile)")
    pr = PaymentReconciler(erp, fetch_company_defaults(erp))

    def prog(n, stats):
        print(f"  {n} parties  planned={stats.get('planned', 0)} "
              f"reconciled={stats.get('reconciled', 0)} skipped={stats['skipped']} "
              f"error={stats['error']} allocated={stats['allocated']:,.2f}")
    stats = pr.run(only_party=args.party, limit=args.limit, progress=prog)
    print("  payment reconciliation:", stats)
    print("  -> report: data/reports/payment_reconciliation.{json,csv}")
    return 0


def cmd_load_closing_stock(args) -> int:
    from .load_closing_stock import ClosingStockLoader
    from .load_masters import fetch_company_defaults
    erp = ERPNextClient(dry_run=not args.confirm)
    _banner(erp.dry_run)
    loader = ClosingStockLoader(erp, fetch_company_defaults(erp))
    stats, results = loader.run()
    for date, opening, closing, status, name in results:
        print(f"  {date}  open={opening:>16,.2f} close={closing:>16,.2f} "
              f"delta={closing - opening:>16,.2f}  -> {status} {name or ''}")
    print("  closing-stock:", stats)
    return 0


def cmd_pl_check(args) -> int:
    from . import pl_check
    cfg = get_config()
    pl_check.run(from_date=args.from_date or str(cfg.tally.get("from_date", "20000101")),
                 to_date=args.to_date or str(cfg.tally.get("to_date", "20990101")))
    return 0


def cmd_load_openings(args) -> int:
    from .load_masters import fetch_company_defaults
    from .load_openings import OpeningsLoader
    erp, s = ERPNextClient(dry_run=not args.confirm), Staging()
    _banner(erp.dry_run)
    loader = OpeningsLoader(erp, s, fetch_company_defaults(erp))
    stats, preview, name = loader.run()
    for nm, acc, party, op in preview:
        side = f"Cr {op:,.2f}" if op > 0 else f"Dr {-op:,.2f}"
        print(f"  {nm[:34]:<36} -> {acc[:34]:<36} {('['+party+']') if party else '':<20} {side}")
    if loader.unresolved:
        print(f"  ! {len(loader.unresolved)} unresolved ledgers: {loader.unresolved}")
    print("  opening balances:", stats, "->", name)
    s.close()
    return 0


def cmd_ensure_fiscal_years(args) -> int:
    from .load_masters import ensure_fiscal_years
    cfg = get_config()
    erp = ERPNextClient(dry_run=not args.confirm)
    _banner(erp.dry_run)
    res = ensure_fiscal_years(
        erp,
        str(cfg.tally.get("from_date", "20220101")),
        str(cfg.tally.get("to_date", "20990101")),
        dry_run=erp.dry_run)
    for name, status in sorted(res.items()):
        print(f"  {name}: {status}")
    return 0


def cmd_bs_check(args) -> int:
    from . import bs_check
    cfg = get_config()
    bs_check.run(from_date=args.from_date or str(cfg.tally.get("from_date", "20000101")),
                 to_date=args.to_date or str(cfg.tally.get("to_date", "20990101")))
    return 0


def cmd_pi_remaster_staging(args) -> int:
    from . import pi_remaster as pr
    _banner(dry_run=True)
    path = pr.write_pilot_staging()
    rows = __import__("json").loads(path.read_text(encoding="utf-8"))
    print(f"  wrote {path} ({len(rows)} rows)")
    print(f"  csv:   {pr.STAGING_CSV}")
    for r in rows:
        ok = "lines_ok" if r.get("lines_ok") else "LINES_BAD"
        print(f"  - {r.get('erp_pi_name')}  {r.get('erp_bill_no')}  "
              f"{r.get('match_status')}  {ok}  "
              f"sum={r.get('lines_sum')} net={r.get('erp_net_total')}")
    return 0


def cmd_pi_remaster_extract(args) -> int:
    from . import pi_remaster as pr
    _banner(dry_run=True)
    pr.extract_migration(force=args.force, limit_pages=args.limit_pages or 0)
    return 0


def cmd_pi_remaster_extract_vision(args) -> int:
    """GPT-4o vision extract: one page (trial) or --needed batch into staging."""
    from . import pi_vision as pv
    import json
    _banner(dry_run=True)
    if args.needed:
        stats = pv.extract_vision_needed(
            model=args.model,
            force=not args.cache,
            limit=args.limit or 0,
            all_unmatched=args.all_unmatched,
        )
        print(f"  vision batch: {stats}")
        print("  next: python -m t2e --env dev pi-remaster match")
        return 0 if stats.get("failed", 0) == 0 else 1
    if args.for_pis:
        stats = pv.extract_vision_for_pis(
            model=args.model,
            force=not args.cache,
            limit=args.limit or 0,
            top_k=args.top_k or 1,
        )
        print(f"  pi-driven vision: {stats}")
        print("  next: python -m t2e --env dev pi-remaster match")
        return 0 if stats.get("failed", 0) == 0 else 1

    path = pv.trial_one_page(
        pdf_name=args.pdf,
        page=args.page,
        model=args.model,
        force=not args.cache,
    )
    result = json.loads(path.read_text(encoding="utf-8"))
    print(f"  wrote {path}")
    print(f"  bill_no:     {result.get('ocr_bill_no')}")
    print(f"  supplier:    {result.get('ocr_supplier')}")
    print(f"  date:        {result.get('ocr_date')}")
    print(f"  grand_total: {result.get('ocr_grand_total')}")
    print(f"  net_total:   {result.get('ocr_net_total')}")
    print(f"  lines:       {len(result.get('ocr_lines') or [])}  "
          f"sum={result.get('lines_sum')}")
    for i, ln in enumerate(result.get("ocr_lines") or [], 1):
        print(f"    {i}. {ln.get('item_name')}  "
              f"qty={ln.get('qty')} {ln.get('uom')} @ {ln.get('rate')} "
              f"= {ln.get('amount')}  HSN={ln.get('gst_hsn_code')}")
    if result.get("notes"):
        print(f"  notes: {result['notes']}")
    usage = result.get("usage") or {}
    if usage.get("prompt_tokens"):
        print(f"  tokens: prompt={usage.get('prompt_tokens')} "
              f"completion={usage.get('completion_tokens')}")
    return 0


def cmd_pi_remaster_match(args) -> int:
    from . import pi_remaster as pr
    _banner(dry_run=True)
    pr.match_staging()
    return 0


def cmd_pi_remaster_apply(args) -> int:
    from . import pi_remaster as pr
    _banner(dry_run=not args.confirm)
    results = pr.apply_pilot(
        confirm=args.confirm, limit=args.limit or 0, names=args.name,
        use_batch=not args.pilot,
    )
    ok_n = sum(1 for r in results if r.get("ok"))
    print(f"  done: {ok_n}/{len(results)} ok")
    return 0 if not results or ok_n == len(results) else 1


def cmd_pi_remaster_export_extract(args) -> int:
    """Write durable page/line CSVs + JSON bundle for remigration without re-OCR."""
    from . import pi_remaster as pr
    _banner(dry_run=True)
    stats = pr.export_extract_csvs()
    print(f"  pages: {stats['pages_csv']}")
    print(f"  lines: {stats['lines_csv']}")
    print(f"  bundle: {stats['bundle_json']}")
    return 0


def cmd_pi_remaster_verify(args) -> int:
    from . import pi_remaster as pr
    import json
    _banner(dry_run=True)
    rows = pr.verify_pilot(names=args.name)
    print(json.dumps(rows, indent=2, default=str))
    ok_rows = [r for r in rows if r.get("pi") and not r.get("error")]
    bad = [r for r in ok_rows if r.get("has_generic_item")]
    if not ok_rows and any(r.get("error") for r in rows):
        return 1
    return 1 if bad else 0


def cmd_run_all(args) -> int:
    from .load_invoices import InvoiceLoader, ensure_generic_item
    from .load_masters import fetch_company_defaults
    from .load_vouchers import VoucherLoader
    from .mapping import LedgerResolver
    from .reconcile import build_report, print_summary

    erp, s = ERPNextClient(dry_run=not args.confirm), Staging()
    _banner(erp.dry_run)

    print("\n[1/5] DB wipe of transactions (masters preserved)")
    if not erp.dry_run:
        from .db_wipe import db_wipe, reset_staging
        print("  wiped:", db_wipe(get_config().erpnext["company"], dry_run=False))
        reset_staging()
        s.clear_bill_refs()

    print("\n[2/5] Load masters")
    defaults = _masters(erp, s)

    print("\n[3/5] Load Sales/Purchase invoices (so they can be settled)")
    if not erp.dry_run:
        ensure_generic_item(erp)
    resolver = LedgerResolver(s, defaults)
    il = InvoiceLoader(erp, s, defaults, resolver)

    def iprog(i, total, st):
        print(f"  {i}/{total}  loaded={st['loaded']} fallback={st['fallback']} error={st['error']}")
    if not erp.dry_run:
        erp.set_doctype_property("Sales Invoice", "allow_rename", "1", "Check")
    try:
        print("  invoices:", il.run(progress=iprog))
    finally:
        if not erp.dry_run:
            erp.set_doctype_property("Sales Invoice", "allow_rename", "0", "Check")

    print("\n[4/5] Load payments / journals (linked to invoices)")
    resolver = LedgerResolver(s, defaults)  # refresh after invoices/bill index
    vl = VoucherLoader(erp, s, defaults, resolver)

    def prog(i, total, stats):
        print(f"  {i}/{total}  loaded={stats['loaded']} error={stats['error']}")
    print("  voucher load:", vl.run(progress=prog))
    if vl.unresolved:
        print(f"  ! {len(vl.unresolved)} unresolved ledgers")

    print("\n[5/5] Reconcile")
    print_summary(build_report(erp, s))
    s.close()
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="t2e", description="Tally -> ERPNext migration")
    p.add_argument("--env", choices=["prd", "dev", "PRD", "DEV"], default=None,
                   help="target environment (PRD or DEV); default from config/PRD. "
                        "Use as: python -m t2e --env dev <command>")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("extract").set_defaults(func=cmd_extract)

    for name, func in [("load-masters", cmd_load_masters), ("wipe", cmd_wipe),
                       ("run-all", cmd_run_all)]:
        sp = sub.add_parser(name)
        sp.add_argument("--confirm", action="store_true", help="execute writes")
        sp.add_argument("--with-masters", action="store_true",
                        help="also wipe migration-created masters")
        sp.set_defaults(func=func)

    wdb = sub.add_parser("wipe-db", help="fast DB-level transaction wipe")
    wdb.add_argument("--confirm", action="store_true", help="execute deletes")
    wdb.set_defaults(func=cmd_wipe_db)

    for nm, fn in [("load-vouchers", cmd_load_vouchers), ("load-invoices", cmd_load_invoices)]:
        lv = sub.add_parser(nm)
        lv.add_argument("--confirm", action="store_true")
        lv.add_argument("--type", default=None, help="only this Tally voucher type")
        lv.add_argument("--limit", type=int, default=0)
        if nm == "load-invoices":
            lv.add_argument(
                "--latest", action="store_true",
                help="with --limit, load the newest invoices instead of the oldest",
            )
        lv.set_defaults(func=fn)

    sub.add_parser("reconcile").set_defaults(func=cmd_reconcile)
    sub.add_parser(
        "preflight-invoices",
        help="validate all staged invoice names, rounding, tax and OCR mappings",
    ).set_defaults(func=cmd_preflight_invoices)

    rp = sub.add_parser("reconcile-payments",
                        help="net unallocated payments/advances against outstanding "
                             "invoices (FIFO, per party)")
    rp.add_argument("--confirm", action="store_true", help="execute reconciliation")
    rp.add_argument("--party", default=None, help="only this customer/supplier")
    rp.add_argument("--limit", type=int, default=0, help="max parties to process")
    rp.set_defaults(func=cmd_reconcile_payments)

    cs = sub.add_parser("load-closing-stock",
                        help="post year-end closing-stock adjustment Journal Entries")
    cs.add_argument("--confirm", action="store_true", help="execute writes")
    cs.set_defaults(func=cmd_load_closing_stock)

    fy = sub.add_parser("ensure-fiscal-years",
                        help="create fiscal years spanning the Tally window (fresh-site setup)")
    fy.add_argument("--confirm", action="store_true", help="execute writes")
    fy.set_defaults(func=cmd_ensure_fiscal_years)

    op = sub.add_parser("load-openings",
                        help="post Tally ledger opening balances as one opening JE")
    op.add_argument("--confirm", action="store_true", help="execute writes")
    op.set_defaults(func=cmd_load_openings)

    pc = sub.add_parser("pl-check", help="compare Tally vs ERPNext Profit & Loss")
    pc.add_argument("--from-date", default=None, help="yyyymmdd")
    pc.add_argument("--to-date", default=None, help="yyyymmdd")
    pc.set_defaults(func=cmd_pl_check)

    bc = sub.add_parser("bs-check", help="compare Tally vs ERPNext Balance Sheet")
    bc.add_argument("--from-date", default=None, help="yyyymmdd")
    bc.add_argument("--to-date", default=None, help="yyyymmdd")
    bc.set_defaults(func=cmd_bs_check)

    pr = sub.add_parser(
        "pi-remaster",
        help="replace Tally Migration Item lines on Purchase Invoices with real items",
    )
    pr_sub = pr.add_subparsers(dest="pi_cmd", required=True)

    pr_st = pr_sub.add_parser(
        "staging", help="write pilot staging JSON/CSV from seed + DEV DB match"
    )
    pr_st.set_defaults(func=cmd_pi_remaster_staging)

    pr_ex = pr_sub.add_parser(
        "extract", help="OCR all Migration *Purchase*.pdf pages into staging"
    )
    pr_ex.add_argument("--force", action="store_true", help="re-OCR ignoring cache")
    pr_ex.add_argument("--limit-pages", type=int, default=0,
                       help="stop after N pages (debug)")
    pr_ex.set_defaults(func=cmd_pi_remaster_extract)

    pr_xv = pr_sub.add_parser(
        "extract-vision",
        help="GPT-4o vision extract (one page, or --needed batch into staging)",
    )
    pr_xv.add_argument(
        "--pdf", default="April 2026_Purchase Invoices_1.pdf",
        help="filename under data/Migration/ (single-page mode)",
    )
    pr_xv.add_argument("--page", type=int, default=1, help="1-based page (single-page mode)")
    pr_xv.add_argument("--model", default="gpt-4o", help="OpenAI vision model")
    pr_xv.add_argument(
        "--cache", action="store_true",
        help="reuse cached vision JSON if present (default: always call API)",
    )
    pr_xv.add_argument(
        "--needed", action="store_true",
        help="batch: re-extract unmatched/high-without-lines pages near DEV totals",
    )
    pr_xv.add_argument(
        "--all-unmatched", action="store_true",
        help="with --needed, send EVERY unmatched staging page to OpenAI",
    )
    pr_xv.add_argument(
        "--for-pis", action="store_true",
        help="PI-driven: locate best page per still-generic PI, extract with ERPNext hints",
    )
    pr_xv.add_argument(
        "--top-k", type=int, default=1,
        help="with --for-pis, max pages to try per PI (default 1)",
    )
    pr_xv.add_argument(
        "--limit", type=int, default=0,
        help="with --needed/--for-pis, max PIs or pages to send to OpenAI",
    )
    pr_xv.set_defaults(func=cmd_pi_remaster_extract_vision)

    pr_mt = pr_sub.add_parser(
        "match", help="match OCR staging pages to DEV Apr–Jun Purchase Invoices"
    )
    pr_mt.set_defaults(func=cmd_pi_remaster_match)

    pr_ap = pr_sub.add_parser(
        "apply", help="recreate PIs with real items (dry-run unless --confirm)"
    )
    pr_ap.add_argument("--confirm", action="store_true", help="execute writes")
    pr_ap.add_argument("--limit", type=int, default=0, help="max staging rows")
    pr_ap.add_argument(
        "--name", action="append", default=None,
        help="only this ERPNext PI name (repeatable)",
    )
    pr_ap.add_argument(
        "--pilot", action="store_true",
        help="use pilot staging JSON instead of batch OCR staging",
    )
    pr_ap.set_defaults(func=cmd_pi_remaster_apply)

    pr_vf = pr_sub.add_parser("verify", help="verify last apply log against DEV DB")
    pr_vf.add_argument(
        "--name", action="append", default=None,
        help="only this PI name (repeatable)",
    )
    pr_vf.set_defaults(func=cmd_pi_remaster_verify)

    pr_exout = pr_sub.add_parser(
        "export-extract",
        help="export durable page/line CSVs + JSON bundle (no re-OCR needed later)",
    )
    pr_exout.set_defaults(func=cmd_pi_remaster_export_extract)

    args = p.parse_args(argv)
    from .config import set_environment
    set_environment(getattr(args, "env", None))
    try:
        return args.func(args)
    except KeyboardInterrupt:
        print("\ninterrupted", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
