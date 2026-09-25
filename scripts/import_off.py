"""Import Open Food Facts products you can buy in Czechia into data/foods.db.

Kept: everything tagged Czechia or Slovakia, plus the own brands of the chains here (Lidl,
Kaufland, Albert, Billa, Penny, Tesco, Globus) whatever country they are tagged with.

    python scripts/import_off.py                    # stream the dump from openfoodfacts.org
    python scripts/import_off.py --file dump.csv.gz # or read a local copy
    python scripts/import_off.py --basic            # also (re)import seed/basic_foods.json

The dump is a multi-gigabyte gzip of tab-separated text. It is streamed and decompressed on
the fly and never stored on disk or held in memory; a line is only split into fields once a
cheap substring test says it mentions Czechia or Slovakia. Products are upserted by barcode,
so re-running the import refreshes the catalogue and never touches the food log, which keeps
its own snapshot of every entry.

Food data © Open Food Facts contributors, available under the Open Database License (ODbL).
"""

import argparse
import gzip
import io
import sys
import time
import unicodedata
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from food.catalog import clean_food, now_iso, open_catalog, rebuild_fts, upsert_foods  # noqa: E402

DUMP_URL = "https://static.openfoodfacts.org/data/en.openfoodfacts.org.products.csv.gz"
USER_AGENT = "GymTrack/1.0 (self-hosted)"
COUNTRIES = ("en:czech-republic", "en:slovakia")

# Chains that sell in Czechia, and their own brands. Their products carry the same barcode all
# over Europe but are often tagged only with the country where someone first added them, so
# they are kept whatever the country. Matched exactly against the (folded) brand or store name.
STORES = {"lidl", "kaufland", "albert", "billa", "penny", "penny market", "tesco", "globus"}
BRANDS = {
    # Lidl
    "lidl", "pilos", "milbona", "chef select", "freeway", "crownfield", "solevita", "deluxe",
    "snack day", "favorina", "combino", "harvest basket", "alesto", "fin carre", "bellarom",
    "sondey", "mcennedy", "italiamo", "nixe", "kania", "vitasia", "trattoria alfredo", "baresa",
    "dulano", "pikok", "gelatelli", "sol & mar", "eridanous", "el tequito", "grafschafter",
    "fairglobe", "envia", "golden seafood", "ocean sea", "belbake", "tastino", "maribel",
    "freshona", "lord nelson", "bon gelati", "vemondo", "mister choc",
    # Kaufland
    "k-classic", "k classic", "k-bio", "k-to go", "k-free", "k-purland", "k-favourites",
    # Albert, Billa, Penny, Tesco, Globus
    "albert", "albert quality", "albert excellent", "albert bio", "billa", "clever", "billa bio",
    "penny", "tesco", "tesco finest", "tesco value", "globus",
}
FILTER_COLUMNS = ("countries_tags", "brands", "stores")


def fold(text: str) -> str:
    text = text.lower()
    if not text.isascii():
        text = "".join(c for c in unicodedata.normalize("NFKD", text) if not unicodedata.combining(c))
    return " ".join(text.split())


def wanted(countries: str, brands: str, stores: str) -> bool:
    if countries and any(c in countries.split(",") for c in COUNTRIES):
        return True
    if brands and any(fold(b) in BRANDS for b in brands.split(",")):
        return True
    return bool(stores) and any(fold(s) in STORES for s in stores.split(","))
BATCH = 2000


def open_stream(url: str = "", path: str = ""):
    """A text stream over the decompressed dump, from a URL or a local file."""
    if path:
        raw = open(path, "rb")
    else:
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        raw = urllib.request.urlopen(req, timeout=60)
    binary = gzip.GzipFile(fileobj=raw) if (path or url).endswith(".gz") else raw
    return io.TextIOWrapper(binary, encoding="utf-8", errors="replace", newline="")


def product_row(fields: list[str], col: dict[str, int], stamp: str) -> dict | None:
    def get(name: str) -> str:
        i = col.get(name)
        return fields[i].strip() if i is not None and i < len(fields) else ""

    if not wanted(get("countries_tags"), get("brands"), get("stores")):
        return None
    barcode = get("code")
    if not barcode.isdigit() or get("energy-kcal_100g") == "":
        return None
    serving_g = get("serving_quantity")
    food = clean_food({
        "name": get("product_name_cs") or get("product_name_sk") or get("product_name") or get("generic_name"),
        "brand": get("brands").split(",")[0],
        "kcal_100g": get("energy-kcal_100g"),
        "protein_100g": get("proteins_100g"),
        "carbs_100g": get("carbohydrates_100g"),
        "fat_100g": get("fat_100g"),
        "fiber_100g": get("fiber_100g"),
        "serving_g": serving_g,
        "serving_label": get("serving_size") or (f"{serving_g} g" if serving_g else ""),
    })
    if food is None:
        return None
    return {**food, "source": "off", "source_id": barcode, "barcode": barcode,
            "aliases": "", "updated_at": stamp}


def import_stream(conn, stream, progress=True, on_progress=None) -> tuple[int, int]:
    """Returns (products imported, lines read)."""
    header = stream.readline().rstrip("\r\n").split("\t")
    col = {name: i for i, name in enumerate(header)}
    missing = {"code", "countries_tags", "energy-kcal_100g"} - col.keys()
    if missing:
        raise SystemExit(f"dump has no {', '.join(sorted(missing))} column — format changed?")

    filter_idx = [col.get(c, 10**6) for c in FILTER_COLUMNS]
    split_at = max(i for i in filter_idx if i < 10**6) + 1
    stamp, batch, kept, lines = now_iso(), [], 0, 0
    started = time.monotonic()
    for line in stream:
        lines += 1
        if on_progress and lines % 100_000 == 0:
            on_progress(lines, kept)
        if progress and lines % 500_000 == 0:
            print(f"  {lines:>10,} lines read, {kept:,} kept, {time.monotonic() - started:.0f} s",
                  flush=True)
        # Only the first few dozen columns are split to decide; the full split is for keepers.
        head = line.split("\t", split_at)
        if not wanted(*(head[i] if i < len(head) else "" for i in filter_idx)):
            continue
        row = product_row(line.rstrip("\r\n").split("\t"), col, stamp)
        if row is None:
            continue
        batch.append(row)
        if len(batch) >= BATCH:
            upsert_foods(conn, batch)
            conn.commit()
            kept += len(batch)
            batch = []
    if batch:
        upsert_foods(conn, batch)
        kept += len(batch)
    conn.commit()
    return kept, lines


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--url", default=DUMP_URL, help="dump URL (default: the official OFF export)")
    ap.add_argument("--file", default="", help="read a local .csv or .csv.gz instead of downloading")
    ap.add_argument("--db", default="", help="catalogue path (default: GYMTRACK_FOODS_DB or data/foods.db)")
    ap.add_argument("--basic", action="store_true", help="import seed/basic_foods.json as well")
    args = ap.parse_args(argv)

    conn = open_catalog(args.db)
    if args.basic:
        from scripts.import_basic import import_seed   # noqa: PLC0415
        print(f"basic foods: {import_seed(conn)}")

    started = time.monotonic()
    print(f"streaming {args.file or args.url}", flush=True)
    with open_stream(args.url, args.file) as stream:
        kept, lines = import_stream(conn, stream)
    print("rebuilding the search index", flush=True)
    rebuild_fts(conn)
    conn.commit()
    total = conn.execute("SELECT COUNT(*) FROM foods WHERE source='off'").fetchone()[0]
    conn.close()
    print(f"done in {time.monotonic() - started:.0f} s: {lines:,} lines read, {kept:,} products "
          f"imported, {total:,} Open Food Facts products in the catalogue")


if __name__ == "__main__":
    main()
