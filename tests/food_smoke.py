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

print(f"\n{checks} checks passed")
