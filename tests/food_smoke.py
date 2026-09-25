"""End-to-end smoke test of the Food module. Throwaway databases, no network.

    python tests/food_smoke.py

The catalogue is built from tiny synthetic stand-ins for the USDA release and the Open Food
Facts dump, generated below. Their numbers are test data, not nutrition facts.
"""

import csv
import gzip
import io
import json
import os
import sys
import tempfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
TMP = Path(tempfile.mkdtemp())
os.environ["GYMTRACK_DB"] = str(TMP / "test.db")
os.environ["GYMTRACK_FOODS_DB"] = str(TMP / "foods.db")
os.environ.pop("MORDORCOOK_URL", None)
os.environ["FOOD_AI_ENABLED"] = "false"

from fastapi.testclient import TestClient  # noqa: E402

import main  # noqa: E402
from scripts import build_basic_seed, import_basic, import_off  # noqa: E402

checks = 0


def check(label, condition):
    global checks
    checks += 1
    if not condition:
        print(f"FAIL  {label}")
        sys.exit(1)
    print(f"ok    {label}")


def csv_bytes(header, rows):
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(header)
    w.writerows(rows)
    return buf.getvalue().encode()


# ─── a fake FoodData Central release ────────────────────────────────────────
FDC = [  # fdc_id, description, kcal, protein, fat, carbs, fiber
    (1001, "Chicken, broilers or fryers, breast, meat only, raw", 120, 22.5, 2.6, 0, None),
    (1002, "Egg, whole, raw, fresh", 143, 12.6, 9.5, 0.7, 0),
    (1003, "Rice, white, long-grain, regular, enriched, cooked", 130, 2.7, 0.3, 28.2, 0.4),
    (1004, "Yogurt, plain, whole milk", 61, 3.5, 3.3, 4.7, 0),
    (1005, "Oil, olive, salad or cooking", 884, 0, 100, 0, 0),
    (1006, "Onions, raw", 40, 1.1, 0.1, 9.3, 1.7),
    (1007, "Garlic, raw", 149, 6.4, 0.5, 33, 2.1),
    (1008, "Milk, whole, 3.25% milkfat, with added vitamin D", 61, 3.2, 3.3, 4.8, 0),
    (1009, "Chicken, broilers or fryers, breast, meat and skin, cooked, fried, batter", 260, 25, 13, 9, 0.3),
    (1010, "Salt, table", 0, 0, 0, 0, 0),
]
NUTRIENT_IDS = ("1008", "1003", "1004", "1005", "1079")
release = TMP / "fdc.zip"
with zipfile.ZipFile(release, "w") as zf:
    zf.writestr("FoodData_Central_test/food.csv", csv_bytes(
        ["fdc_id", "data_type", "description", "food_category_id", "publication_date"],
        [(f[0], "sr_legacy_food", f[1], "", "2019-04-01") for f in FDC]))
    rows, n = [], 0
    for f in FDC:
        for nid, value in zip(NUTRIENT_IDS, f[2:]):
            if value is not None:
                n += 1
                rows.append((n, f[0], nid, value))
    zf.writestr("FoodData_Central_test/food_nutrient.csv",
                csv_bytes(["id", "fdc_id", "nutrient_id", "amount"], rows))
    zf.writestr("FoodData_Central_test/food_portion.csv", csv_bytes(
        ["id", "fdc_id", "seq_num", "amount", "measure_unit_id", "portion_description", "modifier", "gram_weight"],
        [(1, 1002, 1, 1, 9999, "", "large", 50), (2, 1006, 1, 1, 9999, "", "medium (2-1/2\" dia)", 110),
         (3, 1007, 1, 1, 9999, "", "clove", 3)]))

src = {"foods": [
    {"name": "Kuřecí prsa syrová", "usda": ["Chicken, broilers or fryers, breast, meat only, raw"]},
    {"name": "Vejce slepičí syrové", "usda": ["Egg, whole, raw, fresh"], "portion": {"match": "large", "label": "1 ks"}},
    {"name": "Rýže bílá dlouhozrnná (jasmínová, basmati) vařená",
     "usda": ["Rice, white, long-grain, regular, raw, enriched", "Rice, white, long-grain, regular, enriched, cooked"]},
    {"name": "Jogurt bílý plnotučný", "usda": ["Yogurt, plain, whole milk"]},
    {"name": "Olivový olej", "usda": ["Oil, olive, salad or cooking"]},
    {"name": "Cibule", "usda": ["Onions, raw"], "portion": {"match": "medium", "label": "1 ks"}},
    {"name": "Česnek", "usda": ["Garlic, raw"], "portion": {"match": "clove", "label": "1 stroužek"}},
    {"name": "Mléko plnotučné (3,25 %)", "usda": ["Milk, whole, 3.25% milkfat, with added vitamin D"]},
    {"name": "Kuřecí prsa smažená v těstíčku (řízek)",
     "usda": ["Chicken, broilers or fryers, breast, meat and skin, cooked, fried, batter"]},
    {"name": "Sůl", "usda": ["Salt, table"]},
    {"name": "Tvaroh polotučný", "usda": ["Cheese, quark, not in USDA"]},
]}
built = build_basic_seed.build(src, build_basic_seed.load_release(
    [str(release)], {build_basic_seed.norm(d) for f in src["foods"] for d in f["usda"]}))
seed_foods, skipped = built
check("seed keeps every food USDA has", len(seed_foods) == 10)
check("seed skips a food USDA lacks", any("Tvaroh" in s for s in skipped))
check("seed carries the fdc_id", seed_foods[0]["fdc_id"] == 1001)
check("seed takes the first description found", seed_foods[2]["fdc_id"] == 1003)
check("seed reads the piece weight", seed_foods[1]["serving_g"] == 50 and seed_foods[1]["serving_label"] == "1 ks")
check("seed values come from the release", seed_foods[3]["kcal_100g"] == 61 and seed_foods[3]["protein_100g"] == 3.5)
check("committed source list parses",
      len(json.loads((ROOT / "seed" / "basic_foods.src.json").read_text())["foods"]) >= 250)

seed_file = TMP / "basic_foods.json"
seed_file.write_text(json.dumps({"foods": seed_foods}, ensure_ascii=False))
cat = import_basic.open_catalog(os.environ["GYMTRACK_FOODS_DB"])
check("basic foods import", import_basic.import_seed(cat, seed_file) == 10)
check("re-import is an upsert", import_basic.import_seed(cat, seed_file) == 10
      and cat.execute("SELECT COUNT(*) FROM foods").fetchone()[0] == 10)

# ─── a fake Open Food Facts dump ────────────────────────────────────────────
OFF_HEADER = ["code", "product_name", "brands", "countries_tags", "serving_size", "serving_quantity",
              "energy-kcal_100g", "proteins_100g", "carbohydrates_100g", "fat_100g", "fiber_100g"]
OFF_ROWS = [
    ["8590000000011", "Jogurt bílý", "Mlékárna Test", "en:czech-republic", "150 g", "150", "68", "4.5", "5.1", "3.2", ""],
    ["8590000000028", "Jogurt jahodový", "Mlékárna Test", "en:czech-republic,en:slovakia", "", "", "95", "3.5", "14", "2.9", "0.2"],
    ["8580000000035", "Kuřecí prsní řízek", "Řeznictví", "en:slovakia", "", "", "210", "18", "12", "10", ""],
    ["4000000000042", "Joghurt Natur", "Test DE", "en:germany", "", "", "65", "4", "5", "3", ""],
    ["8590000000059", "Nesmysl", "", "en:czech-republic", "", "", "1500", "1", "1", "1", ""],
    ["8590000000066", "", "", "en:czech-republic", "", "", "100", "1", "1", "1", ""],
    ["8590000000073", "Záporný", "", "en:czech-republic", "", "", "100", "-1", "1", "1", ""],
    ["8590000000080", "Bez energie", "", "en:czech-republic", "", "", "", "1", "1", "1", ""],
    ["8590000000097", "Tatranka lísková", "Opavia", "en:czech-republic", "1 ks (50 g)", "50", "520", "6.5", "60", "28", "2"],
]
dump = TMP / "off.csv.gz"
with gzip.open(dump, "wt", encoding="utf-8") as fh:
    fh.write("\t".join(OFF_HEADER) + "\n")
    for r in OFF_ROWS:
        fh.write("\t".join(r) + "\n")
with import_off.open_stream(path=str(dump)) as stream:
    kept, lines = import_off.import_stream(cat, stream, progress=False)
import_basic.rebuild_fts(cat)
cat.commit()
check("OFF import reads every line", lines == len(OFF_ROWS))
check("OFF import keeps only valid CZ/SK products", kept == 4)
check("OFF import stores the barcode", cat.execute(
    "SELECT name FROM foods WHERE barcode='8590000000097'").fetchone()[0] == "Tatranka lísková")
check("OFF import keeps the serving", cat.execute(
    "SELECT serving_g FROM foods WHERE barcode='8590000000097'").fetchone()[0] == 50)
with import_off.open_stream(path=str(dump)) as stream:
    import_off.import_stream(cat, stream, progress=False)
check("OFF re-import upserts by barcode",
      cat.execute("SELECT COUNT(*) FROM foods WHERE source='off'").fetchone()[0] == 4)
cat.close()

client = TestClient(main.app)

# ─── search ─────────────────────────────────────────────────────────────────
def names(q, **kw):
    params = {"q": q, **kw}
    return [r["name"] for r in client.get("/api/food/search", params=params).json()["results"]]


check("catalogue stats", client.get("/api/food/catalog").json() == {"usda": 10, "off": 4, "user": 0})
check("search finds without diacritics: jogurt", "Jogurt bílý" in names("jogurt"))
check("search folds diacritics: rizek → řízek", any("řízek" in n for n in names("rizek")))
check("prefix per word: kuř pr → kuřecí prsa", names("kuř pr")[0] == "Kuřecí prsa syrová")
check("basic foods rank above OFF", names("jogurt")[0] == "Jogurt bílý plnotučný")
check("English USDA description is searchable", names("chicken breast")[0] == "Kuřecí prsa syrová")
check("barcode query finds the product", names("8590000000097") == ["Tatranka lísková"])
check("limit is honoured", len(names("j", limit=2)) <= 2)
check("empty query is empty before anything is used", names("") == [])
check("junk query does not crash", client.get("/api/food/search", params={"q": '"*(:'}).status_code == 200)
check("food lookup by ref", client.get("/api/food/foods/usda:1002").json()["serving_g"] == 50)
check("unknown ref is 404", client.get("/api/food/foods/off:0000").status_code == 404)
check("training state still loads", client.get("/api/state").status_code == 200)

# ─── log, day view, snapshots ───────────────────────────────────────────────
D1, D2 = "2026-09-20", "2026-09-21"
r = client.post("/api/food/log", json={"date": D1, "meal": "breakfast", "food_ref": "usda:1002", "grams": 100})
egg = r.json()["entry"]
check("logging a food stores a snapshot", egg["kcal"] == 143 and egg["protein"] == 12.6 and egg["name"] == "Vejce slepičí syrové")
client.post("/api/food/log", json={"date": D1, "meal": "lunch", "food_ref": "usda:1003", "grams": 250})
quick = client.post("/api/food/log", json={"date": D1, "meal": "dinner", "kcal": 800, "name": "Restaurace"}).json()["entry"]
check("quick add needs no food", quick["food_ref"] is None and quick["entry_method"] == "quick" and quick["kcal"] == 800)
check("bad meal is rejected", client.post("/api/food/log", json={"date": D1, "meal": "brunch", "kcal": 10}).status_code == 400)
check("bad date is rejected", client.post("/api/food/log", json={"date": "yesterday", "meal": "lunch", "kcal": 10}).status_code == 400)
check("unknown food is rejected", client.post("/api/food/log", json={"date": D1, "meal": "lunch", "food_ref": "usda:9", "grams": 5}).status_code == 400)
check("zero grams is rejected", client.post("/api/food/log", json={"date": D1, "meal": "lunch", "food_ref": "usda:1002", "grams": 0}).status_code == 400)
check("quick add without kcal is rejected", client.post("/api/food/log", json={"date": D1, "meal": "lunch"}).status_code == 400)

day = client.get("/api/food/day", params={"date": D1}).json()
check("day lists its entries", len(day["entries"]) == 3)
check("day totals add up", day["totals"]["kcal"] == round(143 + 325 + 800, 1))
check("day splits by meal", day["meals"]["lunch"]["kcal"] == 325 and day["meals"]["snack"]["kcal"] == 0)
check("no goal yet", day["goal"] is None)

edited = client.patch(f"/api/food/log/{egg['id']}", json={"grams": 50, "meal": "snack"}).json()["entry"]
check("editing grams rescales the snapshot", edited["kcal"] == 71.5 and edited["grams"] == 50 and edited["meal"] == "snack")
check("editing a quick add changes kcal",
      client.patch(f"/api/food/log/{quick['id']}", json={"kcal": 750}).json()["entry"]["kcal"] == 750)
check("editing a missing entry is 404", client.patch("/api/food/log/99999", json={"grams": 5}).status_code == 404)

cat = import_basic.open_catalog(os.environ["GYMTRACK_FOODS_DB"])
cat.execute("UPDATE foods SET kcal_100g=999, name='Changed' WHERE source_id='1003'")
cat.commit()
cat.close()
check("catalogue changes never rewrite history",
      [e for e in client.get("/api/food/day", params={"date": D1}).json()["entries"] if e["meal"] == "lunch"][0]["kcal"] == 325)

copied = client.post("/api/food/log/copy", json={"from_date": D1, "to_date": D2, "meal": "lunch"}).json()
check("copy a meal to another day", copied["copied"] == 1
      and client.get("/api/food/day", params={"date": D2}).json()["meals"]["lunch"]["kcal"] == 325)
client.post("/api/food/log/copy", json={"from_date": D1, "to_date": D2, "meal": "dinner", "to_meal": "lunch"})
check("copy into a different meal", client.get("/api/food/day", params={"date": D2}).json()["meals"]["lunch"]["kcal"] == 1075)
check("copy rejects an unknown meal", client.post("/api/food/log/copy", json={"from_date": D1, "to_date": D2, "meal": "x"}).status_code == 400)

batch = client.post("/api/food/log/batch", json={"items": [
    {"date": D2, "meal": "dinner", "food_ref": "usda:1001", "grams": 150, "entry_method": "recipe"},
    {"date": D2, "meal": "dinner", "food_ref": "usda:1005", "grams": 10, "entry_method": "recipe"}]}).json()
check("multi-add logs every item", len(batch["entries"]) == 2 and batch["entries"][0]["entry_method"] == "recipe")
check("multi-add is all or nothing", client.post("/api/food/log/batch", json={"items": [
    {"date": D2, "meal": "dinner", "food_ref": "usda:1001", "grams": 150},
    {"date": D2, "meal": "dinner", "food_ref": "usda:nope", "grams": 10}]}).status_code == 400
      and len(client.get("/api/food/day", params={"date": D2}).json()["entries"]) == 4)

victim = client.get("/api/food/day", params={"date": D2}).json()["entries"][0]["id"]
client.delete(f"/api/food/log/{victim}")
check("delete an entry", len(client.get("/api/food/day", params={"date": D2}).json()["entries"]) == 3)

# ─── goals ──────────────────────────────────────────────────────────────────
client.post("/api/food/goals", json={"effective_from": "2026-09-01", "kcal": 2200, "protein": 150, "carbs": 220, "fat": 70})
client.post("/api/food/goals", json={"effective_from": D2, "kcal": 2000, "protein": 160, "carbs": 180, "fat": 65})
check("goal applies from its date", client.get("/api/food/day", params={"date": D1}).json()["goal"]["kcal"] == 2200)
check("a later goal takes over", client.get("/api/food/day", params={"date": D2}).json()["goal"]["kcal"] == 2000)
check("days before any goal have none", client.get("/api/food/day", params={"date": "2026-08-01"}).json()["goal"] is None)
check("same date replaces the goal",
      len(client.post("/api/food/goals", json={"effective_from": D2, "kcal": 2100}).json()["goals"]) == 2)
check("absurd goal is rejected", client.post("/api/food/goals", json={"effective_from": D2, "kcal": 50}).status_code == 400)
summ = client.get("/api/food/summary", params={"start": "2026-09-01", "end": "2026-09-30"}).json()["days"]
check("summary has one row per logged day", [d["date"] for d in summ] == [D1, D2])
check("summary compares with that day's goal", summ[0]["goal"]["kcal"] == 2200 and summ[1]["goal"]["kcal"] == 2100)

# ─── favourites, recent, custom foods ───────────────────────────────────────
check("recently used foods come back on an empty query", "Rýže bílá dlouhozrnná (jasmínová, basmati) vařená" in names("")
      or "Changed" in names(""))
client.put("/api/food/favorites/off:8590000000097")
check("favourite shows on an empty query", names("")[0] == "Tatranka lísková")
check("favourite ranks first in search", names("t")[0] == "Tatranka lísková")
check("favourite flag is returned", client.get("/api/food/foods/off:8590000000097").json()["favorite"] is True)
client.delete("/api/food/favorites/off:8590000000097")
check("unfavourite", client.get("/api/food/foods/off:8590000000097").json()["favorite"] is False)
check("favouriting an unknown food is 404", client.put("/api/food/favorites/usda:0").status_code == 404)

mine = client.post("/api/food/foods", json={"name": "Babiččin guláš", "kcal_100g": 180, "protein_100g": 12,
                                            "carbs_100g": 8, "fat_100g": 11, "serving_g": 350, "serving_label": "1 talíř"}).json()
check("custom food is created", mine["ref"].startswith("user:") and mine["source"] == "custom")
check("custom food is searchable", names("gulas")[0] == "Babiččin guláš")
check("impossible custom food is rejected", client.post("/api/food/foods", json={"name": "X", "kcal_100g": 5000}).status_code == 400)
check("catalogue foods cannot be edited", client.put("/api/food/foods/usda:1002", json={"name": "X", "kcal_100g": 1}).status_code == 400)
client.post("/api/food/log", json={"date": D2, "meal": "lunch", "food_ref": mine["ref"], "grams": 350})
client.put(f"/api/food/foods/{mine['ref']}", json={"name": "Guláš", "kcal_100g": 100})
check("editing a custom food keeps logged snapshots",
      any(e["name"] == "Babiččin guláš" and e["kcal"] == 630 for e in client.get("/api/food/day", params={"date": D2}).json()["entries"]))
client.delete(f"/api/food/foods/{mine['ref']}")
check("deleting a custom food keeps the log", len(client.get("/api/food/day", params={"date": D2}).json()["entries"]) == 4)

# ─── body weight ────────────────────────────────────────────────────────────
for i, kg in enumerate([80, 80.4, 79.8, 80.2, 79.6, 79.9, 79.4, 79.2]):
    client.post("/api/food/weight", json={"date": f"2026-09-{i + 1:02d}", "kg": kg})
w = client.get("/api/food/weight", params={"start": "2026-09-01", "end": "2026-09-30"}).json()["weights"]
check("weights are listed", len(w) == 8)
check("7-day moving average", w[6]["avg7"] == round(sum([80, 80.4, 79.8, 80.2, 79.6, 79.9, 79.4]) / 7, 2)
      and w[7]["avg7"] == round(sum([80.4, 79.8, 80.2, 79.6, 79.9, 79.4, 79.2]) / 7, 2))
check("absurd weight is rejected", client.post("/api/food/weight", json={"date": D1, "kg": 5}).status_code == 400)
client.delete("/api/food/weight/2026-09-08")
check("delete a weight", len(client.get("/api/food/weight", params={"start": "2026-09-01", "end": "2026-09-30"}).json()["weights"]) == 7)

backup = client.get("/api/export").json()
check("backup includes the food log", len(backup["food"]["log"]) == 7 and len(backup["food"]["goals"]) == 2)
check("backup keeps the training sections", {"profile", "routines", "days", "workouts"} <= backup.keys())

print(f"\n{checks} checks passed")
