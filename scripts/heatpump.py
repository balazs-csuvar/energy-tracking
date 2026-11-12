#!/usr/bin/env python3
"""Turn econet24 CSV exports in heatpump/ into docs/heatpump.json.

econet24 logs a value only when it changes (roughly every 5 minutes), so each column is carried
forward minute by minute. Heat output is estimated from flow rate x (outlet - inlet) while the
compressor runs. The heat pump's electricity isn't logged, so it is separated from the Octopus
half-hourly total with a regression: house load by hour of day + kWh per kWh of heat for space
heating and for hot water. Standard library only.
"""
import csv, glob, json, os
from collections import defaultdict
from datetime import datetime, timedelta

ROOT = os.path.join(os.path.dirname(__file__), "..")
SRC = os.path.join(ROOT, "heatpump", "*.csv")
DATA = os.path.join(ROOT, "docs", "data.json")
HIST = os.path.join(ROOT, "docs", "history.json")
OUT = os.path.join(ROOT, "docs", "heatpump.json")
START = os.environ.get("HISTORY_START") or "2026-09-10"
GAP_MIN = 20  # minutes without any log line = unknown state

COLS = {"f": "Compressor actual frequency", "out": "Outlet temperature", "inl": "Inlet temperature",
        "room": "Circuit 1 thermostat temperature", "set": "Heat pump set temp.", "dhw": "DHW temperature",
        "ext": "External temperature", "flow": "Flow rate", "valve": "3-way valve position"}
DHW_VALVE = 3


def load():
    rows = {}
    for path in sorted(glob.glob(SRC)):
        with open(path, newline="", encoding="utf-8-sig") as f:
            r = csv.reader(f, delimiter=";")
            head = next(r)
            for row in r:
                if not row or not row[0]:
                    continue
                d = rows.setdefault(row[0], {})
                for k, v in zip(head[1:], row[1:]):
                    if v != "":
                        try:
                            d[k] = float(v)
                        except ValueError:
                            pass
    return [(datetime.fromisoformat(t), d) for t, d in sorted(rows.items())]


def solve(A, b):
    """Gaussian elimination with partial pivoting."""
    n = len(b)
    M = [row[:] + [b[i]] for i, row in enumerate(A)]
    for c in range(n):
        p = max(range(c, n), key=lambda r: abs(M[r][c]))
        if abs(M[p][c]) < 1e-12:
            return None
        M[c], M[p] = M[p], M[c]
        for r in range(n):
            if r != c:
                f = M[r][c] / M[c][c]
                M[r] = [x - f * y for x, y in zip(M[r], M[c])]
    return [M[i][n] / M[i][i] for i in range(n)]


def main():
    ev = load()
    if not ev:
        print("No heat pump exports found.")
        return
    # Minute-by-minute pass.
    hh = defaultdict(lambda: {"ch": 0.0, "dhw": 0.0, "on": 0, "n": 0})
    day = defaultdict(lambda: {"ch": 0.0, "dhw": 0.0, "on": 0, "starts": 0, "n": 0, "flow": [], "room": [],
                               "ext": [], "dhwmax": None, "set": []})
    st, i, prev_on, last_ev = {}, 0, False, ev[0][0]
    t, end = ev[0][0].replace(second=0, microsecond=0), ev[-1][0]
    legionella, hot_since = [], None
    while t <= end:
        while i < len(ev) and ev[i][0] <= t:
            for k, full in COLS.items():
                if full in ev[i][1]:
                    st[k] = ev[i][1][full]
            last_ev = ev[i][0]
            i += 1
        known = (t - last_ev) <= timedelta(minutes=GAP_MIN)
        dkey, slot = t.date().isoformat(), t.hour * 2 + t.minute // 30
        if known and dkey >= START:
            on = st.get("f", 0) > 0
            dhw_mode = st.get("valve") == DHW_VALVE
            q = 0.0
            if on and st.get("flow", 0) > 0 and "out" in st and "inl" in st:
                q = max(0.0, st["flow"] / 60 * 4.18 * (st["out"] - st["inl"])) / 60  # kWh this minute
            h, d = hh[(dkey, slot)], day[dkey]
            h["dhw" if dhw_mode else "ch"] += q
            h["on"] += on
            h["n"] += 1
            d["dhw" if dhw_mode else "ch"] += q
            d["on"] += on
            d["n"] += 1
            d["starts"] += on and not prev_on
            if on and not dhw_mode and "out" in st:
                d["flow"].append(st["out"])
            if on and not dhw_mode and "set" in st:
                d["set"].append(st["set"])
            for k in ("room", "ext"):
                if k in st and t.minute % 10 == 0:
                    d[k].append(st[k])
            if "dhw" in st:
                d["dhwmax"] = max(d["dhwmax"] or 0, st["dhw"])
                if st["dhw"] >= 58 and (hot_since is None or t - hot_since > timedelta(hours=20)):
                    hot_since = t
                    legionella.append(t.isoformat(timespec="minutes"))
            prev_on = on
        else:
            prev_on = False
        t += timedelta(minutes=1)

    # Separate heat pump electricity from the rest of the house using Octopus half-hours.
    data = json.load(open(DATA)) if os.path.exists(DATA) else {"days": []}
    elec = {d["date"]: d["hh"] for d in data["days"] if d.get("hh")}
    X, y = [], []
    for (dk, s), h in hh.items():
        if dk in elec and h["n"] >= 28:
            row = [0.0] * 26
            row[s // 2] = 1.0
            row[24], row[25] = h["ch"], h["dhw"]
            X.append(row)
            y.append(elec[dk][s])
    model = None
    if len(y) > 500:
        XtX = [[sum(r[a] * r[b] for r in X) for b in range(26)] for a in range(26)]
        Xty = [sum(r[a] * v for r, v in zip(X, y)) for a in range(26)]
        c = solve(XtX, Xty)
        if c and c[24] > 0 and c[25] > 0:
            house = [max(0.0, v) for v in c[:24]]  # kWh per half hour, by hour of day
            model = {"house": house, "k_ch": c[24], "k_dhw": c[25]}

    temps = {}
    if os.path.exists(HIST):
        temps = {d["date"]: d.get("temp_c") for d in json.load(open(HIST))["days"]}
    out_days = []
    for dk, d in sorted(day.items()):
        if d["n"] < 23 * 60:
            continue
        rec = {"date": dk, "heat_ch": round(d["ch"], 2), "heat_dhw": round(d["dhw"], 2),
               "run_h": round(d["on"] / 60, 1), "starts": d["starts"],
               "flow_avg": round(sum(d["flow"]) / len(d["flow"]), 1) if d["flow"] else None,
               "flow_max": round(sorted(d["flow"])[int(len(d["flow"]) * 0.95)], 1) if d["flow"] else None,  # 95th percentile
               "room_avg": round(sum(d["room"]) / len(d["room"]), 1) if d["room"] else None,
               "ext_avg": round(sum(d["ext"]) / len(d["ext"]), 1) if d["ext"] else None,
               "dhw_max": d["dhwmax"], "temp_c": temps.get(dk)}
        if model and dk in elec:
            house_day = sum(model["house"][s // 2] for s in range(48))
            hp = max(0.0, sum(elec[dk]) - house_day)
            rec["elec_total"] = round(sum(elec[dk]), 2)
            rec["elec_hp"] = round(hp, 2)
        out_days.append(rec)

    hourly = [{"ch": 0.0, "dhw": 0.0, "n": 0} for _ in range(24)]
    for (dk, s), h in hh.items():
        if h["n"] >= 28:
            hourly[s // 2]["ch"] += h["ch"]
            hourly[s // 2]["dhw"] += h["dhw"]
            hourly[s // 2]["n"] += 1
    prof = [{"ch": round(v["ch"] / v["n"] * 2, 3), "dhw": round(v["dhw"] / v["n"] * 2, 3)} if v["n"] else None for v in hourly]

    res = {"updated": datetime.now().isoformat(timespec="minutes"), "first": ev[0][0].date().isoformat(),
           "last": ev[-1][0].isoformat(timespec="minutes"), "days": out_days, "hourly_heat": prof,
           "legionella": legionella}
    if model:
        res.update({"cop_heating": round(1 / model["k_ch"], 2), "cop_dhw": round(1 / model["k_dhw"], 2),
                    "house_kwh_day": round(sum(model["house"]) * 2, 1), "fit_halfhours": len(y)})
    json.dump(res, open(OUT, "w"), indent=1)
    print(f"Heat pump: {len(out_days)} days, {len(legionella)} legionella cycles"
          + (f", COP heating {res['cop_heating']}, hot water {res['cop_dhw']}, house {res['house_kwh_day']} kWh/day" if model else ""))


if __name__ == "__main__":
    main()
