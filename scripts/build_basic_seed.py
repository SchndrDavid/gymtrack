"""Build seed/basic_foods.json from the official USDA FoodData Central CSV release.

    python scripts/build_basic_seed.py                         # download SR Legacy + Foundation Foods
    python scripts/build_basic_seed.py --zip sr.zip --zip foundation.zip   # or use local copies
    python scripts/build_basic_seed.py --out /data/basic_foods.json

SR Legacy has most foods; raw skinless chicken breast and thigh exist only in Foundation Foods.

seed/basic_foods.src.json lists the basic foods by Czech name, each with one or more USDA
descriptions. The first description found in the release wins. Nothing is typed in by hand:
the nutrient values and the fdc_id come straight from the release, so any value can be checked
at https://fdc.nal.usda.gov/food-details/<fdc_id>/nutrients. An entry that USDA does not have is
skipped and reported, never approximated.

USDA FoodData Central is in the public domain (CC0).
"""

import argparse
import csv
import io
import json
import sys
import tempfile
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "seed" / "basic_foods.src.json"
OUT = ROOT / "seed" / "basic_foods.json"
SR_LEGACY_URL = "https://fdc.nal.usda.gov/fdc-datasets/FoodData_Central_sr_legacy_food_csv_2018-04.zip"
FOUNDATION_URL = "https://fdc.nal.usda.gov/fdc-datasets/FoodData_Central_foundation_food_csv_2025-12-18.zip"
# The Foundation release also lists thousands of sub-samples and branded look-alikes with the same
# descriptions; only the main entries carry the averaged values.
DATA_TYPES = {"sr_legacy_food", "foundation_food"}
USER_AGENT = "GymTrack/1.0 (self-hosted)"

# FDC nutrient ids, in order of preference. Foundation Foods report energy only as
# Atwater factors (2047/2048) and sometimes carbohydrate by summation (1050).
NUTRIENT_IDS = {
    "kcal_100g": ("1008", "2047", "2048"),
    "protein_100g": ("1003",),
    "fat_100g": ("1004", "1085"),
    "carbs_100g": ("1005", "1050"),
    "fiber_100g": ("1079",),
}
WANTED = {i for ids in NUTRIENT_IDS.values() for i in ids}


def norm(text: str) -> str:
    return " ".join(str(text).lower().replace("″", '"').replace("''", '"').split())


def members(zf: zipfile.ZipFile) -> dict[str, str]:
    """Basename -> member path. The releases nest their CSVs in a dated folder."""
    return {Path(n).name: n for n in zf.namelist() if n.endswith(".csv")}


def read_csv(zf: zipfile.ZipFile, name: str):
    files = members(zf)
    if name not in files:
        return iter(())
    return csv.DictReader(io.TextIOWrapper(zf.open(files[name]), encoding="utf-8", newline=""))


def load_release(paths: list[str], wanted_descriptions: set[str]) -> dict[str, dict]:
    """norm(description) -> {fdc_id, description, data_type, nutrients, portions}."""
    foods: dict[str, dict] = {}
    by_id: dict[str, dict] = {}
    for path in paths:
        with zipfile.ZipFile(path) as zf:
            for r in read_csv(zf, "food.csv"):
                key = norm(r["description"])
                if r.get("data_type", "sr_legacy_food") not in DATA_TYPES:
                    continue
                if key in wanted_descriptions and key not in foods:
                    entry = {"fdc_id": r["fdc_id"], "description": r["description"],
                             "data_type": r.get("data_type", ""), "nutrients": {}, "portions": []}
                    foods[key] = entry
                    by_id[r["fdc_id"]] = entry
            for r in read_csv(zf, "food_nutrient.csv"):
                entry = by_id.get(r["fdc_id"])
                if entry is not None and r["nutrient_id"] in WANTED and r["amount"] != "":
                    entry["nutrients"].setdefault(r["nutrient_id"], float(r["amount"]))
            for r in read_csv(zf, "food_portion.csv"):
                entry = by_id.get(r["fdc_id"])
                if entry is None or not r.get("gram_weight"):
                    continue
                amount = float(r.get("amount") or 1) or 1
                label = " ".join(x for x in (r.get("modifier", ""), r.get("portion_description", "")) if x)
                entry["portions"].append((int(r.get("seq_num") or 0), label.lower(),
                                          float(r["gram_weight"]) / amount))
    return foods


def build(src: dict, release: dict[str, dict]) -> tuple[list[dict], list[str]]:
    out, skipped = [], []
    for item in src["foods"]:
        hit = next((release[norm(d)] for d in item["usda"] if norm(d) in release), None)
        if hit is None:
            skipped.append(f"{item['name']}: not in USDA ({item['usda'][0]})")
            continue
        values = {}
        for key, ids in NUTRIENT_IDS.items():
            values[key] = next((hit["nutrients"][i] for i in ids if i in hit["nutrients"]), None)
        if values["kcal_100g"] is None:
            skipped.append(f"{item['name']}: USDA {hit['fdc_id']} has no energy value")
            continue
        food = {
            "name": item["name"],
            "fdc_id": int(hit["fdc_id"]),
            "usda_description": hit["description"],
            "data_type": hit["data_type"],
            **{k: (None if v is None else round(v, 2)) for k, v in values.items()},
            "serving_g": None,
            "serving_label": None,
        }
        portion = item.get("portion")
        if portion:
            match = next((g for _, label, g in sorted(hit["portions"]) if portion["match"] in label), None)
            if match:
                food["serving_g"] = round(match, 1)
                food["serving_label"] = portion["label"]
            else:
                skipped.append(f"{item['name']}: kept, but no '{portion['match']}' portion in USDA")
        out.append(food)
    return out, skipped


def download(url: str) -> str:
    print(f"downloading {url}", flush=True)
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    tmp = tempfile.NamedTemporaryFile(suffix=".zip", delete=False)
    with urllib.request.urlopen(req, timeout=120) as resp, tmp:
        while chunk := resp.read(1 << 20):
            tmp.write(chunk)
    return tmp.name


def main(argv=None) -> Path:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--zip", action="append", default=[], help="local FDC CSV zip (repeatable)")
    ap.add_argument("--url", action="append", default=[],
                    help="release to download when no --zip is given (repeatable; default SR Legacy + Foundation Foods)")
    ap.add_argument("--out", default=str(OUT), help=f"output file (default {OUT})")
    args = ap.parse_args(argv)

    src = json.loads(SRC.read_text(encoding="utf-8"))
    wanted = {norm(d) for item in src["foods"] for d in item["usda"]}
    paths = args.zip or [download(u) for u in (args.url or [SR_LEGACY_URL, FOUNDATION_URL])]
    try:
        release = load_release(paths, wanted)
    finally:
        if not args.zip:
            for tmp in paths:
                Path(tmp).unlink(missing_ok=True)

    foods, skipped = build(src, release)
    payload = {
        "_source": "USDA FoodData Central, public domain (CC0). Built by scripts/build_basic_seed.py "
                   "from seed/basic_foods.src.json; verify any row at "
                   "https://fdc.nal.usda.gov/food-details/<fdc_id>/nutrients",
        "foods": foods,
    }
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    lines = ["{", '  "_source": ' + json.dumps(payload["_source"]) + ",", '  "foods": [']
    lines += ["    " + json.dumps(f, ensure_ascii=False) + ("," if i < len(foods) - 1 else "")
              for i, f in enumerate(foods)]
    lines += ["  ]", "}"]
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    for line in skipped:
        print(f"  skipped  {line}")
    print(f"{len(foods)} of {len(src['foods'])} basic foods written to {out}")
    return out


if __name__ == "__main__":
    sys.exit(0 if main() else 1)
