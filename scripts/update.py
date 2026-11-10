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
            candidates.append((score, mp))
    if not candidates:
        sys.exit("No electricity import meter point found on this account.")
    mp = max(candidates, key=lambda c: c[0])[1]
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
        d = days.setdefault(day, {"date": day, "kwh": 0.0, "cost_p": 0.0, "slots": 0})
        d["kwh"] += r["consumption"]
        d["cost_p"] += r["consumption"] * p
        d["slots"] += 1

    # Drop a trailing partial day (data usually lands a day late).
    out_days = sorted((d for d in days.values() if d["slots"] >= 46), key=lambda d: d["date"])
    for d in out_days:
        d["kwh"] = round(d["kwh"], 3)
        d["cost_p"] = round(d["cost_p"], 2)
        sc = standing_on(d["date"])
        if sc is not None:
            d["standing_p"] = round(sc, 2)

    with open(OUT, "w") as f:
        json.dump({"updated": now.isoformat(timespec="minutes"), "flat_rate": FLAT_RATE,
                   "tariff": (([a for a in agreements if active(a)] or agreements)[-1])["tariff_code"], "days": out_days}, f, indent=1)
    print(f"Tracking from {start.date()}, {len(readings)} half-hours read.")
    print(f"Wrote {len(out_days)} days ({missing} half-hours without a rate skipped).")


if __name__ == "__main__":
    main()
