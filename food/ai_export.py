"""One whole day as a prompt for an AI — copied from the Food tab and pasted into Claude.

Everything the model needs to rate the day without asking back: who I am, the goal valid that
day, every entry with its amount, what I trained here and in openGym, and the week around it.
"""

import json
import sqlite3
from datetime import date as Date, timedelta

from .log import MEALS, day_view, summary

MEAL_LABEL = {"breakfast": "Breakfast", "lunch": "Lunch", "dinner": "Dinner", "snack": "Snacks"}
METHOD_NOTE = {"quick": "quick add, estimated", "recipe": "recipe", "photo": "from a photo, estimated",
               "barcode": "scanned"}


def f0(x) -> str:
    return f"{round(float(x or 0)):,}".replace(",", " ")


def f1(x) -> str:
    v = round(float(x or 0), 1)
    return str(int(v)) if v == int(v) else f"{v}"


def _profile(conn: sqlite3.Connection) -> dict:
    row = conn.execute("SELECT value FROM kv WHERE key='profile'").fetchone()
    try:
        return json.loads(row["value"]) if row else {}
    except ValueError:
        return {}


def _weight(conn: sqlite3.Connection, day: str) -> tuple[float | None, float | None, str | None]:
    """(latest weigh-in on or before the day, 7-day average, its date)."""
    row = conn.execute("SELECT date, kg FROM body_weight WHERE date <= ? ORDER BY date DESC LIMIT 1",
                       (day,)).fetchone()
    if row is None:
        return None, None, None
    since = (Date.fromisoformat(row["date"]) - timedelta(days=6)).isoformat()
    kgs = [r["kg"] for r in conn.execute("SELECT kg FROM body_weight WHERE date BETWEEN ? AND ?",
                                         (since, row["date"]))]
    return row["kg"], round(sum(kgs) / len(kgs), 1), row["date"]


def _training(conn: sqlite3.Connection, day: str) -> list[str]:
    out = []
    for r in conn.execute("SELECT name, seconds, payload FROM workouts WHERE date=? ORDER BY id", (day,)):
        try:
            exercises = json.loads(r["payload"] or "[]")
        except ValueError:
            exercises = []
        minutes = round((r["seconds"] or 0) / 60)
        run = next((e for e in exercises if e.get("type") == "run"), None)
        if run:
            bits = [f"{run['distance_km']} km" if run.get("distance_km") else "", run.get("pace") or ""]
            out.append(f"- Run: {r['name']}, {minutes} min" + "".join(f", {b}" for b in bits if b))
        else:
            sets = sum(len(e.get("sets") or []) for e in exercises)
            names = ", ".join(e.get("name", "") for e in exercises[:8] if e.get("name"))
            out.append(f"- Workout: {r['name']}, {minutes} min, {sets} sets" + (f" ({names})" if names else ""))
    return out


def day_report(conn: sqlite3.Connection, day: str, opengym: dict | None = None) -> str:
    """`opengym` is food.opengym.for_day(); None leaves openGym out of the prompt entirely."""
    d = Date.fromisoformat(day)
    view = day_view(conn, day)
    t, goal, entries = view["totals"], view["goal"], view["entries"]
    profile = _profile(conn)
    kg, avg7, kg_date = _weight(conn, day)
    og_kg = (opengym or {}).get("bodyweight")
    body_kg = kg or og_kg or (profile.get("weight") or None)

    L = [f"Here is my whole day, {d.strftime('%A')} {d.isoformat()}: everything I ate, as logged in my food "
         "tracker, and everything I trained. Rate my day and analyse it.", ""]

    L.append("## ME")
    if kg:
        L.append(f"Bodyweight: {f1(kg)} kg (weighed {kg_date}; 7-day average {f1(avg7)} kg)")
    elif og_kg:
        L.append(f"Bodyweight: {f1(og_kg)} kg (latest weigh-in in openGym)")
    elif body_kg:
        L.append(f"Bodyweight: {f1(body_kg)} kg")
    else:
        L.append("Bodyweight: NOT PROVIDED — assume 75 kg, and say clearly that you assumed it.")
    for key, label, unit in (("height", "Height", " cm"), ("age", "Age", ""), ("sex", "Sex", "")):
        if profile.get(key):
            L.append(f"{label}: {profile[key]}{unit}")
    L.append("")

    L.append("## GOAL FOR THIS DAY")
    if goal:
        L.append(f"{f0(goal['kcal'])} kcal · protein {f0(goal['protein'])} g · carbs {f0(goal['carbs'])} g · "
                 f"fat {f0(goal['fat'])} g (valid from {goal['effective_from']})")
    else:
        L.append("No goal set — judge the day against what suits my body and training.")
    L.append("")

    L.append("## TOTALS")
    line = f"{f0(t['kcal'])} kcal · protein {f1(t['protein'])} g · carbs {f1(t['carbs'])} g · fat {f1(t['fat'])} g"
    L.append(line)
    if goal and goal["kcal"]:
        diff = t["kcal"] - goal["kcal"]
        L.append(f"That is {round(t['kcal'] / goal['kcal'] * 100)} % of the calorie goal "
                 f"({'+' if diff >= 0 else '−'}{f0(abs(diff))} kcal).")
    macro_kcal = t["protein"] * 4 + t["carbs"] * 4 + t["fat"] * 9
    if macro_kcal:
        L.append(f"Energy split: protein {round(t['protein'] * 4 / macro_kcal * 100)} % · "
                 f"carbs {round(t['carbs'] * 4 / macro_kcal * 100)} % · fat {round(t['fat'] * 9 / macro_kcal * 100)} %")
    if body_kg and t["protein"]:
        L.append(f"Protein per kg bodyweight: {round(t['protein'] / float(body_kg), 2)} g/kg")
    L.append("")

    L.append("## EVERYTHING I ATE")
    if not entries:
        L.append("Nothing logged for this day.")
    for m in MEALS:
        items = [e for e in entries if e["meal"] == m]
        if not items:
            continue
        mt = view["meals"][m]
        L.append(f"### {MEAL_LABEL[m]} — {f0(mt['kcal'])} kcal")
        for e in items:
            amount = f"{f1(e['grams'])} g · " if e["grams"] else ""
            note = METHOD_NOTE.get(e["entry_method"])
            L.append(f"- {e['name']} — {amount}{f0(e['kcal'])} kcal · P {f1(e['protein'])} · "
                     f"C {f1(e['carbs'])} · F {f1(e['fat'])}" + (f" ({note})" if note else ""))
    L.append("")

    L.append("## TRAINING THIS DAY")
    training = _training(conn, day)
    if opengym is not None:
        if opengym.get("ok"):
            training += opengym["lines"]
        else:
            training.append(f"- (openGym could not be read: {opengym.get('error')} — "
                            "there may be a workout missing here.)")
    L.extend(training or ["Rest day — nothing logged."])
    L.append("")

    week = summary(conn, (d - timedelta(days=7)).isoformat(), (d - timedelta(days=1)).isoformat())
    if week:
        L.append("## THE 7 DAYS BEFORE (context)")
        L.append("| Date | kcal | goal | protein g | carbs g | fat g |")
        L.append("|---|---|---|---|---|---|")
        for w in week:
            g = w["goal"]["kcal"] if w["goal"] else None
            L.append(f"| {w['date']} | {f0(w['kcal'])} | {f0(g) if g else '—'} | {f0(w['protein'])} | "
                     f"{f0(w['carbs'])} | {f0(w['fat'])} |")
        avg = sum(w["kcal"] for w in week) / len(week)
        L.append(f"Average over the {len(week)} logged day{'s' if len(week) > 1 else ''}: {f0(avg)} kcal")
        L.append("")

    L.append("## WHAT I WANT BACK, IN THIS ORDER")
    L.append("1. RATING — the whole day out of 10, then separately food /10 and training /10, and one or")
    L.append("   two sentences why. I am losing weight: judge it by that, without being soft on me.")
    L.append("2. ENERGY BALANCE — estimate what I burned (training included) and my deficit or surplus.")
    L.append("3. MACROS — is protein enough for my weight and training, and is the split sensible?")
    L.append("4. FOOD QUALITY — what was good, what was missing (fibre, vegetables, fruit, micronutrients),")
    L.append("   and anything that looks like a logging mistake (implausible amounts, duplicates).")
    L.append("5. TOMORROW — concrete changes, with example foods and amounts, given what I trained.")
    L.append("")
    L.append("Be concrete and numeric. Do not ask me follow-up questions first — estimate, and state your "
             "assumptions. Answer in Czech.")
    return "\n".join(L)

