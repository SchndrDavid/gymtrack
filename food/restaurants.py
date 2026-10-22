"""Restaurant menus: what a place serves, with its estimates per portion, logged as portions.

A menu is imported whole (POST /api/food/restaurants/import, or scripts/import_restaurant.py from a
spreadsheet). Items are matched by their menu code, or by name when there is none; an item that is
no longer on the menu disappears, and days already logged keep their values.
"""

import re
import sqlite3

from .catalog import now_iso, num

# "cca 350 g (s přílohou)" → 350, "0,3 l" → 300, "0,75 l (celá láhev)" → 750; "6 ks", "porce" → None.
_AMOUNT = re.compile(r"(\d+(?:[.,]\d+)?)\s*(kg|g|ml|dl|l)\b", re.I)
_TO_GRAMS = {"g": 1, "kg": 1000, "ml": 1, "dl": 100, "l": 1000}


def portion_grams(portion: str) -> float | None:
    """Grams (or millilitres, taken as grams) of a portion, when the menu states a weight or volume."""
    m = _AMOUNT.search(portion or "")
    if not m:
        return None
    return round(float(m.group(1).replace(",", ".")) * _TO_GRAMS[m.group(2).lower()], 1)


def clean_item(raw: dict, position: int) -> dict:
    """One menu item, validated. Raises ValueError with a readable reason."""
    name = re.sub(r"\s+", " ", str(raw.get("name") or "")).strip()[:200]
    if not name:
        raise ValueError(f"item {position + 1}: name required")
    values = {}
    for key, hi in (("kcal", 5000), ("protein", 500), ("carbs", 1000), ("fat", 500), ("alcohol", 500)):
        v = num(raw.get(key), 0, hi)
        if v is None and raw.get(key) not in (None, ""):
            raise ValueError(f"{name}: {key} must be a number between 0 and {hi}")
        values[key] = round(v or 0, 1)
    code = str(raw.get("code") or "").strip()[:20] or None
    portion = re.sub(r"\s+", " ", str(raw.get("portion") or "")).strip()[:80]
    return {"code": code, "item_key": code or name.lower(), "name": name,
            "category": str(raw.get("category") or "").strip()[:80], "position": position,
            "portion": portion, "grams": portion_grams(portion), "price": num(raw.get("price"), 0, 100000),
            **values}


def import_menu(conn: sqlite3.Connection, restaurant: str, items: list[dict]) -> dict:
    """Replace one restaurant's menu: new and changed items are written, missing ones removed."""
    restaurant = re.sub(r"\s+", " ", str(restaurant or "")).strip()[:120]
    if not restaurant:
        raise ValueError("restaurant name required")
    if not items:
        raise ValueError("the menu has no items")
    clean = [clean_item(x, i) for i, x in enumerate(items)]
    keys = [c["item_key"] for c in clean]
    if len(set(keys)) != len(keys):
        dup = next(k for k in keys if keys.count(k) > 1)
        raise ValueError(f"the menu lists {dup!r} twice")
    stamp = now_iso()
    before = {r["item_key"] for r in conn.execute(
        "SELECT item_key FROM restaurant_items WHERE restaurant=?", (restaurant,))}
    for c in clean:
        conn.execute(
            """INSERT INTO restaurant_items (restaurant, item_key, code, name, category, position, portion, grams,
                   price, kcal, protein, carbs, fat, alcohol, updated_at)
               VALUES (:restaurant, :item_key, :code, :name, :category, :position, :portion, :grams, :price,
                   :kcal, :protein, :carbs, :fat, :alcohol, :stamp)
               ON CONFLICT (restaurant, item_key) DO UPDATE SET code=excluded.code, name=excluded.name,
                   category=excluded.category, position=excluded.position, portion=excluded.portion,
                   grams=excluded.grams, price=excluded.price, kcal=excluded.kcal, protein=excluded.protein,
                   carbs=excluded.carbs, fat=excluded.fat, alcohol=excluded.alcohol, updated_at=excluded.updated_at""",
            {**c, "restaurant": restaurant, "stamp": stamp})
    removed = before - set(keys)
    conn.executemany("DELETE FROM restaurant_items WHERE restaurant=? AND item_key=?",
                     [(restaurant, k) for k in removed])
    return {"restaurant": restaurant, "items": len(clean), "added": len(set(keys) - before),
            "removed": len(removed)}


def item_dict(r) -> dict:
    return {k: r[k] for k in ("id", "restaurant", "code", "name", "category", "portion", "grams", "price",
                              "kcal", "protein", "carbs", "fat", "alcohol")}


def menus(conn: sqlite3.Connection) -> list[dict]:
    """Every restaurant with its items in menu order, grouped by category."""
    out: dict[str, dict] = {}
    for r in conn.execute("SELECT * FROM restaurant_items ORDER BY restaurant, position, id"):
        menu = out.setdefault(r["restaurant"], {"name": r["restaurant"], "categories": []})
        cats = menu["categories"]
        if not cats or cats[-1]["name"] != r["category"]:
            cats.append({"name": r["category"], "items": []})
        cats[-1]["items"].append(item_dict(r))
    return list(out.values())


def delete_menu(conn: sqlite3.Connection, restaurant: str) -> int:
    return conn.execute("DELETE FROM restaurant_items WHERE restaurant=?", (restaurant,)).rowcount


def log_item(conn: sqlite3.Connection, item_id: int, portions: float) -> dict:
    """What a log entry for `portions` of an item holds — name with the portion, and the values."""
    r = conn.execute("SELECT * FROM restaurant_items WHERE id=?", (item_id,)).fetchone()
    if r is None:
        raise ValueError("unknown menu item")
    if not 0 < portions <= 10:
        raise ValueError("portions must be between 0 and 10")
    k = portions
    amount = "" if k == 1 else ("½ × " if k == 0.5 else f"{k:g} × ")
    note = ", ".join(b for b in (amount + r["portion"] if r["portion"] else "",
                                 f"alkohol {r['alcohol'] * k:g} g" if r["alcohol"] else "") if b)
    return {"name": f"{r['name']} · {r['restaurant']}" + (f" ({note})" if note else ""),
            "grams": round(r["grams"] * k, 1) if r["grams"] else None,
            **{m: round(r[m] * k, 1) for m in ("kcal", "protein", "carbs", "fat")}}
