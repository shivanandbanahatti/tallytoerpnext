"""ERPNext (Frappe v16) REST client.

Token auth (api_key:api_secret). All mutating calls (insert/update/delete/
submit) are routed through ``_write`` which is a no-op unless the client was
constructed with ``dry_run=False`` -- the CLI only disables dry-run when the
user passes ``--confirm``.
"""
from __future__ import annotations

import json
import time
import urllib.parse
from typing import Any

import requests

from .config import get_config


class ERPNextError(RuntimeError):
    def __init__(self, message: str, status: int | None = None, body: str | None = None):
        super().__init__(message)
        self.status = status
        self.body = body


class ERPNextClient:
    def __init__(self, dry_run: bool = True) -> None:
        cfg = get_config()
        self.base = cfg.erp_url
        self.dry_run = dry_run
        self.session = requests.Session()
        self.session.headers.update({
            "Authorization": f"token {cfg.erp_token}",
            "Accept": "application/json",
            "Content-Type": "application/json",
        })
        self.session.verify = cfg.erp_verify_ssl
        if not cfg.erp_verify_ssl:
            import urllib3
            urllib3.disable_warnings()  # insecure SSL is intentional per .env
        self.timeout = 60

    # ---- low-level -------------------------------------------------------
    def _request(self, method: str, path: str, **kw) -> Any:
        url = f"{self.base}{path}"
        last: Exception | None = None
        for attempt in range(1, 4):
            try:
                resp = self.session.request(method, url, timeout=self.timeout, **kw)
                if resp.status_code in (429, 502, 503, 504):
                    time.sleep(2 * attempt)
                    continue
                if not resp.ok:
                    raise ERPNextError(
                        f"{method} {path} -> {resp.status_code}",
                        resp.status_code, resp.text[:2000],
                    )
                return resp.json() if resp.content else {}
            except requests.RequestException as exc:
                last = exc
                time.sleep(2 * attempt)
        raise ERPNextError(f"{method} {path} failed: {last}")

    def _write(self, method: str, path: str, **kw) -> Any:
        if self.dry_run:
            return {"_dry_run": True, "method": method, "path": path,
                    "payload": kw.get("json")}
        return self._request(method, path, **kw)

    def run_doc_method(self, method: str, doc: dict,
                       args: dict | None = None) -> dict:
        """Call a whitelisted controller method on an (unsaved) doc, exactly the
        way the Frappe desk form does (``run_doc_method``): the server builds the
        doc in memory, runs ``method`` on it and returns the mutated doc.

        Returns the updated doc (``docs[0]``) so the caller can chain calls, e.g.
        get_unreconciled_entries -> allocate_entries -> reconcile on Payment
        Reconciliation. This is always routed through ``_request`` (even in
        dry-run) because read-only methods must run to produce a plan; callers
        MUST gate any state-changing method (e.g. ``reconcile``) on
        ``self.dry_run`` themselves.
        """
        body: dict[str, Any] = {"method": method, "docs": json.dumps(doc)}
        if args is not None:
            body["args"] = json.dumps(args)
        res = self._request("POST", "/api/method/run_doc_method", json=body)
        docs = res.get("docs") or []
        return docs[0] if docs else {}

    # ---- reads -----------------------------------------------------------
    def get_count(self, doctype: str, filters: dict | None = None) -> int:
        params = {"doctype": doctype}
        if filters:
            params["filters"] = json.dumps([[k, "=", v] for k, v in filters.items()])
        dt = urllib.parse.quote(doctype)
        # frappe.client.get_count expects doctype + filters as query args
        q = urllib.parse.urlencode(params)
        return self._request("GET", f"/api/method/frappe.client.get_count?{q}")["message"]

    def get_list(self, doctype: str, fields: list[str] | None = None,
                 filters: list | None = None, limit: int = 0) -> list[dict]:
        dt = urllib.parse.quote(doctype)
        params: dict[str, str] = {}
        params["fields"] = json.dumps(fields or ["name"])
        if filters:
            params["filters"] = json.dumps(filters)
        params["limit_page_length"] = str(limit)
        out: list[dict] = []
        start = 0
        page = limit or 500
        while True:
            params["limit_start"] = str(start)
            params["limit_page_length"] = str(page)
            q = urllib.parse.urlencode(params)
            data = self._request("GET", f"/api/resource/{dt}?{q}")["data"]
            out.extend(data)
            if limit or len(data) < page:
                break
            start += page
        return out

    def exists(self, doctype: str, name: str) -> bool:
        dt = urllib.parse.quote(doctype)
        nm = urllib.parse.quote(str(name), safe="")
        try:
            self._request("GET", f"/api/resource/{dt}/{nm}")
            return True
        except ERPNextError as exc:
            if exc.status == 404:
                return False
            raise

    def find_by_field(self, doctype: str, field: str, value: str) -> str | None:
        rows = self.get_list(doctype, fields=["name"],
                             filters=[[field, "=", value]], limit=1)
        return rows[0]["name"] if rows else None

    # ---- writes ----------------------------------------------------------
    def insert(self, doctype: str, doc: dict) -> Any:
        dt = urllib.parse.quote(doctype)
        doc = {**doc, "doctype": doctype}
        return self._write("POST", f"/api/resource/{dt}", json=doc)

    def update(self, doctype: str, name: str, values: dict) -> Any:
        dt = urllib.parse.quote(doctype)
        nm = urllib.parse.quote(str(name), safe="")
        return self._write("PUT", f"/api/resource/{dt}/{nm}", json=values)

    def upload_file(self, filename: str, content: bytes, attach_to_doctype: str,
                    attach_to_name: str, is_private: int = 1) -> Any:
        """Upload a file and attach it to a doc via Frappe's upload_file endpoint.

        Multipart, so the session's default JSON Content-Type must be dropped for
        this one call (requests sets the multipart boundary itself)."""
        if self.dry_run:
            return {"_dry_run": True, "file": filename,
                    "attached_to": f"{attach_to_doctype}/{attach_to_name}"}
        # Null Content-Type so requests drops the session's JSON default and sets
        # the multipart boundary itself (auth still comes from the session).
        headers = {"Content-Type": None}
        data = {
            "is_private": str(is_private),
            "folder": "Home/Attachments",
            "doctype": attach_to_doctype,
            "docname": attach_to_name,
        }
        resp = self.session.post(
            f"{self.base}/api/method/upload_file",
            files={"file": (filename, content)}, data=data,
            headers=headers, timeout=max(self.timeout, 300))
        if not resp.ok:
            raise ERPNextError(f"upload_file {filename} -> {resp.status_code}",
                               resp.status_code, resp.text[:1000])
        return resp.json()

    def submit_doc(self, doctype: str, doc: dict) -> Any:
        """Insert a submittable doc and submit it in one shot."""
        doc = {**doc, "doctype": doctype, "docstatus": 1}
        return self._write("POST", "/api/method/frappe.client.submit",
                           json={"doc": json.dumps(doc)})

    def insert_and_submit(self, doctype: str, doc: dict) -> Any:
        """Insert a draft, optionally rename it to a requested name, then submit.

        Sales Invoice on this site uses Prompt autoname, so a ``name`` must be
        supplied at insert (Tally invoice number). Sites that use naming_series
        ignore a supplied name and generate one; we then rename the draft to the
        requested Tally number before submit.
        """
        if self.dry_run:
            return {"_dry_run": True, "doctype": doctype}
        dt = urllib.parse.quote(doctype)
        draft = {k: v for k, v in doc.items() if k != "docstatus"}
        rounding_override = draft.pop("_tally_rounding_override", None)
        tally_total_target = draft.pop("_tally_total_target", None)
        requested_name = str(draft.pop("name", "") or "").strip()
        draft["doctype"] = doctype
        # Prompt autoname requires name on insert; series autoname ignores it.
        if requested_name:
            draft["name"] = requested_name
        res = self._request("POST", f"/api/resource/{dt}", json=draft)
        name = res["data"]["name"]
        if requested_name and requested_name != name:
            self._request(
                "POST", "/api/method/frappe.client.rename_doc",
                json={
                    "doctype": doctype,
                    "old_name": name,
                    "new_name": requested_name,
                    "force": 0,
                    "merge": 0,
                },
            )
            name = requested_name
        nm = urllib.parse.quote(str(name), safe="")
        submit_doc = None
        if rounding_override or tally_total_target is not None:
            submit_doc = self._request(
                "GET", f"/api/resource/{dt}/{nm}"
            )["data"]
        if tally_total_target is not None and submit_doc is not None:
            native_total = float(submit_doc.get("rounded_total") or 0)
            target = float(tally_total_target)
            # Compare signed totals. Using abs(target) for returns used to force
            # a positive rounded_total on Credit/Debit Notes and invert party GL.
            if abs(native_total - target) > 0.50:
                grand_total = float(submit_doc.get("grand_total") or 0)
                rounding_override = {
                    "rounded_total": target,
                    "rounding_adjustment": target - grand_total,
                }
            elif not rounding_override:
                rounding_override = None
        if rounding_override:
            # ERPNext normally recomputes rounded_total from grand_total during
            # validation. Tally can explicitly round 17,912.40 to 17,913, which
            # no global nearest-value rounding mode can express. Submit the
            # complete document with ERPNext's internal consolidation guard so
            # its native rounded_total/rounding_adjustment fields (and round-off
            # GL entry) remain authoritative, without adding a tax row.
            conversion_rate = float(submit_doc.get("conversion_rate") or 1)
            submit_doc.update({
                "is_consolidated": 1,
                "rounded_total": rounding_override["rounded_total"],
                "rounding_adjustment": rounding_override["rounding_adjustment"],
                "base_rounded_total": (
                    rounding_override["rounded_total"] * conversion_rate
                ),
                "base_rounding_adjustment": (
                    rounding_override["rounding_adjustment"] * conversion_rate
                ),
            })
            self._request(
                "POST", "/api/method/frappe.client.submit",
                json={"doc": json.dumps(submit_doc)},
            )
            if doctype == "Sales Invoice":
                # is_consolidated is a real Sales Invoice field used only as a
                # transient calculation guard here. Frappe rejects changing it
                # through the document API after submission, so restore the
                # business value directly without touching modified timestamps
                # or rerunning invoice validation.
                self._restore_sales_consolidation_flag(name)
        else:
            self._request("PUT", f"/api/resource/{dt}/{nm}", json={"docstatus": 1})
        return {"data": {"name": name}}

    def _restore_sales_consolidation_flag(self, name: str) -> None:
        """Clear the temporary Sales Invoice calculation guard after submit."""
        import pymysql

        p = get_config().db_params
        conn = pymysql.connect(
            host=p["host"], port=p["port"], user=p["user"],
            password=p["password"], database=p["database"],
            connect_timeout=20, autocommit=False,
        )
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE `tabSales Invoice` SET is_consolidated=0 "
                    "WHERE name=%s AND docstatus=1",
                    (name,),
                )
                if cur.rowcount != 1:
                    raise ERPNextError(
                        f"could not restore is_consolidated for Sales Invoice {name}"
                    )
            conn.commit()
        finally:
            conn.close()

    def restore_payment_remarks(self, name: str, remarks: str) -> None:
        """Restore source remarks replaced by Payment Entry's submit hook.

        ``remarks`` is not allow-on-submit, while ERPNext always rewrites it
        during submission. Updating this non-accounting display field directly
        is the only way to preserve Tally narration without cancelling and
        reposting the payment; GL and modified timestamps remain untouched.
        """
        if self.dry_run:
            return
        import pymysql

        p = get_config().db_params
        conn = pymysql.connect(
            host=p["host"], port=p["port"], user=p["user"],
            password=p["password"], database=p["database"],
            connect_timeout=20, autocommit=False,
        )
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE `tabPayment Entry` SET remarks=%s "
                    "WHERE name=%s AND docstatus=1",
                    (remarks[:1000], name),
                )
                if cur.rowcount != 1:
                    raise ERPNextError(
                        f"could not restore remarks for Payment Entry {name}"
                    )
            conn.commit()
        finally:
            conn.close()

    def delete(self, doctype: str, name: str) -> Any:
        dt = urllib.parse.quote(doctype)
        nm = urllib.parse.quote(str(name), safe="")
        return self._write("DELETE", f"/api/resource/{dt}/{nm}")

    def delete_items(self, doctype: str, names: list[str], chunk: int = 30,
                     timeout: int = 300) -> int:
        """Bulk-delete docs by name (Frappe's reportview.delete_items), chunked.
        Batches >10 are enqueued server-side; deletes that cascade to attachments
        can be slow, so use a generous per-call timeout. Errors on a chunk are
        swallowed (the caller re-queries to see what actually went). Returns the
        number of names submitted; no-op in dry-run."""
        if self.dry_run or not names:
            return 0
        saved, self.timeout = self.timeout, timeout
        try:
            for i in range(0, len(names), chunk):
                batch = names[i:i + chunk]
                # delete_items expects `items` as a JSON string (it json.loads it).
                try:
                    self._request("POST", "/api/method/frappe.desk.reportview.delete_items",
                                  json={"doctype": doctype, "items": json.dumps(batch)})
                except ERPNextError:
                    pass  # server may still have enqueued it; caller verifies
        finally:
            self.timeout = saved
        return len(names)

    def cancel(self, doctype: str, name: str) -> Any:
        return self._write("POST", "/api/method/frappe.client.cancel",
                           json={"doctype": doctype, "name": name})

    def rename(self, doctype: str, old_name: str, new_name: str) -> Any:
        return self._write(
            "POST", "/api/method/frappe.client.rename_doc",
            json={
                "doctype": doctype, "old_name": old_name,
                "new_name": new_name, "force": 0, "merge": 0,
            },
        )

    def submit_existing(self, doctype: str, name: str) -> Any:
        dt = urllib.parse.quote(doctype)
        nm = urllib.parse.quote(str(name), safe="")
        return self._write("PUT", f"/api/resource/{dt}/{nm}",
                           json={"docstatus": 1})

    def set_doctype_property(self, doctype: str, property_name: str,
                             value: str, property_type: str) -> None:
        """Set a DocType property through Frappe's cache-aware Property Setter."""
        rows = self.get_list(
            "Property Setter", fields=["name", "value"],
            filters=[
                ["doc_type", "=", doctype],
                ["doctype_or_field", "=", "DocType"],
                ["property", "=", property_name],
            ], limit=1,
        )
        values = {
            "doc_type": doctype,
            "doctype_or_field": "DocType",
            "property": property_name,
            "property_type": property_type,
            "value": str(value),
        }
        if rows:
            if str(rows[0].get("value")) != str(value):
                self.update("Property Setter", rows[0]["name"], values)
        else:
            self.insert("Property Setter", values)

    def ensure_custom_field(self, doctype: str, fieldname: str,
                            label: str | None = None, fieldtype: str = "Data") -> None:
        """Create a custom field (idempotency key) on a doctype if absent."""
        cf_name = f"{doctype}-{fieldname}"
        if self.exists("Custom Field", cf_name):
            return
        self.insert("Custom Field", {
            "dt": doctype,
            "fieldname": fieldname,
            "label": label or fieldname.replace("_", " ").title(),
            "fieldtype": fieldtype,
            "read_only": 1,
            "no_copy": 1,
            "insert_after": "name",
            "translatable": 0,
        })
