"""Food log, daily totals and goals.

Every log entry stores a snapshot of the name and the nutrition at the time it was written,
so re-importing the catalogue, editing a custom food or re-syncing a recipe never rewrites
what was eaten on a past day.
"""

import sqlite3
from datetime import date as Date

from .catalog import get_food, now_iso

MEALS = ("breakfast", "lunch", "dinner", "snack")
METHODS = ("search", "barcode", "recipe", "photo", "quick")
MACROS = ("kcal", "protein", "carbs", "fat")


def valid_date(value: str) -> str:
    try:
        return Date.fromisoformat(str(value)).isoformat()
    except ValueError:
        raise ValueError("date must be YYYY-MM-DD") from None


def r1(x) -> float:
    return round(float(x or 0), 1)


def snapshot(food, grams: float) -> dict:
    f = grams / 100.0
    return {
        "name": food["name"],
        "kcal": r1(food["kcal_100g"] * f),
        "protein": r1(food["protein_100g"] * f),
        "carbs": r1(food["carbs_100g"] * f),
        "fat": r1(food["fat_100g"] * f),
    }


def entry_dict(r) -> dict:
    return {k: r[k] for k in ("id", "date", "meal", "food_ref", "grams", "name", "kcal", "protein",
                              "carbs", "fat", "entry_method", "created_at")}


def build_entry(conn: sqlite3.Connection, item: dict) -> dict:
    """Validate one entry and resolve its snapshot. Raises ValueError with a readable reason."""
    day = valid_date(item.get("date"))
    meal = item.get("meal")
    if meal not in MEALS:
        raise ValueError(f"meal must be one of {', '.join(MEALS)}")
    method = item.get("entry_method") or ("quick" if not item.get("food_ref") else "search")
    if method not in METHODS:
        raise ValueError(f"entry_method must be one of {', '.join(METHODS)}")

    ref = item.get("food_ref")
    if ref:
        food = get_food(conn, ref)
        if food is None:
            raise ValueError(f"unknown food {ref}")
        grams = float(item.get("grams") or 0)
        if not 0 < grams <= 5000:
            raise ValueError("grams must be between 0 and 5000")
        snap = snapshot(food, grams)
        if item.get("name"):                  # e.g. a recipe line, kept under its own label
            snap["name"] = str(item["name"])[:200]
        return {"date": day, "meal": meal, "food_ref": ref, "grams": round(grams, 1),
                "entry_method": method, **snap}

    # quick add: calories, macros optional, no food behind it
    kcal = float(item.get("kcal") or 0)
    if not 0 < kcal <= 20000:
        raise ValueError("a quick add needs kcal between 0 and 20000")
    grams = item.get("grams")
    return {
        "date": day, "meal": meal, "food_ref": None,
        "grams": round(float(grams), 1) if grams else None,
        "name": (str(item.get("name") or "").strip() or "Quick add")[:200],
        "kcal": r1(kcal),
        **{k: r1(max(0.0, min(2000.0, float(item.get(k) or 0)))) for k in ("protein", "carbs", "fat")},
        "entry_method": method,
    }


def insert_entry(conn: sqlite3.Connection, e: dict) -> int:
    cur = conn.execute(
        """INSERT INTO food_log (date, meal, food_ref, grams, name, kcal, protein, carbs, fat,
                                 entry_method, created_at)
           VALUES (:date, :meal, :food_ref, :grams, :name, :kcal, :protein, :carbs, :fat,
                   :entry_method, :created_at)""",
        {**e, "created_at": now_iso()},
    )
    return cur.lastrowid


def patch_entry(conn: sqlite3.Connection, entry_id: int, patch: dict) -> dict | None:
    row = conn.execute("SELECT * FROM food_log WHERE id=?", (entry_id,)).fetchone()
    if row is None:
        return None
    e = entry_dict(row)
    if patch.get("date") is not None:
        e["date"] = valid_date(patch["date"])
    if patch.get("meal") is not None:
        if patch["meal"] not in MEALS:
            raise ValueError("unknown meal")
        e["meal"] = patch["meal"]
    if patch.get("name"):
        e["name"] = str(patch["name"])[:200]
    grams = patch.get("grams")
    if grams is not None and e["food_ref"] and e["grams"]:
        grams = float(grams)
        if not 0 < grams <= 5000:
            raise ValueError("grams must be between 0 and 5000")
        # Scale the stored snapshot rather than re-reading the catalogue: the entry keeps
        # the values it was logged with, just for a different amount.
        factor = grams / e["grams"]
        for k in MACROS:
            e[k] = r1(e[k] * factor)
        e["grams"] = round(grams, 1)
    if e["food_ref"] is None:
        for k in MACROS:
            if patch.get(k) is not None:
                e[k] = r1(max(0.0, float(patch[k])))
        if e["kcal"] <= 0:
            raise ValueError("kcal must be above zero")
    conn.execute(
        "UPDATE food_log SET date=?, meal=?, name=?, grams=?, kcal=?, protein=?, carbs=?, fat=? WHERE id=?",
        (e["date"], e["meal"], e["name"], e["grams"], e["kcal"], e["protein"], e["carbs"], e["fat"], entry_id),
    )
    return e


def goal_for(conn: sqlite3.Connection, day: str) -> dict | None:
    r = conn.execute(
        "SELECT * FROM nutrition_goals WHERE effective_from <= ? ORDER BY effective_from DESC LIMIT 1",
        (day,)).fetchone()
    return goal_dict(r) if r else None


def goal_dict(r) -> dict:
    return {"id": r["id"], "effective_from": r["effective_from"], "kcal": r["kcal"],
            "protein": r["protein"], "carbs": r["carbs"], "fat": r["fat"]}


def totals(rows) -> dict:
    return {k: r1(sum(r[k] for r in rows)) for k in MACROS}


def day_view(conn: sqlite3.Connection, day: str) -> dict:
    rows = [entry_dict(r) for r in conn.execute(
        "SELECT * FROM food_log WHERE date=? ORDER BY created_at, id", (day,))]
    return {
        "date": day,
        "goal": goal_for(conn, day),
        "entries": rows,
        "totals": totals(rows),
        "meals": {m: totals([r for r in rows if r["meal"] == m]) for m in MEALS},
    }


def summary(conn: sqlite3.Connection, start: str, end: str) -> list[dict]:
    """Per-day totals for days with any entry, each with the goal valid on that day."""
    goals = [goal_dict(r) for r in conn.execute("SELECT * FROM nutrition_goals ORDER BY effective_from")]
    out = []
    for r in conn.execute(
        """SELECT date, COUNT(*) AS n, SUM(kcal) AS kcal, SUM(protein) AS protein,
                  SUM(carbs) AS carbs, SUM(fat) AS fat
             FROM food_log WHERE date BETWEEN ? AND ? GROUP BY date ORDER BY date""",
        (start, end),
    ):
        goal = None
        for g in goals:
            if g["effective_from"] <= r["date"]:
                goal = g
        out.append({"date": r["date"], "entries": r["n"], **{k: r1(r[k]) for k in MACROS},
                    "goal": {k: goal[k] for k in MACROS} if goal else None})
    return out


def copy_meal(conn: sqlite3.Connection, src: str, dst: str, meal: str | None, to_meal: str | None) -> int:
    """Copy one meal (or the whole day) to another day. Snapshots are copied as they are."""
    q, params = "SELECT * FROM food_log WHERE date=?", [src]
    if meal:
        q += " AND meal=?"
        params.append(meal)
    n = 0
    for r in conn.execute(q + " ORDER BY created_at, id", params).fetchall():
        e = entry_dict(r)
        e.update(date=dst, meal=to_meal or e["meal"])
        e.pop("id"), e.pop("created_at")
        insert_entry(conn, e)
        n += 1
    return n


def weights(conn: sqlite3.Connection, start: str, end: str) -> list[dict]:
    """Body weight with a trailing 7-day moving average (over the logged days in that window)."""
    rows = [dict(r) for r in conn.execute("SELECT date, kg FROM body_weight ORDER BY date")]
    out = []
    for i, r in enumerate(rows):
        d = Date.fromisoformat(r["date"])
        window = [x["kg"] for x in rows[: i + 1] if (d - Date.fromisoformat(x["date"])).days < 7]
        if start <= r["date"] <= end:
            out.append({"date": r["date"], "kg": r["kg"], "avg7": round(sum(window) / len(window), 2)})
    return out
