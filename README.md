# AisleKept product data

A weekly, compact SQLite database of US and Canada grocery products, built from
[Open Food Facts](https://world.openfoodfacts.org) for offline barcode lookups in the AisleKept app.

## Attribution and licence

Contains information from Open Food Facts, made available under the
[Open Database License (ODbL) 1.0](https://opendatacommons.org/licenses/odbl/1-0/).
Individual entries are under the [Database Contents License](https://opendatacommons.org/licenses/dbcl/1-0/).

The published database is a derivative database and is shared alike under the ODbL. If you reuse it,
keep the attribution, and share your own derived database under the ODbL too. See [DATA-LICENSE.md](DATA-LICENSE.md).
The scripts are MIT licensed. This project is not made by or affiliated with Open Food Facts.

## Downloads

The newest release always has these stable links:

- Manifest: https://github.com/jvullo01/aislekept-data/releases/latest/download/manifest.json
- Database: https://github.com/jvullo01/aislekept-data/releases/latest/download/AisleKept-products.sqlite.deflate

The database is raw DEFLATE compressed (`python3 -c "import zlib,sys; sys.stdout.buffer.write(zlib.decompress(open(sys.argv[1],'rb').read(), -15))" AisleKept-products.sqlite.deflate > products.sqlite`).
`manifest.json` lists the version, build time, schema version, minimum app version, product count,
and SHA-256 and size of both the compressed and the decompressed file.

## How it is built

`.github/workflows/weekly-release.yml` runs every Monday (and on demand):

1. Downloads the official Open Food Facts CSV export.
2. `scripts/build_product_database.py` keeps US and Canada products with a name and a valid barcode, including
   ones without nutrition data (the app labels them "Partial data"). Alcohol, dietary supplements, infant formula,
   pet food and cleaning products are kept and tagged in `unscored_kind`; the app shows their ingredients and
   allergens without a score.
3. `scripts/package_release.py` compresses the database and writes `manifest.json`.
4. `scripts/brand_coverage.py` counts 11 store brands (Great Value, Kirkland, Trader Joe's, Aldi, Member's Mark,
   Good & Gather, Market Pantry, 365, Simply Nature, Kroger, Wegmans), writes `brand-coverage.json` to the release
   and the job summary, and adds a warning annotation when a count falls below the previous release.
5. Publishes a release tagged `db-YYYYMMDD` (`db-YYYYMMDD-HHMM` for a second run on the same day). Every release
   is kept; nothing is overwritten or deleted.

## Schema 2 (from build 8 of the app)

- `products` gains `category_id`, `unscored_kind`, `data_flags` (1 energy, 2 at least four core nutrients,
  4 ingredients, 8 image) and `ingredients_text` (stored only for unscored or nutrition-less products).
- `categories(id, tag, name, product_count)` indexes the Open Food Facts main category.
- `category_top_picks(category_id, rank, barcode, pick_score)` holds the ten best products per category by an
  approximate score; the app re-scores them before suggesting a better pick.
- `manifest.json` has `schemaVersion: 2`. AisleKept 0.1.0 (8) and later read schema 1 and 2.

If the build fails or the product count looks wrong, no release is made and the previous one stays current.

Run locally: `pip install duckdb && python3 scripts/build_product_database.py --csv en.openfoodfacts.org.products.csv.gz --output AisleKept-products.sqlite`.
