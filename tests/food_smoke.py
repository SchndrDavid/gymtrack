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
OFF_HEADER = ["code", "product_name", "brands", "stores", "countries_tags", "serving_size", "serving_quantity",
              "energy-kcal_100g", "proteins_100g", "carbohydrates_100g", "fat_100g", "fiber_100g"]
OFF_ROWS = [
    ["8590000000011", "Jogurt bílý", "Mlékárna Test", "", "en:czech-republic", "150 g", "150", "68", "4.5", "5.1", "3.2", ""],
    ["8590000000028", "Jogurt jahodový", "Mlékárna Test", "", "en:czech-republic,en:slovakia", "", "", "95", "3.5", "14", "2.9", "0.2"],
    ["8580000000035", "Kuřecí prsní řízek", "Řeznictví", "", "en:slovakia", "", "", "210", "18", "12", "10", ""],
    ["4000000000042", "Joghurt Natur", "Test DE", "", "en:germany", "", "", "65", "4", "5", "3", ""],
    ["8590000000059", "Nesmysl", "", "", "en:czech-republic", "", "", "1500", "1", "1", "1", ""],
    ["8590000000066", "", "", "", "en:czech-republic", "", "", "100", "1", "1", "1", ""],
    ["8590000000073", "Záporný", "", "", "en:czech-republic", "", "", "100", "-1", "1", "1", ""],
    ["8590000000080", "Bez energie", "", "", "en:czech-republic", "", "", "", "1", "1", "1", ""],
    ["8590000000097", "Tatranka lísková", "Opavia", "", "en:czech-republic", "1 ks (50 g)", "50", "520", "6.5", "60", "28", "2"],
    ["4056489000011", "Skyr natur", "Milbona", "", "en:germany", "150 g", "150", "63", "11", "4", "0.2", ""],
    ["4337185000012", "Haferflocken", "Brand X", "Kaufland", "en:germany", "", "", "372", "13", "59", "7", "10"],
    ["8710400000013", "Yoghurt", "AH", "Albert Heijn", "en:netherlands", "", "", "60", "4", "5", "3", ""],
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
check("OFF import keeps valid CZ/SK products and chain brands", kept == 6)
check("a Lidl brand tagged only for Germany is kept", cat.execute("SELECT 1 FROM foods WHERE barcode='4056489000011'").fetchone() is not None)
check("a product sold at Kaufland is kept", cat.execute("SELECT 1 FROM foods WHERE barcode='4337185000012'").fetchone() is not None)
check("Albert Heijn is not Albert", cat.execute("SELECT 1 FROM foods WHERE barcode='8710400000013'").fetchone() is None)
check("OFF import stores the barcode", cat.execute(
    "SELECT name FROM foods WHERE barcode='8590000000097'").fetchone()[0] == "Tatranka lísková")
check("OFF import keeps the serving", cat.execute(
    "SELECT serving_g FROM foods WHERE barcode='8590000000097'").fetchone()[0] == 50)
with import_off.open_stream(path=str(dump)) as stream:
    import_off.import_stream(cat, stream, progress=False)
check("OFF re-import upserts by barcode",
      cat.execute("SELECT COUNT(*) FROM foods WHERE source='off'").fetchone()[0] == 6)
cat.close()

client = TestClient(main.app)

# ─── search ─────────────────────────────────────────────────────────────────
def names(q, **kw):
    params = {"q": q, **kw}
    return [r["name"] for r in client.get("/api/food/search", params=params).json()["results"]]


stats = client.get("/api/food/catalog").json()
check("catalogue stats", (stats["usda"], stats["off"], stats["user"], stats["import"]) == (10, 6, 0, None))
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

# ─── barcodes ───────────────────────────────────────────────────────────────
import food.api  # noqa: E402
import zxingcpp  # noqa: E402
from PIL import Image  # noqa: E402


def ean13(d12):
    return d12 + str((10 - sum(int(c) * (3 if i % 2 else 1) for i, c in enumerate(d12)) % 10) % 10)


def barcode_photo(code):
    img = zxingcpp.write_barcode_to_image(zxingcpp.create_barcode(code, zxingcpp.BarcodeFormat.EAN13), scale=4)
    mv = memoryview(img)
    bars = Image.frombytes("L", (mv.shape[1], mv.shape[0]), bytes(mv)).convert("RGB")
    photo = Image.new("RGB", (bars.width + 300, bars.height + 400), (235, 230, 220))
    photo.paste(bars, (150, 200))
    buf = io.BytesIO()
    photo.save(buf, "JPEG", quality=90)
    return buf.getvalue()


fetched = []
OFF_PRODUCTS = {
    "8594001234565": {"product_name": "Rohlík tukový", "brands": "Pekárna Test", "serving_quantity": 43, "serving_size": "1 ks",
                      "nutriments": {"energy-kcal_100g": 290, "proteins_100g": 9, "carbohydrates_100g": 52, "fat_100g": 5}},
    "8594001111110": {"product_name": "Rozbitý", "nutriments": {"energy-kcal_100g": 4000}},
}


def fake_off(code):
    fetched.append(code)
    return OFF_PRODUCTS.get(code)


food.api.barcode_fetch = fake_off
hit = client.get("/api/food/barcode/8590000000097").json()
check("barcode found in the catalogue", hit["food"]["name"] == "Tatranka lísková" and hit["found"] == "local" and not fetched)
live = client.get("/api/food/barcode/8594001234565").json()
check("unknown barcode falls back to OFF", live["found"] == "off" and live["food"]["name"] == "Rohlík tukový" and fetched == ["8594001234565"])
check("OFF result is saved to the catalogue", client.get("/api/food/barcode/8594001234565").json()["found"] == "local" and len(fetched) == 1)
check("saved OFF product is searchable", names("rohlik tuk")[0] == "Rohlík tukový")
check("nonsense from OFF is not saved", client.get("/api/food/barcode/8594001111110").json()["food"] is None)
check("barcode unknown everywhere is not found", client.get("/api/food/barcode/8594009999998").json() == {"barcode": "8594009999998", "food": None, "found": None})
check("non-digit barcode is rejected", client.get("/api/food/barcode/abc").status_code == 400)
client.post("/api/food/foods", json={"name": "Americký dovoz", "barcode": "0036000291452", "kcal_100g": 100})
check("UPC-A matches its EAN-13 form", client.get("/api/food/barcode/036000291452").json()["food"]["name"] == "Americký dovoz")


def offline(code):
    raise AssertionError("must not be called")


food.api.barcode_fetch = None
import food.barcode as fb  # noqa: E402
fb.OFF_API = "http://127.0.0.1:9/api/v2/product/{code}.json"   # nothing listens there
check("offline OFF lookup is just not found", client.get("/api/food/barcode/8594007777773").json()["food"] is None)
food.api.barcode_fetch = fake_off

mine_code = ean13("859400555555")
client.post("/api/food/foods", json={"name": "Domácí granola", "barcode": mine_code, "kcal_100g": 450, "protein_100g": 10,
                                     "carbs_100g": 55, "fat_100g": 20})
scan = client.post("/api/food/barcode/scan", files={"image": ("scan.jpg", barcode_photo(mine_code), "image/jpeg")}).json()
check("barcode is read from a photo", scan["barcode"] == mine_code)
check("scanned code finds my own food first", scan["food"]["name"] == "Domácí granola")
frame = client.post("/api/food/barcode/decode", files={"image": ("frame.jpg", barcode_photo(mine_code), "image/jpeg")}).json()
check("live frame decode returns just the codes", frame == {"codes": [mine_code]})
check("empty live frame decodes to nothing", client.post("/api/food/barcode/decode", files={"image": ("f.jpg", b"x", "image/jpeg")}).json() == {"codes": []})
unknown = ean13("859400666666")
scan = client.post("/api/food/barcode/scan", files={"image": ("scan.jpg", barcode_photo(unknown), "image/jpeg")}).json()
check("scan of an unknown product returns the code for a new food", scan["barcode"] == unknown and scan["food"] is None)
blank = io.BytesIO()
Image.new("RGB", (400, 300), "white").save(blank, "JPEG")
check("photo without a barcode", client.post("/api/food/barcode/scan", files={"image": ("x.jpg", blank.getvalue(), "image/jpeg")}).json()["barcode"] is None)
check("non-image upload does not crash", client.post("/api/food/barcode/scan", files={"image": ("x.heic", b"not an image", "image/heic")}).status_code == 200)

# ─── recipes and MordorCook ─────────────────────────────────────────────────
import http.server  # noqa: E402
import threading  # noqa: E402

GARLIC_CHICKEN = {
    "id": "a1b2c3", "title": "Kuře na česneku", "servings": 4, "updated_at": "2026-09-01T10:00:00Z",
    "ingredients": [
        {"amount": 600, "unit": "g", "item": "kuřecí prsa", "note": "", "group": ""},
        {"amount": 2, "unit": "lžíce", "item": "olivového oleje", "note": "", "group": ""},
        {"amount": 4, "unit": "stroužky", "item": "česneku", "note": "", "group": ""},
        {"amount": 1, "unit": "", "item": "cibule", "note": "", "group": ""},
        {"amount": 2, "unit": "ks", "item": "vejce", "note": "", "group": ""},
        {"amount": 200, "unit": "ml", "item": "mléka", "note": "", "group": ""},
        {"amount": None, "unit": "", "item": "sůl", "note": "podle chuti", "group": ""},
        {"amount": 1, "unit": "špetka", "item": "pepře", "note": "", "group": ""},
    ],
}
MYSTERY = {
    "id": "d4e5f6", "title": "Smetanová omáčka", "servings": 2, "updated_at": "2026-09-02T10:00:00Z",
    "ingredients": [
        {"amount": 1, "unit": "cup", "item": "zakysaná smetana"},
        {"amount": 1, "unit": "tbsp", "item": "olive oil"},
        {"amount": None, "unit": "", "item": "olej na smažení"},
    ],
}
ENGLISH = {"id": "e1", "title": "Eggs and onion", "servings": 1, "updated_at": "x",
           "ingredients": [{"amount": 3, "unit": "", "item": "large eggs"}, {"amount": 1, "unit": "", "item": "onion, chopped"}]}

a = client.post("/api/food/recipes/preview", json=GARLIC_CHICKEN).json()
by_item = {x["item"]: x for x in a["ingredients"]}
check("recipe: grams pass through", by_item["kuřecí prsa"]["grams"] == 600 and by_item["kuřecí prsa"]["food"]["name"] == "Kuřecí prsa syrová")
check("recipe: Czech inflection still matches", by_item["olivového oleje"]["food"]["name"] == "Olivový olej")
check("recipe: spoons of oil use its density", by_item["olivového oleje"]["grams"] == round(2 * 15 * 0.92, 1))
check("recipe: cloves use the USDA clove weight", by_item["česneku"]["grams"] == 12)
check("recipe: a bare count uses the piece weight", by_item["cibule"]["grams"] == 110 and by_item["vejce"]["grams"] == 100)
check("recipe: millilitres of milk", by_item["mléka"]["grams"] == 206)
check("recipe: salt and pepper are negligible", by_item["sůl"]["status"] == "ignored" and by_item["pepře"]["status"] == "ignored")
check("recipe: fully matched", a["ok"] and a["problems"] == 0)
check("recipe: per portion is a quarter", a["food"]["serving_g"] == round(a["total_grams"] / 4, 1)
      and a["food"]["per_serving"]["kcal"] == round(a["totals"]["kcal"] / 4, 1))

e = client.post("/api/food/recipes/preview", json=ENGLISH).json()
check("recipe: English ingredients find basic foods", e["ok"] and e["ingredients"][0]["grams"] == 150 and e["ingredients"][1]["grams"] == 110)

m = client.post("/api/food/recipes/preview", json=MYSTERY).json()
st = {x["item"]: x for x in m["ingredients"]}
check("recipe: unknown ingredient is unmatched", st["zakysaná smetana"]["status"] == "unmatched")
check("recipe: tablespoon of oil is fine", st["olive oil"]["status"] == "matched" and st["olive oil"]["grams"] == 13.8)
check("recipe: no amount means ask", st["olej na smažení"]["status"] == "unmatched" and st["olej na smažení"]["reason"] == "no amount given")
check("recipe with problems is not saved", client.post("/api/food/recipes/import", json=MYSTERY).status_code == 422)

client.post("/api/food/mappings", json={"item": "zakysaná smetana", "food_ref": "usda:1004", "grams": 230, "amount": 1, "unit": "cup"})
client.post("/api/food/mappings", json={"item": "olej na smažení", "food_ref": "usda:1005", "grams": 15})
m = client.post("/api/food/recipes/preview", json=MYSTERY).json()
st = {x["item"]: x for x in m["ingredients"]}
check("mapping: remembered unit weight", st["zakysaná smetana"]["grams"] == 230 and st["zakysaná smetana"]["reason"] == "remembered unit weight")
check("mapping: remembered fixed amount", st["olej na smažení"]["grams"] == 15)
check("mapping: scales with the amount", client.post("/api/food/recipes/preview", json={
    "title": "x", "ingredients": [{"amount": 2, "unit": "cup", "item": "Zakysaná  smetana"}]}).json()["ingredients"][0]["grams"] == 460)
check("mapping: unknown food is rejected", client.post("/api/food/mappings", json={"item": "x", "food_ref": "usda:0"}).status_code == 400)
client.post("/api/food/mappings", json={"item": "mléka", "ignore": True})
check("mapping: ignore on purpose", {x["item"]: x for x in client.post("/api/food/recipes/preview", json=GARLIC_CHICKEN).json()["ingredients"]}["mléka"]["status"] == "ignored")
client.delete("/api/food/mappings/mleka")
check("mapping: listed and deletable", all(x["key"] != "mleka" for x in client.get("/api/food/mappings").json()["mappings"]))

imp = client.post("/api/food/recipes/import", json={"recipe": MYSTERY}).json()
check("recipe import stores a food", imp["ok"] and imp["food"]["source"] == "recipe" and imp["food"]["serving_label"] == "1 portion")
check("recipe food is searchable", names("smetanova omacka")[0] == "Smetanová omáčka")
own = client.post("/api/food/recipes/import", json={"title": "Protein shake", "servings": 1,
      "ingredients": [{"amount": 300, "unit": "g", "item": "x"}],
      "nutrition": {"per": "serving", "kcal": 350, "protein": 40, "carbs": 30, "fat": 6, "serving_g": 350}}).json()
check("recipe with its own nutrition uses it", own["ok"] and own["food"]["kcal_100g"] == 100 and own["analysis"]["nutrition_source"] == "recipe")
check("recipe without a title is rejected", client.post("/api/food/recipes/import", json={"servings": 2}).status_code == 400)

check("sync without MORDORCOOK_URL is refused", client.post("/api/food/recipes/sync").status_code == 400)
check("config hides MordorCook when unset", client.get("/api/config").json()["mordorcook_enabled"] is False)


class FakeMordorCook(http.server.BaseHTTPRequestHandler):
    recipes = [GARLIC_CHICKEN, MYSTERY, {"title": ""}]

    def do_GET(self):  # noqa: N802
        body = json.dumps(self.recipes).encode() if self.path == "/api/recipes" else b"{}"
        self.send_response(200 if self.path == "/api/recipes" else 404)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


srv = http.server.HTTPServer(("127.0.0.1", 0), FakeMordorCook)
threading.Thread(target=srv.serve_forever, daemon=True).start()
os.environ["MORDORCOOK_URL"] = f"http://127.0.0.1:{srv.server_port}/"
check("config shows MordorCook when set", client.get("/api/config").json()["mordorcook_enabled"] is True)
sync = client.post("/api/food/recipes/sync").json()
check("sync reads every recipe", sync["total"] == 3 and len(sync["errors"]) == 1)
check("sync saves matched recipes", sync["saved"] == ["Kuře na česneku"])
check("sync recognises a recipe imported before", sync["unchanged"] == 1)
again = client.post("/api/food/recipes/sync").json()
check("sync skips unchanged recipes", again["unchanged"] == 2 and again["saved"] == [])
FakeMordorCook.recipes = [dict(GARLIC_CHICKEN, updated_at="2026-09-03T00:00:00Z",
                               ingredients=GARLIC_CHICKEN["ingredients"] + [{"amount": 100, "unit": "g", "item": "tajná přísada"}])]
changed = client.post("/api/food/recipes/sync").json()
check("changed recipe with a new unknown ingredient waits", changed["saved"] == [] and len(changed["pending"]) == 1
      and changed["pending"][0]["problems"] == 1)
listed = client.get("/api/food/recipes").json()
check("recipes are listed", {r["name"] for r in listed["recipes"]} >= {"Kuře na česneku", "Smetanová omáčka"})
garlic = [r for r in listed["recipes"] if r["name"] == "Kuře na česneku"][0]
check("stored recipe keeps its previous values until resolved", garlic["details"]["updated_at"] == "2026-09-01T10:00:00Z")
items = client.get(f"/api/food/recipes/{garlic['ref']}/items", params={"portions": 2}).json()["items"]
check("recipe breaks down into ingredients per portion", len(items) == 6 and items[0]["grams"] == 300)
check("breakdown carries the food", items[0]["food"]["name"] == "Kuřecí prsa syrová")
FakeMordorCook.recipes = [MYSTERY]
gone = client.post("/api/food/recipes/sync").json()
check("a recipe deleted in MordorCook disappears", gone["removed"] == ["Kuře na česneku"]
      and "Kuře na česneku" not in {r["name"] for r in client.get("/api/food/recipes").json()["recipes"]})
check("recipes imported by hand are not removed by sync", "Protein shake" in {r["name"] for r in client.get("/api/food/recipes").json()["recipes"]})
FakeMordorCook.recipes = [GARLIC_CHICKEN, MYSTERY]
client.post("/api/food/recipes/sync")
garlic = [r for r in client.get("/api/food/recipes").json()["recipes"] if r["name"] == "Kuře na česneku"][0]
srv.shutdown()
os.environ["MORDORCOOK_URL"] = "http://127.0.0.1:9"
check("unreachable MordorCook is a clear error", client.post("/api/food/recipes/sync").status_code == 502)
os.environ.pop("MORDORCOOK_URL")
logged = client.post("/api/food/log", json={"date": D2, "meal": "dinner", "food_ref": garlic["ref"],
                                            "grams": garlic["serving_g"], "entry_method": "recipe"}).json()["entry"]
check("a portion of a recipe is logged", logged["kcal"] == round(garlic["kcal_100g"] * garlic["serving_g"] / 100, 1))
client.delete(f"/api/food/log/{logged['id']}")

# ─── photo recognition: infrastructure only ─────────────────────────────────
import food.recognize  # noqa: E402
import recognition  # noqa: E402

jpeg = barcode_photo(ean13("859400111111"))
off = client.post("/api/food/recognize", files={"image": ("meal.jpg", jpeg, "image/jpeg")})
check("recognize is 501 while the flag is off", off.status_code == 501 and off.json() == {"error": "food_ai_disabled"})
check("config reports food AI off", client.get("/api/config").json()["food_ai_enabled"] is False)
os.environ["FOOD_AI_ENABLED"] = "true"
food.recognize.set_recognizer(recognition.get_recognizer())
check("the flag alone does not switch it on", client.post("/api/food/recognize", files={"image": ("m.jpg", jpeg, "image/jpeg")}).status_code == 501)
check("disabled recognizer satisfies the interface", isinstance(recognition.DisabledRecognizer(), recognition.FoodRecognizer))


class FakeRecognizer:
    available = True
    seen = []

    def recognize(self, image_bytes):
        self.seen.append(len(image_bytes))
        return [recognition.RecognizedItem("kuřecí prsa", 150, 0.9),
                recognition.RecognizedItem("vejce", 50, 0.7),
                recognition.RecognizedItem("dračí maso", 80, 0.3)]


class BrokenRecognizer:
    available = True

    def recognize(self, image_bytes):
        raise RuntimeError("model unavailable")


food.recognize.set_recognizer(FakeRecognizer())
check("config reports food AI on with a recognizer", client.get("/api/config").json()["food_ai_enabled"] is True)
started = client.post("/api/food/recognize", files={"image": ("m.jpg", jpeg, "image/jpeg")})
check("recognize creates a job", started.status_code == 202 and started.json()["status"] == "pending")
job = client.get(f"/api/food/recognize/{started.json()['id']}").json()
check("job finishes with items", job["status"] == "done" and len(job["items"]) == 3 and FakeRecognizer.seen == [len(jpeg)])
check("recognised items are matched to foods", job["items"][0]["matched_food_ref"] == "usda:1001" and job["items"][0]["food"]["name"] == "Kuřecí prsa syrová")
check("unknown items stay unmatched", job["items"][2]["matched_food_ref"] is None and job["items"][2]["food"] is None)
check("recognition logs nothing by itself", len(client.get("/api/food/day", params={"date": D2}).json()["entries"]) == 4)
food.recognize.set_recognizer(BrokenRecognizer())
failed = client.get(f"/api/food/recognize/{client.post('/api/food/recognize', files={'image': ('m.jpg', jpeg, 'image/jpeg')}).json()['id']}").json()
check("a failing recognizer marks the job as error", failed["status"] == "error" and "model unavailable" in failed["error"])
check("unknown job is 404", client.get("/api/food/recognize/nope").status_code == 404)
os.environ["FOOD_AI_ENABLED"] = "false"
food.recognize.set_recognizer(None)
check("switched off again", client.post("/api/food/recognize", files={"image": ("m.jpg", jpeg, "image/jpeg")}).status_code == 501)

backup = client.get("/api/export").json()
check("backup includes the food log", len(backup["food"]["log"]) == 7 and len(backup["food"]["goals"]) == 2)
check("backup keeps the training sections", {"profile", "routines", "days", "workouts"} <= backup.keys())

Path(os.environ["GYMTRACK_FOODS_DB"]).unlink()
check("deleting the catalogue while running is harmless", client.get("/api/food/search", params={"q": "vejce"}).status_code == 200
      and client.get("/api/food/catalog").json()["usda"] == 0)
check("the log survives a deleted catalogue", len(client.get("/api/food/day", params={"date": D1}).json()["entries"]) == 3)

# ─── catalogue import started from the app ──────────────────────────────────
import time  # noqa: E402

food.api.IMPORT_ARGS = ["--fdc-zip", str(release), "--off-file", str(dump)]
Path(os.environ["GYMTRACK_FOODS_DB"]).unlink(missing_ok=True)
check("catalogue import starts", client.post("/api/food/catalog/import").status_code == 200)
check("a second import is refused while one runs", client.post("/api/food/catalog/import").status_code == 409
      or client.get("/api/food/catalog").json()["import"]["state"] == "done")
for _ in range(120):
    st = client.get("/api/food/catalog").json()
    if st["import"] and st["import"]["state"] != "running":
        break
    time.sleep(0.25)
check("catalogue import finishes", st["import"]["state"] == "done" and not st["import"]["errors"])
check("catalogue import fills both sources", st["usda"] > 0 and st["off"] == 6)
check("imported catalogue is searchable", names("tatranka")[0] == "Tatranka lísková")

print(f"\n{checks} checks passed")
