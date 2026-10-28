"""HTTP API of the Food module. Mounted by main.py under /api/food."""

import json
import os
import subprocess
import sys
from datetime import date as Date, timedelta
from typing import Any

from fastapi import APIRouter, BackgroundTasks, File, HTTPException, Request, UploadFile
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from . import ai_export, barcode, batches, catalog, frequent, log, meat, mymeals, opengym, progress, recipes, recognize, restaurants, seed
from .catalog import clean_food, connect, food_dict, get_food, now_iso
from .search import search as run_search

router = APIRouter(prefix="/api/food")


def init() -> None:
    catalog.init()
    seed.sync()
    with connect() as conn:
        recognize.cleanup(conn)


def bad(e: Exception):
    raise HTTPException(400, str(e))


# ─── catalogue and search ───────────────────────────────────────────────────

@router.get("/search")
def search(q: str = "", limit: int = 20):
    with connect() as conn:
        return {"q": q, "results": run_search(conn, q, limit)}


@router.get("/catalog")
def catalog_stats():
    """How much of the catalogue is imported, and how a running import is getting on."""
    with connect() as conn:
        counts = {r["source"]: r["n"] for r in conn.execute(
            "SELECT source, COUNT(*) AS n FROM cat.foods GROUP BY source")}
        custom = conn.execute("SELECT COUNT(*) AS n FROM user_foods").fetchone()["n"]
    return {"usda": counts.get("usda", 0), "off": counts.get("off", 0), "user": custom,
            "import": import_status()}


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
IMPORT_STATUS = os.path.join(os.path.dirname(os.path.abspath(catalog.FOODS_DB)), "catalog_import.json")
IMPORT_ARGS: list[str] = []   # tests point the importer at local files
_import_proc = None


def import_status() -> dict | None:
    try:
        with open(IMPORT_STATUS, encoding="utf-8") as fh:
            status = json.load(fh)
    except (OSError, ValueError):
        return None
    running = _import_proc is not None and _import_proc.poll() is None
    if status.get("state") == "running" and not running:
        status["state"] = "interrupted"      # the process is gone — a restart, a crash
    return status


@router.post("/catalog/import")
def start_catalog_import():
    """Download and import the food catalogue in a separate, low-priority process.

    Started by a button, never on its own. Takes minutes; the Food tab polls GET /catalog."""
    global _import_proc
    if _import_proc is not None and _import_proc.poll() is None:
        raise HTTPException(409, "an import is already running")
    log_path = os.path.join(os.path.dirname(IMPORT_STATUS), "catalog_import.log")
    with open(log_path, "w", encoding="utf-8") as log_file:
        _import_proc = subprocess.Popen(
            [sys.executable, os.path.join(ROOT, "scripts", "import_all.py"), *IMPORT_ARGS],
            cwd=ROOT, stdout=log_file, stderr=subprocess.STDOUT,
            preexec_fn=lambda: os.nice(19), start_new_session=True)
    return {"ok": True, "pid": _import_proc.pid}


@router.get("/frequent")
def get_frequent(days: int = 60, limit: int = 12):
    """What is logged again and again, with the usual portion — one tap each in Quick add."""
    with connect() as conn:
        return {"days": max(7, min(365, days)), "items": frequent.frequent(conn, days, limit)}


@router.get("/meat")
def get_meat():
    """The curated meat tree for Quick add: species → cuts → raw and cooked values per 100 g."""
    with connect() as conn:
        return meat.tree(conn)


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


@router.post("/barcode/decode")
async def barcode_decode(image: UploadFile = File(...)):
    """Codes in one camera frame, nothing else — the live scanner calls this a couple of times a second."""
    try:
        data = await image.read(barcode.MAX_IMAGE_BYTES + 1)
    finally:
        await image.close()
    if len(data) > barcode.MAX_IMAGE_BYTES:
        raise HTTPException(413, "image too large")
    return {"codes": barcode.decode(data)}


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
        r = conn.execute("SELECT batch_id, grams FROM food_log WHERE id=?", (entry_id,)).fetchone()
        conn.execute("DELETE FROM food_log WHERE id=?", (entry_id,))
        if r and r["batch_id"] and r["grams"]:
            batches.take(conn, r["batch_id"], -r["grams"])      # taken back: the batch has it again
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


@router.get("/export/day")
def export_day(request: Request, date: str = ""):
    """The whole day as a prompt for an AI — food, and training here and in openGym."""
    try:
        day = log.valid_date(date or Date.today().isoformat())
    except ValueError as err:
        bad(err)
    og = opengym.for_day(day, request.headers.get("host", ""))
    with connect() as conn:
        return {"date": day, "filename": f"day-{day}.md", "markdown": ai_export.day_report(conn, day, og),
                "opengym": {"ok": og["ok"], "workouts": og["workouts"], "error": og["error"]}}


@router.get("/export/week")
def export_week(request: Request, end: str = ""):
    """Seven days up to `end` as a prompt for an AI — the unit weight loss is judged in."""
    try:
        day = log.valid_date(end or Date.today().isoformat())
    except ValueError as err:
        bad(err)
    host = request.headers.get("host", "")
    og_days: dict[str, dict] = {}
    for i in range(6, -1, -1):
        d = (Date.fromisoformat(day) - timedelta(days=i)).isoformat()
        # One unreachable openGym is enough to know: do not wait out the timeout seven times.
        if og_days and not any(v["ok"] for v in og_days.values()):
            og_days[d] = {"ok": False, "lines": [], "workouts": 0, "error": "skipped"}
            continue
        og_days[d] = opengym.for_day(d, host)
    with connect() as conn:
        start = (Date.fromisoformat(day) - timedelta(days=6)).isoformat()
        return {"start": start, "end": day, "filename": f"week-{start}-{day}.md",
                "markdown": ai_export.week_report(conn, day, og_days),
                "opengym": {"ok": all(v["ok"] for v in og_days.values()),
                            "workouts": sum(v["workouts"] for v in og_days.values())}}


# ─── progress: weight trend, maintenance, target ────────────────────────────

class TargetIn(BaseModel):
    kg: float | None = None


@router.get("/progress")
def get_progress(date: str = ""):
    try:
        day = log.valid_date(date or Date.today().isoformat())
    except ValueError as err:
        bad(err)
    with connect() as conn:
        return progress.progress(conn, day)


@router.put("/target")
def put_target(body: TargetIn):
    with connect() as conn:
        try:
            progress.set_target(conn, body.kg, Date.today().isoformat())
        except ValueError as err:
            bad(err)
        return {"ok": True, "target": progress.get_target(conn)}


# ─── my foods ───────────────────────────────────────────────────────────────

class MealIn(BaseModel):
    name: str
    kcal: float | None = None
    protein: float | None = None
    carbs: float | None = None
    fat: float | None = None
    grams: float | None = None
    items: list[dict[str, Any]] = []


class MealLog(BaseModel):
    meal_id: int
    portions: float = 1
    date: str
    meal: str


@router.get("/mymeals")
def list_my_meals():
    with connect() as conn:
        return {"meals": mymeals.all_meals(conn)}


@router.post("/mymeals")
def create_my_meal(body: MealIn):
    with connect() as conn:
        try:
            return mymeals.save(conn, body.model_dump())
        except ValueError as err:
            bad(err)


@router.put("/mymeals/{meal_id}")
def update_my_meal(meal_id: int, body: MealIn):
    with connect() as conn:
        try:
            return mymeals.save(conn, body.model_dump(), meal_id)
        except ValueError as err:
            bad(err)
        except LookupError:
            raise HTTPException(404, "no such meal") from None


@router.delete("/mymeals/{meal_id}")
def delete_my_meal(meal_id: int):
    """Logged days keep their snapshots."""
    with connect() as conn:
        if not conn.execute("DELETE FROM my_meals WHERE id=?", (meal_id,)).rowcount:
            raise HTTPException(404, "no such meal")
    return {"ok": True}


@router.post("/mymeals/log")
def log_my_meal(body: MealLog):
    with connect() as conn:
        try:
            day = log.valid_date(body.date)
            if body.meal not in log.MEALS:
                raise ValueError(f"meal must be one of {', '.join(log.MEALS)}")
            e = {"date": day, "meal": body.meal, "food_ref": None, "entry_method": "mine",
                 **mymeals.log_item(conn, body.meal_id, body.portions)}
        except ValueError as err:
            bad(err)
        e["id"] = log.insert_entry(conn, e)
        return {"ok": True, "entry": e}


# ─── containers and batches ─────────────────────────────────────────────────

class ContainerIn(BaseModel):
    name: str
    tare_g: float | str | None = None


class BatchIn(BaseModel):
    name: str | None = None
    recipe_ref: str | None = None
    kcal: float | str | None = None
    protein: float | str | None = None
    carbs: float | str | None = None
    fat: float | str | None = None
    cooked_on: str | None = None
    container_id: int | None = None
    tare_g: float | str | None = None
    gross_g: float | str | None = None
    boxes: float | str | None = None
    box_weights: list[float | str | None] = []


class BatchPatch(BaseModel):
    name: str | None = None
    cooked_on: str | None = None
    storage: str | None = None
    archived: bool | None = None
    remaining_g: float | str | None = None
    remaining_boxes: float | str | None = None


class BatchLog(BaseModel):
    mode: str                                   # grams | box | fraction | gross
    value: float | str
    date: str
    meal: str
    tare_g: float | str | None = None           # the empty box, for mode "gross"
    container_id: int | None = None


@router.get("/containers")
def list_containers():
    with connect() as conn:
        return {"containers": batches.all_containers(conn)}


@router.post("/containers")
def create_container(body: ContainerIn):
    with connect() as conn:
        try:
            return batches.save_container(conn, body.model_dump())
        except ValueError as err:
            bad(err)


@router.put("/containers/{container_id}")
def update_container(container_id: int, body: ContainerIn):
    with connect() as conn:
        try:
            return batches.save_container(conn, body.model_dump(), container_id)
        except ValueError as err:
            bad(err)
        except LookupError:
            raise HTTPException(404, "no such container") from None


@router.delete("/containers/{container_id}")
def delete_container(container_id: int):
    """Batches made in it keep its name and weight."""
    with connect() as conn:
        if not conn.execute("DELETE FROM containers WHERE id=?", (container_id,)).rowcount:
            raise HTTPException(404, "no such container")
    return {"ok": True}


@router.get("/batches")
def list_batches(done: bool = False):
    """Active batches; `done=true` adds the finished and archived ones."""
    with connect() as conn:
        return {"fridge_days": batches.FRIDGE_DAYS, "batches": batches.all_batches(conn, include_done=done)}


@router.post("/batches")
def create_batch(body: BatchIn):
    with connect() as conn:
        try:
            return batches.create(conn, body.model_dump())
        except ValueError as err:
            bad(err)


@router.get("/batches/{batch_id}")
def get_one_batch(batch_id: int):
    with connect() as conn:
        r = batches.get_batch(conn, batch_id)
        if r is None:
            raise HTTPException(404, "no such batch")
        return batches.batch_dict(r)


@router.patch("/batches/{batch_id}")
def patch_batch(batch_id: int, body: BatchPatch):
    with connect() as conn:
        try:
            return batches.update(conn, batch_id, body.model_dump(exclude_unset=True))
        except ValueError as err:
            bad(err)
        except LookupError:
            raise HTTPException(404, "no such batch") from None


@router.delete("/batches/{batch_id}")
def delete_batch(batch_id: int):
    """Entries already logged from it keep their values."""
    with connect() as conn:
        if not conn.execute("DELETE FROM batches WHERE id=?", (batch_id,)).rowcount:
            raise HTTPException(404, "no such batch")
    return {"ok": True}


@router.post("/batches/{batch_id}/log")
def log_batch(batch_id: int, body: BatchLog):
    with connect() as conn:
        try:
            day = log.valid_date(body.date)
            if body.meal not in log.MEALS:
                raise ValueError(f"meal must be one of {', '.join(log.MEALS)}")
            item = batches.log_item(conn, batch_id, body.mode, body.value, body.tare_g, body.container_id)
        except ValueError as err:
            bad(err)
        except LookupError:
            raise HTTPException(404, "no such batch") from None
        e = {"date": day, "meal": body.meal, "food_ref": None, "entry_method": "batch", **item}
        e["id"] = log.insert_entry(conn, e)
        conn.execute("UPDATE food_log SET batch_id=? WHERE id=?", (batch_id, e["id"]))
        batches.take(conn, batch_id, item["grams"])
        return {"ok": True, "entry": e, "batch": batches.batch_dict(batches.get_batch(conn, batch_id))}


# ─── restaurants ────────────────────────────────────────────────────────────

class RestaurantImport(BaseModel):
    restaurant: str
    items: list[dict[str, Any]]


class RestaurantLog(BaseModel):
    item_id: int
    portions: float = 1
    date: str
    meal: str


@router.get("/restaurants")
def list_restaurants():
    with connect() as conn:
        return {"restaurants": restaurants.menus(conn)}


@router.post("/restaurants/import")
def import_restaurant(body: RestaurantImport):
    """A whole menu at once; items missing from it are removed. Logged days keep their values."""
    with connect() as conn:
        try:
            return {"ok": True, **restaurants.import_menu(conn, body.restaurant, body.items)}
        except ValueError as err:
            bad(err)


@router.delete("/restaurants/{name}")
def delete_restaurant(name: str):
    with connect() as conn:
        if not restaurants.delete_menu(conn, name):
            raise HTTPException(404, "no such restaurant")
    return {"ok": True}


@router.post("/restaurants/log")
def log_restaurant_item(body: RestaurantLog):
    """Portions of a menu item into the log, as a snapshot (no food behind it, like a quick add)."""
    with connect() as conn:
        try:
            day = log.valid_date(body.date)
            if body.meal not in log.MEALS:
                raise ValueError(f"meal must be one of {', '.join(log.MEALS)}")
            e = {"date": day, "meal": body.meal, "food_ref": None, "entry_method": "restaurant",
                 **restaurants.log_item(conn, body.item_id, body.portions)}
        except ValueError as err:
            bad(err)
        e["id"] = log.insert_entry(conn, e)
        return {"ok": True, "entry": e}


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


@router.get("/trend")
def get_trend(start: str = "", end: str = ""):
    """The weight trend for every day of a range — the same fit that gives the rate and the ETA.

    Days with no weigh-in in the three weeks before them have no trend and are left out."""
    start, end = _range(start, end, 90)
    first, last = Date.fromisoformat(start), Date.fromisoformat(end)
    if last < first or (last - first).days > 400:
        raise HTTPException(400, "a range of 1 to 400 days")
    out, day = [], first
    with connect() as conn:
        while day <= last:
            t = progress.trend(conn, day.isoformat())
            if t and t["points"] > 0:
                out.append({"date": day.isoformat(), "kg": t["kg"], "rate_week": t["rate_week"], "fitted": t["fitted"]})
            day += timedelta(days=1)
    return {"start": start, "end": end, "trend": out}


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


# ─── recipes ────────────────────────────────────────────────────────────────

def mordorcook_url() -> str:
    return os.environ.get("MORDORCOOK_URL", "").strip().rstrip("/")


class RecipeImport(BaseModel):
    recipe: dict[str, Any] | None = None     # {"recipe": {...}} or the recipe itself
    origin: str = "import"

    model_config = {"extra": "allow"}

    def payload(self) -> dict:
        if self.recipe is not None:
            return self.recipe
        return {k: v for k, v in (self.model_extra or {}).items()}


class MappingIn(BaseModel):
    item: str
    food_ref: str | None = None
    ignore: bool = False
    grams: float | None = None       # weight of the line as written in the recipe
    amount: float | None = None
    unit: str = ""


@router.get("/recipes")
def list_recipes():
    with connect() as conn:
        rows = conn.execute("SELECT * FROM user_foods WHERE source='recipe' ORDER BY name COLLATE NOCASE").fetchall()
        return {"mordorcook": bool(mordorcook_url()), "recipes": [recipes.recipe_dict(r) for r in rows]}


@router.post("/recipes/preview")
def preview_recipe(body: RecipeImport):
    """Match every ingredient and compute the values. Nothing is stored."""
    with connect() as conn:
        try:
            return recipes.analyse(conn, body.payload())
        except ValueError as err:
            bad(err)


@router.post("/recipes/import")
def import_recipe(body: RecipeImport):
    """Store a recipe as a food — only when every ingredient is accounted for."""
    with connect() as conn:
        try:
            analysis = recipes.analyse(conn, body.payload())
        except ValueError as err:
            bad(err)
        if not analysis["ok"]:
            return JSONResponse({"error": "unmatched_ingredients", "analysis": analysis}, status_code=422)
        origin = "mordorcook" if body.origin == "mordorcook" else "import"
        return {"ok": True, "food": recipes.save(conn, analysis, origin), "analysis": analysis}


@router.post("/recipes/sync")
def sync_recipes(force: bool = False):
    """Pull every recipe from MordorCook on demand. Nothing runs in the background."""
    url = mordorcook_url()
    if not url:
        raise HTTPException(400, "MORDORCOOK_URL is not set")
    try:
        items = recipes.fetch_mordorcook(url)
    except RuntimeError as err:
        raise HTTPException(502, str(err)) from None
    with connect() as conn:
        if force:
            conn.execute("UPDATE user_foods SET details=json_set(details, '$.updated_at', '') WHERE source='recipe'")
        return recipes.sync(conn, items)


@router.get("/recipes/{ref}/items")
def recipe_items(ref: str, portions: float = 1):
    """A recipe split back into its ingredients, for logging them one by one."""
    with connect() as conn:
        row = get_food(conn, ref)
        if row is None or row["source"] != "recipe":
            raise HTTPException(404, "recipe not found")
        portions = max(0.1, min(50.0, portions))
        items = recipes.breakdown(row, portions)
        foods = catalog.get_foods(conn, [i["food_ref"] for i in items])
        return {"name": row["name"], "portions": portions,
                "items": [{**i, "food": food_dict(foods[i["food_ref"]])} for i in items if i["food_ref"] in foods]}


@router.get("/mappings")
def list_mappings():
    with connect() as conn:
        keys = [r["key"] for r in conn.execute("SELECT key FROM ingredient_mappings ORDER BY key")]
        return {"mappings": [recipes.get_mapping(conn, k) for k in keys]}


@router.post("/mappings")
def set_mapping(body: MappingIn):
    with connect() as conn:
        try:
            return recipes.save_mapping(conn, body.item, body.food_ref, body.ignore, body.grams, body.amount, body.unit)
        except ValueError as err:
            bad(err)


@router.delete("/mappings/{key}")
def delete_mapping(key: str):
    with connect() as conn:
        conn.execute("DELETE FROM ingredient_mappings WHERE key=?", (key,))
    return {"ok": True}


# ─── photo recognition (infrastructure; see recognition/) ───────────────────

@router.post("/recognize")
async def start_recognition(background: BackgroundTasks, image: UploadFile = File(...)):
    if not recognize.enabled():
        await image.close()
        return JSONResponse({"error": "food_ai_disabled"}, status_code=501)
    try:
        data = await image.read(barcode.MAX_IMAGE_BYTES + 1)
    finally:
        await image.close()
    if len(data) > barcode.MAX_IMAGE_BYTES:
        raise HTTPException(413, "image too large")
    with connect() as conn:
        job_id = recognize.create_job(conn)
    background.add_task(recognize.run_job, job_id, data)
    return JSONResponse({"id": job_id, "status": "pending"}, status_code=202)


@router.get("/recognize/{job_id}")
def recognition_status(job_id: str):
    with connect() as conn:
        job = recognize.get_job(conn, job_id)
    if job is None:
        raise HTTPException(404, "job not found")
    return job


# ─── config ─────────────────────────────────────────────────────────────────

def config() -> dict[str, Any]:
    """Feature flags for the frontend."""
    with connect() as conn:
        recipes = conn.execute("SELECT COUNT(*) AS n FROM user_foods WHERE source='recipe'").fetchone()["n"]
    return {
        "food_ai_enabled": recognize.enabled(),
        "mordorcook_enabled": bool(mordorcook_url()),
        "recipes": recipes,
        "barcode": True,
        "history": True,
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
            "weight_target": rows("SELECT * FROM weight_target"),
            "my_meals": rows("SELECT * FROM my_meals ORDER BY id"),
            "user_foods": rows("SELECT * FROM user_foods ORDER BY id"),
            "ingredient_mappings": rows("SELECT * FROM ingredient_mappings"),
            "restaurant_items": rows("SELECT * FROM restaurant_items ORDER BY restaurant, position"),
            "containers": rows("SELECT * FROM containers ORDER BY id"),
            "batches": rows("SELECT * FROM batches ORDER BY id"),
        }
