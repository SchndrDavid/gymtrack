"""Recipes as foods: MordorCook sync, JSON import, ingredient matching.

A recipe becomes a row in user_foods (source='recipe') with values per 100 g and a serving of
one portion. When the recipe carries no nutrition of its own, the values are computed from its
ingredients: every ingredient is matched against the foods and its amount converted to grams.

Nothing is guessed silently. An ingredient is one of

  matched    a remembered mapping, or a basic/own food whose name contains every word
  uncertain  only a loose match (fewer words, or an Open Food Facts product) — needs a yes
  unmatched  no food, or the amount cannot be turned into grams unambiguously
  ignored    negligible (salt, water, pepper) or ignored on purpose

and a recipe is saved only when nothing is uncertain or unmatched. The user's answers are
kept in ingredient_mappings, so the same ingredient is never asked about twice.
"""

import json
import re
import sqlite3
import urllib.error
import urllib.request

from .catalog import food_dict, fold, fts_query, get_food, now_iso, ref_of

# ─── units ──────────────────────────────────────────────────────────────────

MASS = {"g": 1, "gr": 1, "gram": 1, "grams": 1, "gramu": 1, "gramy": 1, "dkg": 10, "dag": 10,
        "kg": 1000, "mg": 0.001, "oz": 28.35, "lb": 453.6, "lbs": 453.6}
VOLUME_ML = {"ml": 1, "cl": 10, "dl": 100, "l": 1000, "litr": 1000, "litru": 1000,
             "tsp": 5, "tsps": 5, "teaspoon": 5, "teaspoons": 5, "lzicka": 5, "lzicky": 5, "lzicek": 5, "kl": 5,
             "tbsp": 15, "tbsps": 15, "tablespoon": 15, "tablespoons": 15, "lzice": 15, "lzic": 15, "pl": 15,
             "cup": 240, "cups": 240, "hrnek": 240, "hrnky": 240, "hrnku": 240}
PIECE = {"", "ks", "kus", "kusy", "kusu", "piece", "pieces", "pc", "pcs", "x", "whole"}
CLOVE = {"clove", "cloves", "strouzek", "strouzky", "strouzku"}
SLICE = {"slice", "slices", "platek", "platky", "platku"}

# g/ml, only where a spoon or a cup of it weighs the same every time. Matched on the food's
# name or the ingredient text, folded (no diacritics). Anything else asks for grams.
DENSITY = [
    (("voda", "water"), 1.0), (("mleko", "milk"), 1.03), (("olej", "oil"), 0.92),
    (("ocet", "vinegar"), 1.01), (("vyvar", "broth", "stock"), 1.0), (("vino", "wine"), 0.99),
    (("pivo", "beer"), 1.01), (("dzus", "stava", "juice"), 1.04), (("smetana", "cream"), 1.0),
    (("jogurt", "yogurt", "yoghurt"), 1.03), (("med", "honey"), 1.42), (("sirup", "syrup"), 1.33),
    (("cukr", "sugar"), 0.85), (("mouka", "flour"), 0.53), (("sul", "salt"), 1.2),
    (("maslo", "butter"), 0.96), (("ryze", "rice"), 0.85), (("vlocky", "oats"), 0.34),
]

# Adds no meaningful energy; ignored without asking.
NEGLIGIBLE = ("sul", "soli", "sol", "salt", "pepr", "pepper", "voda", "vody", "vodou", "water", "led", "ice")


def negligible(words: list[str]) -> bool:
    return bool(words) and all(any(w == k or (w.startswith(k) and len(w) <= len(k) + 2) for k in NEGLIGIBLE)
                               for w in words)

STOP = {"a", "and", "or", "of", "the", "to", "for", "with", "na", "do", "s", "se", "z", "ze", "i", "nebo",
        "fresh", "freshly", "chopped", "sliced", "diced", "minced", "large", "small", "medium", "big",
        "finely", "roughly", "ground", "cerstvy", "cerstva", "cerstve", "nasekany", "nasekana", "nasekane",
        "nakrajeny", "nakrajena", "nakrajene", "velky", "velka", "velke", "maly", "mala", "male", "stredni",
        "jemne", "hrube", "mlety", "mleta", "mlete", "podle", "chuti", "taste", "optional", "volitelne", "cca"}


def unit_key(unit: str) -> str:
    return fold(unit).strip().rstrip(".")


def ingredient_key(item: str) -> str:
    text = re.sub(r"\(.*?\)", " ", fold(item))
    return " ".join(re.findall(r"\w+", text))


def words_of(item: str) -> list[str]:
    return [w for w in ingredient_key(item).split() if w not in STOP and not w.isdigit()]


def stem(word: str) -> str:
    """A crude stem for Czech endings: 'olivového oleje' finds 'Olivový olej', 'eggs' finds 'egg'.

    Every word of the ingredient still has to match, which keeps the loose stem honest.
    """
    if len(word) >= 6:
        return word[:max(4, len(word) - 3)]
    if len(word) >= 4:
        return word[:-1]
    return word


def density(*texts: str) -> float | None:
    folded = " ".join(fold(t) for t in texts if t)
    words = set(re.findall(r"\w+", folded))
    for keys, value in DENSITY:
        if any(k in words or any(w.startswith(k) for w in words if len(k) >= 4) for k in keys):
            return value
    return None


def piece_grams(food, unit: str) -> float | None:
    """Weight of one piece, clove or slice — only when the food itself says what that is."""
    if food is None or not food["serving_g"]:
        return None
    label = fold(food["serving_label"] or "")
    u = unit_key(unit)
    if u in PIECE and re.match(r"^1\s*(ks|kus|piece)\b", label):
        return food["serving_g"]
    if u in CLOVE and "strouz" in label:
        return food["serving_g"]
    if u in SLICE and "plat" in label:
        return food["serving_g"]
    return None


def to_grams(amount, unit: str, food, item: str, mapping: dict | None) -> tuple[float | None, str]:
    """(grams, how) or (None, why not)."""
    u = unit_key(unit)
    unit_grams = (mapping or {}).get("unit_grams") or {}
    if amount is None:
        if mapping and mapping.get("fixed_grams"):
            return mapping["fixed_grams"], "remembered amount"
        return None, "no amount given"
    if amount <= 0:
        return None, "amount is zero"
    if u in MASS:
        return amount * MASS[u], ""
    if u in unit_grams:
        return amount * unit_grams[u], "remembered unit weight"
    if u in VOLUME_ML:
        d = density(food["name"] if food else "", (food["aliases"] if food and "aliases" in food.keys() else ""), item)
        if d is None:
            return None, f"no fixed weight for {unit or 'this unit'} of this"
        return amount * VOLUME_ML[u] * d, ""
    g = piece_grams(food, unit)
    if g is not None:
        return amount * g, ""
    return None, f"no fixed weight for {unit or 'a piece'} of this"


# ─── mappings ───────────────────────────────────────────────────────────────

def get_mapping(conn: sqlite3.Connection, key: str) -> dict | None:
    r = conn.execute("SELECT * FROM ingredient_mappings WHERE key=?", (key,)).fetchone()
    if r is None:
        return None
    return {"key": r["key"], "food_ref": r["food_ref"], "ignore": bool(r["ignore"]),
            "unit_grams": json.loads(r["unit_grams"] or "{}"), "fixed_grams": r["fixed_grams"]}


def save_mapping(conn: sqlite3.Connection, item: str, food_ref: str | None, ignore: bool,
                 grams: float | None, amount: float | None, unit: str) -> dict:
    """Remember how an ingredient maps. `grams` is the weight of this line as written."""
    key = ingredient_key(item)
    if not key:
        raise ValueError("empty ingredient")
    if not ignore:
        food = get_food(conn, food_ref or "")
        if food is None:
            raise ValueError("unknown food")
    old = get_mapping(conn, key) or {"unit_grams": {}, "fixed_grams": None}
    if old.get("food_ref") != food_ref:
        old = {"unit_grams": {}, "fixed_grams": None}     # a new food, old weights no longer apply
    unit_grams, fixed = dict(old["unit_grams"]), old["fixed_grams"]
    u = unit_key(unit)
    if not ignore and grams:
        if amount:
            if u not in MASS:
                unit_grams[u] = round(grams / amount, 2)
        else:
            fixed = round(grams, 1)
    conn.execute(
        """INSERT INTO ingredient_mappings (key, food_ref, ignore, unit_grams, fixed_grams, created_at)
           VALUES (?,?,?,?,?,?)
           ON CONFLICT(key) DO UPDATE SET food_ref=excluded.food_ref, ignore=excluded.ignore,
               unit_grams=excluded.unit_grams, fixed_grams=excluded.fixed_grams""",
        (key, None if ignore else food_ref, int(ignore), json.dumps(unit_grams), fixed, now_iso()))
    return get_mapping(conn, key)


# ─── matching ───────────────────────────────────────────────────────────────

def _candidates(conn, words: list[str], table: str, where: str = "", limit: int = 12):
    match = fts_query(" ".join(stem(w) for w in words))
    if not match:
        return []
    name = "cat.foods" if table == "foods" else "user_foods"
    return conn.execute(
        f"""SELECT t.*, bm25({table}_fts, 10.0, 2.0, 1.0) AS score
              FROM {'cat.' if table == 'foods' else ''}{table}_fts JOIN {name} t ON t.id = {table}_fts.rowid
             WHERE {table}_fts MATCH ? {where} ORDER BY score LIMIT ?""",
        (match, limit)).fetchall()


def _best(rows, words: list[str]):
    """Prefer a name that starts with the first word, then raw over cooked, then the shortest."""
    if not rows:
        return None
    first = stem(words[0]) if words else ""

    def key(r):
        name = fold(r["name"])
        return (0 if name.startswith(first) else 1,
                0 if ("syrov" in name or "raw" in fold(r["aliases"])) else 1,
                len(name))
    return sorted(rows, key=key)[0]


def find_food(conn: sqlite3.Connection, item: str) -> tuple[object, str]:
    """(row, status) — status 'matched' or 'uncertain'; (None, 'unmatched') when nothing fits."""
    words = words_of(item)
    if not words:
        return None, "unmatched"
    for table, where in (("foods", "AND t.source = 'usda'"), ("user_foods", "AND t.source = 'custom'")):
        best = _best(_candidates(conn, words, table, where), words)
        if best is not None:
            return best, "matched"
    # Looser: drop trailing words, or fall back to branded products. Never auto-accepted.
    for n in range(len(words) - 1, 0, -1):
        best = _best(_candidates(conn, words[:n], "foods", "AND t.source = 'usda'"), words)
        if best is not None:
            return best, "uncertain"
    rows = _candidates(conn, words, "foods", "AND t.source = 'off'", limit=5)
    if rows:
        return rows[0], "uncertain"
    return None, "unmatched"


def line_text(ing: dict) -> str:
    amount = ing.get("amount")
    num = "" if amount is None else (f"{amount:g}" if isinstance(amount, (int, float)) else str(amount))
    return " ".join(x for x in (num, ing.get("unit") or "", ing.get("item") or "") if x).strip()


def analyse_ingredient(conn: sqlite3.Connection, ing: dict) -> dict:
    item = str(ing.get("item") or "").strip()
    amount = ing.get("amount")
    try:
        amount = None if amount in (None, "") else float(str(amount).replace(",", "."))
    except ValueError:
        amount = None
    unit = str(ing.get("unit") or "").strip()
    out = {"text": line_text({**ing, "amount": amount}), "item": item, "amount": amount, "unit": unit,
           "key": ingredient_key(item), "status": "unmatched", "food": None, "grams": None, "reason": ""}
    if not out["key"]:
        out.update(status="ignored", reason="empty line")
        return out

    mapping = get_mapping(conn, out["key"])
    if mapping and mapping["ignore"]:
        out.update(status="ignored", reason="ignored by you")
        return out

    food, status = (None, "unmatched")
    if mapping and mapping["food_ref"]:
        food = get_food(conn, mapping["food_ref"])
        status = "matched" if food is not None else "unmatched"
    if food is None:
        if negligible(words_of(item)):
            out.update(status="ignored", reason="negligible")
            return out
        food, status = find_food(conn, item)
    if food is None:
        out["reason"] = "no matching food"
        return out

    out["food"] = food_dict(food)
    grams, how = to_grams(amount, unit, food, item, mapping)
    if grams is None:
        out.update(status="unmatched", reason=how)
        return out
    out["grams"] = round(grams, 1)
    out["status"] = status
    out["reason"] = how if status == "matched" else "loose match — please confirm"
    k = grams / 100.0
    out.update(kcal=round(food["kcal_100g"] * k, 1), protein=round(food["protein_100g"] * k, 1),
               carbs=round(food["carbs_100g"] * k, 1), fat=round(food["fat_100g"] * k, 1))
    return out


def normalise_recipe(raw: dict) -> dict:
    """Accept MordorCook's recipe shape and the documented import shape."""
    if not isinstance(raw, dict):
        raise ValueError("a recipe is a JSON object")
    title = str(raw.get("title") or raw.get("name") or "").strip()
    if not title:
        raise ValueError("recipe needs a title")
    try:
        servings = int(float(raw.get("servings") or 1))
    except (TypeError, ValueError):
        servings = 1
    ingredients = []
    for ing in raw.get("ingredients") or []:
        if isinstance(ing, str):
            ing = {"item": ing}
        if isinstance(ing, dict):
            ingredients.append({"amount": ing.get("amount"), "unit": ing.get("unit") or "",
                                "item": ing.get("item") or ing.get("name") or ""})
    nutrition = raw.get("nutrition") if isinstance(raw.get("nutrition"), dict) else None
    rid = str(raw.get("id") or "").strip()
    return {"id": rid, "title": title[:200], "servings": max(1, min(99, servings)),
            "ingredients": ingredients, "nutrition": nutrition,
            "updated_at": str(raw.get("updated_at") or "")}


def analyse(conn: sqlite3.Connection, raw: dict) -> dict:
    r = normalise_recipe(raw)
    lines = [analyse_ingredient(conn, ing) for ing in r["ingredients"]]
    problems = [x for x in lines if x["status"] in ("unmatched", "uncertain")]
    counted = [x for x in lines if x["status"] == "matched"]
    total_g = sum(x["grams"] for x in counted)
    totals = {k: round(sum(x[k] for x in counted), 1) for k in ("kcal", "protein", "carbs", "fat")}
    result = {"recipe": r, "ingredients": lines, "ok": not problems and total_g > 0,
              "problems": len(problems), "total_grams": round(total_g, 1), "totals": totals}

    n = r["nutrition"]
    if n and n.get("kcal"):
        # The recipe brings its own numbers — trust them, per serving unless stated otherwise.
        per = str(n.get("per") or "serving")
        factor = r["servings"] if per == "serving" else 1
        result["totals"] = {k: round(float(n.get(k) or 0) * factor, 1) for k in ("kcal", "protein", "carbs", "fat")}
        result["nutrition_source"] = "recipe"
        weight = float(n.get("serving_g") or 0) * r["servings"] or total_g
        result["total_grams"] = round(weight, 1)
        result["ok"] = weight > 0
        if not result["ok"]:
            result["problems"] = max(1, result["problems"])
    else:
        result["nutrition_source"] = "ingredients"
    if result["ok"]:
        t, g, s = result["totals"], result["total_grams"], r["servings"]
        result["food"] = {
            "name": r["title"],
            **{f"{k}_100g": round(v / g * 100, 2) for k, v in (("kcal", t["kcal"]), ("protein", t["protein"]),
                                                               ("carbs", t["carbs"]), ("fat", t["fat"]))},
            "serving_g": round(g / s, 1),
            "serving_label": "1 portion",
            "per_serving": {k: round(v / s, 1) for k, v in t.items()},
        }
    return result


def save(conn: sqlite3.Connection, analysis: dict, origin: str) -> dict:
    """Store an analysed recipe as a food. Upserts by recipe id."""
    r, f = analysis["recipe"], analysis["food"]
    source_id = r["id"] or ("import:" + ingredient_key(r["title"]).replace(" ", "-"))
    details = {
        "origin": origin, "servings": r["servings"], "total_grams": analysis["total_grams"],
        "nutrition_source": analysis["nutrition_source"], "updated_at": r["updated_at"],
        "ingredients": [{k: x.get(k) for k in ("text", "item", "grams", "status", "kcal", "protein", "carbs", "fat")}
                        | {"food_ref": x["food"]["ref"] if x.get("food") else None,
                           "food_name": x["food"]["name"] if x.get("food") else None}
                        for x in analysis["ingredients"]],
    }
    values = {"name": f["name"], "brand": "MordorCook" if origin == "mordorcook" else "Recipe",
              "aliases": " ".join(x["item"] for x in analysis["ingredients"])[:500],
              "kcal_100g": f["kcal_100g"], "protein_100g": f["protein_100g"], "carbs_100g": f["carbs_100g"],
              "fat_100g": f["fat_100g"], "serving_g": f["serving_g"], "serving_label": f["serving_label"],
              "details": json.dumps(details, ensure_ascii=False), "stamp": now_iso(), "source_id": source_id}
    row = conn.execute("SELECT id FROM user_foods WHERE source='recipe' AND source_id=?", (source_id,)).fetchone()
    if row:
        conn.execute(
            """UPDATE user_foods SET name=:name, brand=:brand, aliases=:aliases, kcal_100g=:kcal_100g,
                   protein_100g=:protein_100g, carbs_100g=:carbs_100g, fat_100g=:fat_100g, serving_g=:serving_g,
                   serving_label=:serving_label, details=:details, updated_at=:stamp WHERE id=:id""",
            {**values, "id": row["id"]})
        rid = row["id"]
    else:
        rid = conn.execute(
            """INSERT INTO user_foods (source, source_id, barcode, name, brand, aliases, kcal_100g, protein_100g,
                   carbs_100g, fat_100g, fiber_100g, serving_g, serving_label, details, updated_at, created_at)
               VALUES ('recipe', :source_id, NULL, :name, :brand, :aliases, :kcal_100g, :protein_100g, :carbs_100g,
                   :fat_100g, NULL, :serving_g, :serving_label, :details, :stamp, :stamp)""", values).lastrowid
    return recipe_dict(conn.execute("SELECT * FROM user_foods WHERE id=?", (rid,)).fetchone())


def recipe_dict(row) -> dict:
    d = food_dict(row)
    d["details"] = json.loads(row["details"] or "{}")
    return d


def unchanged(conn: sqlite3.Connection, recipe: dict) -> bool:
    """A synced recipe whose MordorCook updated_at is the same needs no work."""
    if not recipe["id"] or not recipe["updated_at"]:
        return False
    row = conn.execute("SELECT details FROM user_foods WHERE source='recipe' AND source_id=?",
                       (recipe["id"],)).fetchone()
    return bool(row) and json.loads(row["details"] or "{}").get("updated_at") == recipe["updated_at"]


def breakdown(row, portions: float) -> list[dict]:
    """The recipe's ingredients for `portions` portions, for the multi-add sheet."""
    d = json.loads(row["details"] or "{}")
    servings = d.get("servings") or 1
    out = []
    for x in d.get("ingredients") or []:
        if x.get("status") != "matched" or not x.get("food_ref") or not x.get("grams"):
            continue
        out.append({"food_ref": x["food_ref"], "name": x.get("food_name") or x["item"], "label": x.get("text"),
                    "grams": round(x["grams"] * portions / servings, 1)})
    return out


# ─── MordorCook ─────────────────────────────────────────────────────────────

def fetch_mordorcook(base_url: str, timeout: float = 10) -> list[dict]:
    """Every recipe from MordorCook's own API. Raises RuntimeError with a readable reason."""
    url = base_url.rstrip("/") + "/api/recipes"
    req = urllib.request.Request(url, headers={"User-Agent": "GymTrack/1.0 (self-hosted)", "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.load(resp)
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise RuntimeError(f"MordorCook is not reachable at {base_url}: {getattr(e, 'reason', e)}") from None
    except ValueError:
        raise RuntimeError("MordorCook answered with something that is not JSON") from None
    if not isinstance(data, list):
        raise RuntimeError("MordorCook /api/recipes did not return a list")
    return data


def sync(conn: sqlite3.Connection, recipes: list[dict]) -> dict:
    """Mirror MordorCook: new and changed recipes are (re)computed, deleted ones disappear.

    Days already logged keep their values — the log stores its own snapshot."""
    saved, same, pending, errors = [], 0, [], []
    present = {str(r.get("id")) for r in recipes if isinstance(r, dict) and r.get("id")}
    removed = []
    for row in conn.execute("SELECT id, source_id, name, details FROM user_foods WHERE source='recipe'").fetchall():
        origin = json.loads(row["details"] or "{}").get("origin")
        if origin == "mordorcook" and row["source_id"] not in present:
            conn.execute("DELETE FROM user_foods WHERE id=?", (row["id"],))
            conn.execute("DELETE FROM food_favorites WHERE food_ref=?", (f"user:{row['id']}",))
            removed.append(row["name"])
    for raw in recipes:
        try:
            r = normalise_recipe(raw)
        except ValueError as e:
            errors.append(str(e))
            continue
        if unchanged(conn, r):
            same += 1
            continue
        a = analyse(conn, raw)
        if a["ok"]:
            saved.append(save(conn, a, "mordorcook")["name"])
        else:
            pending.append(a)
    return {"total": len(recipes), "saved": saved, "unchanged": same, "pending": pending, "errors": errors,
            "removed": removed}
