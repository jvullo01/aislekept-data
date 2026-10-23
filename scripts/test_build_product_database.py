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


def selected_row(**overrides):
    """A selected row in selection_query order, with nutrition, a category and no ingredients."""
    values = dict(barcode=4963406, name="Cola", brand="Brand", energy=42.0, fat=0.0, saturated_fat=0.0,
                  sugars=10.6, salt=0.01, fiber=None, protein=0.0, additives=None, allergens=None,
                  processing=4, image=None, category_tag="en:sodas", category_name="Sodas",
                  unscored_kind=None, ingredients=None)
    values.update(overrides)
    return tuple(values.values())


class SchemaTwoTests(unittest.TestCase):
    def test_schema_version_is_two(self):
        self.assertEqual(build.SCHEMA_VERSION, 2)

    def test_energy_is_no_longer_required(self):
        query = build.selection_query("x.csv", ["en:united-states"])
        self.assertNotIn("energy", query.split("WHERE", 1)[1])

    def test_unscored_categories_are_named(self):
        sql = build.unscored_kind_sql()
        for kind in ("alcohol", "supplement", "infant_formula", "pet_food", "cleaning"):
            self.assertIn(f"'{kind}'", sql)

    def test_data_flags(self):
        complete = selected_row(image="https://x/y.jpg", ingredients="Water, sugar")
        self.assertEqual(build.data_flags_of(complete), 1 | 2 | 4 | 8)
        bare = selected_row(energy=None, fat=None, saturated_fat=None, sugars=None, protein=None)
        self.assertEqual(build.data_flags_of(bare), 0)

    def test_ingredients_kept_only_where_shown_instead_of_a_score(self):
        scored = selected_row(ingredients="Water")
        self.assertIsNone(build.stored_ingredients(scored, build.data_flags_of(scored)))
        wine = selected_row(unscored_kind="alcohol", ingredients="Grapes")
        self.assertEqual(build.stored_ingredients(wine, build.data_flags_of(wine)), "Grapes")

    def test_stored_row_assigns_category_and_ranks_scored_products(self):
        categories = build.CategoryIndex()
        picks = build.TopPicks(10)
        stored = build.stored_row(selected_row(), categories, picks)
        self.assertEqual(len(stored), 18)
        self.assertEqual(stored[14], 1)
        self.assertEqual(categories.rows(), [(1, "en:sodas", "Sodas", 1)])
        self.assertEqual(len(picks.rows()), 1)
        build.stored_row(selected_row(barcode=12345670, unscored_kind="alcohol"), categories, picks)
        self.assertEqual(len(picks.rows()), 1)


class TopPicksTests(unittest.TestCase):
    def test_keeps_best_scores_in_rank_order(self):
        picks = build.TopPicks(2)
        for barcode, score in ((1, 50), (2, 90), (3, 70), (4, 10)):
            picks.offer(7, barcode, score)
        self.assertEqual(picks.rows(), [(7, 1, 2, 90), (7, 2, 3, 70)])

    def test_approximate_score_matches_app_for_a_clean_food(self):
        oats = selected_row(energy=380.0, fat=7.0, saturated_fat=1.2, sugars=1.0, salt=0.0, fiber=10.0,
                            protein=13.0, processing=1)
        # nutrition 60 - 4 (calories medium) - 2 (fat medium) + 6 + 4 capped at 60 -> 60; +25 +15
        self.assertEqual(build.approximate_score(oats), 100)


if __name__ == "__main__":
    unittest.main()
