"""Fill the whole food catalogue: basic foods from USDA, then Open Food Facts.

The Food tab starts this in the background when the catalogue is empty (button "Download food
database"), so nothing has to be typed on the server. It can also be run by hand:

    python scripts/import_all.py

Progress goes to catalog_import.json next to foods.db, which the app reads to show it.
"""

import argparse
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from food.catalog import FOODS_DB, now_iso, open_catalog, rebuild_fts  # noqa: E402
from scripts import build_basic_seed, import_basic, import_off  # noqa: E402

STATUS = Path(FOODS_DB).parent / "catalog_import.json"


def write(status: dict) -> None:
    tmp = STATUS.with_suffix(".tmp")
    tmp.write_text(json.dumps(status), encoding="utf-8")
    os.replace(tmp, STATUS)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--fdc-zip", default="", help="local USDA FoodData Central CSV zip instead of downloading")
    ap.add_argument("--off-file", default="", help="local OFF .csv.gz instead of downloading")
    args = ap.parse_args(argv)

    status = {"state": "running", "stage": "basic", "pid": os.getpid(), "started_at": now_iso(),
              "finished_at": None, "basic": None, "off_lines": 0, "off_kept": 0, "errors": []}
    write(status)
    conn = open_catalog()

    try:
        seed = import_basic.seed_path()
        if seed is None:
            seed = build_basic_seed.main([*(["--zip", args.fdc_zip] if args.fdc_zip else []),
                                          "--out", str(import_basic.BUILT)])
        status["basic"] = import_basic.import_seed(conn, seed)
    except (Exception, SystemExit) as e:   # noqa: BLE001 — record it and carry on with OFF
        status["errors"].append(f"Basic foods (USDA): {e}")
    status["stage"] = "off"
    write(status)

    last = [0.0]

    def progress(lines, kept):
        if time.monotonic() - last[0] > 2:
            last[0] = time.monotonic()
            status["off_lines"], status["off_kept"] = lines, kept
            write(status)

    try:
        with import_off.open_stream(import_off.DUMP_URL, args.off_file) as stream:
            kept, lines = import_off.import_stream(conn, stream, progress=True, on_progress=progress)
        status["stage"] = "index"
        write(status)
        rebuild_fts(conn)
        conn.commit()
        status["off_lines"], status["off_kept"] = lines, kept
    except (Exception, SystemExit) as e:   # noqa: BLE001
        status["errors"].append(f"Open Food Facts: {e}")
    conn.close()

    status["state"] = "error" if len(status["errors"]) == 2 else "done"
    status["stage"] = None
    status["finished_at"] = now_iso()
    write(status)
    print(json.dumps(status, indent=1))
    return 0 if status["state"] == "done" else 1


if __name__ == "__main__":
    sys.exit(main())
