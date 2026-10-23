"""Run with: python3 scripts/test_build_product_database.py (needs duckdb installed for the import)."""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(__file__))
import build_product_database as build  # noqa: E402


class PlausibleBarcodeTests(unittest.TestCase):
    def test_short_junk_keys_are_dropped(self):
        for value in (0, 8, 29, 116, 40617, 1234567, None):
            self.assertFalse(build.is_plausible_barcode(value), value)

    def test_real_codes_are_kept(self):
        for value in (4963406, 12345670, 236555909, 49000028911, 9999999999994):
            self.assertTrue(build.is_plausible_barcode(value), value)

    def test_short_upc_e_needs_nutrition(self):
        # 04963406 is the UPC-E printed on a Coca-Cola can (UPC-A 049000006346).
        self.assertTrue(build.is_plausible_barcode(4963406, core_nutrient_count=4))
        self.assertFalse(build.is_plausible_barcode(4963406, core_nutrient_count=3))

    def test_row_filter_counts_core_nutrients(self):
        row = (4963406, "Cola", "Brand", 42.0, 0.0, 0.0, 10.6, 0.01, None, 0.0, None, None, 4, None)
        self.assertTrue(build.is_plausible_row(row))
        sparse_row = row[:4] + (None, None, None, 0.01, None, 0.0) + row[10:]
        self.assertFalse(build.is_plausible_row(sparse_row))


if __name__ == "__main__":
    unittest.main()
