#!/usr/bin/env python3
"""Fetch half-hourly usage + tariff rates from the Octopus API and write docs/data.json.

Env vars:
  OCTOPUS_API_KEY   (required) personal API key from octopus.energy/dashboard/new/accounts/personal-details/api-access
  OCTOPUS_ACCOUNT   (required) account number, e.g. A-1234ABCD
  START_DATE        (optional) first day to track, YYYY-MM-DD. Default: start of the current import agreement.
  FLAT_RATE         (optional) comparison rate in p/kWh inc VAT. Default 26.9 (the page lets you change it too).
"""
import base64, bisect, json, os, sys, urllib.parse, urllib.request
from collections import OrderedDict
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

API = "https://api.octopus.energy/v1"
LONDON = ZoneInfo("Europe/London")
KEY = os.environ["OCTOPUS_API_KEY"]
ACCOUNT = os.environ["OCTOPUS_ACCOUNT"]
FLAT_RATE = float(os.environ.get("FLAT_RATE") or 26.9)
OUT = os.path.join(os.path.dirname(__file__), "..", "docs", "data.json")


def get(url, params=None, auth=True):
    if params:
        url += ("&" if "?" in url else "?") + urllib.parse.urlencode(params)
    req = urllib.request.Request(url)
    if auth:
        req.add_header("Authorization", "Basic " + base64.b64encode(f"{KEY}:".encode()).decode())
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.load(r)


def get_all(url, params=None, auth=True):
    out, data = [], get(url, params, auth)
    while True:
        out += data["results"]
        if not data.get("next"):
            return out
        data = get(data["next"], auth=auth)


def parse(ts):
    return datetime.fromisoformat(ts.replace("Z", "+00:00")) if ts else None


_coords = {}


def coords(postcode):
    if postcode not in _coords:
        pc = get(f"https://api.postcodes.io/postcodes/{urllib.parse.quote(postcode)}", auth=False)["result"]
        _coords[postcode] = (round(pc["latitude"], 2), round(pc["longitude"], 2))
    return _coords[postcode]


def typical_temps(postcode, years=(2021, 2025)):
    """Average daily mean temperature for each calendar day (MM-DD) over recent years, for annual projections."""
    try:
        lat, lon = coords(postcode)
        a = get("https://archive-api.open-meteo.com/v1/archive",
                {"latitude": lat, "longitude": lon, "daily": "temperature_2m_mean", "timezone": "Europe/London",
                 "start_date": f"{years[0]}-01-01", "end_date": f"{years[1]}-12-31"}, auth=False)
        by = {}
        for day, t in zip(a["daily"]["time"], a["daily"]["temperature_2m_mean"]):
            if t is not None and day[5:] != "02-29":
                by.setdefault(day[5:], []).append(t)
        return {k: round(sum(v) / len(v), 2) for k, v in sorted(by.items())}
    except Exception as e:
        print(f"Typical-year temperature lookup failed: {e}")
        return {}


def daily_temps(postcode, start, now):
    """Daily mean outside temperature (°C) near the property, keyed by local date.
    Location comes from the account postcode and is never written out. Failures return {}."""
    if not postcode:
        print("No postcode on the account; skipping temperatures.")
        return {}
    try:
        lat, lon = coords(postcode)
        out = {}
        first, last = start.astimezone(LONDON).date(), now.astimezone(LONDON).date()
        archive_end = last - timedelta(days=6)
        common = {"latitude": lat, "longitude": lon, "daily": "temperature_2m_mean", "timezone": "Europe/London"}
        if archive_end >= first:
            a = get("https://archive-api.open-meteo.com/v1/archive",
                    {**common, "start_date": first.isoformat(), "end_date": archive_end.isoformat()}, auth=False)
            out.update(zip(a["daily"]["time"], a["daily"]["temperature_2m_mean"]))
        f = get("https://api.open-meteo.com/v1/forecast", {**common, "past_days": 14, "forecast_days": 1}, auth=False)
        for day, t in zip(f["daily"]["time"], f["daily"]["temperature_2m_mean"]):
            if out.get(day) is None:
                out[day] = t
        out = {k: round(v, 1) for k, v in out.items() if v is not None}
        print(f"Temperatures for {len(out)} days.")
        return out
    except Exception as e:
        print(f"Temperature lookup failed: {e}")
        return {}


def write_history(mp, agreements, postcode, now):
    """Daily kWh and temperature since the first agreement at this property, plus a typical year of
    temperatures. Used for the design comparison (summer baseline and annual projection)."""
    try:
        first = parse(agreements[0]["valid_from"])
        readings = []
        for m in mp["meters"]:
            r = get_all(f"{API}/electricity-meter-points/{mp['mpan']}/meters/{m['serial_number']}/consumption/",
                        {"period_from": first.isoformat(), "page_size": 25000, "order_by": "period"})
            if len(r) > len(readings):
                readings = r
        days = {}
        for r in readings:
            day = parse(r["interval_start"]).astimezone(LONDON).date().isoformat()
            d = days.setdefault(day, [0.0, 0])
            d[0] += r["consumption"]
            d[1] += 1
        temps = daily_temps(postcode, first, now) if postcode else {}
        out = [{"date": k, "kwh": round(v[0], 2), **({"temp_c": temps[k]} if k in temps else {})}
               for k, v in sorted(days.items()) if v[1] >= 46]
        with open(os.path.join(os.path.dirname(OUT), "history.json"), "w") as f:
            json.dump({"updated": now.isoformat(timespec="minutes"), "days": out,
                       "typical_temps": typical_temps(postcode) if postcode else {}}, f)
        print(f"History: {len(out)} days from {out[0]['date'] if out else '-'}.")
    except Exception as e:
        print(f"History failed: {e}")


def main():
    acct = get(f"{API}/accounts/{ACCOUNT}/")
    now = datetime.now(timezone.utc)

    def active(a):
        return parse(a["valid_from"]) <= now and (not a["valid_to"] or parse(a["valid_to"]) > now)

    # Accounts keep old properties after a move. Prefer the property you haven't moved out of,
    # then the import meter point with an agreement running today, then the most recently moved-into.
    candidates = []
    for prop in acct["properties"]:
        print(f"Property moved_in={prop.get('moved_in_at')} moved_out={prop.get('moved_out_at')}")
        for mp in prop["electricity_meter_points"]:
            ags = mp.get("agreements", [])
            print(f"  meter point ...{mp['mpan'][-4:]} export={mp.get('is_export')} "
                  + ", ".join(f"{a['tariff_code']} {a['valid_from'][:10]}→{(a['valid_to'] or 'open')[:10]}" for a in ags))
            if mp.get("is_export"):
                continue
            score = (prop.get("moved_out_at") is None, any(active(a) for a in ags), prop.get("moved_in_at") or "")
            candidates.append((score, mp, prop.get("postcode")))
    if not candidates:
        sys.exit("No electricity import meter point found on this account.")
    _, mp, postcode = max(candidates, key=lambda c: c[0])
    mpan = mp["mpan"]
    agreements = sorted(mp["agreements"], key=lambda a: a["valid_from"])
    print(f"Using meter point ...{mpan[-4:]}")

    start_env = os.environ.get("START_DATE")
    if start_env:
        start = datetime.fromisoformat(start_env).replace(tzinfo=LONDON).astimezone(timezone.utc)
    else:
        current = [a for a in agreements if active(a)]
        start = parse((current or agreements)[-1]["valid_from"])

    # Pick the meter serial that actually returns data (smart meter swaps leave stale serials).
    readings = []
    for m in mp["meters"]:
        r = get_all(f"{API}/electricity-meter-points/{mpan}/meters/{m['serial_number']}/consumption/",
                    {"period_from": start.isoformat(), "page_size": 25000, "order_by": "period"})
        if len(r) > len(readings):
            readings = r
    if not readings:
        sys.exit("No consumption data returned yet.")

    # Unit rates for every agreement overlapping the window.
    rates = []  # (from, to, p/kWh inc VAT)
    standing = []  # (from, to, p/day inc VAT)
    for a in agreements:
        a_from, a_to = parse(a["valid_from"]), parse(a["valid_to"]) or now + timedelta(days=1)
        if a_to <= start:
            continue
        code = a["tariff_code"]               # e.g. E-1R-COSY-FIX-12M-25-07-01-C
        product = code[5:-2]                  # COSY-FIX-12M-25-07-01
        rs = get_all(f"{API}/products/{product}/electricity-tariffs/{code}/standard-unit-rates/",
                     {"period_from": max(a_from, start).isoformat(), "period_to": a_to.isoformat(),
                      "page_size": 1500}, auth=False)
        for r in rs:
            if r.get("payment_method") not in (None, "DIRECT_DEBIT"):
                continue
            f, t = parse(r["valid_from"]), parse(r["valid_to"]) or a_to
            rates.append((max(f, a_from), min(t, a_to), r["value_inc_vat"]))
        try:
            sc = get_all(f"{API}/products/{product}/electricity-tariffs/{code}/standing-charges/",
                         {"period_from": max(a_from, start).isoformat(), "period_to": a_to.isoformat()}, auth=False)
        except Exception as e:  # standing charge is a nice-to-have; never fail the run over it
            print(f"Standing charge lookup failed for {code}: {e}")
            sc = []
        for r in sc:
            if r.get("payment_method") not in (None, "DIRECT_DEBIT"):
                continue
            f, t = parse(r["valid_from"]), parse(r["valid_to"]) or a_to
            standing.append((max(f, a_from), min(t, a_to), r["value_inc_vat"]))
    rates.sort()

    def standing_on(day):
        noon = datetime.fromisoformat(day + "T12:00").replace(tzinfo=LONDON).astimezone(timezone.utc)
        for f, t, v in standing:
            if f <= noon < t:
                return v
        return None
    starts = [r[0] for r in rates]

    def rate_at(ts):
        i = bisect.bisect_right(starts, ts) - 1
        if i >= 0 and rates[i][0] <= ts < rates[i][1]:
            return rates[i][2]
        return None

    days = OrderedDict()
    missing = 0
    for r in readings:
        ts = parse(r["interval_start"])
        p = rate_at(ts.astimezone(timezone.utc))
        if p is None:
            missing += 1
            continue
        day = ts.astimezone(LONDON).date().isoformat()
        d = days.setdefault(day, {"date": day, "kwh": 0.0, "cost_p": 0.0, "slots": 0, "hh": [0.0] * 48})
        local = ts.astimezone(LONDON)
        d["hh"][local.hour * 2 + local.minute // 30] += r["consumption"]
        d["kwh"] += r["consumption"]
        d["cost_p"] += r["consumption"] * p
        d["slots"] += 1

    temps = daily_temps(postcode, start, now)

    # Drop a trailing partial day (data usually lands a day late).
    out_days = sorted((d for d in days.values() if d["slots"] >= 46), key=lambda d: d["date"])
    for d in out_days:
        d["kwh"] = round(d["kwh"], 3)
        d["cost_p"] = round(d["cost_p"], 2)
        sc = standing_on(d["date"])
        if sc is not None:
            d["standing_p"] = round(sc, 2)
        d["hh"] = [round(x, 3) for x in d["hh"]]
        t = temps.get(d["date"])
        if t is not None:
            d["temp_c"] = t

    # Unit rate for each half hour of the latest day, to colour the daily profile by price band.
    slot_rates = []
    if out_days:
        base = datetime.fromisoformat(out_days[-1]["date"]).replace(tzinfo=LONDON)
        for i in range(48):
            r = rate_at((base + timedelta(minutes=30 * i)).astimezone(timezone.utc))
            slot_rates.append(round(r, 2) if r is not None else None)

    with open(OUT, "w") as f:
        json.dump({"updated": now.isoformat(timespec="minutes"), "flat_rate": FLAT_RATE,
                   "tariff": (([a for a in agreements if active(a)] or agreements)[-1])["tariff_code"], "slot_rates": slot_rates, "days": out_days}, f, indent=1)
    write_history(mp, agreements, postcode, now)
    print(f"Tracking from {start.date()}, {len(readings)} half-hours read.")
    print(f"Wrote {len(out_days)} days ({missing} half-hours without a rate skipped).")


if __name__ == "__main__":
    main()
