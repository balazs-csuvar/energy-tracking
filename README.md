# Cosy tracker

A running tab of whether Octopus Cosy is beating a flat electricity rate. Each day a GitHub Action pulls your half-hourly usage and tariff rates from the Octopus API, prices every half hour, and commits the daily totals to `docs/data.json`. GitHub Pages serves `docs/index.html`, and you can change the comparison rate on the page itself.

Unit costs only; standing charges are left out.

## Setup

1. **Get your Octopus details.** Your API key and account number are at
   octopus.energy → Personal details → API access (the account number looks like `A-1234ABCD`).
2. **Add repo secrets.** Settings → Secrets and variables → Actions → *New repository secret*:
   - `OCTOPUS_API_KEY`
   - `OCTOPUS_ACCOUNT`
3. **Optional repo variables** (same page, *Variables* tab):
   - `START_DATE`: first day to track, e.g. `2026-10-07`. Defaults to the start of your current tariff.
   - `FLAT_RATE`: default comparison rate in p/kWh, e.g. `26.9`.
4. **Turn on Pages.** Settings → Pages → *Deploy from a branch* → `main`, folder `/docs`.
5. **Run it once.** Actions → *Update tariff tracker* → *Run workflow*. After that it runs every morning.

## Privacy

On a free GitHub plan, Pages sites are public, so anyone with the URL can see your daily kWh and costs. The API key is never published; it lives only in Actions secrets.

## Files

- `scripts/update.py`: fetches and aggregates the data (standard library only).
- `.github/workflows/update.yml`: daily schedule.
- `docs/index.html`: the tracker page.
- `docs/data.json`: generated data, seeded from an October CSV export.
