"""Run with: python3 scripts/test_brand_coverage.py"""
import json
import os
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(__file__))
import brand_coverage  # noqa: E402


class BrandCoverageTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.database_path = os.path.join(self.directory.name, "test.sqlite")
        database = sqlite3.connect(self.database_path)
        database.execute("CREATE TABLE products (barcode INTEGER PRIMARY KEY, brand TEXT)")
        database.execute("CREATE TABLE metadata (key TEXT, value TEXT)")
        database.execute("INSERT INTO metadata VALUES ('version', '20261023')")
        brands = ["Great Value", "great value ", "Trader Joe\u2019s", "Trader Joe's", "Kirkland Signature",
                  "365 by Whole Foods Market", "Other"]
        database.executemany("INSERT INTO products VALUES (?, ?)", list(enumerate(brands, start=1)))
        database.commit()
        database.close()

    def tearDown(self):
        self.directory.cleanup()

    def test_counts_spellings_case_and_curly_apostrophes(self):
        counts, version = brand_coverage.count_brands(self.database_path)
        self.assertEqual(version, "20261023")
        self.assertEqual(counts["Great Value"], 2)
        self.assertEqual(counts["Trader Joe's"], 2)
        self.assertEqual(counts["Kirkland"], 1)
        self.assertEqual(counts["365"], 1)
        self.assertEqual(counts["Wegmans"], 0)
        self.assertEqual(len(counts), 11)

    def test_drops_are_reported_only_for_falling_counts(self):
        self.assertEqual(brand_coverage.drops({"Aldi": 5, "Kroger": 9}, {"Aldi": 6, "Kroger": 3}), [("Aldi", 6, 5)])
        self.assertEqual(brand_coverage.drops({"Aldi": 5}, {}), [])

    def test_missing_previous_file_means_no_comparison(self):
        self.assertEqual(brand_coverage.read_previous_counts(os.path.join(self.directory.name, "none.json")), {})
        path = os.path.join(self.directory.name, "previous.json")
        with open(path, "w") as handle:
            json.dump({"counts": {"Aldi": 4}}, handle)
        self.assertEqual(brand_coverage.read_previous_counts(path), {"Aldi": 4})


if __name__ == "__main__":
    unittest.main()
