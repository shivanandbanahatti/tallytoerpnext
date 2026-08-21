"""Unit tests for GST BS root classification and aliases."""
from __future__ import annotations

import unittest
from unittest.mock import MagicMock

from t2e.bs_check import _gst_canonical, _merge_gst_aliases
from t2e.mapping import GroupTree


class GstBsClassificationTests(unittest.TestCase):
    def test_output_gst_is_liability_even_under_current_assets(self):
        store = MagicMock()
        store.masters.return_value = [
            {"name": "GST", "parent": "Current Assets"},
            {"name": "Current Assets", "parent": ""},
            {"name": "Duties & Taxes", "parent": "Current Liabilities"},
            {"name": "Current Liabilities", "parent": ""},
        ]
        tree = GroupTree(store)
        self.assertEqual(tree.root_type("GST"), "Asset")
        self.assertEqual(
            tree.ledger_root_type("OUT PUT CGST @ 9%", "GST"), "Liability"
        )
        self.assertEqual(
            tree.ledger_root_type("CGST INPUT @ 9%", "GST"), "Asset"
        )

    def test_gst_aliases_collapse_to_india_compliance_heads(self):
        self.assertEqual(_gst_canonical("OUT PUT CGST @ 9%"), "Output Tax CGST")
        self.assertEqual(_gst_canonical("CGST INPUT @ 9%"), "Input Tax CGST")
        self.assertEqual(_gst_canonical("Unclaimed SGST"), "Input Tax SGST")
        merged = _merge_gst_aliases({
            "OUT PUT CGST @ 9%": 100.0,
            "CGST INPUT @ 9%": 40.0,
            "Unclaimed CGST": 10.0,
            "Canara": 5.0,
        })
        self.assertEqual(merged["Output Tax CGST"], 100.0)
        self.assertEqual(merged["Input Tax CGST"], 50.0)
        self.assertEqual(merged["Canara"], 5.0)
        self.assertNotIn("OUT PUT CGST @ 9%", merged)


if __name__ == "__main__":
    unittest.main()
