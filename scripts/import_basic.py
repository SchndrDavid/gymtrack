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

from food.catalog import open_catalog, rebuild_fts  # noqa: E402,F401
from food.seed import SEED, built_path, import_seed, seed_path  # noqa: E402,F401

BUILT = built_path()


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
