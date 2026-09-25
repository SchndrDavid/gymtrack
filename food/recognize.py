"""Recognition jobs: an upload becomes a job row, the work runs after the response is sent,
and the frontend polls for the result. The photo is only ever held in memory."""

import json
import logging
import sqlite3
import uuid

import recognition

from .catalog import connect, food_dict, get_food, now_iso
from .recipes import find_food, get_mapping, ingredient_key

log = logging.getLogger("gymtrack.food")

_recognizer = None


def recognizer():
    global _recognizer
    if _recognizer is None:
        _recognizer = recognition.get_recognizer()
    return _recognizer


def set_recognizer(r) -> None:
    """Tests (and a future implementation's warm-up) swap the recognizer in here."""
    global _recognizer
    _recognizer = r


def enabled() -> bool:
    return recognition.flag_enabled() and bool(getattr(recognizer(), "available", False))


def create_job(conn: sqlite3.Connection) -> str:
    job_id = uuid.uuid4().hex
    conn.execute("INSERT INTO recognition_jobs (id, status, created_at) VALUES (?, 'pending', ?)",
                 (job_id, now_iso()))
    return job_id


def match(conn: sqlite3.Connection, item: recognition.RecognizedItem) -> dict:
    """Same matching as recipe ingredients: a remembered mapping first, then the catalogue."""
    out = item.to_dict()
    out["match_status"] = "unmatched"
    mapping = get_mapping(conn, ingredient_key(item.name))
    row = get_food(conn, mapping["food_ref"]) if mapping and mapping.get("food_ref") else None
    status = "matched" if row is not None else "unmatched"
    if row is None:
        row, status = find_food(conn, item.name)
    if row is not None:
        out["matched_food_ref"] = food_dict(row)["ref"]
        out["match_status"] = status
    return out


def run_job(job_id: str, image: bytes) -> None:
    """Runs after the response. Whatever happens, the image goes away with this frame."""
    try:
        with connect() as conn:
            conn.execute("UPDATE recognition_jobs SET status='running' WHERE id=?", (job_id,))
        items = recognizer().recognize(image)
        del image
        with connect() as conn:
            result = [match(conn, i) for i in items]
            conn.execute("UPDATE recognition_jobs SET status='done', result_json=? WHERE id=?",
                         (json.dumps(result, ensure_ascii=False), job_id))
    except Exception as e:   # noqa: BLE001 — the job records the failure for the frontend
        log.warning("recognition job %s failed: %s", job_id, e)
        with connect() as conn:
            conn.execute("UPDATE recognition_jobs SET status='error', error=? WHERE id=?",
                         (str(e)[:500], job_id))


def get_job(conn: sqlite3.Connection, job_id: str) -> dict | None:
    r = conn.execute("SELECT * FROM recognition_jobs WHERE id=?", (job_id,)).fetchone()
    if r is None:
        return None
    out = {"id": r["id"], "status": r["status"], "error": r["error"], "created_at": r["created_at"], "items": []}
    for item in json.loads(r["result_json"] or "[]"):
        row = get_food(conn, item.get("matched_food_ref") or "")
        out["items"].append({**item, "food": food_dict(row) if row is not None else None})
    return out


def cleanup(conn: sqlite3.Connection) -> None:
    """A restart kills running jobs; say so instead of leaving them pending forever."""
    conn.execute("UPDATE recognition_jobs SET status='error', error='interrupted by a restart' "
                 "WHERE status IN ('pending', 'running')")
    conn.execute("DELETE FROM recognition_jobs WHERE created_at < datetime('now', '-30 days')")
