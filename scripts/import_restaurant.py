"""Import a restaurant's menu from a spreadsheet into GymTrack's Food → Restaurants.

    python scripts/import_restaurant.py menu.xlsx --name "Čínské Zátiší" --url http://mordor:8101
    python scripts/import_restaurant.py menu.xlsx --name "Čínské Zátiší" --dry-run

Reads the first sheet: one header row, then one item per row. Columns are found by their header,
in Czech or English — Kód/Code, Název/Name, Kategorie/Category, Porce/Portion, Cena/Price,
Kalorie/kcal, Bílkoviny/Protein, Sacharidy/Carbs, Tuky/Fat, Alkohol/Alcohol — so their order does
not matter. Values are per portion. Re-running it replaces the menu: changed items are updated and
items no longer in the sheet are removed; days already logged keep their values.

Needs openpyxl (pip install openpyxl), only here — the app itself does not read spreadsheets.
"""

import argparse
import json
import sys
import unicodedata
import urllib.error
import urllib.request

COLUMNS = {
    "code": ("kod", "code"),
    "name": ("nazev", "name", "jidlo", "polozka"),
    "category": ("kategorie", "category"),
    "portion": ("porce", "portion", "serving"),
    "price": ("cena", "price"),
    "kcal": ("kalorie", "kcal", "energie", "calories"),
    "protein": ("bilkoviny", "protein"),
    "carbs": ("sacharidy", "carbs", "carbohydrates"),
    "fat": ("tuky", "fat"),
    "alcohol": ("alkohol", "alcohol"),
}


def plain(text) -> str:
    s = unicodedata.normalize("NFKD", str(text or "")).encode("ascii", "ignore").decode().lower()
    return s.split("(")[0].strip()


def read_menu(path: str) -> list[dict]:
    import openpyxl
    ws = openpyxl.load_workbook(path, data_only=True, read_only=True).worksheets[0]
    rows = ws.iter_rows(values_only=True)
    header = [plain(h) for h in next(rows)]
    where = {}
    for key, names in COLUMNS.items():
        hit = next((i for i, h in enumerate(header) if h in names), None)
        if hit is not None:
            where[key] = hit
    missing = {"name", "kcal"} - where.keys()
    if missing:
        sys.exit(f"no column for {', '.join(sorted(missing))} in the header: {header}")
    items = []
    for row in rows:
        item = {k: row[i] for k, i in where.items() if i < len(row)}
        if not str(item.get("name") or "").strip():
            continue
        if item.get("code") is not None:
            item["code"] = str(item["code"]).strip()
        items.append(item)
    return items


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("file")
    ap.add_argument("--name", required=True, help="the restaurant's name, as the app shows it")
    ap.add_argument("--url", default="http://localhost:8000", help="GymTrack's address")
    ap.add_argument("--dry-run", action="store_true", help="print what would be sent")
    args = ap.parse_args()

    items = read_menu(args.file)
    body = {"restaurant": args.name, "items": items}
    if args.dry_run:
        print(json.dumps(body, ensure_ascii=False, indent=1, default=str))
        return
    req = urllib.request.Request(args.url.rstrip("/") + "/api/food/restaurants/import",
                                 json.dumps(body, ensure_ascii=False, default=str).encode(),
                                 {"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            res = json.load(resp)
    except urllib.error.HTTPError as e:
        sys.exit(f"GymTrack refused the menu ({e.code}): {e.read().decode()}")
    except urllib.error.URLError as e:
        sys.exit(f"GymTrack is not reachable at {args.url}: {e.reason}")
    print(f"{res['restaurant']}: {res['items']} items ({res['added']} new, {res['removed']} removed)")


if __name__ == "__main__":
    main()
