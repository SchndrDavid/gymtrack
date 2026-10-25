"""The curated meat tree: species → cut → raw and cooked food, with values per 100 g.

seed/meat_presets.json names every cut's raw and cooked food by its Czech name from the basic
foods, so each value traces back to a USDA fdc_id. Cuts whose foods are not in the catalogue
are left out of the tree and reported under `missing`.
"""

import json
from functools import lru_cache
from pathlib import Path

from .catalog import fold, food_dict

PRESETS = Path(__file__).resolve().parent.parent / "seed" / "meat_presets.json"


@lru_cache(maxsize=1)
def presets() -> dict:
    return json.loads(PRESETS.read_text(encoding="utf-8"))


def _food(conn, name: str):
    return conn.execute("SELECT * FROM cat.foods WHERE source='usda' AND name=? ORDER BY id LIMIT 1",
                        (name,)).fetchone()


def _macros(row) -> dict:
    d = food_dict(row)
    return {k: d[k] for k in ("ref", "name", "kcal_100g", "protein_100g", "carbs_100g", "fat_100g")}


def tree(conn) -> dict:
    p, missing, species = presets(), [], []
    for s in p["species"]:
        cuts = []
        for c in s["cuts"]:
            raw, cooked = _food(conn, c["raw"]), _food(conn, c["cooked"])
            if raw is None:
                missing.append(c["raw"])
                continue
            if cooked is None:
                missing.append(c["cooked"])
            cuts.append({"id": c["id"], "label_cs": c["label_cs"], "label_en": c["label_en"],
                         "aliases": c["aliases"], "raw": _macros(raw),
                         "cooked": _macros(cooked) if cooked is not None else None})
        if cuts:
            species.append({"id": s["id"], "label_cs": s["label_cs"], "label_en": s["label_en"], "cuts": cuts})
    return {"portions": p["portions"], "species": species, "missing": missing, "skipped": p.get("skipped", [])}


def alias_refs(conn, q: str) -> list[str]:
    """Refs of the cuts a query names, raw before cooked: 'rizek', 'mlete', 'kureci stehno'.

    Every word must be the start of a cut or species alias, and at least one must name the cut —
    a lone 'kureci' is an ordinary search, not a request for the whole chicken menu."""
    words = [w for w in fold(q).replace("-", " ").split() if w]
    if not words:
        return []
    refs: list[str] = []
    for s in presets()["species"]:
        species_words = [fold(a) for a in s.get("aliases", [])] + [fold(s["label_cs"])]
        for c in s["cuts"]:
            cut_words = [fold(a) for a in c["aliases"]] + [fold(c["label_cs"])]
            named = [any(cw.startswith(w) for cw in cut_words) for w in words]
            ok = all(n or any(sw.startswith(w) for sw in species_words) for n, w in zip(named, words))
            if not (ok and any(named)):
                continue
            for name in (c["raw"], c["cooked"]):
                row = _food(conn, name)
                if row is not None:
                    ref = food_dict(row)["ref"]
                    if ref not in refs:
                        refs.append(ref)
    return refs
