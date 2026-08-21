"""Compare the Balance Sheet between Tally and ERPNext.

Mirrors ``pl_check`` for the balance-sheet (Asset / Liability / Equity) side:

  * Headline: root-type totals (Tally per-ledger closing, natural sign) vs
    ERPNext GL, calling out the structural reconciling items a GL-faithful
    voucher migration leaves behind --
      - Retained earnings: ERPNext keeps the period's net profit in Income/
        Expense until a Period-Closing voucher is posted.
      - Capital grouping: ERPNext's CoA files the Capital Account under
        root_type Liability, whereas Tally reports it as Equity.
      - GST Output: Tally nests OUT PUT under GST→Current Assets; we classify
        it as Liability (India Compliance / ERPNext Duties and Taxes).
  * Party control: Sundry Debtors / Creditors aggregates vs Debtors / Creditors.
  * Per-account: non-party BS ledgers, with GST aliases collapsed onto
    Output Tax / Input Tax heads; full table -> data/reports/bs_compare.csv.

Balances are cumulative to ``to_date`` (ERPNext GL filtered to that as-of date).
"""
from __future__ import annotations

import csv

import pymysql

from .config import DATA_DIR, get_config
from .mapping import GroupTree
from .staging import Staging
from .tally_client import TallyClient

BS_ROOTS = ("Asset", "Liability", "Equity")


def _f(s) -> float:
    try:
        return float(str(s).strip())
    except (TypeError, ValueError):
        return 0.0


def _gst_canonical(name: str) -> str | None:
    """Map Tally/ERP GST ledger names onto India Compliance heads for compare."""
    upper = " ".join((name or "").upper().split())
    if "RCM" in upper or "REFUND" in upper or "PAYABLE" in upper:
        return None
    component = next(
        (token for token in ("CGST", "SGST", "UTGST", "IGST") if token in upper),
        None,
    )
    if not component:
        return None
    if component == "UTGST":
        component = "SGST"
    if (
        "OUT PUT" in upper
        or upper.startswith("OUTPUT TAX")
        or (upper.startswith("OUTPUT") and "INPUT" not in upper)
        or upper.startswith("PROVISION FOR")
    ):
        return f"Output Tax {component}"
    if "INPUT" in upper or "UNCLAIMED" in upper:
        return f"Input Tax {component}"
    return None


def _merge_gst_aliases(accounts: dict[str, float]) -> dict[str, float]:
    """Collapse Tally OUT PUT / * INPUT @ / Unclaimed names into Output/Input Tax."""
    out = dict(accounts)
    extras: dict[str, float] = {}
    drop: list[str] = []
    for name, val in accounts.items():
        canon = _gst_canonical(name)
        if not canon or canon == name:
            continue
        extras[canon] = extras.get(canon, 0.0) + val
        drop.append(name)
    for name in drop:
        out.pop(name, None)
    for canon, val in extras.items():
        out[canon] = out.get(canon, 0.0) + val
    return out


# ---- Tally side ----------------------------------------------------------
def tally_bs_accounts(client: TallyClient, store: Staging, from_date: str,
                      to_date: str) -> tuple[dict[str, float], dict[str, float],
                                             dict[str, float]]:
    """Per-ledger natural balances for every BS ledger, plus root totals and the
    Sundry Debtors / Sundry Creditors group aggregates (party controls)."""
    client.from_date, client.to_date = from_date, to_date
    root = client.export_collection(
        "bs_ledgers", "Ledger",
        methods=["Name", "Parent", "ClosingBalance"], dated=True,
        save_as="bs_ledgers")
    tree = GroupTree(store)
    accounts: dict[str, float] = {}
    totals = {r: 0.0 for r in BS_ROOTS}
    party = {"Sundry Debtors": 0.0, "Sundry Creditors": 0.0}
    for el in root.findall(".//LEDGER"):
        name = (el.get("NAME") or "").strip()
        if not name:
            continue
        parent = (el.findtext("PARENT") or "").strip()
        rt = tree.ledger_root_type(name, parent)
        if rt not in BS_ROOTS:
            continue
        bal = _f(el.findtext("CLOSINGBALANCE"))
        # Tally CLOSINGBALANCE: credit positive, debit negative. Assets carry a
        # debit balance, so flip them to a positive "natural" figure; liabilities
        # and equity keep their credit-positive value.
        val = -bal if rt == "Asset" else bal
        accounts[" ".join(name.split())] = val
        totals[rt] += val
        anc = set(tree.ancestry(parent))
        for grp in party:
            if grp in anc:
                party[grp] += val
    return accounts, totals, party


# ---- ERPNext side --------------------------------------------------------
def erpnext_bs_accounts(company: str, as_of: str | None = None
                        ) -> tuple[dict[str, float], dict[str, float],
                                   dict[str, float], float]:
    """Per-account BS balances from GL as of ``as_of`` (YYYYMMDD or YYYY-MM-DD)."""
    if as_of and len(as_of) == 8 and as_of.isdigit():
        as_of = f"{as_of[0:4]}-{as_of[4:6]}-{as_of[6:8]}"
    p = get_config().db_params
    conn = pymysql.connect(host=p["host"], port=p["port"], user=p["user"],
                           password=p["password"], database=p["database"],
                           connect_timeout=20)
    try:
        cur = conn.cursor()
        date_clause = " AND g.posting_date <= %s" if as_of else ""
        params: list = [company]
        if as_of:
            params.append(as_of)
        cur.execute(
            f"""SELECT a.account_name, a.root_type,
                      ROUND(SUM(g.debit-g.credit),2)
               FROM `tabGL Entry` g JOIN `tabAccount` a ON g.account=a.name
               WHERE g.company=%s AND g.is_cancelled=0
                 AND a.root_type IN ('Asset','Liability','Equity')
                 {date_clause}
               GROUP BY a.account_name, a.root_type""", tuple(params))
        accounts, totals, controls = {}, {r: 0.0 for r in BS_ROOTS}, {}
        for acc_nm, rt, net in cur.fetchall():
            net = float(net)
            val = net if rt == "Asset" else -net
            nm = " ".join(acc_nm.split())
            accounts[nm] = val
            totals[rt] += val
            if nm in ("Debtors", "Creditors"):
                controls[nm] = val
        cur.execute(
            f"""SELECT ROUND(SUM(g.debit-g.credit),2)
               FROM `tabGL Entry` g JOIN `tabAccount` a ON g.account=a.name
               WHERE g.company=%s AND g.is_cancelled=0
                 AND a.root_type IN ('Income','Expense')
                 {date_clause}""", tuple(params))
        unclosed_pl = float(cur.fetchone()[0] or 0.0)
        return accounts, {k: round(v, 2) for k, v in totals.items()}, controls, unclosed_pl
    finally:
        conn.close()


# ---- comparison ----------------------------------------------------------
def run(from_date="20000101", to_date="20990101", top=25) -> None:
    cfg = get_config()
    company = cfg.erpnext["company"]
    client, store = TallyClient(), Staging()

    print("Fetching Tally balance-sheet ledgers ...")
    t_acc, t_tot, t_party = tally_bs_accounts(client, store, from_date, to_date)
    print("Fetching ERPNext balance sheet from GL ...")
    e_acc, e_tot, e_ctrl, unclosed = erpnext_bs_accounts(company, as_of=to_date)

    t_acc = _merge_gst_aliases(t_acc)
    e_acc = _merge_gst_aliases(e_acc)

    print("\n================  BALANCE SHEET HEADLINE  ================")
    print(f"{'':26}{'Tally':>18}{'ERPNext':>18}{'Diff':>15}")
    for rt in BS_ROOTS:
        _line(rt, t_tot[rt], e_tot[rt])
    _line("Liability + Equity", t_tot["Liability"] + t_tot["Equity"],
          e_tot["Liability"] + e_tot["Equity"])
    print("\n  Reconciling items (expected for a GL-faithful voucher migration):")
    print(f"  - ERPNext BS is out of balance by {unclosed:,.2f} = net P&L still in")
    print("    Income/Expense (no Period-Closing voucher). Tally shows this on the")
    print("    BS as its 'Profit & Loss A/c' line.")
    print("  - Capital Account is root_type Liability in ERPNext's CoA vs Equity in")
    print("    Tally -- compare the combined Liability+Equity row above.")
    print("  - Tally nests Output GST under GST->Current Assets; BS totals treat")
    print("    OUT PUT / Output Tax as Liability (India Compliance / ERPNext).")

    print("\n================  PARTY CONTROL ACCOUNTS  ================")
    print(f"{'':26}{'Tally':>18}{'ERPNext':>18}{'Diff':>15}")
    _line("Debtors (Sundry Debtors)", t_party["Sundry Debtors"],
          e_ctrl.get("Debtors", 0.0))
    _line("Creditors (Sundry Cred.)", t_party["Sundry Creditors"],
          e_ctrl.get("Creditors", 0.0))
    print("  Note: individual party ledgers appear only in Tally; ERPNext holds"
          "\n        them in the control accounts above with the party as a dimension.")

    print("\n================  GST FAMILY (aliased)  ================")
    print(f"{'':26}{'Tally':>18}{'ERPNext':>18}{'Diff':>15}")
    for label in (
        "Output Tax CGST", "Output Tax SGST", "Output Tax IGST",
        "Input Tax CGST", "Input Tax SGST", "Input Tax IGST",
    ):
        _line(label, t_acc.get(label, 0.0), e_acc.get(label, 0.0))
    t_net = sum(t_acc.get(k, 0.0) for k in (
        "Input Tax CGST", "Input Tax SGST", "Input Tax IGST")) - sum(
        t_acc.get(k, 0.0) for k in (
            "Output Tax CGST", "Output Tax SGST", "Output Tax IGST"))
    e_net = sum(e_acc.get(k, 0.0) for k in (
        "Input Tax CGST", "Input Tax SGST", "Input Tax IGST")) - sum(
        e_acc.get(k, 0.0) for k in (
            "Output Tax CGST", "Output Tax SGST", "Output Tax IGST"))
    _line("Net GST (Input-Output)", t_net, e_net)

    party_names = _party_ledger_names(store)
    # Control accounts are compared in the party section; skip name-level rows.
    party_names.update({"Debtors", "Creditors"})
    t_low = {k.lower(): (k, v) for k, v in t_acc.items()}
    e_low = {k.lower(): (k, v) for k, v in e_acc.items()}
    rows = []
    for key in sorted(set(t_low) | set(e_low)):
        name = (e_low.get(key) or t_low.get(key))[0]
        if name in party_names:
            continue
        tv = t_low.get(key, (None, 0.0))[1]
        ev = e_low.get(key, (None, 0.0))[1]
        d = round(ev - tv, 2)
        if abs(d) >= 1.0:
            rows.append((abs(d), name, tv, ev, d))
    rows.sort(reverse=True)

    rep = DATA_DIR / "reports" / "bs_compare.csv"
    with rep.open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["account", "tally", "erpnext", "diff(erp-tally)"])
        for _, name, tv, ev, d in rows:
            w.writerow([name, tv, ev, d])

    print(f"\n=======  TOP {top} NON-PARTY ACCOUNT DIFFERENCES  =======")
    print(f"{'account':<34}{'Tally':>16}{'ERPNext':>16}{'Diff':>14}")
    for _, name, tv, ev, d in rows[:top]:
        print(f"  {name[:32]:<32}{tv:>16,.2f}{ev:>16,.2f}{d:>14,.2f}")
    print(f"\nFull per-account comparison -> {rep}")
    print(f"Non-party accounts with a difference >= Rs 1: {len(rows)}")
    store.close()


def _party_ledger_names(store: Staging) -> set[str]:
    """Tally ledgers that map to an ERPNext party -- excluded from the per-account
    diff because ERPNext consolidates them into the control accounts."""
    tree = GroupTree(store)
    names: set[str] = set()
    for row in store.conn.execute("SELECT name, parent FROM master WHERE kind='ledger'"):
        parent = (row["parent"] or "").strip()
        if tree.party_kind(parent):
            names.add(" ".join((row["name"] or "").split()))
    return names


def _line(label, t, e):
    print(f"  {label:26}{t:>18,.2f}{e:>18,.2f}{e - t:>15,.2f}")
