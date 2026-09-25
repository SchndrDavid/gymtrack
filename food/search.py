"""Food search — the one thing in the Food tab that has to feel instant.

Order of results:
  1. favourites and foods used recently, by how often and how lately
  2. basic foods (USDA) and the user's own foods and recipes
  3. Open Food Facts products, by FTS bm25
"""

import math
import re
import sqlite3
from datetime import date, timedelta

from .catalog import fold, food_dict, fts_query, get_foods, ref_of

RECENT_DAYS = 120
HALF_LIFE_DAYS = 14
BARCODE = re.compile(r"^\d{8,14}$")


def usage(conn: sqlite3.Connection) -> dict[str, float]:
    """Frecency per food ref: every use counts, halving in weight every two weeks."""
    since = (date.today() - timedelta(days=RECENT_DAYS)).isoformat()
    today = date.today()
    scores: dict[str, float] = {}
    for r in conn.execute(
        "SELECT food_ref, date FROM food_log WHERE food_ref IS NOT NULL AND date >= ?", (since,)
    ):
        try:
            age = (today - date.fromisoformat(r["date"])).days
        except ValueError:
            continue
        scores[r["food_ref"]] = scores.get(r["food_ref"], 0.0) + math.pow(0.5, max(age, 0) / HALF_LIFE_DAYS)
    return scores


def favourites(conn: sqlite3.Connection) -> set[str]:
    return {r["food_ref"] for r in conn.execute("SELECT food_ref FROM food_favorites")}


def _fts(conn, table: str, match: str, where: str = "", params=(), limit: int = 60):
    return conn.execute(
        f"""SELECT t.*, bm25({table}_fts, 10.0, 2.0, 1.0) AS score
              FROM {table}_fts JOIN {table} t ON t.id = {table}_fts.rowid
             WHERE {table}_fts MATCH ? {where}
             ORDER BY score LIMIT ?""",
        (match, *params, limit),
    ).fetchall()


def _name_bonus(name: str, needle: str) -> int:
    """Whole-name and leading-word matches go first: 'vejce' before 'vaječné těstoviny'."""
    folded = fold(name)
    if folded == needle:
        return 0
    if folded.startswith(needle):
        return 1
    return 2


def search(conn: sqlite3.Connection, q: str, limit: int = 20) -> list[dict]:
    limit = max(1, min(100, limit))
    q = (q or "").strip()
    scores = usage(conn)
    favs = favourites(conn)

    if not q:
        return recent(conn, limit, scores, favs)

    def item(row):
        ref = ref_of(row)
        return food_dict(row, favorite=ref in favs, recent=ref in scores)

    found: dict[str, tuple] = {}   # ref -> (dict, sort key)

    if BARCODE.match(q):
        for r in conn.execute("SELECT * FROM user_foods WHERE barcode=?", (q,)):
            found[ref_of(r)] = (item(r), (0, 0, 0))
        for r in conn.execute("SELECT * FROM cat.foods WHERE barcode=?", (q,)):
            found.setdefault(ref_of(r), (item(r), (0, 0, 0)))

    match = fts_query(q)
    if match:
        needle = fold(q)
        pools = [
            (_fts(conn, "user_foods", match), 1),
            (_cat_fts(conn, match, "usda", 80), 1),
            (_cat_fts(conn, match, "off", max(limit * 3, 60)), 2),
        ]
        for rows, tier in pools:
            for pos, r in enumerate(rows):
                ref = ref_of(r)
                if ref in found:
                    continue
                d = item(r)
                boosted = ref in favs or ref in scores
                if boosted:
                    key = (0, -(scores.get(ref, 0) + (5 if ref in favs else 0)), pos)
                elif tier == 1:
                    key = (1, _name_bonus(r["name"], needle), pos)
                else:
                    key = (2, 0, pos)       # bm25 order as FTS returned it
                found[ref] = (d, key)

    ranked = sorted(found.values(), key=lambda x: x[1])
    return [d for d, _ in ranked[:limit]]


def _cat_fts(conn, match: str, source: str, limit: int):
    return conn.execute(
        """SELECT t.*, bm25(foods_fts, 10.0, 2.0, 1.0) AS score
             FROM cat.foods_fts JOIN cat.foods t ON t.id = foods_fts.rowid
            WHERE foods_fts MATCH ? AND t.source = ?
            ORDER BY score LIMIT ?""",
        (match, source, limit),
    ).fetchall()


def recent(conn, limit: int, scores=None, favs=None) -> list[dict]:
    """Empty query: favourites and recently used foods, most used first."""
    scores = usage(conn) if scores is None else scores
    favs = favourites(conn) if favs is None else favs
    refs = sorted(set(scores) | favs, key=lambda r: (-(scores.get(r, 0) + (5 if r in favs else 0)), r))
    rows = get_foods(conn, refs[: limit * 2])
    out = []
    for ref in refs:
        row = rows.get(ref)
        if row is None:
            continue
        out.append(food_dict(row, favorite=ref in favs, recent=ref in scores))
        if len(out) >= limit:
            break
    return out
