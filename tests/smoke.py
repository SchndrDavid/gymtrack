"""End-to-end smoke test. Runs the app against a throwaway database.

    python tests/smoke.py
"""

import os
import re
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
check("health endpoint responds",
      client.get("/health").json() == {"ok": True, "version": main.VERSION})
check("state reports the version", client.get("/api/state").json()["version"] == main.VERSION)
check("version looks like a release", re.fullmatch(r"\d+\.\d+\.\d+", main.VERSION) is not None)

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

# Profile — the numbers the AI prompt needs to estimate calories
check("profile starts empty", client.get("/api/state").json()["profile"]["weight"] == 0)
check("profile defaults to gemini", client.get("/api/state").json()["profile"]["ai"] == "gemini")
saved = client.post("/api/profile", json={"profile": {
    "weight": "82.4", "height": 184, "age": "31", "sex": "MALE", "ai": "chatgpt"}}).json()["profile"]
check("bodyweight is stored", saved["weight"] == 82)
check("sex is lowercased", saved["sex"] == "male")
check("ai target is stored", saved["ai"] == "chatgpt")
check("profile survives a reload", client.get("/api/state").json()["profile"]["height"] == 184)
junk = client.post("/api/profile", json={"profile": {
    "weight": "nonsense", "age": 999, "sex": "yes", "ai": "skynet"}}).json()["profile"]
check("junk weight falls back to zero", junk["weight"] == 0)
check("absurd age is clamped", junk["age"] == 120)
check("unknown sex is dropped", junk["sex"] == "")
check("unknown ai target falls back", junk["ai"] == "gemini")

# Full history, newest first and pageable
for day in ("2026-08-20", "2026-08-21", "2026-08-22"):
    client.post("/api/workout", json={"date": day, "name": "Session " + day, "seconds": 600,
                                      "exercises": [{"name": "Row", "type": "weight",
                                                     "sets": [{"kg": "50", "reps": "10"}]}]})
history = client.get("/api/workouts").json()
check("history counts every workout", history["total"] == 4)
check("history returns every workout", len(history["workouts"]) == 4)
check("history is newest first", history["workouts"][0]["date"] == "2026-08-25")
check("history carries the length", history["workouts"][0]["seconds"] == 1800)
check("history carries the logged sets", history["workouts"][0]["exercises"][0]["sets"][0]["kg"] == "60")
page = client.get("/api/workouts?limit=2&offset=2").json()
check("history pages", [w["date"] for w in page["workouts"]] == ["2026-08-21", "2026-08-20"])
check("a page still reports the total", page["total"] == 4)
check("an absurd limit is clamped", len(client.get("/api/workouts?limit=99999").json()["workouts"]) == 4)

# Deleting a workout drops it from the history
victim = history["workouts"][0]["id"]
client.delete(f"/api/workout/{victim}")
check("deleted workout leaves the history", client.get("/api/workouts").json()["total"] == 3)

# Backup
backup = client.get("/api/export").json()
check("export carries every section",
      {"gymtrack", "profile", "routines", "days", "workouts"} <= backup.keys())
check("export includes history", len(backup["workouts"]) == 3)
check("export includes the profile", backup["profile"] == client.get("/api/state").json()["profile"])

print(f"\n{checks} checks passed")
