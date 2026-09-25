"""HTTP API of the Food module. Mounted by main.py under /api/food."""

from fastapi import APIRouter, HTTPException

from . import catalog
from .catalog import connect, food_dict, get_food
from .search import search as run_search

router = APIRouter(prefix="/api/food")


def init() -> None:
    catalog.init()


@router.get("/search")
def search(q: str = "", limit: int = 20):
    with connect() as conn:
        return {"q": q, "results": run_search(conn, q, limit)}


@router.get("/foods/{ref}")
def get_one(ref: str):
    with connect() as conn:
        row = get_food(conn, ref)
        if row is None:
            raise HTTPException(404, "food not found")
        fav = conn.execute("SELECT 1 FROM food_favorites WHERE food_ref=?", (ref,)).fetchone()
        return food_dict(row, favorite=fav is not None)


@router.get("/catalog")
def catalog_stats():
    """How much of the catalogue is imported — the Food tab says so when it is empty."""
    with connect() as conn:
        counts = {r["source"]: r["n"] for r in conn.execute(
            "SELECT source, COUNT(*) AS n FROM cat.foods GROUP BY source")}
        custom = conn.execute("SELECT COUNT(*) AS n FROM user_foods").fetchone()["n"]
    return {"usda": counts.get("usda", 0), "off": counts.get("off", 0), "user": custom}
