# Tally → ERPNext Migration

A resilient, idempotent, **fully-reconciled** migration of a Tally Prime
company into ERPNext (Frappe v16). It extracts every master and voucher from
Tally over its HTTP-XML gateway, stages them locally, transforms them into
GL-faithful ERPNext documents, loads them via the REST API, and proves
completeness with a reconciliation report.

## What this migrates

Source: Tally Prime company **"Spaceki Designs LLP"** (gateway `http://localhost:9000`).
Target: ERPNext company **"Spaceki Designs LLP"** (`https://erp.spaceki.com`,
Frappe v16 / ERPNext v16, India Compliance).

| Tally master | Count | → ERPNext |
|---|---|---|
| Groups | 36 | Account (group nodes) |
| Ledgers (non-party) | ~260 | Account (GL leaf) |
| Ledgers under Sundry Debtors | ~40 | Customer |
| Ledgers under Sundry Creditors | ~570 | Supplier |
| Cost centres / Godowns / Units / Items | few | Cost Center / Warehouse / UOM / Item |

| Tally voucher | Count | → ERPNext |
|---|---|---|
| Purchase | 1,268 | **Purchase Invoice** (settleable AR/AP) |
| Sales | 71 | **Sales Invoice** |
| Debit Note | 8 | Purchase Invoice (return) |
| Credit Note | 3 | Sales Invoice (return) |
| Payment | 3,092 | Payment Entry (Pay) **with invoice references** — JE fallback |
| Receipt | 337 | Payment Entry (Receive) with references — JE fallback |
| Journal | 2,497 | Journal Entry (party rows linked to invoices) |
| Contra | 17 | Journal Entry |
| **Total** | **~7,293** | |

### Approach: invoices first, then linked settlements
Sales/Purchase vouchers become **real Sales/Purchase Invoice documents** so they
carry an outstanding balance and a Paid / Partly Paid / Overdue status. Payments,
receipts and journals are then loaded **with references** to those invoices —
matched on Tally's bill allocations (`New Ref` on the invoice, `Agst Ref` on the
settlement) — so each invoice's status updates automatically.

Invoice lines mirror the Tally voucher exactly: expense/income ledgers become
item rows (each posting to its own account, on a generic non-stock item), GST
ledgers become "Actual" tax rows, and the party gets the control account — so the
GL and trial balance still tie to the rupee. Allocations are capped to each
invoice's live outstanding to avoid rounding over-allocation. Vouchers that can't
be modelled as a clean invoice (no party, party-kind mismatch) fall back to a
Journal Entry; nothing is dropped.

Live-run outcome: 1,268 invoices (73 Sales + 1,195 Purchase), 5,210 Journal
Entries, 815 Payment Entries; 481 Purchase Invoices fully Paid + 107 partly paid
(4 + 27 on the sales side); trial balance balanced to the rupee.

## Architecture

```
Tally (HTTP-XML)                ERPNext (REST)
      │  export collections          ▲  insert / submit (idempotent by tally_guid)
      ▼                              │
 tally_client ─► tally_export ─► staging.sqlite ─► load_masters ─► load_vouchers
                                     │                                   │
                                     └──────────► reconcile ◄────────────┘
                                          (counts + failures report)
```

* **Raw capture first.** Every Tally response is saved verbatim to
  `data/raw/*.xml` before parsing, so a complete source snapshot always exists.
* **SQLite staging** (`data/staging.sqlite`) decouples extract from load. Each
  record carries a `load_status` (`pending|loaded|skipped|error`) and the
  resulting ERPNext name, which drives retries and reconciliation.
* **Idempotency.** A read-only custom field `tally_guid` is added to every
  migrated doctype and stores the Tally GUID. Re-runs match on it instead of
  creating duplicates; the `wipe` command removes exactly what was migrated.
* **Month-chunked voucher extraction.** The Tally *Voucher collection* honours
  `SVFROMDATE/SVTODATE` (the Day Book report does not), so vouchers are pulled
  one month at a time — small, resilient requests with per-chunk counts.
* **Nothing is dropped.** Unresolvable ledgers post to a dedicated
  `Tally Migration Suspense` account and are flagged in the report rather than
  failing the voucher.

## Setup

```bash
pip install -r requirements.txt
```

Secrets are read from two gitignored files (already present):
* `.env.db` — ERPNext MariaDB host/credentials (used only for fast read-only counts).
* `.env.erpnext` — `ERPNEXT_URL`, `ERPNEXT_API_KEY`, `ERPNEXT_API_SECRET`, `ERPNEXT_INSECURE_SSL`.

Non-secret mapping/runtime config is in `config.yaml` (company name, date window,
group→root-type map, voucher-type map).

## Usage

All write operations are **dry-run by default**. Pass `--confirm` to actually
write to ERPNext.

```bash
# 1. Pull everything from Tally into staging (read-only)
python -m t2e extract

# 2. Dry-run the loaders to validate mapping (no writes)
python -m t2e load-masters
python -m t2e load-vouchers --limit 200

# 3. Execute for real (order matters: invoices before settlements)
python -m t2e wipe-db --confirm              # fast DB reset, transactions only
python -m t2e load-masters --confirm
python -m t2e load-invoices --confirm        # Sales/Purchase -> SI/PI + bill index
python -m t2e load-vouchers --confirm         # payments/journals linked to invoices

# 4. Prove completeness
python -m t2e reconcile

# 5. Verify the books: compare Tally vs ERPNext Profit & Loss
python -m t2e pl-check --from-date 20220101 --to-date 20260630

# …or do it all in order:
python -m t2e run-all --confirm
```

Useful flags:
* `load-vouchers --type Payment` — load only one Tally voucher type.
* `load-vouchers --limit N` — first N (handy for a smoke test).
* `wipe --with-masters` — also delete migration-created masters (those carrying
  `tally_guid`). Off by default to protect the standard chart of accounts.

## Reconciliation

`python -m t2e reconcile` writes `data/reports/reconciliation.json` and
`data/reports/failures.csv` and prints a table comparing, per master kind and
voucher type: Tally count vs loaded / skipped / error / pending, plus the live
ERPNext count of migrated documents. Re-run `load-vouchers` to retry any rows
left in `error` after fixing the cause — the load is idempotent.

## Layout

```
config.yaml            mapping + runtime config (non-secret)
t2e/
  config.py            secrets + config loading
  tally_client.py      HTTP-XML gateway client, XML sanitizing, raw capture
  tally_export.py      master + month-chunked voucher extraction → staging
  staging.py           SQLite staging store
  erpnext_client.py    REST client (dry-run gated), idempotency custom field
  mapping.py           group→root-type, account naming, ledger resolver
  load_masters.py      accounts / parties / cost centers / items loader
  load_vouchers.py     GL-faithful Journal Entry + Payment Entry loader
  wipe.py              clean-slate reset of migrated transactions
  reconcile.py         completeness report
  cli.py               command-line orchestration
data/                  raw XML, staging.sqlite, reports (gitignored)
```
