#!/usr/bin/env python3
"""
Kebab Oracle — should you walk to the Döner Laden?

Pulls community Döner prices from kebabprice.de's public Firestore database,
joins them against Google Maps ratings (optional), and prints a ranked
GO / SKIP list for a given starting location in Berlin.

The math (per shop):
    adj_rating = bayesian-smoothed Google rating
                 (so a 5.0 with 3 reviews can't beat a 4.4 with 800)
    value      = adj_rating / price        # quality you buy per euro
    score      = value - walk_minutes * K  # minus the cost of walking there
    GO if score >= threshold               # threshold = 75th percentile of
                                            # candidates, or --threshold

No third-party dependencies — standard library only.
"""

import argparse
import json
import math
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

FIRESTORE_QUERY_URL = (
    "https://firestore.googleapis.com/v1/projects/kebab-prices-2/"
    "databases/(default)/documents:runQuery"
)
PLACES_SEARCH_URL = "https://places.googleapis.com/v1/places:searchText"

# --- model constants -------------------------------------------------------
PRIOR_REVIEWS = 50      # m: how many reviews before we trust a rating
PRIOR_RATING = 3.9      # C: Berlin Döner baseline rating
WALK_SPEED_M_PER_MIN = 83.3   # ~5 km/h
DETOUR_FACTOR = 1.3     # straight-line distance is optimistic
WALK_COST_K = 0.015     # how much one minute of walking "costs" in score
MIN_UPVOTES_TRUSTED = 2  # below this, treat the price as less certain
PRICE_UNCERTAINTY = 1.10  # inflate unverified prices by 10%


def _post_json(url, payload, headers=None):
    """POST a JSON body and return the parsed JSON response."""
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("Content-Type", "application/json")
    for key, value in (headers or {}).items():
        req.add_header(key, value)
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _scalar(field):
    """Unwrap a Firestore typed value (e.g. {'doubleValue': 3.5}) to a scalar."""
    if not field:
        return None
    kind, raw = next(iter(field.items()))
    if kind == "nullValue":
        return None
    if kind == "integerValue":
        return int(raw)
    if kind == "doubleValue":
        return float(raw)
    if kind == "mapValue":
        return {k: _scalar(v) for k, v in raw.get("fields", {}).items()}
    return raw


def fetch_priced_kebabs():
    """Return every shop in Firestore that has a community price."""
    query = {
        "structuredQuery": {
            "from": [{"collectionId": "kebabs"}],
            "where": {
                "fieldFilter": {
                    "field": {"fieldPath": "bestPrice"},
                    "op": "GREATER_THAN",
                    "value": {"doubleValue": 0},
                }
            },
            "orderBy": [{"field": {"fieldPath": "bestPrice"},
                         "direction": "ASCENDING"}],
        }
    }
    rows = _post_json(FIRESTORE_QUERY_URL, query)
    shops = []
    for row in rows:
        doc = row.get("document")
        if not doc:
            continue
        f = doc["fields"]
        coords = _scalar(f.get("coordinates")) or {}
        shops.append({
            "name": _scalar(f.get("name")) or "Unknown",
            "price": _scalar(f.get("bestPrice")),
            "upvotes": _scalar(f.get("bestPriceUpvotes")) or 0,
            "district": _scalar(f.get("district")) or "",
            "address": _scalar(f.get("address")) or "",
            "lat": coords.get("lat"),
            "lng": coords.get("lng"),
            "hours": _scalar(f.get("openingHours")) or "",
            "status": _scalar(f.get("status")) or "",
        })
    return shops


def haversine_m(lat1, lng1, lat2, lng2):
    """Great-circle distance in metres."""
    r = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lng2 - lng1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def walk_minutes(distance_m):
    """Estimated walking minutes, padding straight-line distance for detours."""
    return (distance_m * DETOUR_FACTOR) / WALK_SPEED_M_PER_MIN


def fetch_maps_rating(shop, api_key):
    """Look up a shop's Google rating + review count by name and coords.

    Returns (rating, review_count) or (None, None) if not found.
    """
    payload = {
        "textQuery": f"{shop['name']} Döner Berlin",
        "maxResultCount": 1,
        "locationBias": {
            "circle": {
                "center": {"latitude": shop["lat"], "longitude": shop["lng"]},
                "radius": 200.0,
            }
        },
    }
    headers = {
        "X-Goog-Api-Key": api_key,
        "X-Goog-FieldMask": "places.rating,places.userRatingCount",
    }
    try:
        result = _post_json(PLACES_SEARCH_URL, payload, headers)
    except urllib.error.HTTPError as exc:
        print(f"  ! Maps lookup failed for {shop['name']}: {exc}", file=sys.stderr)
        return None, None
    places = result.get("places") or []
    if not places:
        return None, None
    return places[0].get("rating"), places[0].get("userRatingCount")


def adjusted_rating(rating, review_count):
    """Bayesian-smoothed rating that discounts thin review counts."""
    if rating is None or review_count is None:
        return None
    v = review_count
    return (v / (v + PRIOR_REVIEWS)) * rating + \
           (PRIOR_REVIEWS / (v + PRIOR_REVIEWS)) * PRIOR_RATING


def effective_price(shop):
    """Inflate the price when too few people have confirmed it."""
    if shop["upvotes"] < MIN_UPVOTES_TRUSTED:
        return shop["price"] * PRICE_UNCERTAINTY
    return shop["price"]


def score_shop(shop):
    """Compute value and final score for a shop. Mutates and returns it."""
    price = effective_price(shop)
    adj = shop.get("adj_rating")
    if adj is not None:
        shop["value"] = adj / price
    else:
        # No rating available: value is purely cheapness (rating assumed baseline).
        shop["value"] = PRIOR_RATING / price
    shop["score"] = shop["value"] - shop["walk_min"] * WALK_COST_K
    return shop


def percentile(values, pct):
    """Linear-interpolated percentile of a list (pct in 0..100)."""
    if not values:
        return 0.0
    ordered = sorted(values)
    k = (len(ordered) - 1) * (pct / 100.0)
    lo = math.floor(k)
    hi = math.ceil(k)
    if lo == hi:
        return ordered[int(k)]
    return ordered[lo] * (hi - k) + ordered[hi] * (k - lo)


def build_candidates(shops, start, max_walk):
    """Filter to open shops within walking range and attach distance/time."""
    candidates = []
    for shop in shops:
        if shop["lat"] is None or shop["lng"] is None:
            continue
        if shop["status"] and shop["status"] != "active":
            continue
        dist = haversine_m(start[0], start[1], shop["lat"], shop["lng"])
        mins = walk_minutes(dist)
        if mins > max_walk:
            continue
        shop["distance_m"] = dist
        shop["walk_min"] = mins
        candidates.append(shop)
    return candidates


def enrich_with_maps(candidates, api_key):
    """Attach Google ratings to each candidate (best-effort)."""
    for shop in candidates:
        rating, count = fetch_maps_rating(shop, api_key)
        shop["rating"] = rating
        shop["review_count"] = count
        shop["adj_rating"] = adjusted_rating(rating, count)


def render(candidates, threshold):
    """Print the ranked GO / SKIP table."""
    candidates.sort(key=lambda s: s["score"], reverse=True)
    print(f"\n{'':2} {'SHOP':<26} {'€':>5} {'RATING':>11} {'WALK':>6} "
          f"{'SCORE':>6}  DISTRICT")
    print("-" * 78)
    for shop in candidates:
        go = "✅" if shop["score"] >= threshold else "🟥"
        if shop.get("rating") is not None:
            rating = f"{shop['rating']:.1f}({shop['review_count']})"
        else:
            rating = "—"
        conf = "" if shop["upvotes"] >= MIN_UPVOTES_TRUSTED else "?"
        print(f"{go} {shop['name'][:26]:<26} "
              f"{shop['price']:>4.2f}{conf:<1} {rating:>11} "
              f"{shop['walk_min']:>4.0f}m {shop['score']:>6.3f}  {shop['district']}")
    print("-" * 78)
    print(f"GO threshold (score) = {threshold:.3f}   "
          f"€ marked '?' = price not yet confirmed by {MIN_UPVOTES_TRUSTED}+ people")


def parse_args():
    p = argparse.ArgumentParser(description="Should you walk to the Döner Laden?")
    p.add_argument("--lat", type=float, required=True, help="your latitude")
    p.add_argument("--lng", type=float, required=True, help="your longitude")
    p.add_argument("--max-walk", type=float, default=15.0,
                   help="max walking minutes to consider (default 15)")
    p.add_argument("--threshold", type=float, default=None,
                   help="fixed GO score; default = 75th percentile of candidates")
    p.add_argument("--maps-key", default=os.environ.get("GOOGLE_MAPS_API_KEY"),
                   help="Google Maps API key (or set GOOGLE_MAPS_API_KEY)")
    p.add_argument("--no-maps", action="store_true",
                   help="skip Google Maps; rank on price + confidence only")
    return p.parse_args()


def _force_utf8():
    """Windows consoles default to cp1252; emoji + Umlauts need UTF-8."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):
            pass


def main():
    _force_utf8()
    args = parse_args()
    start = (args.lat, args.lng)

    print("Fetching community Döner prices from kebabprice.de …", file=sys.stderr)
    shops = fetch_priced_kebabs()
    candidates = build_candidates(shops, start, args.max_walk)
    print(f"{len(shops)} priced shops; {len(candidates)} within "
          f"{args.max_walk:.0f} min walk.", file=sys.stderr)

    if not candidates:
        print("No open kebab shops within range. Sad.", file=sys.stderr)
        return

    use_maps = not args.no_maps and args.maps_key
    if use_maps:
        print("Looking up Google ratings …", file=sys.stderr)
        enrich_with_maps(candidates, args.maps_key)
    elif not args.no_maps:
        print("No Maps key found — ranking on price + confidence only "
              "(set GOOGLE_MAPS_API_KEY or pass --maps-key for ratings).",
              file=sys.stderr)

    for shop in candidates:
        score_shop(shop)

    if args.threshold is not None:
        threshold = args.threshold
    else:
        threshold = percentile([s["score"] for s in candidates], 75)

    render(candidates, threshold)


if __name__ == "__main__":
    main()
