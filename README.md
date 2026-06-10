# Kebab Oracle 🥙

Should you walk to the Döner Laden? This tool answers it with math.

It pulls community-reported Döner prices from
[kebabprice.de](https://kebabprice.de)'s public Firestore database, optionally
joins them against Google Maps ratings, and prints a ranked **GO / SKIP** list
for wherever you're standing in Berlin.

No third-party packages — Python 3.7+ standard library only.

## Usage

```bash
# Price + confidence only (no Maps key needed)
python kebab_oracle.py --lat 52.4990 --lng 13.4180 --max-walk 20

# With Google Maps ratings folded in
export GOOGLE_MAPS_API_KEY=your_key_here
python kebab_oracle.py --lat 52.4990 --lng 13.4180 --max-walk 20
```

### Options

| Flag | Meaning |
|------|---------|
| `--lat` / `--lng` | your current coordinates (required) |
| `--max-walk` | max walking minutes to consider (default 15) |
| `--threshold` | fixed GO score; default = 75th percentile of candidates |
| `--maps-key` | Google Maps API key (or set `GOOGLE_MAPS_API_KEY`) |
| `--no-maps` | skip Maps entirely, rank on price + confidence |

## The math

Per shop within walking range:

```
adj_rating = (v/(v+m))·rating + (m/(v+m))·C      # Bayesian smoothing
                                                  #  v = review count
                                                  #  m = 50 (prior strength)
                                                  #  C = 3.9 (Berlin baseline)
value      = adj_rating / price                   # quality bought per euro
score      = value − walk_minutes · K             # K = 0.015 walk penalty
GO if score >= threshold
```

- **Bayesian rating** stops a 5.0-with-3-reviews shop beating a 4.4-with-800.
- **Confidence gate:** prices upvoted by fewer than 2 people are inflated 10%
  and flagged with `?`.
- **Threshold** defaults to the 75th percentile of the candidates in range —
  i.e. "only walk if this place is top-quartile value right now" — so it
  self-calibrates instead of relying on a magic number. Override with
  `--threshold`.
- Closed/non-`active` shops and shops beyond `--max-walk` are dropped before
  scoring. Walking time = straight-line distance × 1.3 detour factor at 5 km/h.

## Data source

The site is a Firebase/Firestore-backed PWA with public read access. Data is
fetched directly from the Firestore REST API (project `kebab-prices-2`,
collection `kebabs`). No scraping, no auth. Each shop record carries `name`,
`address`, `district`, `coordinates`, `bestPrice`, `bestPriceUpvotes`,
`openingHours`, and `status`.

> Note: only ~25 of the ~900 mapped shops currently have community prices, so
> widen `--max-walk` if you get no candidates near you.

## Get a Google Maps API key

Enable the **Places API (New)** in a Google Cloud project and create an API
key. The tool uses `places:searchText` with a 200 m location bias around each
shop's coordinates, requesting only `rating` and `userRatingCount` (cheap
field mask). Without a key the tool still runs and ranks on price + confidence.
