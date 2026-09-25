"""Food catalogue and the user's food tables.

Two databases:

  foods.db     the catalogue — Open Food Facts products and the USDA-sourced basic foods.
               Disposable: delete it and re-run the importers, nothing personal lives here.
  gymtrack.db  everything the user wrote — the log, favourites, goals, custom foods, recipes.

The catalogue is attached to every connection as schema ``cat``, so one query can join both.

A food is addressed by a ``ref`` that survives a catalogue re-import:

  usda:<fdc_id>   basic food, USDA FoodData Central id
  off:<barcode>   Open Food Facts product
  user:<id>       custom food or recipe, row in user_foods
"""

import os
import re
import sqlite3
import unicodedata
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

DB_PATH = os.environ.get("GYMTRACK_DB", "/data/gymtrack.db")
FOODS_DB = os.environ.get("GYMTRACK_FOODS_DB") or str(Path(DB_PATH).parent / "foods.db")

NUTRIENTS = ("kcal_100g", "protein_100g", "carbs_100g", "fat_100g", "fiber_100g")

# Both tables share their food columns, so a row from either reads the same way.
FOOD_COLUMNS = """
    barcode       TEXT,
    name          TEXT NOT NULL,
    brand         TEXT NOT NULL DEFAULT '',
    aliases       TEXT NOT NULL DEFAULT '',
    kcal_100g     REAL NOT NULL,
    protein_100g  REAL NOT NULL DEFAULT 0,
    carbs_100g    REAL NOT NULL DEFAULT 0,
    fat_100g      REAL NOT NULL DEFAULT 0,
    fiber_100g    REAL,
    serving_g     REAL,
    serving_label TEXT,
    updated_at    TEXT NOT NULL
"""

# `aliases` is indexed alongside name and brand: the English USDA description of a basic
# food, so an ingredient written as "chicken breast" in a recipe still finds "Kuřecí prsa".
FTS_TOKENIZE = "unicode61 remove_diacritics 2"


def _fts_block(schema: str, table: str) -> str:
    fts = f"{table}_fts"
    return f"""
    CREATE VIRTUAL TABLE IF NOT EXISTS {schema}.{fts} USING fts5(
        name, brand, aliases, content='{table}', content_rowid='id', tokenize="{FTS_TOKENIZE}");
    CREATE TRIGGER IF NOT EXISTS {schema}.{table}_ai AFTER INSERT ON {table} BEGIN
        INSERT INTO {fts}(rowid, name, brand, aliases) VALUES (new.id, new.name, new.brand, new.aliases);
    END;
    CREATE TRIGGER IF NOT EXISTS {schema}.{table}_ad AFTER DELETE ON {table} BEGIN
        INSERT INTO {fts}({fts}, rowid, name, brand, aliases)
            VALUES ('delete', old.id, old.name, old.brand, old.aliases);
    END;
    CREATE TRIGGER IF NOT EXISTS {schema}.{table}_au AFTER UPDATE ON {table} BEGIN
        INSERT INTO {fts}({fts}, rowid, name, brand, aliases)
            VALUES ('delete', old.id, old.name, old.brand, old.aliases);
        INSERT INTO {fts}(rowid, name, brand, aliases) VALUES (new.id, new.name, new.brand, new.aliases);
    END;
    """


def catalog_schema(schema: str = "main") -> str:
    return f"""
    CREATE TABLE IF NOT EXISTS {schema}.foods (
        id        INTEGER PRIMARY KEY,
        source    TEXT NOT NULL,          -- off | usda
        source_id TEXT NOT NULL,          -- barcode for off, fdc_id for usda
        {FOOD_COLUMNS},
        UNIQUE (source, source_id)
    );
    CREATE INDEX IF NOT EXISTS {schema}.idx_foods_barcode ON foods(barcode);
    """ + _fts_block(schema, "foods")


USER_SCHEMA = """
CREATE TABLE IF NOT EXISTS user_foods (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    source     TEXT NOT NULL,             -- custom | recipe
    source_id  TEXT,                      -- recipe id for recipes
    """ + FOOD_COLUMNS + """,
    details    TEXT,                      -- recipe breakdown as JSON
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_user_foods_source ON user_foods(source, source_id);
CREATE INDEX IF NOT EXISTS idx_user_foods_barcode ON user_foods(barcode);

CREATE TABLE IF NOT EXISTS food_log (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    date         TEXT NOT NULL,
    meal         TEXT NOT NULL,           -- breakfast | lunch | dinner | snack
    food_ref     TEXT,                    -- NULL for a quick add
    grams        REAL,
    name         TEXT NOT NULL,           -- snapshot: a re-import never rewrites history
    kcal         REAL NOT NULL,
    protein      REAL NOT NULL DEFAULT 0,
    carbs        REAL NOT NULL DEFAULT 0,
    fat          REAL NOT NULL DEFAULT 0,
    entry_method TEXT NOT NULL,           -- search | barcode | recipe | photo | quick
    created_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_food_log_date ON food_log(date);
CREATE INDEX IF NOT EXISTS idx_food_log_ref ON food_log(food_ref);

CREATE TABLE IF NOT EXISTS food_favorites (
    food_ref   TEXT PRIMARY KEY,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS nutrition_goals (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    effective_from TEXT NOT NULL UNIQUE,
    kcal           REAL NOT NULL,
    protein        REAL NOT NULL DEFAULT 0,
    carbs          REAL NOT NULL DEFAULT 0,
    fat            REAL NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS body_weight (
    date TEXT PRIMARY KEY,
    kg   REAL NOT NULL
);

-- How a recipe ingredient maps onto a food, remembered so the next recipe that uses
-- "stroužek česneku" does not ask again.
CREATE TABLE IF NOT EXISTS ingredient_mappings (
    key         TEXT PRIMARY KEY,          -- normalised ingredient name
    food_ref    TEXT,                      -- NULL when ignored
    ignore      INTEGER NOT NULL DEFAULT 0,
    unit_grams  TEXT NOT NULL DEFAULT '{}', -- {"ks": 50, "lzice": 12}
    fixed_grams REAL,                      -- for lines with no amount ("olej na smažení")
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS recognition_jobs (
    id          TEXT PRIMARY KEY,
    status      TEXT NOT NULL,             -- pending | running | done | error
    result_json TEXT,
    error       TEXT,
    created_at  TEXT NOT NULL
);
""" + _fts_block("main", "user_foods")


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


@contextmanager
def connect():
    """User database with the catalogue attached as ``cat``."""
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("ATTACH DATABASE ? AS cat", (FOODS_DB,))
    # The catalogue may be deleted at any time to re-import it; recreate an empty one.
    if conn.execute("SELECT 1 FROM cat.sqlite_master WHERE name='foods'").fetchone() is None:
        conn.executescript(catalog_schema("cat"))
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init() -> None:
    Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    Path(FOODS_DB).parent.mkdir(parents=True, exist_ok=True)
    with connect() as conn:
        conn.executescript(catalog_schema("cat") + USER_SCHEMA)


def open_catalog(path: str = "") -> sqlite3.Connection:
    """Standalone catalogue connection for the import scripts."""
    path = path or FOODS_DB
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.executescript(catalog_schema("main"))
    return conn


def rebuild_fts(conn: sqlite3.Connection, schema: str = "main") -> None:
    conn.execute(f"INSERT INTO {schema}.foods_fts(foods_fts) VALUES ('rebuild')")


UPSERT = """
INSERT INTO foods (source, source_id, barcode, name, brand, aliases, kcal_100g, protein_100g,
                   carbs_100g, fat_100g, fiber_100g, serving_g, serving_label, updated_at)
VALUES (:source, :source_id, :barcode, :name, :brand, :aliases, :kcal_100g, :protein_100g,
        :carbs_100g, :fat_100g, :fiber_100g, :serving_g, :serving_label, :updated_at)
ON CONFLICT (source, source_id) DO UPDATE SET
    barcode=excluded.barcode, name=excluded.name, brand=excluded.brand, aliases=excluded.aliases,
    kcal_100g=excluded.kcal_100g, protein_100g=excluded.protein_100g,
    carbs_100g=excluded.carbs_100g, fat_100g=excluded.fat_100g, fiber_100g=excluded.fiber_100g,
    serving_g=excluded.serving_g, serving_label=excluded.serving_label,
    updated_at=excluded.updated_at
"""


def upsert_foods(conn: sqlite3.Connection, rows: list[dict], schema: str = "main") -> None:
    sql = UPSERT.replace("INSERT INTO foods", f"INSERT INTO {schema}.foods", 1)
    conn.executemany(sql, rows)


# ─── values ─────────────────────────────────────────────────────────────────

def num(value, lo: float = 0.0, hi: float | None = None) -> float | None:
    """A float within bounds, or None. Accepts "12,5" as well as "12.5"."""
    if value is None or value == "":
        return None
    try:
        v = float(str(value).strip().replace(",", "."))
    except ValueError:
        return None
    if v != v or v < lo or (hi is not None and v > hi):   # NaN or out of range
        return None
    return v


def clean_food(raw: dict) -> dict | None:
    """Validate a food's nutrition. None when it is obviously wrong."""
    name = re.sub(r"\s+", " ", str(raw.get("name") or "")).strip()[:200]
    kcal = num(raw.get("kcal_100g"), 0, 900)
    if not name or kcal is None:
        return None
    macros = {}
    for key in ("protein_100g", "carbs_100g", "fat_100g", "fiber_100g"):
        value = raw.get(key)
        if value in (None, ""):
            macros[key] = None
            continue
        v = num(value, 0, 100)
        if v is None:
            return None                    # negative or impossible — drop, don't guess
        macros[key] = v
    if sum(macros[k] or 0 for k in ("protein_100g", "carbs_100g", "fat_100g")) > 105:
        return None
    serving = num(raw.get("serving_g"), 0.1, 5000)
    return {
        "name": name,
        "brand": re.sub(r"\s+", " ", str(raw.get("brand") or "")).strip()[:120],
        "kcal_100g": round(kcal, 1),
        "protein_100g": round(macros["protein_100g"] or 0, 2),
        "carbs_100g": round(macros["carbs_100g"] or 0, 2),
        "fat_100g": round(macros["fat_100g"] or 0, 2),
        "fiber_100g": None if macros["fiber_100g"] is None else round(macros["fiber_100g"], 2),
        "serving_g": round(serving, 1) if serving else None,
        "serving_label": (str(raw.get("serving_label") or "").strip()[:60] or None) if serving else None,
    }


def fold(text: str) -> str:
    """Lowercase without diacritics — 'Řízek' and 'rizek' compare equal."""
    text = unicodedata.normalize("NFKD", str(text or "").lower())
    return "".join(c for c in text if not unicodedata.combining(c))


WORD = re.compile(r"\w+", re.UNICODE)


def fts_query(text: str) -> str:
    """Prefix match on every word: 'kuř pr' → "kuř"* AND "pr"*."""
    words = WORD.findall(text or "")[:8]
    return " AND ".join('"%s"*' % w.replace('"', "") for w in words)


def ref_of(row) -> str:
    source = row["source"]
    if source in ("custom", "recipe"):
        return f"user:{row['id']}"
    return f"{source}:{row['source_id']}"


def food_dict(row, **extra) -> dict:
    keys = row.keys()
    out = {
        "ref": ref_of(row),
        "source": row["source"],
        "name": row["name"],
        "brand": row["brand"] or "",
        "barcode": row["barcode"],
        "kcal_100g": row["kcal_100g"],
        "protein_100g": row["protein_100g"],
        "carbs_100g": row["carbs_100g"],
        "fat_100g": row["fat_100g"],
        "fiber_100g": row["fiber_100g"],
        "serving_g": row["serving_g"],
        "serving_label": row["serving_label"],
    }
    if "source_id" in keys and row["source"] == "recipe":
        out["recipe_id"] = row["source_id"]
    out.update(extra)
    return out


def get_food(conn: sqlite3.Connection, ref: str):
    """Row for a ref, or None."""
    kind, _, key = (ref or "").partition(":")
    if kind == "user" and key.isdigit():
        return conn.execute("SELECT * FROM user_foods WHERE id=?", (int(key),)).fetchone()
    if kind in ("usda", "off") and key:
        return conn.execute("SELECT * FROM cat.foods WHERE source=? AND source_id=?",
                            (kind, key)).fetchone()
    return None


def get_foods(conn: sqlite3.Connection, refs) -> dict:
    """Many refs at once: {ref: row}."""
    out: dict = {}
    by_kind: dict[str, list[str]] = {}
    for ref in set(refs):
        kind, _, key = (ref or "").partition(":")
        by_kind.setdefault(kind, []).append(key)
    for kind, keys in by_kind.items():
        for i in range(0, len(keys), 500):
            chunk = keys[i:i + 500]
            marks = ",".join("?" * len(chunk))
            if kind == "user":
                ids = [int(k) for k in chunk if k.isdigit()]
                if not ids:
                    continue
                rows = conn.execute(f"SELECT * FROM user_foods WHERE id IN ({','.join('?' * len(ids))})",
                                    ids)
            elif kind in ("usda", "off"):
                rows = conn.execute(
                    f"SELECT * FROM cat.foods WHERE source=? AND source_id IN ({marks})", [kind, *chunk])
            else:
                continue
            for r in rows:
                out[ref_of(r)] = r
    return out
