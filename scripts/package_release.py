#!/usr/bin/env python3
"""Compresses a built AisleKept product database and writes the release manifest.

The database is compressed with raw DEFLATE (zlib, no header), which Apple's Compression
framework reads as COMPRESSION_ZLIB. The manifest describes both the compressed asset
(what the app downloads) and the decompressed database (what the app installs).

Usage:
    python3 scripts/package_release.py --database AisleKept-products.sqlite \
        --version 20261023 --release-base-url https://github.com/OWNER/REPO/releases/download/TAG
"""
import argparse
import datetime
import hashlib
import json
import os
import sqlite3
import zlib

SCHEMA_VERSION = 2  # bump when the products/metadata tables change shape
MINIMUM_APP_VERSION = "0.1.0"
CHUNK_SIZE = 1 << 20
LICENSE_TEXT = "Open Database License (ODbL) 1.0, https://opendatacommons.org/licenses/odbl/1-0/"
ATTRIBUTION_TEXT = "Contains information from Open Food Facts (https://world.openfoodfacts.org), made available under the ODbL."


def parse_arguments():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--database", required=True, help="SQLite file written by build_product_database.py")
    parser.add_argument("--version", required=True, help="Release version label, e.g. 20261023")
    parser.add_argument("--release-base-url", required=True, help="Download URL prefix of the tagged release")
    parser.add_argument("--output-directory", default=".", help="Where to write the asset and manifest.json")
    return parser.parse_args()


def sha256_of_file(path):
    digest = hashlib.sha256()
    with open(path, "rb") as file:
        for chunk in iter(lambda: file.read(CHUNK_SIZE), b""):
            digest.update(chunk)
    return digest.hexdigest()


def compress_raw_deflate(source_path, destination_path):
    compressor = zlib.compressobj(9, zlib.DEFLATED, -15)
    with open(source_path, "rb") as source, open(destination_path, "wb") as destination:
        for chunk in iter(lambda: source.read(CHUNK_SIZE), b""):
            destination.write(compressor.compress(chunk))
        destination.write(compressor.flush())


def product_count_of(database_path):
    connection = sqlite3.connect(f"file:{database_path}?mode=ro", uri=True)
    try:
        return connection.execute("SELECT count(*) FROM products").fetchone()[0]
    finally:
        connection.close()


def build_manifest(database_path, asset_path, version, release_base_url):
    asset_name = os.path.basename(asset_path)
    return {
        "version": version,
        "builtAt": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
        "schemaVersion": SCHEMA_VERSION,
        "minimumAppVersion": MINIMUM_APP_VERSION,
        "productCount": product_count_of(database_path),
        "databaseFileName": asset_name,
        "databaseURL": f"{release_base_url.rstrip('/')}/{asset_name}",
        "compression": "deflate",
        "sha256": sha256_of_file(asset_path),
        "byteCount": os.path.getsize(asset_path),
        "decompressedSHA256": sha256_of_file(database_path),
        "decompressedByteCount": os.path.getsize(database_path),
        "license": LICENSE_TEXT,
        "attribution": ATTRIBUTION_TEXT,
    }


def main():
    arguments = parse_arguments()
    os.makedirs(arguments.output_directory, exist_ok=True)
    asset_name = os.path.basename(arguments.database) + ".deflate"
    asset_path = os.path.join(arguments.output_directory, asset_name)
    compress_raw_deflate(arguments.database, asset_path)
    manifest = build_manifest(arguments.database, asset_path, arguments.version, arguments.release_base_url)
    manifest_path = os.path.join(arguments.output_directory, "manifest.json")
    with open(manifest_path, "w") as file:
        json.dump(manifest, file, indent=2)
    print(json.dumps(manifest, indent=2))


if __name__ == "__main__":
    main()
