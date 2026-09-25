"""Barcodes: decode a photo, look the code up, fall back to the live Open Food Facts API.

Over plain http:// Safari will not open a live camera stream, so the phone takes an ordinary
photo instead and the server reads the barcode out of it with zxing-cpp. The photo is decoded
in memory and never written anywhere.
"""

import io
import json
import logging
import os
import sqlite3
import urllib.error
import urllib.request

from .catalog import clean_food, get_food, now_iso, upsert_foods

log = logging.getLogger("gymtrack.food")

OFF_API = "https://world.openfoodfacts.org/api/v2/product/{code}.json"
USER_AGENT = "GymTrack/1.0 (self-hosted)"
TIMEOUT = 5
MAX_IMAGE_BYTES = 12 * 1024 * 1024


def live_enabled() -> bool:
    return os.environ.get("FOOD_OFF_LIVE", "true").strip().lower() not in ("0", "false", "no", "off")


def variants(code: str) -> list[str]:
    """UPC-A and EAN-13 are the same number with or without a leading zero."""
    code = code.strip()
    out = [code]
    if len(code) == 12:
        out.append("0" + code)
    if len(code) == 13 and code.startswith("0"):
        out.append(code[1:])
    return out


def decode(image_bytes: bytes) -> list[str]:
    """Every EAN/UPC found in the photo, best first. Empty when there is none or no image."""
    import zxingcpp                       # noqa: PLC0415 — keep startup light
    from PIL import Image, ImageOps       # noqa: PLC0415

    try:
        img = Image.open(io.BytesIO(image_bytes))
        img = ImageOps.exif_transpose(img).convert("L")
    except Exception:                     # not an image, or one Pillow cannot read (HEIC)
        return []
    if max(img.size) > 2400:              # the frontend already shrinks; this guards direct uploads
        img.thumbnail((2400, 2400))
    found = []
    for r in zxingcpp.read_barcodes(img):
        text = (r.text or "").strip()
        if text.isdigit() and 8 <= len(text) <= 14 and text not in found:
            found.append(text)
    return found


def local(conn: sqlite3.Connection, code: str):
    """The user's own food with this barcode wins over the catalogue."""
    for c in variants(code):
        row = conn.execute("SELECT * FROM user_foods WHERE barcode=? ORDER BY id DESC LIMIT 1", (c,)).fetchone()
        if row:
            return row
    for c in variants(code):
        row = conn.execute("SELECT * FROM cat.foods WHERE barcode=? LIMIT 1", (c,)).fetchone()
        if row:
            return row
    return None


def fetch_off(code: str) -> dict | None:
    """One product from the live OFF API, or None — offline, timed out, unknown or unusable."""
    req = urllib.request.Request(OFF_API.format(code=code), headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            data = json.load(resp)
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as e:
        log.info("OFF lookup for %s failed: %s", code, e)
        return None
    if data.get("status") != 1 or not isinstance(data.get("product"), dict):
        return None
    return data["product"]


def product_to_food(code: str, p: dict) -> dict | None:
    n = p.get("nutriments") or {}
    kcal = n.get("energy-kcal_100g")
    if kcal in (None, "") and n.get("energy_100g") not in (None, ""):
        try:
            kcal = float(n["energy_100g"]) / 4.184      # kJ
        except (TypeError, ValueError):
            kcal = None
    serving = p.get("serving_quantity")
    food = clean_food({
        "name": p.get("product_name_cs") or p.get("product_name") or p.get("generic_name"),
        "brand": (p.get("brands") or "").split(",")[0],
        "kcal_100g": kcal,
        "protein_100g": n.get("proteins_100g"),
        "carbs_100g": n.get("carbohydrates_100g"),
        "fat_100g": n.get("fat_100g"),
        "fiber_100g": n.get("fiber_100g"),
        "serving_g": serving,
        "serving_label": p.get("serving_size") or (f"{serving} g" if serving else ""),
    })
    if food is None:
        return None
    return {**food, "source": "off", "source_id": code, "barcode": code, "aliases": "", "updated_at": now_iso()}


def lookup(conn: sqlite3.Connection, code: str, fetch=None) -> tuple[object, str | None]:
    """(row, where) — where is 'local', 'off' (fetched now and saved to the catalogue) or None."""
    row = local(conn, code)
    if row is not None:
        return row, "local"
    if not live_enabled():
        return None, None
    product = (fetch or fetch_off)(code)
    food = product_to_food(code, product) if product else None
    if food is None:
        return None, None
    upsert_foods(conn, [food], schema="cat")
    return get_food(conn, f"off:{code}"), "off"
