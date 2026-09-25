# GymTrack

A small self-hosted gym tracker. One year of training at a glance, a workout runner that knows the
difference between a barbell and a plank, a calorie counter that finds "kuř pr" before you finish
typing, and no account to sign up for.

Built to run on a home server behind a private network. A handful of Python modules, SQLite, and a
frontend that is one HTML file with no build step.

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

**History.** Every session you have logged, newest first and grouped by month, with its length, set
count and volume. Tap one to see every set you did, exercise by exercise.

**Ask an AI about a session.** From any workout, one button copies the whole session — your
bodyweight, the length, every set, and the five sessions before it — as a prompt asking for calories
burned, a muscle map, what to change next time, and an easy recovery recipe. Calories come first.

| Target | What the button does |
|--------|----------------------|
| Gemini | Copies the prompt and opens Gemini. Paste it in — Gemini has no URL parameter for prefilling a prompt |
| ChatGPT, Claude, Perplexity | Opens with the prompt already in the box, and copies it as well |
| Clipboard only | Copies and opens nothing |

Pick your default under the gear icon. Calorie estimates need a **bodyweight**, so set one there too —
without it the prompt tells the AI to assume 75 kg and say so.

**Food.** A second tab for what you eat — calories and macros against a goal, meal by meal:

- **Search that keeps up with typing.** Prefix match on every word, diacritics optional (`rizek`
  finds *řízek*), favourites and what you ate lately first, then ~400 basic foods with USDA values,
  then Open Food Facts products sold here — Czech and Slovak ones plus the own brands of Lidl,
  Kaufland, Albert, Billa, Penny, Tesco and Globus, whatever country they were entered under.
- **Portions, not arithmetic.** Picking a food opens a panel with the serving prefilled (1 egg,
  1 pot of yoghurt) and ½ / 1 / 2 × buttons, totals update as you type, and the meal is preselected
  by the time of day. **Quick add** takes just a number for the restaurant guess.
- **Barcodes, live or from a photo.** On an `https://` address *Scan* opens a live camera view and
  reads the code as soon as it is in the frame (the browser's own detector where it has one, else
  the server reads a small frame a few times a second). Over plain `http://` browsers refuse a live
  camera, so *Scan* takes an ordinary photo and the server reads that — see
  [HTTPS through Tailscale](docs/deployment.md#live-barcode-scanning-https) to get the live view. Unknown codes are looked up live on Open Food Facts
  and remembered; still unknown, you get a *New food* form with the code filled in. Or type the EAN.
- **Recipes from [MordorCook](https://github.com/SchndrDavid/mordorcook).** *Recipes* lists the recipes
  of your MordorCook, fetched from it each time you open the list. Every ingredient is matched to a
  food and its amount turned into grams; anything ambiguous is shown to you instead of guessed, and
  your answer is remembered. Log a portion, or the ingredients one by one. Recipes are written in
  MordorCook, not here — delete one there and it leaves the list.
- **Days, goals, history.** Edit or delete entries, copy yesterday's breakfast, set goals that apply
  from a date on, see week and month charts, macro averages and a 7-day weight trend. The year grid
  switches between *Training* and *Food*, where each day is coloured by how close it ended to the goal.
- **History never changes behind your back.** Each entry stores its own values, so re-importing the
  catalogue or editing a food leaves past days as they were.

**Plan editor.** Build plans in the app — name, exercise, type, sets, reps or seconds. Saves as you
type.

**Import and export.** Plans move in and out as JSON. There is also a parser for plans written as
prose, which reads bullet lists, block headings like `(4 sets)` and shorthand like
`Bench press 4x8-10`. A full backup endpoint dumps plans, days and workout history in one file.

---

## Quick start

```bash
git clone https://github.com/SchndrDavid/gymtrack.git
cd gymtrack
mkdir -p data
docker compose up -d --build
```

Open <http://localhost:8101>.

To publish on a different port, or run as a different user, copy `.env.example` to `.env` and edit it.
For home-server setups, backups and reaching it from your phone, see [docs/deployment.md](docs/deployment.md).

### Without Docker

Python 3.12 or newer.

```bash
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
mkdir -p data
GYMTRACK_DB=./data/gymtrack.db uvicorn main:app --host 0.0.0.0 --port 8000
```

Open <http://localhost:8000>.

### On iOS

Open the app in Safari and use *Share → Add to Home Screen*. It runs full screen without the browser
chrome. The countdown beep needs one tap on the page first — Safari blocks audio until the user has
interacted with the document.

---

## Configuration

| Variable            | Default                  | Meaning                                                   |
|---------------------|--------------------------|-----------------------------------------------------------|
| `GYMTRACK_PORT`     | `8101`                   | Host port (compose)                                       |
| `GYMTRACK_UID/GID`  | `1000`                   | User the container runs as — must own `./data` (compose)  |
| `GYMTRACK_DB`       | `/data/gymtrack.db`      | Your data: training and everything you log under Food     |
| `GYMTRACK_FOODS_DB` | next to `GYMTRACK_DB`, `foods.db` | Food catalogue — disposable, re-importable       |
| `MORDORCOOK_URL`    | `http://100.108.145.60:8105` in compose | MordorCook base URL. Empty hides *Recipes* |
| `FOOD_OFF_LIVE`     | `true`                   | Look unknown barcodes up on world.openfoodfacts.org (5 s timeout) |
| `FOOD_AI_ENABLED`   | `false`                  | Meal recognition from a photo — plumbing only, see below  |
| `ANTHROPIC_API_KEY` | empty                    | For the future recognizer; unused today                   |
| `FOOD_AI_MODEL`     | `claude-haiku-4-5`       | For the future recognizer; unused today                   |

Everything lives in `.env` (see `.env.example`); compose passes it into the container.

The container must be able to write to the mounted data directory. Set `user:` in
`docker-compose.yml` to whoever owns it, or the database cannot be created.

---

## Food data

The catalogue (`data/foods.db`) holds no personal data and can be deleted and rebuilt at any time.
While it is empty, the Food tab shows **Download food database**: one tap starts
`scripts/import_all.py` in a separate low-priority process on the server and the tab shows its
progress. The same can be done by hand, inside the container:

```bash
# 1. Basic foods: ~400 Czech names mapped to USDA FoodData Central. --build downloads the official
#    SR Legacy release, takes values and fdc_id from it and writes data/basic_foods.json.
docker compose -f /srv/compose/gymtrack.yml exec gymtrack python scripts/import_basic.py --build

# 2. Open Food Facts: streams the whole export and keeps products sold here (see below).
docker compose -f /srv/compose/gymtrack.yml exec gymtrack python scripts/import_off.py
```

The OFF export is a gzip of several gigabytes. It is decompressed on the fly and never stored, and
memory use stays flat. Kept are products tagged Czechia or Slovakia, plus the own brands of the
chains here (Lidl: Pilos, Milbona, Chef Select, Freeway…; Kaufland: K-Classic…; Albert, Billa/Clever,
Penny, Tesco, Globus) and anything listed as sold at those stores, whatever its country — the same
barcode is sold all over Europe and is often entered only once, in Germany or France, so some names
will be German. The lists are `BRANDS` and `STORES` in `scripts/import_off.py`. Expect 10–30 minutes,
mostly the download, on one core; `foods.db` should end up somewhere around 30–100 MB (an estimate —
it depends on how many chain products Open Food Facts has at the time). Re-run it every few months to pick up new products — it upserts by
barcode, and your food log is not affected. `--file dump.csv.gz` reads a local copy instead.

**Basic foods.** `seed/basic_foods.src.json` maps each Czech name (*kuřecí prsa syrová*, *rýže
vařená*, *tvaroh*…) to one or more USDA descriptions. `scripts/build_basic_seed.py` looks those up in
the official release and keeps the fdc_id with each row, so every value can be checked at
`https://fdc.nal.usda.gov/food-details/<fdc_id>/nutrients`. Foods USDA does not have are skipped and
listed, never estimated. If the download is blocked, fetch the SR Legacy CSV zip from
<https://fdc.nal.usda.gov/download-datasets> yourself and pass `--zip path/to/file.zip`. Copy the
generated `data/basic_foods.json` to `seed/` to commit it; the importer prefers that file.

Food data © Open Food Facts contributors, available under the
[Open Database License](https://opendatacommons.org/licenses/odbl/1-0/). Basic foods: USDA
FoodData Central, public domain. The Food tab says so in its footer.

## Recipes

Opening *Food → Recipes* reads `GET /api/recipes` from MordorCook (`MORDORCOOK_URL`) over HTTP.
GymTrack never touches its database and nothing polls in the background. New and changed recipes
are recomputed, deleted ones leave the list (days already logged keep their values). A recipe
becomes a food with values per 100 g of raw ingredients and a serving of one portion.

Every ingredient is matched to a food and converted to grams:

- `g`, `dkg`, `kg`, `oz`, `lb` always;
- `ml`, `l`, spoons and cups only for foods with a fixed density (water, milk, oil, flour, sugar…);
- pieces, cloves and slices only through the food's own USDA piece weight (1 egg = 50 g);
- salt, pepper and water are ignored as negligible.

Anything else — no food found, a loose match, "a handful" — is listed under *needs your help*, and
the recipe is not saved until you have answered. Answers are stored in `ingredient_mappings` and
reused by every later recipe.

For scripts, a recipe can also be posted to `POST /api/food/recipes/import` (there is no form for
it in the app — recipes belong in MordorCook):

```json
{
  "id": "optional-stable-id",
  "title": "Kuře na česneku",
  "servings": 4,
  "ingredients": [
    {"amount": 600, "unit": "g", "item": "kuřecí prsa"},
    {"amount": 2, "unit": "lžíce", "item": "olivový olej"},
    {"amount": 4, "unit": "stroužky", "item": "česnek"},
    {"amount": null, "unit": "", "item": "sůl"}
  ],
  "nutrition": {"per": "serving", "kcal": 420, "protein": 45, "carbs": 8, "fat": 22, "serving_g": 300}
}
```

`nutrition` is optional; when present it is used instead of the ingredients. The endpoint answers
`422` with the full analysis when ingredients still need an answer. `id` makes a re-import update
the same food. `POST /api/food/recipes/preview` returns the same analysis without storing anything.

## Meal recognition (not switched on)

The plumbing for recognising a meal from a photo is in place, but no AI service is called and no
SDK is installed. `recognition/` defines `FoodRecognizer` and `RecognizedItem`; the only
implementation is `DisabledRecognizer`. `POST /api/food/recognize` answers
`501 {"error": "food_ai_disabled"}` and the *Recognize meal* button stays hidden. To finish it,
implement one recognizer class at the marked spot in `recognition/__init__.py`, add its SDK to
`requirements.txt` and set `FOOD_AI_ENABLED=true` and `ANTHROPIC_API_KEY`. Jobs, polling, matching
against the catalogue and the confirmation sheet already work. Nothing is logged without a tap.

---

## API

| Method   | Path                    | Purpose                                          |
|----------|-------------------------|--------------------------------------------------|
| `GET`    | `/api/state`            | Version, days, plans, profile and the last 30 workouts |
| `POST`   | `/api/day`              | Set or clear a day's run/gym flags               |
| `POST`   | `/api/routines`         | Replace all plans (normalised server-side)       |
| `POST`   | `/api/parse`            | Parse a written plan into exercises — preview only |
| `POST`   | `/api/profile`          | Bodyweight, height, age, sex and default AI      |
| `POST`   | `/api/workout`          | Log a finished workout, marks the day as gym     |
| `GET`    | `/api/workouts`         | Full history, newest first (`limit`, `offset`)    |
| `DELETE` | `/api/workout/{id}`     | Remove a logged workout                          |
| `GET`    | `/api/exercise/{name}`  | Most recent sets for one exercise                |
| `GET`    | `/api/export`           | Full backup: profile, plans, days, history       |
| `GET`    | `/health`               | Liveness probe, reports the version              |
| `GET`    | `/api/config`           | Version and Food feature flags                   |

Food, all under `/api/food`:

| Method   | Path                          | Purpose                                                   |
|----------|-------------------------------|-----------------------------------------------------------|
| `GET`    | `/search?q=&limit=20`         | Search; empty `q` returns favourites and recent foods     |
| `GET`    | `/catalog`                    | How many foods are imported, progress of a running import |
| `POST`   | `/catalog/import`             | Download and import the catalogue in the background       |
| `GET`    | `/foods/{ref}`                | One food (`usda:<fdc_id>`, `off:<barcode>`, `user:<id>`)  |
| `POST` `PUT` `DELETE` | `/foods`, `/foods/{ref}` | Your own foods                                    |
| `PUT` `DELETE` | `/favorites/{ref}`      | Star or unstar a food                                     |
| `GET`    | `/day?date=`                  | Entries, totals per meal and the goal valid that day      |
| `POST`   | `/log`                        | Log a food (`food_ref` + `grams`) or a quick add (`kcal`) |
| `POST`   | `/log/batch`                  | Several entries at once, all or nothing                   |
| `PATCH` `DELETE` | `/log/{id}`           | Edit (grams rescale the stored values) or remove          |
| `POST`   | `/log/copy`                   | Copy a meal or a day to another day                       |
| `GET`    | `/summary?start=&end=`        | Per-day totals against that day's goal                    |
| `GET` `POST` | `/goals`                  | Goals with `effective_from`; `DELETE /goals/{id}`         |
| `GET` `POST` | `/weight`                 | Body weight with 7-day average; `DELETE /weight/{date}`   |
| `GET`    | `/barcode/{code}`             | Catalogue, then live Open Food Facts                      |
| `POST`   | `/barcode/scan`               | Read a barcode from an uploaded photo (not stored)        |
| `POST`   | `/barcode/decode`             | Codes in one live-camera frame, no lookup                 |
| `GET`    | `/recipes`                    | Recipes stored as foods                                   |
| `POST`   | `/recipes/sync`               | Mirror the recipes from MordorCook                        |
| `POST`   | `/recipes/import`, `/recipes/preview` | Store or just analyse one recipe JSON             |
| `GET`    | `/recipes/{ref}/items?portions=` | A recipe split into its ingredients                    |
| `GET` `POST` | `/mappings`               | Remembered ingredient answers; `DELETE /mappings/{key}`   |
| `POST`   | `/recognize`                  | Meal photo → job (501 while disabled)                     |
| `GET`    | `/recognize/{id}`             | Job status and recognised items                           |

Back the whole thing up with a single request:

```bash
curl -s localhost:8101/api/export > gymtrack-$(date +%F).json
```

The backup includes a `food` section with the log, goals, weights, your foods, recipes and
ingredient answers. The catalogue is left out on purpose — it can be re-imported.

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
main.py                    FastAPI app, SQLite schema, plan normaliser, text parser
food/                      Food module: catalogue + schema, search, log, barcodes, recipes, jobs
recognition/               Meal-recognition interface (disabled implementation only)
scripts/                   Catalogue importers: import_all.py, import_off.py, import_basic.py, build_basic_seed.py
seed/basic_foods.src.json  Czech basic foods → USDA descriptions
static/index.html          The entire frontend — markup, styles, logic, no dependencies
tests/smoke.py             End-to-end API test, runs against a throwaway database
tests/food_smoke.py        Food module end to end, on a synthetic catalogue, no network
requirements.txt           Runtime dependencies
requirements-dev.txt       Runtime plus httpx, which the test client needs
Dockerfile                 python:3.12-slim, no build stage
docker-compose.yml         Service definition, data volume, port mapping
.env.example               Port and UID/GID overrides — copy to .env
docs/deployment.md         Home server, backups, reaching it from a phone
.github/workflows/ci.yml   Runs the smoke test and builds the image
data/                      gymtrack.db and foods.db (gitignored except .gitkeep)
```

---

## Development

```bash
pip install -r requirements-dev.txt
python tests/smoke.py
python tests/food_smoke.py
```

The suite spins the API up against a temporary database and checks the parser, the normaliser, the
workout history, the profile and the backup endpoint. The Food suite builds its catalogue from small
synthetic stand-ins for the USDA release and the OFF dump, renders real barcodes to decode, and
serves a fake MordorCook over HTTP. It needs no server running and leaves nothing
behind. CI runs it on every push, then builds the image and waits for `/health`.

The version lives in one place, `VERSION` in `main.py`. It is served by `/health` and `/api/state`,
and the frontend prints it in the footer, so bumping that constant is the whole release process.
The footer is desktop-only — it is hidden below 640px and inside the iOS home-screen app.

The frontend has no build step — edit `static/index.html` and reload. Running under Docker, the
frontend is baked into the image, so rebuild to see a change:

```bash
docker compose up -d --build
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
