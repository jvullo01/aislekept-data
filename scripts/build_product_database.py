#!/usr/bin/env python3
"""Builds the compact on-device AisleKept product database from the Open Food Facts CSV export.

Data source: Open Food Facts (https://world.openfoodfacts.org), Open Database License (ODbL 1.0).
The output database is a derivative database and must be shared under the ODbL when published.

Usage:
    python3 scripts/build_product_database.py --csv products.csv.gz --output GroceryScanner.sqlite
    python3 scripts/build_product_database.py --csv sample.tsv --output Sample.sqlite --version sample-1

Requires: duckdb (pip install duckdb). Writes the manifest JSON next to the output file.
"""
import argparse
import datetime
import hashlib
import json
import os
import sqlite3

import sys

import duckdb

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

DEFAULT_COUNTRY_TAGS = ("en:united-states", "en:canada")
INSERT_BATCH_SIZE = 50_000

SCHEMA_STATEMENTS = (
    """CREATE TABLE products (
        barcode INTEGER PRIMARY KEY,   -- digits as a number, so UPC-A and EAN-13 forms match
        name TEXT NOT NULL,
        brand TEXT,
        energy_kcal REAL,              -- all nutrients are per 100 g or 100 ml
        fat REAL,
        saturated_fat REAL,
        sugars REAL,
        salt REAL,
        fiber REAL,
        protein REAL,
        additive_codes TEXT,           -- comma separated, e.g. "e330,e322i"
        allergen_codes TEXT,           -- comma separated, e.g. "milk,soybeans"
        processing_group INTEGER,      -- NOVA group 1-4, when known
        image_url TEXT,
        category_id INTEGER,           -- categories.id of the Open Food Facts main category (schema 2)
        unscored_kind TEXT,            -- alcohol, supplement, infant_formula, pet_food or cleaning (schema 2)
        data_flags INTEGER NOT NULL DEFAULT 0, -- DataFlag bits: what the source record contains (schema 2)
        ingredients_text TEXT          -- only for unscored or nutrition-less products, to keep the file small
    ) WITHOUT ROWID""",
    "CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL) WITHOUT ROWID",
    """CREATE TABLE categories (
        id INTEGER PRIMARY KEY,
        tag TEXT NOT NULL UNIQUE,      -- e.g. "en:greek-yogurts"
        name TEXT NOT NULL,            -- English name, e.g. "Greek yogurts"
        product_count INTEGER NOT NULL
    )""",
    """CREATE TABLE category_top_picks (
        category_id INTEGER NOT NULL,
        rank INTEGER NOT NULL,         -- 1 is the best pick in the category
        barcode INTEGER NOT NULL,
        pick_score INTEGER NOT NULL,   -- approximate 0-100 score; the app re-scores with its own calculator
        PRIMARY KEY (category_id, rank)
    ) WITHOUT ROWID""",
)
SCHEMA_VERSION = 2
INDEX_STATEMENTS = ("CREATE INDEX products_by_category ON products (category_id)",)


from pick_score import TOP_PICKS_PER_CATEGORY, TopPicks, approximate_score  # noqa: E402

# Categories whose products are kept but never scored (Yuka leaves them out entirely).
UNSCORED_CATEGORY_PATTERNS = (
    ("alcohol", ("en:alcoholic-beverages",)),
    ("infant_formula", ("en:infant-formula", "en:baby-milks", "en:infant-milks")),
    ("supplement", ("en:dietary-supplements",)),
    ("pet_food", ("en:pet-food", "en:pet-foods", "en:dog-food", "en:cat-food")),
    ("cleaning", ("en:cleaning-products", "en:detergents", "en:household-products")),
)
MAXIMUM_INGREDIENTS_LENGTH = 2_000


class DataFlag:
    """Bits of products.data_flags. The app derives the 'Partial data' label from them."""
    HAS_ENERGY = 1
    HAS_CORE_NUTRIENTS = 2   # at least 4 of fat, saturated fat, sugars, salt, fiber, protein
    HAS_INGREDIENTS = 4
    HAS_IMAGE = 8


def parse_arguments():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--csv", required=True, help="Open Food Facts CSV export (.csv or .csv.gz, tab separated)")
    parser.add_argument("--output", required=True, help="Path of the SQLite file to write")
    parser.add_argument("--version", help="Database version label (default: today's date, YYYYMMDD)")
    parser.add_argument("--countries", default=",".join(DEFAULT_COUNTRY_TAGS), help="Comma separated country tags")
    return parser.parse_args()


def grams(column):
    """SQL for a per-100 g nutrient; values outside 0-100 g are data-entry errors, so they become NULL."""
    return f"CASE WHEN TRY_CAST({column} AS DOUBLE) BETWEEN 0 AND 100 THEN TRY_CAST({column} AS DOUBLE) END"


def selection_query(csv_path, country_tags):
    """Returns DuckDB SQL that selects and cleans the rows we keep."""
    nutrient_columns = {
        "fat": grams("fat_100g"),
        "saturated_fat": grams('"saturated-fat_100g"'),
        "sugars": grams("sugars_100g"),
        "salt": grams("salt_100g"),
        "fiber": grams("fiber_100g"),
        "protein": grams("proteins_100g"),
    }
    country_filter = " OR ".join(f"countries_tags LIKE '%{tag}%'" for tag in country_tags)
    return f"""
        SELECT
            TRY_CAST(code AS BIGINT) AS barcode,
            trim(product_name) AS name,
            nullif(trim(split_part(brands, ',', 1)), '') AS brand,
            CASE WHEN TRY_CAST("energy-kcal_100g" AS DOUBLE) BETWEEN 0 AND 1000
                 THEN TRY_CAST("energy-kcal_100g" AS DOUBLE) END AS energy_kcal,
            {nutrient_columns["fat"]} AS fat,
            {nutrient_columns["saturated_fat"]} AS saturated_fat,
            {nutrient_columns["sugars"]} AS sugars,
            {nutrient_columns["salt"]} AS salt,
            {nutrient_columns["fiber"]} AS fiber,
            {nutrient_columns["protein"]} AS protein,
            nullif(replace(additives_tags, 'en:', ''), '') AS additive_codes,
            nullif(replace(allergens, 'en:', ''), '') AS allergen_codes,
            TRY_CAST(nova_group AS INTEGER) AS processing_group,
            nullif(image_small_url, '') AS image_url,
            nullif(trim(main_category), '') AS category_tag,
            nullif(trim(main_category_en), '') AS category_name,
            {unscored_kind_sql()} AS unscored_kind,
            nullif(trim(ingredients_text), '') AS ingredients_text
        FROM read_csv('{csv_path}', delim='\t', quote='', header=true, all_varchar=true,
                      ignore_errors=true, null_padding=true, max_line_size=20000000)
        WHERE ({country_filter})
          AND regexp_matches(code, '^[0-9]{{8,14}}$')
          AND nullif(trim(product_name), '') IS NOT NULL
        QUALIFY row_number() OVER (PARTITION BY TRY_CAST(code AS BIGINT) ORDER BY last_modified_t DESC) = 1
    """


def unscored_kind_sql():
    """SQL CASE that names the unscored category of a row from its categories_tags, or NULL."""
    branches = []
    for kind, tags in UNSCORED_CATEGORY_PATTERNS:
        condition = " OR ".join(f"categories_tags LIKE '%{tag}%'" for tag in tags)
        branches.append(f"WHEN {condition} THEN '{kind}'")
    return "CASE " + " ".join(branches) + " END"


SHORT_CODE_LIMIT = 10_000_000  # numeric values below this have fewer than 8 significant digits


def has_valid_gs1_check_digit(digits):
    """True when the last digit is the GS1 (UPC/EAN) check digit for the others."""
    body, check = digits[:-1], int(digits[-1])
    weighted_sum = sum(int(digit) * (3 if index % 2 == 0 else 1) for index, digit in enumerate(reversed(body)))
    return (10 - weighted_sum % 10) % 10 == check


SMALLEST_SHORT_CODE = 100_000  # below this a short code has 5 or fewer digits: produce PLUs and junk
MINIMUM_CORE_NUTRIENTS_FOR_SHORT_CODE = 4  # of fat, saturated fat, sugars, salt, fiber, protein
CORE_NUTRIENT_COLUMNS = slice(4, 10)  # positions of those six columns in a selected row


def expand_upc_e(eight_digits):
    """Expands an 8-digit UPC-E (number system 0 or 1) to its 12-digit UPC-A, or None."""
    number_system, middle, check = eight_digits[0], eight_digits[1:7], eight_digits[7]
    if number_system not in "01":
        return None
    last = middle[5]
    if last in "012":
        body = middle[0:2] + last + "0000" + middle[2:5]
    elif last == "3":
        body = middle[0:3] + "00000" + middle[3:5]
    elif last == "4":
        body = middle[0:4] + "00000" + middle[4]
    else:
        body = middle[0:5] + "0000" + last
    return number_system + body + check


def is_real_upc_e(numeric_barcode):
    """True when the short code is a UPC-E whose UPC-A expansion has a valid check digit."""
    upc_a = expand_upc_e(str(numeric_barcode).zfill(8))
    return upc_a is not None and has_valid_gs1_check_digit(upc_a)


def is_plausible_barcode(numeric_barcode, core_nutrient_count=6):
    """Drops junk keys such as "8" or "29" that Open Food Facts zero-padded to 8 digits (GS-004, GS-008).

    Codes with 8 or more significant digits are kept. In the US and Canada a shorter printed
    code is a UPC-E, so a short code is kept only when it is a real UPC-E (checked through its
    UPC-A expansion), has at least 6 digits, and comes with real nutrition data. Codes of 5 digits
    or fewer are produce PLUs or typing mistakes, and short EAN-8s starting with 0 are store-internal.
    """
    if numeric_barcode is None or numeric_barcode <= 0:
        return False
    if numeric_barcode >= SHORT_CODE_LIMIT:
        return True
    if numeric_barcode < SMALLEST_SHORT_CODE:
        return False
    has_nutrition = core_nutrient_count >= MINIMUM_CORE_NUTRIENTS_FOR_SHORT_CODE
    return has_nutrition and is_real_upc_e(numeric_barcode)


def is_plausible_row(row):
    """Applies `is_plausible_barcode` to a selected row (barcode first, then the nutrient columns)."""
    core_nutrient_count = sum(1 for value in row[CORE_NUTRIENT_COLUMNS] if value is not None)
    return is_plausible_barcode(row[0], core_nutrient_count)


SOURCE_COLUMN_COUNT = 14  # barcode .. image_url, the columns schema 1 already had


def data_flags_of(row):
    """DataFlag bits for a selected row (see `selection_query` for the column order)."""
    core_nutrient_count = sum(1 for value in row[CORE_NUTRIENT_COLUMNS] if value is not None)
    flags = 0
    if row[3] is not None:
        flags |= DataFlag.HAS_ENERGY
    if core_nutrient_count >= MINIMUM_CORE_NUTRIENTS_FOR_SHORT_CODE:
        flags |= DataFlag.HAS_CORE_NUTRIENTS
    if row[17] is not None:
        flags |= DataFlag.HAS_INGREDIENTS
    if row[13] is not None:
        flags |= DataFlag.HAS_IMAGE
    return flags


def stored_ingredients(row, flags):
    """Ingredients are kept only where the app shows them instead of a score, so the file stays small."""
    is_unscored = row[16] is not None
    has_nutrition = flags & DataFlag.HAS_CORE_NUTRIENTS and flags & DataFlag.HAS_ENERGY
    if row[17] is None or (not is_unscored and has_nutrition):
        return None
    return row[17][:MAXIMUM_INGREDIENTS_LENGTH]


class CategoryIndex:
    """Gives each main category a small integer id and counts its products."""

    def __init__(self):
        self.ids_by_tag = {}
        self.names_by_id = {}
        self.counts_by_id = {}

    def id_for(self, tag, name):
        if tag is None:
            return None
        category_id = self.ids_by_tag.get(tag)
        if category_id is None:
            category_id = len(self.ids_by_tag) + 1
            self.ids_by_tag[tag] = category_id
            self.names_by_id[category_id] = name or tag.split(":", 1)[-1].replace("-", " ").capitalize()
            self.counts_by_id[category_id] = 0
        self.counts_by_id[category_id] += 1
        return category_id

    def rows(self):
        return [(category_id, tag, self.names_by_id[category_id], self.counts_by_id[category_id])
                for tag, category_id in self.ids_by_tag.items()]


def stored_row(row, categories, top_picks):
    """Turns a selected row into a products row and feeds the top-picks ranking."""
    flags = data_flags_of(row)
    category_id = categories.id_for(row[14], row[15])
    unscored_kind = row[16]
    is_rankable = unscored_kind is None and flags & DataFlag.HAS_ENERGY and flags & DataFlag.HAS_CORE_NUTRIENTS
    if category_id is not None and is_rankable:
        top_picks.offer(category_id, row[0], approximate_score(row))
    return row[:SOURCE_COLUMN_COUNT] + (category_id, unscored_kind, flags, stored_ingredients(row, flags))


def write_database(rows_cursor, output_path, version, source_description):
    if os.path.exists(output_path):
        os.remove(output_path)
    database = sqlite3.connect(output_path)
    for statement in SCHEMA_STATEMENTS:
        database.execute(statement)
    categories = CategoryIndex()
    top_picks = TopPicks(TOP_PICKS_PER_CATEGORY)
    product_count = 0
    while True:
        batch = rows_cursor.fetchmany(INSERT_BATCH_SIZE)
        if not batch:
            break
        batch = [stored_row(row, categories, top_picks) for row in batch if is_plausible_row(row)]
        database.executemany("INSERT OR REPLACE INTO products VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", batch)
        product_count += len(batch)
    database.executemany("INSERT INTO categories VALUES (?, ?, ?, ?)", categories.rows())
    database.executemany("INSERT INTO category_top_picks VALUES (?, ?, ?, ?)", top_picks.rows())
    for statement in INDEX_STATEMENTS:
        database.execute(statement)
    metadata = {
        "version": version,
        "built_at": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
        "source": source_description,
        "license": "Open Database License (ODbL) 1.0, https://opendatacommons.org/licenses/odbl/1-0/",
        "product_count": str(product_count),
        "schema_version": str(SCHEMA_VERSION),
    }
    database.executemany("INSERT INTO metadata VALUES (?, ?)", metadata.items())
    database.commit()
    database.execute("VACUUM")
    database.close()
    return product_count


def sha256_of_file(path):
    digest = hashlib.sha256()
    with open(path, "rb") as file:
        for chunk in iter(lambda: file.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def write_manifest(output_path, version, product_count):
    manifest = {
        "version": version,
        "databaseFileName": os.path.basename(output_path),
        "sha256": sha256_of_file(output_path),
        "productCount": product_count,
        "byteCount": os.path.getsize(output_path),
    }
    manifest_path = os.path.splitext(output_path)[0] + "-manifest.json"
    with open(manifest_path, "w") as file:
        json.dump(manifest, file, indent=2)
    return manifest_path


def main():
    arguments = parse_arguments()
    version = arguments.version or datetime.date.today().strftime("%Y%m%d")
    country_tags = tuple(tag.strip() for tag in arguments.countries.split(",") if tag.strip())
    connection = duckdb.connect()
    rows_cursor = connection.execute(selection_query(arguments.csv, country_tags))
    source = "Open Food Facts CSV export (https://world.openfoodfacts.org/data)"
    product_count = write_database(rows_cursor, arguments.output, version, source)
    manifest_path = write_manifest(arguments.output, version, product_count)
    size_megabytes = os.path.getsize(arguments.output) / 1_000_000
    print(f"Wrote {product_count} products, {size_megabytes:.1f} MB, to {arguments.output}; manifest {manifest_path}")


if __name__ == "__main__":
    main()
