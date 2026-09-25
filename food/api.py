"""HTTP API of the Food module. Mounted by main.py under /api/food."""

from datetime import date as Date, timedelta
from typing import Any

from fastapi import APIRouter, File, HTTPException, UploadFile
from pydantic import BaseModel

from . import barcode, catalog, log
from .catalog import clean_food, connect, food_dict, get_food, now_iso
from .search import search as run_search

router = APIRouter(prefix="/api/food")


def init() -> None:
    catalog.init()


def bad(e: Exception):
    raise HTTPException(400, str(e))


# ─── catalogue and search ───────────────────────────────────────────────────

@router.get("/search")
def search(q: str = "", limit: int = 20):
    with connect() as conn:
        return {"q": q, "results": run_search(conn, q, limit)}


@router.get("/catalog")
def catalog_stats():
    """How much of the catalogue is imported — the Food tab says so when it is empty."""
    with connect() as conn:
        counts = {r["source"]: r["n"] for r in conn.execute(
            "SELECT source, COUNT(*) AS n FROM cat.foods GROUP BY source")}
        custom = conn.execute("SELECT COUNT(*) AS n FROM user_foods").fetchone()["n"]
    return {"usda": counts.get("usda", 0), "off": counts.get("off", 0), "user": custom}


@router.get("/foods/{ref}")
def get_one(ref: str):
    with connect() as conn:
        row = get_food(conn, ref)
        if row is None:
            raise HTTPException(404, "food not found")
        fav = conn.execute("SELECT 1 FROM food_favorites WHERE food_ref=?", (ref,)).fetchone()
        return food_dict(row, favorite=fav is not None)


# ─── barcodes ───────────────────────────────────────────────────────────────

def _barcode_result(conn, code: str) -> dict:
    row, where = barcode.lookup(conn, code, fetch=barcode_fetch)
    if row is None:
        return {"barcode": code, "food": None, "found": None}
    ref = catalog.ref_of(row)
    fav = conn.execute("SELECT 1 FROM food_favorites WHERE food_ref=?", (ref,)).fetchone()
    return {"barcode": code, "food": food_dict(row, favorite=fav is not None), "found": where}


barcode_fetch = None     # tests swap in a fake OFF API


@router.get("/barcode/{code}")
def barcode_lookup(code: str):
    code = code.strip()
    if not code.isdigit() or not 6 <= len(code) <= 14:
        raise HTTPException(400, "a barcode is 8 to 14 digits")
    with connect() as conn:
        return _barcode_result(conn, code)


@router.post("/barcode/scan")
async def barcode_scan(image: UploadFile = File(...)):
    """Read a barcode from a photo. The upload is held in memory and discarded right after."""
    try:
        data = await image.read(barcode.MAX_IMAGE_BYTES + 1)
    finally:
        await image.close()
    if len(data) > barcode.MAX_IMAGE_BYTES:
        raise HTTPException(413, "image too large")
    codes = barcode.decode(data)
    del data
    if not codes:
        return {"barcode": None, "food": None, "found": None}
    with connect() as conn:
        return _barcode_result(conn, codes[0])


class FoodIn(BaseModel):
    name: str
    brand: str = ""
    barcode: str | None = None
    kcal_100g: float
    protein_100g: float = 0
    carbs_100g: float = 0
    fat_100g: float = 0
    fiber_100g: float | None = None
    serving_g: float | None = None
    serving_label: str | None = None


def _custom_values(body: FoodIn) -> dict:
    clean = clean_food(body.model_dump())
    if clean is None:
        raise HTTPException(400, "name and sensible values per 100 g are required")
    barcode = (body.barcode or "").strip()
    if barcode and not barcode.isdigit():
        raise HTTPException(400, "barcode must be digits")
    return {**clean, "barcode": barcode or None}


@router.post("/foods")
def create_food(body: FoodIn):
    values = _custom_values(body)
    with connect() as conn:
        stamp = now_iso()
        cur = conn.execute(
            """INSERT INTO user_foods (source, source_id, barcode, name, brand, aliases, kcal_100g,
                   protein_100g, carbs_100g, fat_100g, fiber_100g, serving_g, serving_label,
                   updated_at, created_at)
               VALUES ('custom', NULL, :barcode, :name, :brand, '', :kcal_100g, :protein_100g,
                   :carbs_100g, :fat_100g, :fiber_100g, :serving_g, :serving_label, :stamp, :stamp)""",
            {**values, "stamp": stamp})
        row = conn.execute("SELECT * FROM user_foods WHERE id=?", (cur.lastrowid,)).fetchone()
        return food_dict(row, favorite=False)


def _user_row(conn, ref: str):
    row = get_food(conn, ref)
    if row is None:
        raise HTTPException(404, "food not found")
    if not ref.startswith("user:"):
        raise HTTPException(400, "only your own foods can be changed")
    return row


@router.put("/foods/{ref}")
def update_food(ref: str, body: FoodIn):
    values = _custom_values(body)
    with connect() as conn:
        row = _user_row(conn, ref)
        conn.execute(
            """UPDATE user_foods SET barcode=:barcode, name=:name, brand=:brand, kcal_100g=:kcal_100g,
                   protein_100g=:protein_100g, carbs_100g=:carbs_100g, fat_100g=:fat_100g,
                   fiber_100g=:fiber_100g, serving_g=:serving_g, serving_label=:serving_label,
                   updated_at=:stamp WHERE id=:id""",
            {**values, "stamp": now_iso(), "id": row["id"]})
        return food_dict(get_food(conn, ref))


@router.delete("/foods/{ref}")
def delete_food(ref: str):
    """The log keeps its snapshots, so past days do not change."""
    with connect() as conn:
        row = _user_row(conn, ref)
        conn.execute("DELETE FROM user_foods WHERE id=?", (row["id"],))
        conn.execute("DELETE FROM food_favorites WHERE food_ref=?", (ref,))
    return {"ok": True}


@router.put("/favorites/{ref}")
def add_favorite(ref: str):
    with connect() as conn:
        if get_food(conn, ref) is None:
            raise HTTPException(404, "food not found")
        conn.execute("INSERT OR IGNORE INTO food_favorites (food_ref, created_at) VALUES (?,?)",
                     (ref, now_iso()))
    return {"ok": True, "favorite": True}


@router.delete("/favorites/{ref}")
def remove_favorite(ref: str):
    with connect() as conn:
        conn.execute("DELETE FROM food_favorites WHERE food_ref=?", (ref,))
    return {"ok": True, "favorite": False}


# ─── log ────────────────────────────────────────────────────────────────────

class LogIn(BaseModel):
    date: str
    meal: str
    food_ref: str | None = None
    grams: float | None = None
    entry_method: str | None = None
    name: str | None = None
    kcal: float | None = None
    protein: float | None = None
    carbs: float | None = None
    fat: float | None = None


class LogBatch(BaseModel):
    items: list[LogIn]


class LogPatch(BaseModel):
    date: str | None = None
    meal: str | None = None
    grams: float | None = None
    name: str | None = None
    kcal: float | None = None
    protein: float | None = None
    carbs: float | None = None
    fat: float | None = None


class CopyIn(BaseModel):
    from_date: str
    to_date: str
    meal: str | None = None
    to_meal: str | None = None


@router.post("/log")
def add_entry(body: LogIn):
    with connect() as conn:
        try:
            e = log.build_entry(conn, body.model_dump())
        except ValueError as err:
            bad(err)
        e["id"] = log.insert_entry(conn, e)
        return {"ok": True, "entry": e}


@router.post("/log/batch")
def add_entries(body: LogBatch):
    """Multi-add: a recipe broken into ingredients, a recognised photo. All or nothing."""
    if not body.items:
        raise HTTPException(400, "nothing to add")
    with connect() as conn:
        try:
            entries = [log.build_entry(conn, i.model_dump()) for i in body.items]
        except ValueError as err:
            bad(err)
        for e in entries:
            e["id"] = log.insert_entry(conn, e)
        return {"ok": True, "entries": entries}


@router.patch("/log/{entry_id}")
def edit_entry(entry_id: int, body: LogPatch):
    with connect() as conn:
        try:
            e = log.patch_entry(conn, entry_id, body.model_dump())
        except ValueError as err:
            bad(err)
        if e is None:
            raise HTTPException(404, "entry not found")
        return {"ok": True, "entry": e}


@router.delete("/log/{entry_id}")
def delete_entry(entry_id: int):
    with connect() as conn:
        conn.execute("DELETE FROM food_log WHERE id=?", (entry_id,))
    return {"ok": True}


@router.post("/log/copy")
def copy_entries(body: CopyIn):
    try:
        src, dst = log.valid_date(body.from_date), log.valid_date(body.to_date)
    except ValueError as err:
        bad(err)
    for m in (body.meal, body.to_meal):
        if m is not None and m not in log.MEALS:
            raise HTTPException(400, "unknown meal")
    with connect() as conn:
        n = log.copy_meal(conn, src, dst, body.meal, body.to_meal)
    return {"ok": True, "copied": n}


@router.get("/day")
def get_day(date: str = ""):
    try:
        day = log.valid_date(date or Date.today().isoformat())
    except ValueError as err:
        bad(err)
    with connect() as conn:
        return log.day_view(conn, day)


def _range(start: str, end: str, default_days: int) -> tuple[str, str]:
    try:
        end = log.valid_date(end or Date.today().isoformat())
        start = log.valid_date(start or (Date.fromisoformat(end) - timedelta(days=default_days)).isoformat())
    except ValueError as err:
        bad(err)
    return start, end


@router.get("/summary")
def get_summary(start: str = "", end: str = ""):
    """Per-day totals against the goal valid that day — for the history charts and the year grid."""
    start, end = _range(start, end, 370)
    with connect() as conn:
        return {"start": start, "end": end, "days": log.summary(conn, start, end)}


# ─── goals ──────────────────────────────────────────────────────────────────

class GoalIn(BaseModel):
    effective_from: str
    kcal: float
    protein: float = 0
    carbs: float = 0
    fat: float = 0


@router.get("/goals")
def list_goals():
    with connect() as conn:
        goals = [log.goal_dict(r) for r in conn.execute(
            "SELECT * FROM nutrition_goals ORDER BY effective_from DESC")]
        return {"current": log.goal_for(conn, Date.today().isoformat()), "goals": goals}


@router.post("/goals")
def set_goal(body: GoalIn):
    try:
        day = log.valid_date(body.effective_from)
    except ValueError as err:
        bad(err)
    if not 500 <= body.kcal <= 10000:
        raise HTTPException(400, "kcal goal must be between 500 and 10000")
    vals = [max(0.0, min(1000.0, float(v))) for v in (body.protein, body.carbs, body.fat)]
    with connect() as conn:
        conn.execute(
            """INSERT INTO nutrition_goals (effective_from, kcal, protein, carbs, fat) VALUES (?,?,?,?,?)
               ON CONFLICT(effective_from) DO UPDATE SET kcal=excluded.kcal, protein=excluded.protein,
                   carbs=excluded.carbs, fat=excluded.fat""",
            (day, round(body.kcal), *[round(v) for v in vals]))
    return list_goals()


@router.delete("/goals/{goal_id}")
def delete_goal(goal_id: int):
    with connect() as conn:
        conn.execute("DELETE FROM nutrition_goals WHERE id=?", (goal_id,))
    return list_goals()


# ─── body weight ────────────────────────────────────────────────────────────

class WeightIn(BaseModel):
    date: str
    kg: float


@router.get("/weight")
def get_weight(start: str = "", end: str = ""):
    start, end = _range(start, end, 370)
    with connect() as conn:
        return {"start": start, "end": end, "weights": log.weights(conn, start, end)}


@router.post("/weight")
def set_weight(body: WeightIn):
    try:
        day = log.valid_date(body.date)
    except ValueError as err:
        bad(err)
    if not 20 <= body.kg <= 400:
        raise HTTPException(400, "kg must be between 20 and 400")
    with connect() as conn:
        conn.execute("INSERT INTO body_weight (date, kg) VALUES (?,?) "
                     "ON CONFLICT(date) DO UPDATE SET kg=excluded.kg", (day, round(body.kg, 2)))
    return {"ok": True}


@router.delete("/weight/{day}")
def delete_weight(day: str):
    with connect() as conn:
        conn.execute("DELETE FROM body_weight WHERE date=?", (day,))
    return {"ok": True}


# ─── config ─────────────────────────────────────────────────────────────────

def config() -> dict[str, Any]:
    """Feature flags for the frontend."""
    with connect() as conn:
        recipes = conn.execute("SELECT COUNT(*) AS n FROM user_foods WHERE source='recipe'").fetchone()["n"]
    return {
        "food_ai_enabled": False,
        "mordorcook_enabled": False,
        "recipes": recipes,
        "barcode": True,
        "history": False,
    }


# ─── backup ─────────────────────────────────────────────────────────────────

def export() -> dict[str, Any]:
    """Everything the user entered. The catalogue is not included — it is re-importable."""
    with connect() as conn:
        def rows(sql):
            return [dict(r) for r in conn.execute(sql)]
        return {
            "log": rows("SELECT * FROM food_log ORDER BY date, id"),
            "favorites": rows("SELECT * FROM food_favorites"),
            "goals": rows("SELECT * FROM nutrition_goals ORDER BY effective_from"),
            "body_weight": rows("SELECT * FROM body_weight ORDER BY date"),
            "user_foods": rows("SELECT * FROM user_foods ORDER BY id"),
            "ingredient_mappings": rows("SELECT * FROM ingredient_mappings"),
        }
