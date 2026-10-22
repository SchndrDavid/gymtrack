"""Workouts from openGym on the same server, for the day export.

The openGym on mordor is a local fork that runs without sign-in (SINGLE_USER=1) and answers
GET /api/export/day?date= with the day's workouts, exercise names resolved, and the latest
weigh-in. See the fork's api/day-export.js.
"""

import json
import os
import urllib.error
import urllib.request
from urllib.parse import urlsplit

TIMEOUT = 6


def base_url(host: str = "") -> str:
    """OPENGYM_URL, or openGym's usual port on whatever host the browser reached GymTrack at."""
    url = os.environ.get("OPENGYM_URL", "").strip().rstrip("/")
    if url:
        return url
    name = urlsplit("//" + (host or "")).hostname
    return f"http://{name}:8115" if name else ""


def fetch_day(url: str, day: str) -> dict:
    """openGym's export of one day. Raises RuntimeError with a readable reason."""
    req = urllib.request.Request(f"{url}/api/export/day?date={day}",
                                 headers={"Accept": "application/json", "User-Agent": "GymTrack/1.0 (self-hosted)"})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            return json.load(resp)
    except urllib.error.HTTPError as e:
        if e.code == 404:
            raise RuntimeError("this openGym has no day export (not the local fork?)") from None
        if e.code == 401:
            raise RuntimeError("openGym asks for sign-in (SINGLE_USER is not on)") from None
        raise RuntimeError(f"openGym answered {e.code}") from None
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise RuntimeError(f"not reachable at {url}: {getattr(e, 'reason', e)}") from None
    except ValueError:
        raise RuntimeError("openGym answered with something that is not JSON") from None


def _num(x) -> str:
    v = round(float(x), 1)
    return str(int(v)) if v == int(v) else str(v)


def _set_text(s: dict, unit: str) -> str:
    if s.get("min"):
        bits = [f"{_num(s['min'])} min"] + ([f"{_num(s['speed'])} km/h"] if s.get("speed") else []) \
            + ([f"incline {_num(s['incline'])} %"] if s.get("incline") else [])
    elif s.get("sec"):
        bits = [f"{_num(s['sec'])} s"] + ([f"{_num(s['w'])} {unit}"] if s.get("w") else [])
    elif s.get("w"):
        bits = [f"{_num(s['w'])} {unit} × {_num(s.get('r') or 0)}"]
    elif s.get("r"):
        bits = [f"{_num(s['r'])} reps"]
    else:
        return ""
    if s.get("rpe"):
        bits.append(f"RPE {_num(s['rpe'])}")
    elif s.get("rir") is not None:
        bits.append(f"RIR {_num(s['rir'])}")
    return " ".join(bits)


def workout_lines(day: dict) -> list[str]:
    """Markdown lines for every workout in openGym's day export, set by set."""
    unit = day.get("unit") or "kg"
    out = []
    for w in day.get("workouts") or []:
        head = f"- openGym workout: {w.get('name') or 'Workout'}"
        if w.get("minutes"):
            head += f", {w['minutes']} min"
        if w.get("volume"):
            head += f", volume {_num(w['volume'])} {unit}"
        if w.get("prs"):
            head += f", {w['prs']} personal record{'s' if w['prs'] > 1 else ''}"
        out.append(head)
        for e in w.get("entries") or []:
            sets = e.get("sets") or []
            done = "; ".join(t for t in (_set_text(s, unit) for s in sets) if t)
            out.append(f"  - {e.get('name')}" + (f" ({e['target']})" if e.get("target") else "")
                       + f": {len(sets)} set{'s' if len(sets) != 1 else ''}" + (f" — {done}" if done else ""))
    return out


def for_day(day: str, host: str = "") -> dict:
    """{"ok", "lines", "workouts", "bodyweight", "error"} — never raises, the export goes out either way."""
    url = base_url(host)
    if not url:
        return {"ok": False, "lines": [], "workouts": 0, "bodyweight": None, "error": "OPENGYM_URL is not set"}
    try:
        data = fetch_day(url, day)
    except RuntimeError as err:
        return {"ok": False, "lines": [], "workouts": 0, "bodyweight": None, "error": str(err)}
    bw = data.get("bodyweight") or {}
    return {"ok": True, "lines": workout_lines(data), "workouts": len(data.get("workouts") or []),
            "bodyweight": bw.get("kg"), "error": None}
