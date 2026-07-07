"""SQLite staging store.

Every Tally master/voucher is parsed into a normalized row and stored here as
JSON, keyed by Tally GUID. Staging decouples extraction from loading: we can
re-run the loader against ERPNext without re-hitting Tally, and the per-record
``load_status`` / ``erp_name`` / ``error`` columns drive the reconciliation
report and retries.
"""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from typing import Any, Iterator

from .config import get_config

SCHEMA = """
CREATE TABLE IF NOT EXISTS master (
    guid        TEXT PRIMARY KEY,
    kind        TEXT NOT NULL,         -- group | ledger | item | unit | godown | costcentre | vouchertype
    name        TEXT NOT NULL,
    parent      TEXT,
    payload     TEXT NOT NULL,         -- full parsed JSON
    erp_doctype TEXT,
    erp_name    TEXT,
    load_status TEXT DEFAULT 'pending',-- pending | loaded | skipped | error
    error       TEXT
);
CREATE INDEX IF NOT EXISTS idx_master_kind ON master(kind);
CREATE INDEX IF NOT EXISTS idx_master_status ON master(load_status);

CREATE TABLE IF NOT EXISTS voucher (
    guid        TEXT PRIMARY KEY,
    vtype       TEXT NOT NULL,         -- Tally voucher type name
    vnumber     TEXT,
    vdate       TEXT,                  -- yyyy-mm-dd
    party       TEXT,
    amount      REAL,
    payload     TEXT NOT NULL,         -- full parsed JSON (ledger + inventory entries)
    erp_doctype TEXT,
    erp_name    TEXT,
    load_status TEXT DEFAULT 'pending',
    error       TEXT
);
CREATE INDEX IF NOT EXISTS idx_voucher_type ON voucher(vtype);
CREATE INDEX IF NOT EXISTS idx_voucher_status ON voucher(load_status);
CREATE INDEX IF NOT EXISTS idx_voucher_date ON voucher(vdate);

-- Bill (invoice) reference index: maps a Tally party + bill name to the ERPNext
-- invoice created for it, so payments/journals can allocate against it and drive
-- the invoice's paid / partially-paid status.
CREATE TABLE IF NOT EXISTS bill_ref (
    party    TEXT NOT NULL,   -- ERPNext party name
    billname TEXT NOT NULL,   -- Tally bill reference (New Ref name)
    doctype  TEXT NOT NULL,   -- Sales Invoice | Purchase Invoice
    invoice  TEXT NOT NULL,   -- ERPNext invoice name
    PRIMARY KEY (party, billname)
);
"""


class Staging:
    def __init__(self) -> None:
        self.path = get_config().staging_db
        self.conn = sqlite3.connect(self.path)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        try:
            yield self.conn
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise

    # ---- writes ----------------------------------------------------------
    def upsert_master(self, kind: str, guid: str, name: str,
                      parent: str | None, payload: dict[str, Any]) -> None:
        self.conn.execute(
            """INSERT INTO master(guid,kind,name,parent,payload)
                 VALUES(?,?,?,?,?)
               ON CONFLICT(guid) DO UPDATE SET
                 kind=excluded.kind, name=excluded.name,
                 parent=excluded.parent, payload=excluded.payload""",
            (guid, kind, name, parent, json.dumps(payload, ensure_ascii=False)),
        )

    def upsert_voucher(self, guid: str, vtype: str, vnumber: str | None,
                       vdate: str | None, party: str | None, amount: float,
                       payload: dict[str, Any]) -> None:
        self.conn.execute(
            """INSERT INTO voucher(guid,vtype,vnumber,vdate,party,amount,payload)
                 VALUES(?,?,?,?,?,?,?)
               ON CONFLICT(guid) DO UPDATE SET
                 vtype=excluded.vtype, vnumber=excluded.vnumber,
                 vdate=excluded.vdate, party=excluded.party,
                 amount=excluded.amount, payload=excluded.payload""",
            (guid, vtype, vnumber, vdate, party, amount,
             json.dumps(payload, ensure_ascii=False)),
        )

    def mark(self, table: str, guid: str, status: str,
             erp_doctype: str | None = None, erp_name: str | None = None,
             error: str | None = None) -> None:
        assert table in ("master", "voucher")
        self.conn.execute(
            f"""UPDATE {table} SET load_status=?, erp_doctype=COALESCE(?,erp_doctype),
                  erp_name=COALESCE(?,erp_name), error=? WHERE guid=?""",
            (status, erp_doctype, erp_name, error, guid),
        )

    # ---- reads -----------------------------------------------------------
    def masters(self, kind: str | None = None,
                status: str | None = None) -> list[sqlite3.Row]:
        q = "SELECT * FROM master WHERE 1=1"
        args: list[Any] = []
        if kind:
            q += " AND kind=?"; args.append(kind)
        if status:
            q += " AND load_status=?"; args.append(status)
        q += " ORDER BY name"
        return self.conn.execute(q, args).fetchall()

    def vouchers(self, vtype: str | None = None, status: str | None = None,
                 order_by_date: bool = True) -> list[sqlite3.Row]:
        q = "SELECT * FROM voucher WHERE 1=1"
        args: list[Any] = []
        if vtype:
            q += " AND vtype=?"; args.append(vtype)
        if status:
            q += " AND load_status=?"; args.append(status)
        q += " ORDER BY vdate, vnumber" if order_by_date else " ORDER BY guid"
        return self.conn.execute(q, args).fetchall()

    # ---- bill references -------------------------------------------------
    def add_bill_ref(self, party: str, billname: str, doctype: str, invoice: str) -> None:
        self.conn.execute(
            """INSERT INTO bill_ref(party,billname,doctype,invoice) VALUES(?,?,?,?)
               ON CONFLICT(party,billname) DO UPDATE SET
                 doctype=excluded.doctype, invoice=excluded.invoice""",
            (party, billname, doctype, invoice))

    def get_bill_ref(self, party: str, billname: str):
        return self.conn.execute(
            "SELECT doctype, invoice FROM bill_ref WHERE party=? AND billname=?",
            (party, billname)).fetchone()

    def clear_bill_refs(self) -> None:
        self.conn.execute("DELETE FROM bill_ref")
        self.conn.commit()

    def counts(self) -> dict[str, dict[str, int]]:
        out: dict[str, dict[str, int]] = {}
        for table, col in (("master", "kind"), ("voucher", "vtype")):
            rows = self.conn.execute(
                f"SELECT {col} k, load_status s, COUNT(*) c "
                f"FROM {table} GROUP BY {col}, load_status"
            ).fetchall()
            for r in rows:
                out.setdefault(f"{table}:{r['k']}", {})[r["s"]] = r["c"]
        return out

    def close(self) -> None:
        self.conn.close()
