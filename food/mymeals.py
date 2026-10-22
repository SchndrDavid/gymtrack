"""My foods: what is eaten often, saved once per portion and logged with a tap.

A meal is either typed in (kcal and macros per portion) or put together from foods with their
grams — 30 g whey and 300 ml milk make a protein shake — in which case the values are the sum of
the foods' and the ingredients are kept, so the meal can be opened and changed later. A logged
portion is a snapshot: changing the meal never rewrites a past day.
"""

import json
import re
import sqlite3

from .catalog import get_foods, now_iso, num
from .log import snapshot


def clean(conn: sqlite3.Connection, raw: dict) -> dict:
    """A meal as stored. Raises ValueError with a readable reason."""
    name = re.sub(r"\s+", " ", str(raw.get("name") or "")).strip()[:120]
    if not name:
        raise ValueError("name required")
    items = []
    wanted = [x for x in raw.get("items") or [] if isinstance(x, dict) and x.get("food_ref")]
    rows = get_foods(conn, [x["food_ref"] for x in wanted])
    for x in wanted:
        food, grams = rows.get(x["food_ref"]), num(x.get("grams"), 0, 5000)
        if food is None:
            raise ValueError(f"unknown food {x['food_ref']}")
        if not grams:
            raise ValueError(f"{food['name']}: grams required")
        items.append({"food_ref": x["food_ref"], "grams": round(grams, 1), **snapshot(food, grams)})
    if items:
        values = {k: round(sum(i[k] for i in items), 1) for k in ("kcal", "protein", "carbs", "fat")}
        grams = round(sum(i["grams"] for i in items), 1)
    else:
        values = {}
        for key, hi in (("kcal", 10000), ("protein", 1000), ("carbs", 2000), ("fat", 1000)):
            v = num(raw.get(key), 0, hi)
            if v is None and (key == "kcal" or raw.get(key) not in (None, "")):
                raise ValueError(f"{key} must be a number between 0 and {hi}")
            values[key] = round(v or 0, 1)
        grams = num(raw.get("grams"), 0, 5000) or None
    return {"name": name, "grams": grams, **values, "items": items}


def meal_dict(r) -> dict:
    return {**{k: r[k] for k in ("id", "name", "grams", "kcal", "protein", "carbs", "fat")},
            "items": json.loads(r["items"] or "[]")}


def all_meals(conn: sqlite3.Connection) -> list[dict]:
    return [meal_dict(r) for r in conn.execute("SELECT * FROM my_meals ORDER BY name COLLATE NOCASE")]


def save(conn: sqlite3.Connection, raw: dict, meal_id: int | None = None) -> dict:
    m = clean(conn, raw)
    values = {**m, "items": json.dumps(m["items"], ensure_ascii=False), "stamp": now_iso()}
    if meal_id is None:
        meal_id = conn.execute(
            """INSERT INTO my_meals (name, grams, kcal, protein, carbs, fat, items, updated_at)
               VALUES (:name, :grams, :kcal, :protein, :carbs, :fat, :items, :stamp)""", values).lastrowid
    elif not conn.execute(
            """UPDATE my_meals SET name=:name, grams=:grams, kcal=:kcal, protein=:protein, carbs=:carbs,
                   fat=:fat, items=:items, updated_at=:stamp WHERE id=:id""", {**values, "id": meal_id}).rowcount:
        raise LookupError("no such meal")
    return meal_dict(conn.execute("SELECT * FROM my_meals WHERE id=?", (meal_id,)).fetchone())


def log_item(conn: sqlite3.Connection, meal_id: int, portions: float) -> dict:
    """What a log entry for `portions` of a meal holds."""
    r = conn.execute("SELECT * FROM my_meals WHERE id=?", (meal_id,)).fetchone()
    if r is None:
        raise ValueError("unknown meal")
    if not 0 < portions <= 10:
        raise ValueError("portions must be between 0 and 10")
    k = portions
    return {"name": r["name"] + ("" if k == 1 else " (½ portion)" if k == 0.5 else f" ({k:g} portions)"),
            "grams": round(r["grams"] * k, 1) if r["grams"] else None,
            **{m: round(r[m] * k, 1) for m in ("kcal", "protein", "carbs", "fat")}}
