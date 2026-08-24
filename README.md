# GymTrack

A small self-hosted gym tracker. One year of training at a glance, a workout runner that knows the
difference between a barbell and a plank, and no account to sign up for.

Built to run on a home server behind a private network. Two files of application code, one SQLite
database, no build step on the frontend.

![License](https://img.shields.io/badge/license-MIT-blue)
![Python](https://img.shields.io/badge/python-3.12-blue)
![No JS build](https://img.shields.io/badge/frontend-single%20file-brightgreen)

---

## What it does

**Year grid.** A contribution-style square per day. Blue for a gym session, green for a run, split
diagonally when you did both. Tap any square to cycle it by hand.

**Workout runner.** Pick a plan, get your exercises grouped into blocks, log sets as you go. The
elapsed timer runs at the top, and an unfinished workout survives a refresh or a phone lock for six
hours.

**Three exercise types**, because not everything is measured in kilos:

| Type     | Logs                | Behaviour                                                         |
|----------|---------------------|-------------------------------------------------------------------|
| `weight` | load in kg and reps | Prefills what you lifted last time for the same exercise           |
| `reps`   | reps only           | For bodyweight work                                                |
| `time`   | seconds             | Countdown with a start button; the set ticks itself off at zero, beeps and vibrates |

**Plan editor.** Build plans in the app — name, exercise, type, sets, reps or seconds. Saves as you
type.

**Import and export.** Plans move in and out as JSON. There is also a parser for plans written as
prose, which reads bullet lists, block headings like `(4 sets)` and shorthand like
`Bench press 4x8-10`. A full backup endpoint dumps plans, days and workout history in one file.

---

## Quick start

```bash
git clone https://github.com/YOUR_USERNAME/gymtrack.git
cd gymtrack
mkdir -p data
docker compose up -d --build
```

Open <http://localhost:8101>.

To publish on a different port, or run as a different user, copy `.env.example` to `.env` and edit it.

### Without Docker

```bash
pip install -r requirements.txt
GYMTRACK_DB=./data/gymtrack.db uvicorn main:app --host 0.0.0.0 --port 8000
```

### On iOS

Open the app in Safari and use *Share → Add to Home Screen*. It runs full screen without the browser
chrome. The countdown beep needs one tap on the page first — Safari blocks audio until the user has
interacted with the document.

---

## Configuration

| Variable      | Default              | Meaning                        |
|---------------|----------------------|--------------------------------|
| `GYMTRACK_DB` | `/data/gymtrack.db`  | Path to the SQLite database    |

The container must be able to write to the mounted data directory. Set `user:` in
`docker-compose.yml` to whoever owns it, or the database cannot be created.

---

## API

| Method   | Path                    | Purpose                                          |
|----------|-------------------------|--------------------------------------------------|
| `GET`    | `/api/state`            | Days, plans and the last 30 workouts             |
| `POST`   | `/api/day`              | Set or clear a day's run/gym flags               |
| `POST`   | `/api/routines`         | Replace all plans (normalised server-side)       |
| `POST`   | `/api/parse`            | Parse a written plan into exercises — preview only |
| `POST`   | `/api/workout`          | Log a finished workout, marks the day as gym     |
| `DELETE` | `/api/workout/{id}`     | Remove a logged workout                          |
| `GET`    | `/api/exercise/{name}`  | Most recent sets for one exercise                |
| `GET`    | `/api/export`           | Full backup: plans, days, history                |
| `GET`    | `/health`               | Liveness probe                                   |

Back the whole thing up with a single request:

```bash
curl -s localhost:8101/api/export > gymtrack-$(date +%F).json
```

---

## Plan format

```json
{
  "routines": [
    {
      "name": "Full-body circuit",
      "exercises": [
        {
          "name": "Bench press",
          "type": "weight",
          "sets": 4,
          "reps": "8-10",
          "seconds": 45,
          "block": "Chest, back and arms",
          "note": "Elbows tucked."
        }
      ]
    }
  ]
}
```

`seconds` only matters when `type` is `time`; `reps` only when it is `weight` or `reps`. `block`
groups exercises under a heading in the workout runner. Every field is optional — the server fills
in defaults, clamps nonsense values and treats an exercise with no `type` as `weight`, so plans
exported from an older version still import cleanly.

---

## Project layout

```
main.py             FastAPI app, SQLite schema, plan normaliser, text parser
static/index.html   The entire frontend — markup, styles, logic, no dependencies
Dockerfile          python:3.12-slim, no build stage
docker-compose.yml  Service definition, data volume, port mapping
data/               SQLite database (gitignored, created on first run)
```

---

## Notes

The frontend derives its API base path from `window.location.pathname` at runtime, so it works
unchanged behind a reverse proxy on a subpath.

Plan and workout data is normalised on the server rather than the client, which keeps the browser
free to send partially typed values while you edit.

There is no authentication. This is deliberate — it is built to sit on a private network. If you
expose it to the internet, put something in front of it.

---

## License

MIT — see [LICENSE](LICENSE).
