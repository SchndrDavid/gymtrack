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
from datetime import datetime
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel

VERSION = "1.2.0"
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
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                date       TEXT NOT NULL,
                name       TEXT NOT NULL,
                seconds    INTEGER NOT NULL DEFAULT 0,
                payload    TEXT NOT NULL,
                started_at TEXT
            );
            CREATE TABLE IF NOT EXISTS kv (
                key   TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_workouts_date ON workouts(date);
            """
        )
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(workouts)").fetchall()}
        if "started_at" not in cols:
            conn.execute("ALTER TABLE workouts ADD COLUMN started_at TEXT")

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


app = FastAPI(title="GymTrack", version=VERSION, docs_url=None, redoc_url=None)
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
    started_at: str | None = None


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
            "seconds": r["seconds"], "exercises": json.loads(r["payload"]),
            "started_at": r["started_at"]}


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
        return {"version": VERSION, "days": days, "routines": _get_routines(conn),
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


DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


@app.get("/api/log")
def get_log(
    from_date: str | None = Query(None, alias="from"),
    to_date: str | None = Query(None, alias="to"),
):
    if from_date is not None and not DATE_RE.match(from_date):
        raise HTTPException(400, "from must be YYYY-MM-DD")
    if to_date is not None and not DATE_RE.match(to_date):
        raise HTTPException(400, "to must be YYYY-MM-DD")

    query = "SELECT id, date, name, seconds, started_at FROM workouts"
    params: list[Any] = []
    if from_date and to_date:
        query += " WHERE date BETWEEN ? AND ?"
        params.extend([from_date, to_date])
    elif from_date:
        query += " WHERE date >= ?"
        params.append(from_date)
    elif to_date:
        query += " WHERE date <= ?"
        params.append(to_date)
    query += " ORDER BY date, id"

    with db() as conn:
        rows = conn.execute(query, params).fetchall()
        workouts = [
            {
                "id": r["id"],
                "date": r["date"],
                "name": r["name"],
                "seconds": r["seconds"],
                "started_at": r["started_at"],
            }
            for r in rows
        ]
    return {"workouts": workouts}


@app.post("/api/workout")
def add_workout(w: Workout):
    with db() as conn:
        cur = conn.execute(
            "INSERT INTO workouts (date, name, seconds, payload, started_at) VALUES (?,?,?,?,?)",
            (w.date, w.name, w.seconds, json.dumps(w.exercises, ensure_ascii=False), w.started_at),
        )
        conn.execute(
            "INSERT INTO days (date, run, gym) VALUES (?,0,1) "
            "ON CONFLICT(date) DO UPDATE SET gym=1", (w.date,),
        )
        return {"ok": True, "id": cur.lastrowid}


def parse_apple_run(data: dict) -> dict:
    text = data.get("content") or data.get("text") or ""
    if not isinstance(text, str):
        text = str(text)

    # 1. Date & Start time
    date_str = data.get("date")
    started_at = data.get("started_at")

    if not started_at and text:
        m = re.search(
            r"(?:Date|Datum)[\s:]+(\d{1,2})\.\s*(\d{1,2})\.\s*(\d{4})[^\d\n\r]*(\d{1,2}):(\d{2})(?::(\d{2}))?",
            text,
            re.I,
        )
        if m:
            d, mo, y, h, mi, s = (
                int(m.group(1)),
                int(m.group(2)),
                int(m.group(3)),
                int(m.group(4)),
                int(m.group(5)),
                int(m.group(6) or 0),
            )
            date_str = f"{y:04d}-{mo:02d}-{d:02d}"
            started_at = f"{date_str}T{h:02d}:{mi:02d}:{s:02d}"
        else:
            iso_m = re.search(r"(\d{4}-\d{2}-\d{2})[T\s](\d{1,2}):(\d{2})(?::(\d{2}))?", text)
            if iso_m:
                date_str = iso_m.group(1)
                started_at = f"{date_str}T{int(iso_m.group(2)):02d}:{int(iso_m.group(3)):02d}:{int(iso_m.group(4) or 0):02d}"

    if not date_str:
        date_str = datetime.now().strftime("%Y-%m-%d")
    if not started_at:
        started_at = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")

    # 2. Duration / seconds
    seconds = data.get("seconds")
    if not seconds and text:
        dur_m = re.search(
            r"(?:Duration|Trvání|Doba trvání)[\s:]+([\d\s]+)\s*(?:secs?|s\b|sekund)",
            text,
            re.I,
        )
        if dur_m:
            seconds = int(dur_m.group(1).replace(" ", ""))
        else:
            clock_m = re.search(r"(?:Duration|Trvání)[\s:]+(\d+):(\d{2})(?::(\d{2}))?", text, re.I)
            if clock_m:
                if clock_m.group(3):
                    seconds = int(clock_m.group(1)) * 3600 + int(clock_m.group(2)) * 60 + int(clock_m.group(3))
                else:
                    seconds = int(clock_m.group(1)) * 60 + int(clock_m.group(2))
            else:
                end_m = re.search(
                    r"(?:End date|Konec|Datum ukončení)[\s:]+(\d{1,2})\.\s*(\d{1,2})\.\s*(\d{4})[^\d\n\r]*(\d{1,2}):(\d{2})(?::(\d{2}))?",
                    text,
                    re.I,
                )
                if end_m and started_at:
                    try:
                        ed, emo, ey, eh, emi, es = (
                            int(end_m.group(1)),
                            int(end_m.group(2)),
                            int(end_m.group(3)),
                            int(end_m.group(4)),
                            int(end_m.group(5)),
                            int(end_m.group(6) or 0),
                        )
                        end_dt = datetime(ey, emo, ed, eh, emi, es)
                        start_dt = datetime.fromisoformat(started_at)
                        diff = int((end_dt - start_dt).total_seconds())
                        if diff > 0:
                            seconds = diff
                    except Exception:
                        pass
    seconds = int(seconds or 0)

    # 3. Distance
    dist_km = data.get("distance_km") or data.get("distance")
    if dist_km is None and text:
        dist_m = re.search(r"(?:Distance|Vzdálenost)[\s:]+([\d\s,\.]+)\s*(km|m\b)?", text, re.I)
        if dist_m:
            raw = dist_m.group(1).replace(" ", "").replace(",", ".")
            unit = (dist_m.group(2) or "km").lower()
            val = float(raw)
            dist_km = val if unit == "km" or (unit == "" and val < 100) else val / 1000.0
    if dist_km is not None:
        dist_km = float(dist_km)

    # 4. Speed
    speed_kmh = data.get("speed_kmh") or data.get("speed")
    if speed_kmh is None and text:
        spd_m = re.search(r"(?:Avg Speed|Rychlost|Průměrná rychlost)[\s:]+([\d\s,\.]+)", text, re.I)
        if spd_m:
            speed_kmh = float(spd_m.group(1).replace(" ", "").replace(",", "."))
    if not speed_kmh and dist_km and seconds > 0:
        speed_kmh = (dist_km / seconds) * 3600.0
    if speed_kmh is not None:
        speed_kmh = float(speed_kmh)

    title = f"Běh {dist_km:.1f} km" if dist_km else "Běh"

    notes = []
    if dist_km:
        notes.append(f"{dist_km:.2f} km")
    pace_str = None
    if speed_kmh:
        notes.append(f"{speed_kmh:.1f} km/h")
        pace_sec = int(3600 / speed_kmh)
        pace_str = f"{pace_sec // 60}:{pace_sec % 60:02d} /km"
        notes.append(f"tempo {pace_sec // 60}:{pace_sec % 60:02d}/km")

    note = " · ".join(notes)
    exercises = [
        {
            "name": "Běh",
            "type": "run",
            "distance_km": round(dist_km, 2) if dist_km else None,
            "speed_kmh": round(speed_kmh, 1) if speed_kmh else None,
            "pace": pace_str,
            "sets": [{"sec": seconds}],
            "note": note,
        }
    ]

    return {
        "date": date_str,
        "started_at": started_at,
        "name": title,
        "seconds": seconds,
        "exercises": exercises,
    }


@app.post("/api/run")
async def add_run(request: Request):
    """Log a running workout (from Apple Health/Shortcuts or JSON)."""
    try:
        data = await request.json()
    except Exception:
        raw = (await request.body()).decode("utf-8", errors="ignore")
        data = {"content": raw}

    if isinstance(data, str):
        data = {"content": data}
    elif not isinstance(data, dict):
        data = {"content": str(data)}

    parsed = parse_apple_run(data)

    with db() as conn:
        existing = conn.execute(
            "SELECT id FROM workouts WHERE date=? AND started_at=? AND name=?",
            (parsed["date"], parsed["started_at"], parsed["name"]),
        ).fetchone()
        if existing:
            conn.execute(
                "UPDATE workouts SET seconds=?, payload=? WHERE id=?",
                (parsed["seconds"], json.dumps(parsed["exercises"], ensure_ascii=False), existing["id"]),
            )
            workout_id = existing["id"]
        else:
            cur = conn.execute(
                "INSERT INTO workouts (date, name, seconds, payload, started_at) VALUES (?,?,?,?,?)",
                (
                    parsed["date"],
                    parsed["name"],
                    parsed["seconds"],
                    json.dumps(parsed["exercises"], ensure_ascii=False),
                    parsed["started_at"],
                ),
            )
            workout_id = cur.lastrowid

        conn.execute(
            "INSERT INTO days (date, run, gym) VALUES (?,1,0) "
            "ON CONFLICT(date) DO UPDATE SET run=1",
            (parsed["date"],),
        )

    return {"ok": True, "id": workout_id, "workout": parsed}


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
    return {"ok": True, "version": VERSION}
