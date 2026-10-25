"""The foods eaten again and again — what Quick add offers first, one tap each.

From the log, per food: how often it was logged in the last N days, the portion it is usually
logged at (the most common amount, or the median when no amount repeats) and when it was last
eaten. Foods logged only once are one-offs and left out. Ranking is count plus a boost that
halves every two weeks since the last time, so last week's staple beats a habit from the spring.
"""

import sqlite3
from collections import Counter
from datetime import date as Date, timedelta
from statistics import median

from .catalog import food_dict, get_foods
from .search import favourites

HALF_LIFE_DAYS = 14
RECENCY_WEIGHT = 3.0


def usual_grams(entries: list[tuple[str, float]]) -> float:
    """The most common amount (ties: the one eaten last); with no repeat, the median."""
    counts = Counter(g for _, g in entries)
    top = max(counts.values())
    if top >= 2:
        return max((d, g) for d, g in entries if counts[g] == top)[1]
    return round(float(median(g for _, g in entries)), 1)


def frequent(conn: sqlite3.Connection, days: int = 60, limit: int = 12) -> list[dict]:
    days, limit = max(7, min(365, days)), max(1, min(50, limit))
    today = Date.today()
    rows = conn.execute(
        """SELECT food_ref, grams, date FROM food_log
            WHERE food_ref IS NOT NULL AND grams IS NOT NULL AND grams > 0 AND date BETWEEN ? AND ?
            ORDER BY date, id""",
        ((today - timedelta(days=days)).isoformat(), today.isoformat())).fetchall()

    by_ref: dict[str, list[tuple[str, float]]] = {}
    for r in rows:
        by_ref.setdefault(r["food_ref"], []).append((r["date"], r["grams"]))

    scored = []
    for ref, entries in by_ref.items():
        if len(entries) < 2:
            continue                                   # a one-off is not a habit
        last = entries[-1][0]
        age = max(0, (today - Date.fromisoformat(last)).days)
        scored.append((len(entries) + RECENCY_WEIGHT * 0.5 ** (age / HALF_LIFE_DAYS), last, ref, entries))
    scored.sort(key=lambda s: s[2])                    # equal score and date: by ref, so the order is stable
    scored.sort(key=lambda s: s[1], reverse=True)      # ... then the one eaten last first
    scored.sort(key=lambda s: s[0], reverse=True)      # best score first

    foods = get_foods(conn, [s[2] for s in scored[: limit * 2]])
    favs = favourites(conn)
    out = []
    for _, last, ref, entries in scored:
        row = foods.get(ref)
        if row is None:
            continue                                   # the food is gone (a deleted own food)
        grams = usual_grams(entries)
        food = food_dict(row, favorite=ref in favs, recent=True)
        k = grams / 100.0
        out.append({"ref": ref, "name": row["name"], "count": len(entries), "last_date": last, "grams": grams,
                    "kcal": round(row["kcal_100g"] * k, 1), "protein": round(row["protein_100g"] * k, 1),
                    "carbs": round(row["carbs_100g"] * k, 1), "fat": round(row["fat_100g"] * k, 1), "food": food})
        if len(out) >= limit:
            break
    return out
