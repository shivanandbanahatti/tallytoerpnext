import json
import unittest
from decimal import Decimal
from unittest.mock import Mock

from t2e.erpnext_client import ERPNextClient
from t2e.gst_setup import select_gst
from t2e.load_invoices import (
    InvoiceLoader, _gst_item_code, _tax_rate_from_ledger,
)
from t2e.load_masters import _party_tax_values
from t2e.load_vouchers import VoucherLoader
from t2e.mapping import CompanyDefaults, Resolved
from t2e.purchase_ocr import PurchaseOCRCatalog


class FakeResolver:
    def __init__(self, values):
        self.values = values

    def get(self, name):
        return self.values.get(name)


def defaults():
    return CompanyDefaults(
        name="Spaceki Designs LLP", abbr="SDL",
        receivable="Debtors - SDL", payable="Creditors - SDL",
        round_off="Rounded Off - SDL", cost_center="Main - SDL",
        currency="INR", suspense="Suspense - SDL", root_by_type={},
        default_warehouse="Stores - SDL",
    )


class InvoiceBuildTests(unittest.TestCase):
    def _loader(self, catalog=None):
        erp = Mock()
        erp.dry_run = True
        resolver = FakeResolver({
            "Customer A": Resolved("party", "Debtors - SDL", "Customer", "Customer A"),
            "Supplier A": Resolved("party", "Creditors - SDL", "Supplier", "Supplier A"),
            "Sales": Resolved("account", "Sales - SDL"),
            "Purchase": Resolved("account", "Purchase - SDL"),
            "GST PURCHASE": Resolved("account", "GST PURCHASE - SDL"),
            "TRANSPORTATION CHARGES": Resolved(
                "account", "TRANSPORTATION CHARGES - SDL"
            ),
            "OUT PUT CGST @ 9%": Resolved("account", "Output CGST - SDL"),
            "OUT PUT SGST @ 9%": Resolved("account", "Output SGST - SDL"),
            "CGST INPUT @ 9%": Resolved("account", "Input CGST - SDL"),
            "SGST INPUT @ 9%": Resolved("account", "Input SGST - SDL"),
            "CGST INPUT @2.5%": Resolved("account", "Input CGST - SDL"),
            "SGST INPUT @ 6%": Resolved("account", "Input SGST - SDL"),
            "IGST INPUT @ 18 %": Resolved("account", "Input IGST - SDL"),
            "TCS on purchases @ 2.5%": Resolved("account", "TCS Receivable - SDL"),
            "TDS Receivable FY 2025-26": Resolved("account", "TDS Receivable - SDL"),
            "ROUNDING OFF": Resolved("account", "Rounded Off - SDL"),
        })
        return InvoiceLoader(erp, Mock(), defaults(), resolver,
                             catalog or PurchaseOCRCatalog())

    def test_rounding_is_not_a_tax_row_and_erpnext_rounding_is_enabled(self):
        payload = {
            "PARTYGSTIN": "29ABCDE1234F1Z5",
            "NARRATION": "Tally sales narration",
            "ALLLEDGERENTRIES.LIST": [
            {"LEDGERNAME": "Customer A", "AMOUNT": "-118.00"},
            {"LEDGERNAME": "Sales", "AMOUNT": "100.10"},
            {"LEDGERNAME": "OUT PUT CGST @ 9%", "AMOUNT": "9.01"},
            {"LEDGERNAME": "OUT PUT SGST @ 9%", "AMOUNT": "9.01"},
            {"LEDGERNAME": "ROUNDING OFF", "AMOUNT": "-0.12"},
        ]}
        row = {
            "vtype": "Sales", "vnumber": "INV/42", "vdate": "2026-07-01",
            "guid": "guid-1", "payload": json.dumps(payload),
        }
        doc, _, _, _ = self._loader()._build(row)
        self.assertEqual(doc["disable_rounded_total"], 0)
        self.assertEqual(len(doc["taxes"]), 2)
        self.assertNotIn("ROUND", " ".join(t["description"] for t in doc["taxes"]))
        self.assertEqual([t["rate"] for t in doc["taxes"]], [9.0, 9.0])
        self.assertEqual([t["tax_amount"] for t in doc["taxes"]], [9.01, 9.01])
        self.assertTrue(all(t["charge_type"] == "On Net Total" for t in doc["taxes"]))
        self.assertEqual(doc["tax_category"], "In-State")
        self.assertEqual(doc["taxes_and_charges"], "Output GST In-state - SDL")
        self.assertEqual(doc["name"], "INV/42")
        self.assertEqual(doc["items"][0]["item_code"], "GST Sales at 18%")
        self.assertEqual(doc["items"][0]["gst_hsn_code"], "998399")
        self.assertEqual(doc["items"][0]["item_tax_template"], "GST 18% - SDL")
        self.assertEqual(doc["customer_gstin"], "29ABCDE1234F1Z5")
        self.assertEqual(doc["gst_category"], "Registered Regular")
        self.assertEqual(doc["remarks"], "Tally sales narration")
        self.assertEqual(
            [t["account_head"] for t in doc["taxes"]],
            ["Output Tax CGST - SDL", "Output Tax SGST - SDL"],
        )

    def test_credit_note_keeps_signed_tally_total_target(self):
        # Regression: abs(_tally_total_target) forced positive rounded_total on
        # submit and inverted Debtors GL (Rounded Off absorbed 2x the CN).
        payload = {
            "ALLLEDGERENTRIES.LIST": [
                {"LEDGERNAME": "Customer A", "AMOUNT": "1180.00"},
                {"LEDGERNAME": "Sales", "AMOUNT": "-1000.00"},
                {"LEDGERNAME": "OUT PUT CGST @ 9%", "AMOUNT": "-90.00"},
                {"LEDGERNAME": "OUT PUT SGST @ 9%", "AMOUNT": "-90.00"},
            ]
        }
        row = {
            "vtype": "Credit Note", "vnumber": "CN-1", "vdate": "2024-03-31",
            "guid": "guid-cn-1", "payload": json.dumps(payload),
        }
        doc, _, _, _ = self._loader()._build(row)
        self.assertEqual(doc["is_return"], 1)
        self.assertEqual(doc["items"][0]["qty"], -1)
        self.assertEqual(doc["_tally_total_target"], -1180.0)
        self.assertLess(doc["_tally_total_target"], 0)

    def test_tally_rounding_uses_native_fields_not_a_tax_row(self):
        payload = {"PARTYGSTIN": "29ANAPJ2662E1Z7", "ALLLEDGERENTRIES.LIST": [
            {"LEDGERNAME": "Supplier A", "AMOUNT": "17913.00"},
            {"LEDGERNAME": "Purchase", "AMOUNT": "-15180.00"},
            {"LEDGERNAME": "CGST INPUT @ 9%", "AMOUNT": "-1366.20"},
            {"LEDGERNAME": "SGST INPUT @ 9%", "AMOUNT": "-1366.20"},
            {"LEDGERNAME": "ROUNDING OFF", "AMOUNT": "-0.60"},
        ]}
        row = {
            "vtype": "Purchase", "vnumber": "9", "vdate": "2026-07-17",
            "guid": "guid-round-up", "payload": json.dumps(payload),
        }
        doc, _, _, _ = self._loader()._build(row)
        self.assertEqual(len(doc["taxes"]), 2)
        self.assertNotIn("ROUND", " ".join(t["description"] for t in doc["taxes"]))
        self.assertEqual(doc["_tally_rounding_override"], {
            "rounded_total": 17913.0,
            "rounding_adjustment": 0.6,
        })
        self.assertEqual(doc["_tally_total_target"], 17913.0)
        self.assertEqual(doc["items"][0]["gst_hsn_code"], "998399")
        self.assertEqual(doc["items"][0]["item_tax_template"], "GST 18% - SDL")
        self.assertEqual(doc["supplier_gstin"], "29ANAPJ2662E1Z7")

    def test_taxable_transport_is_distinct_item_on_voucher_339_shape(self):
        payload = {
            "NARRATION": (
                "PURCHASE OF 4 MM ACP SHEETS-21 NOS-62.51 SQM FOR SHREYAS SITE"
            ),
            "PARTYGSTIN": "29AABCV9909K1ZN",
            "PLACEOFSUPPLY": "Karnataka",
            "ALLLEDGERENTRIES.LIST": [
                {
                    "LEDGERNAME": "Supplier A", "AMOUNT": "73696.00",
                    "BILLALLOCATIONS.LIST": {
                        "NAME": "KA01AR24/2603900",
                        "BILLTYPE": "New Ref", "AMOUNT": "73696.00",
                    },
                },
                {"LEDGERNAME": "GST PURCHASE", "AMOUNT": "-59954.53"},
                {
                    "LEDGERNAME": "TRANSPORTATION CHARGES",
                    "AMOUNT": "-2500.00",
                    "GSTHSNSACCODE": "",
                },
                {"LEDGERNAME": "CGST INPUT @ 9%", "AMOUNT": "-5620.91"},
                {"LEDGERNAME": "SGST INPUT @ 9%", "AMOUNT": "-5620.91"},
                {"LEDGERNAME": "ROUNDING OFF", "AMOUNT": "0.35"},
            ],
        }
        row = {
            "vtype": "Purchase", "vnumber": "339",
            "vdate": "2025-03-29", "guid": "guid-voucher-339",
            "payload": json.dumps(payload),
        }
        doc, _, _, bill_no = self._loader()._build(row)
        self.assertEqual(bill_no, "KA01AR24/2603900")
        self.assertEqual(
            [(r["item_code"], r["rate"]) for r in doc["items"]],
            [
                ("GST Purchase at 18%", 59954.53),
                ("TRANSPORTATION CHARGES", 2500.0),
            ],
        )
        transport = doc["items"][1]
        self.assertEqual(
            transport["expense_account"], "TRANSPORTATION CHARGES - SDL"
        )
        self.assertEqual(transport["item_tax_template"], "GST 18% - SDL")
        self.assertEqual(transport["gst_hsn_code"], "996511")
        self.assertEqual(
            [(t["rate"], t["tax_amount"]) for t in doc["taxes"]],
            [(9.0, 5620.91), (9.0, 5620.91)],
        )
        self.assertEqual(doc["_tally_total_target"], 73696.0)
        self.assertEqual(
            doc["_tally_rounding_override"],
            {"rounded_total": 73696.0, "rounding_adjustment": -0.35},
        )
        self.assertEqual(doc["remarks"], payload["NARRATION"])

    def test_ocr_replaces_base_item_but_retains_taxable_transport(self):
        catalog = PurchaseOCRCatalog([{
            "bill_no": "SUP/9", "supplier": "Supplier A",
            "pdf_file": "purchase.pdf", "page": 9, "lines_ok": True,
            "lines": [{
                "item_name": "ACP Sheet", "qty": 2, "uom": "Nos",
                "rate": 50, "amount": 100, "gst_hsn_code": "7606",
            }],
        }])
        payload = {"ALLLEDGERENTRIES.LIST": [
            {
                "LEDGERNAME": "Supplier A", "AMOUNT": "129.80",
                "BILLALLOCATIONS.LIST": {
                    "NAME": "SUP/9", "BILLTYPE": "New Ref",
                    "AMOUNT": "129.80",
                },
            },
            {"LEDGERNAME": "Purchase", "AMOUNT": "-100.00"},
            {"LEDGERNAME": "TRANSPORTATION CHARGES", "AMOUNT": "-10.00"},
            {"LEDGERNAME": "CGST INPUT @ 9%", "AMOUNT": "-9.90"},
            {"LEDGERNAME": "SGST INPUT @ 9%", "AMOUNT": "-9.90"},
        ]}
        row = {
            "vtype": "Purchase", "vnumber": "9",
            "vdate": "2026-07-02", "guid": "guid-ocr-charge",
            "payload": json.dumps(payload),
        }
        doc, _, _, _ = self._loader(catalog)._build(row)
        self.assertEqual(
            [r["item_code"] for r in doc["items"]],
            ["ACP Sheet", "TRANSPORTATION CHARGES"],
        )
        self.assertEqual(doc["items"][1]["rate"], 10.0)
        self.assertEqual(doc["items"][1]["item_tax_template"], "GST 18% - SDL")

    def test_mixed_tax_rates_keep_exact_tally_amounts(self):
        payload = {"ALLLEDGERENTRIES.LIST": [
            {"LEDGERNAME": "Supplier A", "AMOUNT": "122.25"},
            {"LEDGERNAME": "Purchase", "AMOUNT": "-100.00"},
            {"LEDGERNAME": "CGST INPUT @2.5%", "AMOUNT": "-2.50"},
            {"LEDGERNAME": "SGST INPUT @ 6%", "AMOUNT": "-2.50"},
            {"LEDGERNAME": "IGST INPUT @ 18 %", "AMOUNT": "-18.00"},
            {"LEDGERNAME": "TCS on purchases @ 2.5%", "AMOUNT": "-1.25"},
            {"LEDGERNAME": "TDS Receivable FY 2025-26", "AMOUNT": "2.00"},
        ]}
        row = {
            "vtype": "Purchase", "vnumber": "2", "vdate": "2026-07-03",
            "guid": "guid-mixed", "payload": json.dumps(payload),
        }
        doc, _, _, _ = self._loader()._build(row)
        self.assertEqual(
            [t["charge_type"] for t in doc["taxes"]],
            ["On Net Total", "On Net Total", "On Net Total", "Actual", "Actual"],
        )
        self.assertEqual(
            [t["rate"] for t in doc["taxes"]],
            [2.5, 6.0, 18.0, 2.5, 0.0],
        )
        self.assertEqual(
            [t["tax_amount"] for t in doc["taxes"]],
            [2.5, 2.5, 18.0, 1.25, -2.0],
        )

    def test_interstate_purchase_uses_supplier_state_for_place_of_supply(self):
        payload = {
            "PARTYGSTIN": "36AANCS7711C1ZC",
            "PLACEOFSUPPLY": "Karnataka",
            "ALLLEDGERENTRIES.LIST": [
            {"LEDGERNAME": "Supplier A", "AMOUNT": "118.00"},
            {"LEDGERNAME": "Purchase", "AMOUNT": "-100.00"},
            {"LEDGERNAME": "IGST INPUT @ 18 %", "AMOUNT": "-18.00"},
        ]}
        row = {
            "vtype": "Purchase", "vnumber": "10", "vdate": "2026-07-17",
            "guid": "guid-interstate", "payload": json.dumps(payload),
        }
        doc, _, _, _ = self._loader()._build(row)
        self.assertEqual(doc["tax_category"], "Out-State")
        self.assertEqual(doc["place_of_supply"], "29-Karnataka")
        self.assertEqual(doc["supplier_address"], "Supplier A - Tally GST-Billing")
        self.assertEqual(doc["taxes"][0]["account_head"], "Input Tax IGST - SDL")

    def test_exact_ocr_bill_and_net_match_replaces_generic_purchase_item(self):
        catalog = PurchaseOCRCatalog([{
            "bill_no": "SUP/001", "supplier": "Supplier A", "ocr_date": "2026-07-02",
            "pdf_file": "purchase.pdf", "page": 4, "lines_ok": True,
            "lines": [{
                "item_name": "Aluminium Sheet", "description": "2mm",
                "qty": 2, "uom": "Nos", "rate": 50, "amount": 100,
                "gst_hsn_code": "7606",
            }],
        }])
        payload = {
            "NARRATION": "Original purchase narration",
            "ALLLEDGERENTRIES.LIST": [
            {"LEDGERNAME": "Supplier A", "AMOUNT": "118.00",
             "BILLALLOCATIONS.LIST": {"NAME": "SUP/001", "BILLTYPE": "New Ref",
                                      "AMOUNT": "118.00"}},
            {"LEDGERNAME": "Purchase", "AMOUNT": "-100.00"},
            {"LEDGERNAME": "CGST INPUT @ 9%", "AMOUNT": "-9.00"},
            {"LEDGERNAME": "SGST INPUT @ 9%", "AMOUNT": "-9.00"},
        ]}
        row = {
            "vtype": "Purchase", "vnumber": "1", "vdate": "2026-07-02",
            "guid": "guid-2", "payload": json.dumps(payload),
        }
        doc, _, _, bill_no = self._loader(catalog)._build(row)
        self.assertEqual(bill_no, "SUP/001")
        self.assertEqual(doc["bill_no"], "SUP/001")
        self.assertEqual(doc["update_stock"], 1)
        self.assertEqual(doc["items"][0]["item_code"], "Aluminium Sheet")
        self.assertEqual(doc["items"][0]["expense_account"], "Purchase - SDL")
        self.assertEqual(doc["taxes_and_charges"], "Input GST In-state - SDL")
        self.assertEqual(
            doc["remarks"],
            "Original purchase narration\n\n"
            "Purchase items extracted from purchase.pdf page 4.",
        )

    def test_ocr_whole_number_uom_preserves_total_and_source_quantity(self):
        match = type("Match", (), {"lines": ({
            "item_name": "Bolt", "qty": 290.278, "uom": "Nos",
            "amount": 5225, "description": "", "gst_hsn_code": "",
        },)})()
        rows, _ = self._loader()._ocr_item_rows(match, "Purchase - SDL")
        self.assertEqual(rows[0]["qty"], 1.0)
        self.assertAlmostEqual(rows[0]["qty"] * rows[0]["rate"], 5225, places=2)
        self.assertIn("OCR quantity: 290.278 Nos", rows[0]["description"])

    def test_ocr_line_uses_accounting_unit_when_currency_rate_loses_amount(self):
        match = type("Match", (), {"lines": ({
            "item_name": "Aluminium Profile", "qty": 53, "uom": "Nos",
            "rate": 617.66717, "amount": 32736.36,
            "description": "Profile", "gst_hsn_code": "7604",
        },)})()
        rows, _ = self._loader()._ocr_item_rows(match, "Purchase - SDL")
        self.assertEqual(rows[0]["qty"], 1.0)
        self.assertEqual(rows[0]["rate"], 32736.36)
        self.assertIn("OCR quantity: 53 Nos", rows[0]["description"])

    def test_invalid_extracted_hsn_falls_back_on_row_and_item_master(self):
        loader = self._loader()
        loader.erp.dry_run = False
        loader.erp.exists.side_effect = lambda doctype, name: doctype == "UOM"
        doc = {
            "items": [{
                "item_code": "Profile", "uom": "Nos",
                "gst_hsn_code": "7604",
                "item_tax_template": "GST 18% - SDL",
            }],
            "_ocr_item_definitions": [{
                "item_code": "Profile", "item_name": "Profile",
                "uom": "Nos", "description": "Profile",
                "gst_hsn_code": "7604",
            }],
        }
        loader._ensure_ocr_items(doc)
        self.assertEqual(doc["items"][0]["gst_hsn_code"], "998399")
        item_insert = next(
            call for call in loader.erp.insert.call_args_list
            if call.args[0] == "Item"
        )
        self.assertEqual(item_insert.args[1]["gst_hsn_code"], "998399")
        self.assertEqual(
            item_insert.args[1]["taxes"],
            [{"item_tax_template": "GST 18% - SDL"}],
        )


class SelectionTests(unittest.TestCase):
    def test_tax_rate_is_read_from_varied_ledger_names(self):
        cases = {
            "CGST INPUT @2.5%": 2.5,
            "SGST INPUT @ 6%": 6.0,
            "IGST 3 % Input": 3.0,
            "TDS on Commission @ 0.1 %": 0.1,
            "TCS on purchases  @ 2.5%": 2.5,
            "TDS Receivable FY 2025-26": 0.0,
            "Provision for CGST": 0.0,
        }
        for ledger, expected in cases.items():
            with self.subTest(ledger=ledger):
                self.assertEqual(_tax_rate_from_ledger(ledger), expected)

    def test_igst_selects_out_state(self):
        value = select_gst("Supplier", ["IGST INPUT @ 18 %"], "SDL")
        self.assertEqual(value.tax_category, "Out-State")
        self.assertEqual(value.template, "Input GST Out-state - SDL")

    def test_rate_specific_purchase_item_uses_combined_gst_rate(self):
        taxes = [
            {"ledger": "CGST INPUT @ 2.5%"},
            {"ledger": "SGST INPUT @ 2.5%"},
        ]
        self.assertEqual(_gst_item_code("Supplier", taxes), "GST Purchase at 5%")

    def test_party_pan_is_derived_from_gstin_when_tally_pan_is_blank(self):
        values = _party_tax_values({
            "PARTYGSTIN": "29ABCDE1234F1Z5",
            "GSTREGISTRATIONTYPE": "Regular",
        })
        self.assertEqual(values["gstin"], "29ABCDE1234F1Z5")
        self.assertEqual(values["pan"], "ABCDE1234F")
        self.assertEqual(values["gst_category"], "Registered Regular")

    def test_ocr_rejects_amount_mismatch(self):
        catalog = PurchaseOCRCatalog([{
            "bill_no": "A-1", "supplier": "Supplier A",
            "lines": [{"amount": 90, "item_name": "Item", "qty": 1}],
        }])
        self.assertIsNone(catalog.match("A/1", Decimal("100.00"), "Supplier A"))

    def test_ocr_nudges_small_line_drift_to_tally_net(self):
        catalog = PurchaseOCRCatalog([{
            "bill_no": "A-1", "supplier": "Supplier A",
            "lines": [{"amount": 99.77, "item_name": "Item", "qty": 2}],
        }])
        match = catalog.match("A/1", Decimal("100.00"), "Supplier A")
        self.assertEqual(match.lines[0]["amount"], 100.0)


class RenameTests(unittest.TestCase):
    def test_requested_name_is_applied_before_submit(self):
        client = ERPNextClient.__new__(ERPNextClient)
        client.dry_run = False
        client._request = Mock(side_effect=[
            {"data": {"name": "SINV-26-00001"}},
            {"message": "INV/42"},
            {"data": {"name": "INV/42"}},
        ])
        result = client.insert_and_submit(
            "Sales Invoice", {"name": "INV/42", "company": "Test"}
        )
        self.assertEqual(result["data"]["name"], "INV/42")
        rename_call = client._request.call_args_list[1]
        self.assertEqual(rename_call.args[1], "/api/method/frappe.client.rename_doc")
        self.assertEqual(rename_call.kwargs["json"]["new_name"], "INV/42")

    def test_rounding_override_is_carried_through_submit(self):
        client = ERPNextClient.__new__(ERPNextClient)
        client.dry_run = False
        client._request = Mock(side_effect=[
            {"data": {"name": "PINV-26-00001"}},
            {"data": {
                "name": "PINV-26-00001",
                "doctype": "Purchase Invoice",
                "conversion_rate": 1,
            }},
            {"message": {"name": "PINV-26-00001"}},
        ])
        client.insert_and_submit("Purchase Invoice", {
            "company": "Test",
            "_tally_rounding_override": {
                "rounded_total": 17913.0,
                "rounding_adjustment": 0.6,
            },
        })
        submit_call = client._request.call_args_list[2]
        self.assertEqual(
            submit_call.args[1], "/api/method/frappe.client.submit"
        )
        submitted = json.loads(submit_call.kwargs["json"]["doc"])
        self.assertEqual(submitted["rounded_total"], 17913.0)
        self.assertEqual(submitted["rounding_adjustment"], 0.6)
        self.assertEqual(submitted["is_consolidated"], 1)

    def test_sales_rounding_restores_consolidation_guard_after_submit(self):
        client = ERPNextClient.__new__(ERPNextClient)
        client.dry_run = False
        client._request = Mock(side_effect=[
            {"data": {"name": "SINV-26-00001"}},
            {"message": "SDL-03/2026-27"},
            {"data": {
                "name": "SDL-03/2026-27",
                "doctype": "Sales Invoice",
                "conversion_rate": 1,
            }},
            {"message": {"name": "SDL-03/2026-27"}},
        ])
        client._restore_sales_consolidation_flag = Mock()

        client.insert_and_submit("Sales Invoice", {
            "name": "SDL-03/2026-27",
            "company": "Test",
            "_tally_rounding_override": {
                "rounded_total": 815323.0,
                "rounding_adjustment": -0.36,
            },
        })

        client._restore_sales_consolidation_flag.assert_called_once_with(
            "SDL-03/2026-27"
        )

    def test_material_draft_drift_uses_native_roundoff_to_tally_target(self):
        client = ERPNextClient.__new__(ERPNextClient)
        client.dry_run = False
        client._request = Mock(side_effect=[
            {"data": {"name": "PINV-26-00027"}},
            {"data": {
                "name": "PINV-26-00027",
                "doctype": "Purchase Invoice",
                "grand_total": 43218.0,
                "rounded_total": 43218.0,
                "conversion_rate": 1,
            }},
            {"message": {"name": "PINV-26-00027"}},
        ])
        client.insert_and_submit("Purchase Invoice", {
            "company": "Test",
            "_tally_total_target": 41020.0,
        })
        submitted = json.loads(
            client._request.call_args_list[2].kwargs["json"]["doc"]
        )
        self.assertEqual(submitted["rounded_total"], 41020.0)
        self.assertEqual(submitted["rounding_adjustment"], -2198.0)


class VoucherNarrationTests(unittest.TestCase):
    def test_payment_submit_restores_tally_narration(self):
        loader = VoucherLoader.__new__(VoucherLoader)
        loader.erp = Mock()
        loader.erp.dry_run = False
        loader.erp.submit_doc.return_value = {
            "message": {"name": "ACC-PAY-2026-00001"},
        }
        loader.store = Mock()
        loader.d = defaults()
        loader.r = FakeResolver({
            "Supplier A": Resolved(
                "party", "Creditors - SDL", "Supplier", "Supplier A"
            ),
            "HDFC Bank": Resolved("account", "HDFC Bank - SDL"),
        })
        loader.field = "tally_guid"
        loader.vmap = {"Payment": {"mode": "pay"}}
        payload = {
            "NARRATION": "Tally payment narration",
            "ALLLEDGERENTRIES.LIST": [
                {"LEDGERNAME": "Supplier A", "AMOUNT": "-100"},
                {"LEDGERNAME": "HDFC Bank", "AMOUNT": "100"},
            ],
        }
        doctype, name = loader.load_one({
            "vtype": "Payment", "vnumber": "1", "vdate": "2026-07-01",
            "guid": "payment-guid", "payload": json.dumps(payload),
        })
        self.assertEqual((doctype, name), (
            "Payment Entry", "ACC-PAY-2026-00001"
        ))
        submitted = loader.erp.submit_doc.call_args.args[1]
        self.assertEqual(submitted["remarks"], "Tally payment narration")
        loader.erp.restore_payment_remarks.assert_called_once_with(
            "ACC-PAY-2026-00001", "Tally payment narration"
        )


if __name__ == "__main__":
    unittest.main()
