"""Import the basic foods (seed/basic_foods.json, USDA values) into data/foods.db.

    python scripts/import_basic.py            # import the committed seed
    python scripts/import_basic.py --build    # build it from the USDA release first, then import

With --build the seed is written next to the catalogue (data/basic_foods.json), because the
application directory inside the container is read-only. Copy that file to seed/ in the
repository to commit it.
"""

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from food.catalog import FOODS_DB, clean_food, now_iso, open_catalog, rebuild_fts, upsert_foods  # noqa: E402

SEED = ROOT / "seed" / "basic_foods.json"
BUILT = Path(FOODS_DB).parent / "basic_foods.json"


def seed_path() -> Path | None:
    for p in (SEED, BUILT):
        if p.exists():
            return p
    return None


def import_seed(conn, path: Path | None = None) -> int:
    path = path or seed_path()
    if path is None:
        raise SystemExit("no basic_foods.json — run `python scripts/import_basic.py --build` first")
    foods = json.loads(path.read_text(encoding="utf-8"))["foods"]
    stamp, rows = now_iso(), []
    for f in foods:
        clean = clean_food(f)
        if clean is None or not f.get("fdc_id"):
            print(f"  skipped {f.get('name')!r}: invalid values")
            continue
        rows.append({**clean, "source": "usda", "source_id": str(f["fdc_id"]), "barcode": None,
                     "aliases": f.get("usda_description", ""), "updated_at": stamp})
    upsert_foods(conn, rows)
    rebuild_fts(conn)
    conn.commit()
    return len(rows)


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--build", action="store_true", help="build the seed from the USDA release first")
    ap.add_argument("--zip", action="append", default=[], help="local FDC CSV zip for --build")
    ap.add_argument("--file", default="", help="seed file to import")
    ap.add_argument("--db", default="", help="catalogue path")
    args = ap.parse_args(argv)

    path = Path(args.file) if args.file else None
    if args.build:
        from scripts.build_basic_seed import main as build   # noqa: PLC0415
        path = build([*sum((["--zip", z] for z in args.zip), []), "--out", str(BUILT)])
    conn = open_catalog(args.db)
    n = import_seed(conn, path)
    total = conn.execute("SELECT COUNT(*) FROM foods WHERE source='usda'").fetchone()[0]
    conn.close()
    print(f"{n} basic foods imported, {total} in the catalogue")


if __name__ == "__main__":
    main()
