"""GymTrack — a small self-hosted gym tracker. FastAPI + SQLite, single user.

Exercise types:
  weight — load in kg plus reps
  reps   — reps only, bodyweight
  time   — a countdown in seconds; the set completes itself when it runs out
"""

import json
import os
import re
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel

DB_PATH = os.environ.get("GYMTRACK_DB", "/data/gymtrack.db")
STATIC = Path(__file__).parent / "static"
TYPES = ("weight", "reps", "time")

DEFAULT_ROUTINES = [
    {
        "id": "circuit1",
        "name": "Full-body circuit",
        "exercises": [
            {"name": "Bulgarian split squats", "type": "reps", "sets": 3, "reps": "12-15/leg",
             "block": "Lower body and dynamics", "note": "Bodyweight or light dumbbells."},
            {"name": "Jumping lunges", "type": "reps", "sets": 3, "reps": "20",
             "block": "Lower body and dynamics", "note": "10 per leg, keep the tempo smooth."},
            {"name": "Mountain climbers", "type": "reps", "sets": 3, "reps": "40",
             "block": "Lower body and dynamics", "note": "20 per leg."},
            {"name": "Explosive push-ups", "type": "reps", "sets": 4, "reps": "10-15",
             "block": "Chest, back and arms", "note": "Lower under control, push up hard."},
            {"name": "Chin-ups", "type": "reps", "sets": 4, "reps": "8-12",
             "block": "Chest, back and arms", "note": "Underhand grip. Swap for inverted rows if needed."},
            {"name": "Bench dips", "type": "reps", "sets": 4, "reps": "15-20",
             "block": "Chest, back and arms", "note": "Keep the elbows close to the body."},
            {"name": "Hammer curls", "type": "weight", "sets": 3, "reps": "15-20",
             "block": "Arms and core", "note": "Palms facing in, forearms get work too."},
            {"name": "Diamond push-ups", "type": "reps", "sets": 3, "reps": "max",
             "block": "Arms and core", "note": "To failure or until form breaks down."},
            {"name": "Plank shoulder taps", "type": "reps", "sets": 3, "reps": "20",
             "block": "Arms and core", "note": "Hips must not rotate."},
        ],
    }
]


@contextmanager
def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db() -> None:
    Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    with db() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS days (
                date TEXT PRIMARY KEY,
                run  INTEGER NOT NULL DEFAULT 0,
                gym  INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS workouts (
                id       INTEGER PRIMARY KEY AUTOINCREMENT,
                date     TEXT NOT NULL,
                name     TEXT NOT NULL,
                seconds  INTEGER NOT NULL DEFAULT 0,
                payload  TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS kv (
                key   TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_workouts_date ON workouts(date);
            """
        )
        row = conn.execute("SELECT value FROM kv WHERE key='routines'").fetchone()
        if row is None:
            conn.execute(
                "INSERT INTO kv (key, value) VALUES ('routines', ?)",
                (json.dumps(DEFAULT_ROUTINES, ensure_ascii=False),),
            )


# ─── normalisation ──────────────────────────────────────────────────────────

def _int(value: Any, default: int) -> int:
    try:
        return int(float(str(value).strip()))
    except (TypeError, ValueError):
        return default


def norm_exercise(e: dict) -> dict:
    """Fill in missing fields. Legacy exercises without a type count as weighted."""
    kind = str(e.get("type") or "").lower()
    if kind not in TYPES:
        kind = "time" if e.get("seconds") else "weight"
    return {
        "name": str(e.get("name") or "").strip(),
        "type": kind,
        "sets": max(1, min(20, _int(e.get("sets"), 3))),
        "reps": str(e.get("reps") or ""),
        "seconds": max(5, min(3600, _int(e.get("seconds"), 45))),
        "block": str(e.get("block") or ""),
        "note": str(e.get("note") or "")[:200],
    }


AI_TARGETS = ("gemini", "chatgpt", "claude", "perplexity", "copy")


def norm_profile(p: dict) -> dict:
    """The AI needs a bodyweight to put a number on calories; the rest sharpens it."""
    p = p or {}
    sex = str(p.get("sex") or "").lower()
    ai = str(p.get("ai") or "").lower()
    return {
        "weight": max(0, min(400, _int(p.get("weight"), 0))),
        "height": max(0, min(260, _int(p.get("height"), 0))),
        "age": max(0, min(120, _int(p.get("age"), 0))),
        "sex": sex if sex in ("male", "female") else "",
        "ai": ai if ai in AI_TARGETS else "gemini",
    }


def norm_routine(r: dict, idx: int = 0) -> dict:
    return {
        "id": str(r.get("id") or f"r{idx}"),
        "name": str(r.get("name") or "Untitled").strip() or "Untitled",
        "exercises": [norm_exercise(e) for e in (r.get("exercises") or []) if e.get("name")],
    }


def norm_routines(rs: list) -> list:
    return [norm_routine(r, i) for i, r in enumerate(rs or [])]


# ─── free-text parser (kept as a second import path) ────────────────────────
# Keywords match English and Czech, since source plans arrive in both.

SKIP_BLOCK = re.compile(r"warm[\s-]?up|cool[\s-]?down|stretch|mobility|zahřát|zahrat|rozcvi|protaž|protaz", re.I)
BULLET = re.compile(r"^[\*\-•▪·]+\s*(.+)$")
NUMBERED = re.compile(r"^\d+[\.\)]\s*(.+)$")
BLOCK_SETS = re.compile(r"\((\d+)\s*(?:set|round|sér|ser|kol)", re.I)
COMPACT = re.compile(r"^(.+?)[\s,:]+(\d+)\s*[x]\s*(\d+(?:\s*-\s*\d+)?|max)\s*$", re.I)
SECONDS = re.compile(r"(\d+)\s*(?:s\b|sec|second|sekund|vteřin|vterin)", re.I)
WEIGHTED = re.compile(r"\bkg\b|\blbs?\b|dumbbell|barbell|weight|load|zátěž|zatez|jednoruč|činka", re.I)
PER_SIDE = re.compile(r"(?:per|each)\s+(leg|side|arm)|na (?:každou|kazdou) (?:nohu|stranu|ruku)", re.I)
RANGE = re.compile(r"(\d+\s*-\s*\d+|\d+)")


def _clean(s: str) -> str:
    s = s.replace("–", "-").replace("—", "-").replace("×", "x")
    return re.sub(r"\*\*|__|`", "", s).strip()


def parse_workout(text: str) -> dict:
    """Turn a written plan into exercises. A block heading like '(4 sets)' sets the set count."""
    name, block, block_sets = "", "", 3
    skipping, first_line = False, True
    exercises: list[dict[str, Any]] = []

    for raw in text.splitlines():
        line = _clean(raw)
        if not line:
            continue
        was_first, first_line = first_line, False
        bullet = BULLET.match(line)
        body = _clean(bullet.group(1)) if bullet else line

        heading = NUMBERED.match(line)
        has_sets = BLOCK_SETS.search(line)
        if not bullet and (heading or has_sets):
            label = _clean(heading.group(1) if heading else line)
            label = re.sub(r"\s*\(.*?\)\s*$", "", label).strip(" :.")
            skipping = bool(SKIP_BLOCK.search(label))
            block = "" if skipping else label
            if has_sets:
                block_sets = max(1, min(20, int(has_sets.group(1))))
            continue
        if skipping:
            continue

        compact = COMPACT.match(body)
        if compact:
            exercises.append(norm_exercise({
                "name": _clean(compact.group(1)).strip(" :.-"),
                "type": "weight" if WEIGHTED.search(body) else "reps",
                "sets": int(compact.group(2)),
                "reps": _clean(compact.group(3)),
                "block": block,
            }))
            continue

        if not bullet:
            if was_first and len(body) < 45 and not RANGE.search(body):
                name = body.strip(" :.")
            continue

        ex_name, _, rest = body.partition(":")
        ex_name = _clean(ex_name).strip(" .-")
        rest = _clean(rest)
        if not ex_name:
            continue

        sentence = rest.split(". ")[0] if rest else ""
        outside = re.sub(r"\(.*?\)", "", sentence)   # counts in brackets clarify the total
        secs = SECONDS.search(sentence)
        if secs:
            ex = {"name": ex_name, "type": "time", "sets": block_sets,
                  "seconds": int(secs.group(1)), "block": block}
        else:
            if re.search(r"\bmax", sentence, re.I):
                reps = "max"
            else:
                found = RANGE.search(sentence)
                reps = re.sub(r"\s+", "", found.group(1)) if found else ""
            side = PER_SIDE.search(outside)
            if reps and side:
                reps += "/side" if re.search(r"side|stran", side.group(0), re.I) else "/leg"
            ex = {"name": ex_name, "type": "weight" if WEIGHTED.search(rest) else "reps",
                  "sets": block_sets, "reps": reps, "block": block}
        ex["note"] = rest[len(sentence):].strip(" .") or (rest if not sentence else "")
        exercises.append(norm_exercise(ex))

    return {"name": name or "New workout", "exercises": exercises}


app = FastAPI(title="GymTrack", docs_url=None, redoc_url=None)
init_db()


class Day(BaseModel):
    date: str
    run: bool = False
    gym: bool = False


class Routines(BaseModel):
    routines: list[dict[str, Any]]


class Workout(BaseModel):
    date: str
    name: str
    seconds: int = 0
    exercises: list[dict[str, Any]] = []


class RawText(BaseModel):
    text: str


class ProfileBody(BaseModel):
    profile: dict[str, Any] = {}


def _get_routines(conn) -> list:
    row = conn.execute("SELECT value FROM kv WHERE key='routines'").fetchone()
    return norm_routines(json.loads(row["value"])) if row else []


def _get_profile(conn) -> dict:
    row = conn.execute("SELECT value FROM kv WHERE key='profile'").fetchone()
    return norm_profile(json.loads(row["value"]) if row else {})


def _row_to_workout(r) -> dict:
    return {"id": r["id"], "date": r["date"], "name": r["name"],
            "seconds": r["seconds"], "exercises": json.loads(r["payload"])}


@app.get("/api/state")
def get_state():
    with db() as conn:
        days = {
            r["date"]: {"run": bool(r["run"]), "gym": bool(r["gym"])}
            for r in conn.execute("SELECT * FROM days WHERE run=1 OR gym=1")
        }
        workouts = [
            _row_to_workout(r) for r in conn.execute(
                "SELECT * FROM workouts ORDER BY date DESC, id DESC LIMIT 30")
        ]
        return {"days": days, "routines": _get_routines(conn),
                "workouts": workouts, "profile": _get_profile(conn)}


@app.post("/api/day")
def set_day(day: Day):
    if len(day.date) != 10:
        raise HTTPException(400, "date must be YYYY-MM-DD")
    with db() as conn:
        if not day.run and not day.gym:
            conn.execute("DELETE FROM days WHERE date=?", (day.date,))
        else:
            conn.execute(
                "INSERT INTO days (date, run, gym) VALUES (?,?,?) "
                "ON CONFLICT(date) DO UPDATE SET run=excluded.run, gym=excluded.gym",
                (day.date, int(day.run), int(day.gym)),
            )
    return {"ok": True}


@app.post("/api/routines")
def set_routines(body: Routines):
    clean = norm_routines(body.routines)
    with db() as conn:
        conn.execute(
            "INSERT INTO kv (key, value) VALUES ('routines', ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (json.dumps(clean, ensure_ascii=False),),
        )
    return {"ok": True, "routines": clean}


@app.post("/api/profile")
def set_profile(body: ProfileBody):
    clean = norm_profile(body.profile)
    with db() as conn:
        conn.execute(
            "INSERT INTO kv (key, value) VALUES ('profile', ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (json.dumps(clean, ensure_ascii=False),),
        )
    return {"ok": True, "profile": clean}


@app.post("/api/parse")
def parse(body: RawText):
    """Preview only — nothing is stored."""
    return parse_workout(body.text)


@app.get("/api/export")
def export_all():
    """Full backup — plans, days and workout history."""
    with db() as conn:
        return {
            "gymtrack": 1,
            "profile": _get_profile(conn),
            "routines": _get_routines(conn),
            "days": [dict(r) for r in conn.execute("SELECT * FROM days")],
            "workouts": [
                {"date": r["date"], "name": r["name"], "seconds": r["seconds"],
                 "exercises": json.loads(r["payload"])}
                for r in conn.execute("SELECT * FROM workouts ORDER BY date")
            ],
        }


@app.post("/api/workout")
def add_workout(w: Workout):
    with db() as conn:
        cur = conn.execute(
            "INSERT INTO workouts (date, name, seconds, payload) VALUES (?,?,?,?)",
            (w.date, w.name, w.seconds, json.dumps(w.exercises, ensure_ascii=False)),
        )
        conn.execute(
            "INSERT INTO days (date, run, gym) VALUES (?,0,1) "
            "ON CONFLICT(date) DO UPDATE SET gym=1", (w.date,),
        )
        return {"ok": True, "id": cur.lastrowid}


@app.get("/api/workouts")
def list_workouts(limit: int = 200, offset: int = 0):
    """The full history, newest first. /api/state only carries the recent slice."""
    limit, offset = max(1, min(500, limit)), max(0, offset)
    with db() as conn:
        total = conn.execute("SELECT COUNT(*) AS c FROM workouts").fetchone()["c"]
        rows = conn.execute(
            "SELECT * FROM workouts ORDER BY date DESC, id DESC LIMIT ? OFFSET ?",
            (limit, offset),
        ).fetchall()
    return {"total": total, "offset": offset,
            "workouts": [_row_to_workout(r) for r in rows]}


@app.delete("/api/workout/{workout_id}")
def delete_workout(workout_id: int):
    with db() as conn:
        conn.execute("DELETE FROM workouts WHERE id=?", (workout_id,))
    return {"ok": True}


@app.get("/api/exercise/{name}")
def exercise_history(name: str):
    """Most recent logged sets for an exercise, used to prefill weights."""
    with db() as conn:
        rows = conn.execute(
            "SELECT date, payload FROM workouts ORDER BY date DESC, id DESC LIMIT 60"
        ).fetchall()
    for r in rows:
        for ex in json.loads(r["payload"]):
            if ex.get("name", "").lower() == name.lower() and ex.get("sets"):
                return {"date": r["date"], "sets": ex["sets"]}
    return {"date": None, "sets": []}


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")


@app.get("/health")
def health():
    return {"ok": True}
