"""Counts store-brand products in a built AisleKept database and flags drops between releases.

Usage:
    python scripts/brand_coverage.py --database release/GroceryScanner.sqlite \
        --output release/brand-coverage.json [--previous previous/brand-coverage.json]

Writes brand-coverage.json, appends a table to $GITHUB_STEP_SUMMARY when it is set, and prints a
GitHub Actions ::warning:: line for every brand whose count fell since the previous release.
It only reads the database and the previous counts; it never deletes anything.
"""

import argparse
import json
import os
import sqlite3

# Display name -> lower-case spellings of the first brand in products.brand.
STORE_BRANDS = {
    "Great Value": ("great value",),
    "Kirkland": ("kirkland", "kirkland signature"),
    "Trader Joe's": ("trader joe's", "trader joes", "trader joe"),
    "Aldi": ("aldi",),
    "Member's Mark": ("member's mark", "members mark"),
    "Good & Gather": ("good & gather", "good and gather", "good&gather"),
    "Market Pantry": ("market pantry",),
    "365": ("365", "365 everyday value", "365 by whole foods market", "whole foods market 365"),
    "Simply Nature": ("simply nature",),
    "Kroger": ("kroger",),
    "Wegmans": ("wegmans", "wegman's"),
}


def normalized_brand_sql():
    """SQL that lower-cases the brand, trims it and turns curly apostrophes into straight ones."""
    return "lower(trim(replace(brand, char(8217), char(39))))"


def count_brands(database_path):
    database = sqlite3.connect(f"file:{database_path}?mode=ro", uri=True)
    try:
        counts = {}
        for display_name, spellings in STORE_BRANDS.items():
            placeholders = ",".join("?" for _ in spellings)
            query = f"SELECT count(*) FROM products WHERE {normalized_brand_sql()} IN ({placeholders})"
            counts[display_name] = database.execute(query, spellings).fetchone()[0]
        version_row = database.execute("SELECT value FROM metadata WHERE key = 'version'").fetchone()
        return counts, (version_row[0] if version_row else None)
    finally:
        database.close()


def read_previous_counts(previous_path):
    if not previous_path or not os.path.exists(previous_path):
        return {}
    with open(previous_path, encoding="utf-8") as handle:
        return json.load(handle).get("counts", {})


def drops(counts, previous_counts):
    """(brand, previous, current) for every brand whose count went down."""
    return [(brand, previous_counts[brand], count) for brand, count in counts.items()
            if brand in previous_counts and count < previous_counts[brand]]


def summary_markdown(version, counts, previous_counts):
    lines = [f"### Store-brand coverage ({version})", "", "| Brand | Products | Previous release |",
             "|---|---:|---:|"]
    for brand, count in counts.items():
        previous = previous_counts.get(brand, "n/a")
        lines.append(f"| {brand} | {count} | {previous} |")
    return "\n".join(lines) + "\n"


def parse_arguments():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--database", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--previous", help="brand-coverage.json of the previous release, if any")
    return parser.parse_args()


def main():
    arguments = parse_arguments()
    counts, version = count_brands(arguments.database)
    previous_counts = read_previous_counts(arguments.previous)
    with open(arguments.output, "w", encoding="utf-8") as handle:
        json.dump({"version": version, "counts": counts}, handle, indent=2)
        handle.write("\n")
    summary = summary_markdown(version, counts, previous_counts)
    print(summary)
    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary_path:
        with open(summary_path, "a", encoding="utf-8") as handle:
            handle.write(summary)
    for brand, previous, current in drops(counts, previous_counts):
        print(f"::warning title=Store-brand coverage dropped::{brand} fell from {previous} to {current} products")


if __name__ == "__main__":
    main()
