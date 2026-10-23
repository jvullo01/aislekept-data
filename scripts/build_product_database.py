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

import duckdb

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
        image_url TEXT
    ) WITHOUT ROWID""",
    "CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL) WITHOUT ROWID",
)


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
            TRY_CAST("energy-kcal_100g" AS DOUBLE) AS energy_kcal,
            {nutrient_columns["fat"]} AS fat,
            {nutrient_columns["saturated_fat"]} AS saturated_fat,
            {nutrient_columns["sugars"]} AS sugars,
            {nutrient_columns["salt"]} AS salt,
            {nutrient_columns["fiber"]} AS fiber,
            {nutrient_columns["protein"]} AS protein,
            nullif(replace(additives_tags, 'en:', ''), '') AS additive_codes,
            nullif(replace(allergens, 'en:', ''), '') AS allergen_codes,
            TRY_CAST(nova_group AS INTEGER) AS processing_group,
            nullif(image_small_url, '') AS image_url
        FROM read_csv('{csv_path}', delim='\t', quote='', header=true, all_varchar=true,
                      ignore_errors=true, null_padding=true, max_line_size=20000000)
        WHERE ({country_filter})
          AND regexp_matches(code, '^[0-9]{{8,14}}$')
          AND nullif(trim(product_name), '') IS NOT NULL
          AND TRY_CAST("energy-kcal_100g" AS DOUBLE) BETWEEN 0 AND 1000
        QUALIFY row_number() OVER (PARTITION BY TRY_CAST(code AS BIGINT) ORDER BY last_modified_t DESC) = 1
    """


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


def write_database(rows_cursor, output_path, version, source_description):
    if os.path.exists(output_path):
        os.remove(output_path)
    database = sqlite3.connect(output_path)
    for statement in SCHEMA_STATEMENTS:
        database.execute(statement)
    product_count = 0
    while True:
        batch = rows_cursor.fetchmany(INSERT_BATCH_SIZE)
        if not batch:
            break
        batch = [row for row in batch if is_plausible_row(row)]
        database.executemany("INSERT OR REPLACE INTO products VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)", batch)
        product_count += len(batch)
    metadata = {
        "version": version,
        "built_at": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
        "source": source_description,
        "license": "Open Database License (ODbL) 1.0, https://opendatacommons.org/licenses/odbl/1-0/",
        "product_count": str(product_count),
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
