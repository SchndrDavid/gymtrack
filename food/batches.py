"""Batches: food cooked once and split into boxes.

The nutrition is added up from the raw ingredients (the recipe) and stored as a snapshot; what is
weighed and logged is the cooked food, whose weight differs from the raw one. So everything per
100 g is the batch total over the cooked net weight:

    net = gross − pot            2 100 g − 900 g = 1 200 g
    per 100 g = total / net      2 000 kcal / 1 200 g = 166.7 kcal
    per box = total / boxes      2 000 kcal / 4 = 500 kcal

A logged entry is a snapshot like any other: editing or deleting the batch never changes a past day.
"""

import json
import re
import sqlite3
from datetime import date as Date

from .catalog import get_food, get_foods, now_iso, num
from .log import snapshot

STORAGE = ("fridge", "freezer", "done")
FRIDGE_DAYS = 3                       # older than this in the fridge gets a warning
MODES = ("grams", "box", "fraction", "gross")
MAX_SIDES = 8
KEYS = ("kcal", "protein", "carbs", "fat")


def r1(x) -> float:
    return round(float(x or 0), 1)


def valid_date(value) -> str:
    try:
        return Date.fromisoformat(str(value)).isoformat()
    except ValueError:
        raise ValueError("date must be YYYY-MM-DD") from None


def clean_name(value, fallback: str = "") -> str:
    name = re.sub(r"\s+", " ", str(value or "")).strip()[:120] or fallback
    if not name:
        raise ValueError("name required")
    return name


# ─── containers ─────────────────────────────────────────────────────────────

def container_dict(r) -> dict:
    return {k: r[k] for k in ("id", "name", "tare_g")}


def all_containers(conn: sqlite3.Connection) -> list[dict]:
    return [container_dict(r) for r in conn.execute("SELECT * FROM containers ORDER BY name COLLATE NOCASE, id")]


def save_container(conn: sqlite3.Connection, raw: dict, container_id: int | None = None) -> dict:
    name = clean_name(raw.get("name"))
    tare = num(raw.get("tare_g"), 0, 20000)
    if tare is None:
        raise ValueError("tare_g must be a number of grams between 0 and 20000")
    tare = round(tare, 1)
    if container_id is None:
        container_id = conn.execute("INSERT INTO containers (name, tare_g, updated_at) VALUES (?,?,?)",
                                    (name, tare, now_iso())).lastrowid
    elif not conn.execute("UPDATE containers SET name=?, tare_g=?, updated_at=? WHERE id=?",
                          (name, tare, now_iso(), container_id)).rowcount:
        raise LookupError("no such container")
    return container_dict(conn.execute("SELECT * FROM containers WHERE id=?", (container_id,)).fetchone())


# ─── the arithmetic ─────────────────────────────────────────────────────────

def net_weight(gross: float, tare: float) -> float:
    """The cooked food alone. Raises ValueError when the scale reading cannot be right."""
    net = gross - tare
    if net <= 0:
        raise ValueError("the weight with the container must be above the container's own weight")
    return net


def split_boxes(net: float, boxes: int, weights: list[float] | None = None) -> list[float]:
    """Grams per box: the given weights, or an equal split."""
    if weights:
        return [round(w, 1) for w in weights]
    return [round(net / boxes, 1)] * boxes


def box_grams(b: dict) -> float:
    """What "one box" is: the average box."""
    return b["net_g"] / b["boxes"]


def per_100(b: dict) -> dict:
    return {k: round(b[k] / b["net_g"] * 100, 2) for k in KEYS}


def per_box(b: dict) -> dict:
    return {k: round(b[k] / b["boxes"], 1) for k in KEYS}


def grams_for(b: dict, mode: str, value: float, tare: float = 0.0) -> float:
    """How many grams of cooked food an entry means, by the way it was entered."""
    if mode not in MODES:
        raise ValueError(f"mode must be one of {', '.join(MODES)}")
    if mode == "grams":
        grams = value
    elif mode == "box":                                 # boxes: 1, 0.5, 2 …
        grams = value * box_grams(b)
    elif mode == "fraction":                            # a share of the whole batch: 1/4, 1/2 …
        if not 0 < value <= 1:
            raise ValueError("a share of the batch is between 0 and 1")
        grams = value * b["net_g"]
    else:                                               # gross: the weighed box minus the empty box
        grams = value - tare
        if grams <= 0:
            raise ValueError("the weight with the box must be above the empty box's weight")
    if not 0 < grams <= 5000:
        raise ValueError("grams must be between 0 and 5000")
    return round(grams, 1)


def nutrition_for(b: dict, grams: float) -> dict:
    """Totals × grams / net — in this order so 300 g of a 1 200 g, 2 000 kcal batch is exactly 500."""
    return {k: r1(b[k] * grams / b["net_g"]) for k in KEYS}


def age_days(b: dict, today: Date | None = None) -> int:
    return max(0, ((today or Date.today()) - Date.fromisoformat(b["cooked_on"])).days)


def clean_sides(conn: sqlite3.Connection, raw) -> list[dict]:
    """What goes into every box besides the batch — 80 g of rice, a slice of bread — as a snapshot per box.

    Each side is a food with the grams that go into ONE box, weighed the way the food is listed (dry rice as dry rice).
    """
    out = []
    for x in raw or []:
        if not isinstance(x, dict):
            continue
        food = get_food(conn, x.get("food_ref") or "")
        if food is None:
            raise ValueError(f"unknown food {x.get('food_ref')}")
        grams = num(x.get("grams"), 0, 2000)
        if not grams:
            raise ValueError(f"{food['name']}: grams per box required")
        out.append({"food_ref": x["food_ref"], "grams": round(grams, 1), **{k: v for k, v in snapshot(food, grams).items()}})
    if len(out) > MAX_SIDES:
        raise ValueError(f"at most {MAX_SIDES} sides")
    return out


def sides_total(sides: list[dict]) -> dict:
    return {k: round(sum(s[k] for s in sides), 1) for k in KEYS}


# ─── storing and reading ────────────────────────────────────────────────────

def batch_dict(r, today: Date | None = None) -> dict:
    b = {k: r[k] for k in ("id", "name", "recipe_ref", "cooked_on", "container_id", "container_name", "tare_g",
                           "gross_g", "net_g", "boxes", "kcal", "protein", "carbs", "fat", "remaining_g",
                           "remaining_boxes", "storage")}
    b["archived"] = bool(r["archived"])
    b["box_weights"] = json.loads(r["box_weights"] or "[]")
    b["box_g"] = round(box_grams(b), 1)
    b["per_100g"] = per_100(b)
    b["per_box"] = per_box(b)
    b["sides"] = json.loads(r["sides"] or "[]")
    b["sides_box"] = sides_total(b["sides"])
    b["box_total"] = {k: round(b["per_box"][k] + b["sides_box"][k], 1) for k in KEYS}
    b["age_days"] = age_days(b, today)
    b["old"] = b["storage"] == "fridge" and b["age_days"] > FRIDGE_DAYS and not b["archived"]
    b["remaining_boxes_g"] = round(b["remaining_g"] / b["box_g"], 1) if b["box_g"] else 0
    return b


def get_batch(conn: sqlite3.Connection, batch_id: int):
    return conn.execute("SELECT * FROM batches WHERE id=?", (batch_id,)).fetchone()


def all_batches(conn: sqlite3.Connection, include_done: bool = False) -> list[dict]:
    """Active batches (in the fridge or freezer, not archived), the newest cooked first."""
    where = "" if include_done else "WHERE archived=0 AND storage != 'done'"
    return [batch_dict(r) for r in conn.execute(
        f"SELECT * FROM batches {where} ORDER BY cooked_on DESC, id DESC")]


def recipe_totals(row) -> tuple[dict, str]:
    """The whole recipe's nutrition and its name: the values per 100 g of raw ingredients × their weight."""
    weight = float(json.loads(row["details"] or "{}").get("total_grams") or 0)
    if weight <= 0:
        raise ValueError("this recipe has no total weight to take the nutrition from")
    return {k: r1(row[f"{k}_100g"] * weight / 100) for k in KEYS}, row["name"]


def create(conn: sqlite3.Connection, raw: dict) -> dict:
    """A batch from a recipe (or typed-in totals), the container's weight and the weighing."""
    ref = raw.get("recipe_ref") or None
    if ref:
        food = get_food(conn, ref)
        if food is None or food["source"] != "recipe":
            raise ValueError("unknown recipe")
        totals, recipe_name = recipe_totals(food)
    else:
        recipe_name = ""
        totals = {}
        for key, hi in (("kcal", 100000), ("protein", 10000), ("carbs", 20000), ("fat", 10000)):
            v = num(raw.get(key), 0, hi)
            if v is None and (key == "kcal" or raw.get(key) not in (None, "")):
                raise ValueError(f"{key} must be a number between 0 and {hi}")
            totals[key] = r1(v)
        if totals["kcal"] <= 0:
            raise ValueError("kcal of the whole batch required")
    name = clean_name(raw.get("name"), recipe_name)
    cooked = valid_date(raw.get("cooked_on") or Date.today().isoformat())

    container = None
    if raw.get("container_id"):
        container = conn.execute("SELECT * FROM containers WHERE id=?", (raw["container_id"],)).fetchone()
        if container is None:
            raise ValueError("unknown container")
    tare = container["tare_g"] if container else (num(raw.get("tare_g"), 0, 20000) or 0.0)
    gross = num(raw.get("gross_g"), 0, 50000)
    if gross is None:
        raise ValueError("gross_g (the food with its container) required")
    net = round(net_weight(gross, tare), 1)

    boxes = num(raw.get("boxes"), 1, 60)
    if boxes is None or boxes != int(boxes):
        raise ValueError("boxes must be a whole number between 1 and 60")
    boxes = int(boxes)
    weights = [num(w, 0, 20000) for w in raw.get("box_weights") or []]
    if weights:
        if len(weights) != boxes or any(not w for w in weights):
            raise ValueError("give a weight for every box, or none")
        if sum(weights) > net * 1.02 + 1:
            raise ValueError("the boxes together weigh more than the batch")
    values = {
        "sides": json.dumps(clean_sides(conn, raw.get("sides")), ensure_ascii=False),
        "name": name, "recipe_ref": ref, "cooked_on": cooked,
        "container_id": container["id"] if container else None,
        "container_name": container["name"] if container else None,
        "tare_g": tare, "gross_g": gross, "net_g": net, "boxes": boxes,
        "box_weights": json.dumps(split_boxes(net, boxes, weights) if weights else []),
        **totals, "remaining_g": net, "remaining_boxes": float(boxes), "stamp": now_iso(),
    }
    bid = conn.execute(
        """INSERT INTO batches (name, recipe_ref, cooked_on, container_id, container_name, tare_g, gross_g, net_g,
               boxes, box_weights, sides, kcal, protein, carbs, fat, remaining_g, remaining_boxes, created_at, updated_at)
           VALUES (:name, :recipe_ref, :cooked_on, :container_id, :container_name, :tare_g, :gross_g, :net_g,
               :boxes, :box_weights, :sides, :kcal, :protein, :carbs, :fat, :remaining_g, :remaining_boxes, :stamp, :stamp)""",
        values).lastrowid
    return batch_dict(get_batch(conn, bid))


def update(conn: sqlite3.Connection, batch_id: int, patch: dict) -> dict:
    """Name, date, state and the remaining amount — the weighing and the totals stay as they were made."""
    r = get_batch(conn, batch_id)
    if r is None:
        raise LookupError("no such batch")
    b = {k: r[k] for k in ("name", "cooked_on", "storage", "archived", "remaining_g", "remaining_boxes", "sides")}
    if patch.get("name") is not None:
        b["name"] = clean_name(patch["name"])
    if patch.get("cooked_on") is not None:
        b["cooked_on"] = valid_date(patch["cooked_on"])
    if patch.get("storage") is not None:
        if patch["storage"] not in STORAGE:
            raise ValueError(f"storage must be one of {', '.join(STORAGE)}")
        b["storage"] = patch["storage"]
    if patch.get("archived") is not None:
        b["archived"] = 1 if patch["archived"] else 0
    if patch.get("sides") is not None:
        b["sides"] = json.dumps(clean_sides(conn, patch["sides"]), ensure_ascii=False)
    if patch.get("remaining_g") is not None:
        v = num(patch["remaining_g"], 0, 50000)
        if v is None:
            raise ValueError("remaining_g must be a number of grams")
        b["remaining_g"] = round(v, 1)
        if patch.get("remaining_boxes") is None:          # keep the box count in step with the grams
            b["remaining_boxes"] = round(min(r["boxes"], v / (r["net_g"] / r["boxes"])), 2)
    if patch.get("remaining_boxes") is not None:
        v = num(patch["remaining_boxes"], 0, 60)
        if v is None:
            raise ValueError("remaining_boxes must be a number")
        b["remaining_boxes"] = round(v, 2)
    conn.execute("""UPDATE batches SET name=?, cooked_on=?, storage=?, archived=?, remaining_g=?,
                        remaining_boxes=?, sides=?, updated_at=? WHERE id=?""",
                 (b["name"], b["cooked_on"], b["storage"], b["archived"], b["remaining_g"],
                  b["remaining_boxes"], b["sides"], now_iso(), batch_id))
    return batch_dict(get_batch(conn, batch_id))


def take(conn: sqlite3.Connection, batch_id: int, grams: float) -> None:
    """Eat (grams > 0) or give back (grams < 0) from what is left. Never below zero or above the batch."""
    r = get_batch(conn, batch_id)
    if r is None:
        return
    box = r["net_g"] / r["boxes"]
    left = min(r["net_g"], max(0.0, r["remaining_g"] - grams))
    boxes = min(float(r["boxes"]), max(0.0, r["remaining_boxes"] - grams / box))
    if left <= 0.05:                                       # nothing left: the box count follows
        left, boxes = 0.0, 0.0
    storage = r["storage"]
    if left <= 0 < r["remaining_g"]:
        storage = "done"
    elif left > 0 and r["remaining_g"] <= 0 and storage == "done":
        storage = "fridge"                                 # an entry was taken back
    conn.execute("UPDATE batches SET remaining_g=?, remaining_boxes=?, storage=?, updated_at=? WHERE id=?",
                 (round(left, 1), round(boxes, 2), storage, now_iso(), batch_id))


def log_item(conn: sqlite3.Connection, batch_id: int, mode: str, value, tare=None, container_id=None,
             with_sides: bool = True) -> dict:
    """What a log entry holds for a portion of the batch (nothing is written yet)."""
    r = get_batch(conn, batch_id)
    if r is None:
        raise LookupError("no such batch")
    v = num(value, 0)
    if not v:
        raise ValueError("an amount is required")
    t = 0.0
    if mode == "gross":
        if container_id:
            c = conn.execute("SELECT tare_g FROM containers WHERE id=?", (container_id,)).fetchone()
            if c is None:
                raise ValueError("unknown container")
            t = c["tare_g"]
        else:
            t = num(tare, 0, 20000)
            if t is None:
                raise ValueError("tare (the empty box's weight) required")
    b = batch_dict(r)
    grams = grams_for(b, mode, v, t)
    if not (with_sides and b["sides"]):
        return {"name": b["name"], "grams": grams, **nutrition_for(b, grams)}
    # The sides go in per box, so they follow the share of a box that is eaten: 150 g of a 300 g box is half the rice.
    k = grams / box_grams(b)
    nut = {m: r1(b[m] * grams / b["net_g"] + b["sides_box"][m] * k) for m in KEYS}
    label = ", ".join(f"{s['name']} {r1(s['grams'] * k):g} g" for s in b["sides"])
    return {"name": f"{b['name']} + {label}"[:200], "grams": grams, **nut}
