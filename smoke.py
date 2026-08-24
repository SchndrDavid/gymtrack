"""End-to-end smoke test. Runs the app against a throwaway database.

    python tests/smoke.py
"""

import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ["GYMTRACK_DB"] = str(Path(tempfile.mkdtemp()) / "test.db")

from fastapi.testclient import TestClient  # noqa: E402

import main  # noqa: E402

client = TestClient(main.app)
checks = 0


def check(label, condition):
    global checks
    checks += 1
    if not condition:
        print(f"FAIL  {label}")
        sys.exit(1)
    print(f"ok    {label}")


# Seeded state
state = client.get("/api/state").json()
check("default plan is seeded", len(state["routines"]) == 1)
check("exercises carry a type", all(e["type"] in main.TYPES for e in state["routines"][0]["exercises"]))
check("index page is served", client.get("/").status_code == 200)
check("health endpoint responds", client.get("/health").json() == {"ok": True})

# Days cycle and clear
client.post("/api/day", json={"date": "2026-08-24", "run": True, "gym": True})
check("day is stored", client.get("/api/state").json()["days"]["2026-08-24"] == {"run": True, "gym": True})
client.post("/api/day", json={"date": "2026-08-24", "run": False, "gym": False})
check("empty day is removed", client.get("/api/state").json()["days"] == {})
check("bad date is rejected", client.post("/api/day", json={"date": "nope"}).status_code == 400)

# Normalisation: blanks, junk and legacy exercises without a type
client.post("/api/routines", json={"routines": [{"name": "Legacy", "exercises": [
    {"name": "Bench", "sets": "", "reps": "8-10"},
    {"name": "Plank", "sets": "junk", "seconds": 60},
    {"name": "", "type": "reps"},
]}]})
exercises = client.get("/api/state").json()["routines"][0]["exercises"]
check("nameless exercise is dropped", len(exercises) == 2)
check("legacy exercise defaults to weight", exercises[0]["type"] == "weight")
check("blank set count falls back to 3", exercises[0]["sets"] == 3)
check("seconds imply a time exercise", exercises[1]["type"] == "time" and exercises[1]["seconds"] == 60)
check("junk set count falls back to 3", exercises[1]["sets"] == 3)

# Text parser
parsed = client.post("/api/parse", json={"text": (
    "3. Core (3 sets)\n"
    "* Plank: 45 s hold. Keep the hips down.\n"
    "* Push-ups: 10-15 reps each side.\n"
    "* Curls: 12 reps with dumbbells.\n"
    "Bench press 4x8-10\n"
    "5. Stretching (5 minutes)\n"
    "* Quads and calves.\n"
)}).json()
by_name = {e["name"]: e for e in parsed["exercises"]}
check("block set count is applied", by_name["Plank"]["sets"] == 3)
check("seconds become a time exercise", by_name["Plank"]["type"] == "time" and by_name["Plank"]["seconds"] == 45)
check("per-side reps are marked", by_name["Push-ups"]["reps"] == "10-15/side")
check("dumbbells imply a weighted exercise", by_name["Curls"]["type"] == "weight")
check("shorthand is parsed", by_name["Bench press"]["sets"] == 4 and by_name["Bench press"]["reps"] == "8-10")
check("stretching block is skipped", "Quads and calves" not in by_name)
check("notes are kept", by_name["Plank"]["note"].startswith("Keep the hips"))

# Logging a workout marks the day and stores every set shape
client.post("/api/workout", json={"date": "2026-08-25", "name": "Mix", "seconds": 1800, "exercises": [
    {"name": "Bench", "type": "weight", "sets": [{"kg": "60", "reps": "8"}]},
    {"name": "Push-ups", "type": "reps", "sets": [{"reps": "15"}]},
    {"name": "Plank", "type": "time", "sets": [{"sec": 45}]},
]})
state = client.get("/api/state").json()
check("workout is stored", len(state["workouts"]) == 1)
check("workout marks the day as gym", state["days"]["2026-08-25"]["gym"] is True)
check("weight history is queryable", client.get("/api/exercise/bench").json()["sets"][0]["kg"] == "60")

# Backup
backup = client.get("/api/export").json()
check("export carries every section", {"gymtrack", "routines", "days", "workouts"} <= backup.keys())
check("export includes history", len(backup["workouts"]) == 1)

print(f"\n{checks} checks passed")
