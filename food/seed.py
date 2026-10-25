"""The basic foods (USDA values, seed/basic_foods.json) in the catalogue.

The seed is committed to the repository, so a fresh install has its basic foods and meat presets
at once, without any download. When the app starts it compares the seed file with the one last
loaded into foods.db and loads it again only if it changed — a local file, no network, a few
milliseconds. The Open Food Facts products are a separate, bigger download.
"""

import hashlib
import json
import os
from pathlib import Path

from .catalog import FOODS_DB, clean_food, now_iso, open_catalog, rebuild_fts, upsert_foods

ROOT = Path(__file__).resolve().parent.parent
SEED = ROOT / "seed" / "basic_foods.json"


def built_path() -> Path:
    return Path(FOODS_DB).parent / "basic_foods.json"


def seed_path() -> Path | None:
    for p in (SEED, built_path()):
        if p.exists():
            return p
    return None


def import_seed(conn, path: Path | None = None, rebuild: bool = True) -> int:
    path = path or seed_path()
    if path is None:
        raise SystemExit("no basic_foods.json — run `python scripts/import_basic.py --build` first")
    foods = json.loads(path.read_text(encoding="utf-8"))["foods"]
    stamp, rows = now_iso(), []
    for f in foods:
        clean = clean_food(f, max_kcal=920)   # USDA values are checked; pure fat is 900–902 kcal
        if clean is None or not f.get("fdc_id"):
            print(f"  skipped {f.get('name')!r}: invalid values")
            continue
        rows.append({**clean, "source": "usda", "source_id": str(f["fdc_id"]), "barcode": None,
                     "aliases": f.get("usda_description", ""), "updated_at": stamp})
    upsert_foods(conn, rows)
    # A food whose USDA source changed gets a new fdc_id; drop the old row so the name is not listed twice.
    keep = {r["source_id"] for r in rows}
    for r in rows:
        stale = [x["source_id"] for x in conn.execute(
            "SELECT source_id FROM foods WHERE source='usda' AND name=?", (r["name"],)) if x["source_id"] not in keep]
        for sid in stale:
            conn.execute("DELETE FROM foods WHERE source='usda' AND source_id=?", (sid,))
    if rebuild:
        rebuild_fts(conn)
    conn.commit()
    return len(rows)


def sync(path: Path | None = None) -> int | None:
    """Load the committed seed if it changed since the last time. Returns the row count, or None."""
    if os.environ.get("FOOD_SEED_SYNC", "1").strip().lower() in ("0", "false", "no", "off"):
        return None
    path = path or seed_path()
    if path is None:
        return None
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    conn = open_catalog()
    try:
        conn.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        row = conn.execute("SELECT value FROM meta WHERE key='basic_seed_sha'").fetchone()
        if row and row["value"] == digest:
            return None
        n = import_seed(conn, path, rebuild=False)   # the triggers keep the search index current
        conn.execute("INSERT INTO meta (key, value) VALUES ('basic_seed_sha', ?) "
                     "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (digest,))
        conn.commit()
        return n
    finally:
        conn.close()
