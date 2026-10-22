"""Weight-loss progress: the weight trend, the energy actually burned, and the way to the target.

Trend. A line fitted through the weigh-ins of the last three weeks: its value today is the trend
weight, its slope the rate. Day-to-day water swings of a kilo average out, a single weigh-in never
moves it much. With too few weigh-ins it falls back to the plain average of the last week.

Maintenance (adaptive TDEE). Over the same three weeks: what was eaten on the logged days, minus
the energy that the change in trend weight accounts for (7700 kcal per kg). Eating 2100 kcal a day
while losing 0.5 kg a week means burning about 2650. It is the user's own number, not a formula
from height and age — and it is only as good as the logging, so it says how sure it is.
"""

import json
import sqlite3
from datetime import date as Date, timedelta

from .log import goal_for, summary

KCAL_PER_KG = 7700
WINDOW = 21                  # days the trend and maintenance look back over
PACE_PCT = 0.7               # suggested loss, % of bodyweight per week — fast enough, keeps muscle


def _profile(conn: sqlite3.Connection) -> dict:
    try:
        row = conn.execute("SELECT value FROM kv WHERE key='profile'").fetchone()
        return json.loads(row["value"]) if row else {}
    except (sqlite3.Error, ValueError):
        return {}


def get_target(conn: sqlite3.Connection) -> dict | None:
    r = conn.execute("SELECT kg, set_on FROM weight_target WHERE id=1").fetchone()
    return {"kg": r["kg"], "set_on": r["set_on"]} if r else None


def set_target(conn: sqlite3.Connection, kg: float | None, today: str) -> None:
    if kg is None:
        conn.execute("DELETE FROM weight_target")
        return
    if not 30 <= kg <= 300:
        raise ValueError("target must be between 30 and 300 kg")
    conn.execute("INSERT INTO weight_target (id, kg, set_on) VALUES (1, ?, ?) "
                 "ON CONFLICT(id) DO UPDATE SET kg=excluded.kg, set_on=excluded.set_on", (round(kg, 1), today))


def trend(conn: sqlite3.Connection, day: str) -> dict | None:
    """{"kg", "rate_week", "points", "fitted"} at the end of the day, or None without a weigh-in."""
    d = Date.fromisoformat(day)
    since = (d - timedelta(days=WINDOW - 1)).isoformat()
    pts = [(Date.fromisoformat(r["date"]), r["kg"]) for r in conn.execute(
        "SELECT date, kg FROM body_weight WHERE date BETWEEN ? AND ? ORDER BY date", (since, day))]
    if not pts:
        last = conn.execute("SELECT date, kg FROM body_weight WHERE date <= ? ORDER BY date DESC LIMIT 1",
                            (day,)).fetchone()
        return {"kg": last["kg"], "rate_week": None, "points": 0, "fitted": False, "since": last["date"]} if last else None
    xs = [(p - d).days for p, _ in pts]
    if len(pts) >= 3 and xs[-1] - xs[0] >= 7:
        n = len(pts)
        mx, my = sum(xs) / n, sum(kg for _, kg in pts) / n
        sxx = sum((x - mx) ** 2 for x in xs)
        slope = sum((x - mx) * (kg - my) for x, (_, kg) in zip(xs, pts)) / sxx
        return {"kg": round(my - slope * mx, 2), "rate_week": round(slope * 7, 2), "points": n, "fitted": True,
                "since": pts[0][0].isoformat()}
    week = [kg for p, kg in pts if (d - p).days < 7] or [pts[-1][1]]
    return {"kg": round(sum(week) / len(week), 2), "rate_week": None, "points": len(pts), "fitted": False,
            "since": pts[0][0].isoformat()}


def maintenance(conn: sqlite3.Connection, day: str, t: dict | None) -> dict:
    """Energy actually burned per day over the window; `kcal` None while there is too little to go on."""
    d = Date.fromisoformat(day)
    start = (d - timedelta(days=WINDOW - 1)).isoformat()
    days = [x for x in summary(conn, start, day) if x["kcal"] > 0]
    out = {"kcal": None, "intake": None, "logged_days": len(days), "window": WINDOW, "confidence": None}
    if days:
        out["intake"] = round(sum(x["kcal"] for x in days) / len(days))
    needs = [x for x, missing in (("at least 10 logged days", len(days) < 10),
                                  ("3 weigh-ins over a week or more", not (t and t["fitted"]))) if missing]
    if needs:
        out["needs"] = " and ".join(needs)
        return out
    out["kcal"] = round(out["intake"] - t["rate_week"] / 7 * KCAL_PER_KG, -1)
    out["confidence"] = "good" if len(days) >= 17 and t["points"] >= 8 else "rough"
    return out


def pace(t: dict | None) -> dict | None:
    """How fast, as % of bodyweight per week, and what that means for losing weight."""
    if not t or t["rate_week"] is None:
        return None
    pct = round(t["rate_week"] / t["kg"] * 100, 2)
    if pct < -1.0:
        verdict = "too_fast"         # faster than 1 %/week costs muscle
    elif pct <= -0.25:
        verdict = "on_track"
    elif pct < 0.25:
        verdict = "stalled"
    else:
        verdict = "gaining"
    return {"pct_week": pct, "verdict": verdict}


def progress(conn: sqlite3.Connection, day: str) -> dict:
    t = trend(conn, day)
    weighed = conn.execute("SELECT kg FROM body_weight WHERE date=?", (day,)).fetchone()
    target = get_target(conn)
    m = maintenance(conn, day, t)
    goal = goal_for(conn, day)
    out = {"date": day, "weighed": weighed["kg"] if weighed else None, "trend": t, "pace": pace(t),
           "maintenance": m, "goal_kcal": goal["kcal"] if goal else None, "target": target, "suggestion": None}

    if target and t:
        left = round(t["kg"] - target["kg"], 1)
        target = out["target"] = {**target, "left": left, "eta": None, "weeks": None}
        if left > 0 and t["rate_week"] and t["rate_week"] < -0.05:
            weeks = left / -t["rate_week"]
            target["weeks"] = round(weeks, 1)
            target["eta"] = (Date.fromisoformat(day) + timedelta(days=round(weeks * 7))).isoformat()

    if m["kcal"] and t:
        sex = (_profile(conn).get("sex") or "").lower()
        floor = 1200 if sex == "female" else 1500
        deficit = min(t["kg"] * PACE_PCT / 100 * KCAL_PER_KG / 7, m["kcal"] * 0.25)
        kcal = max(floor, round(m["kcal"] - deficit, -1))
        out["suggestion"] = {"kcal": kcal, "deficit": round(m["kcal"] - kcal), "pct_week": PACE_PCT,
                             "protein": round(t["kg"] * 1.8)}
    if goal and m["kcal"]:
        out["goal_deficit"] = round(m["kcal"] - goal["kcal"])
    return out


def describe(p: dict) -> list[str]:
    """The progress as lines for an AI prompt."""
    t, m, pc = p["trend"], p["maintenance"], p["pace"]
    L = []
    if t:
        line = f"Trend weight: {t['kg']:.1f} kg"
        if t["rate_week"] is not None:
            line += f", {t['rate_week']:+.2f} kg/week ({pc['pct_week']:+.2f} % of bodyweight per week)"
        L.append(line + f" — fitted through {t['points']} weigh-ins since {t['since']}")
    if m["kcal"]:
        L.append(f"Measured maintenance: about {m['kcal']:.0f} kcal/day ({m['confidence']} estimate: average intake "
                 f"{m['intake']} kcal on {m['logged_days']} logged days of the last {m['window']}, against the trend)")
    elif m["intake"]:
        L.append(f"Average intake on {m['logged_days']} logged days of the last {m['window']}: {m['intake']} kcal "
                 f"(maintenance not measurable yet — needs {m.get('needs')})")
    if p.get("goal_deficit") is not None:
        L.append(f"Calorie goal {p['goal_kcal']:.0f} kcal = a deficit of about {p['goal_deficit']} kcal/day")
    tg = p["target"]
    if tg:
        line = f"Target weight: {tg['kg']:.1f} kg"
        if tg.get("left") is not None:
            line += f", {tg['left']:.1f} kg to go"
        if tg.get("eta"):
            line += f", at this pace around {tg['eta']} ({tg['weeks']} weeks)"
        L.append(line)
    return L
