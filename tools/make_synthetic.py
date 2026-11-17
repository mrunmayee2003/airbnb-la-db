"""Generate a SYNTHETIC dataset in the exact Inside Airbnb file format.

For testing the pipeline/experiments when the real snapshot is not at hand.
Never use these numbers in the report. Includes deliberate edge cases:
18-digit IDs, '$1,234.00' prices, blank calendar prices, multi-line comments
with tabs/quotes/backslashes, accent/case duplicate amenities, blank ratings,
orphan calendar/review rows, and a neighbourhood missing from neighbourhoods.csv.

Usage: python tools/make_synthetic.py --out data/synthetic_raw --listings 6000
"""
import argparse
import csv
import gzip
import json
from datetime import date, timedelta
from pathlib import Path

import numpy as np

NBHDS = ["Venice", "Hollywood", "Santa Monica", "Downtown", "Silver Lake", "Echo Park",
         "Malibu", "Long Beach", "Pasadena", "Koreatown", "West Hollywood", "Beverly Hills",
         "Culver City", "Mar Vista", "Palms", "Westwood", "Sawtelle", "Highland Park",
         "Los Feliz", "Burbank", "Glendale", "Inglewood", "Torrance", "Redondo Beach",
         "Manhattan Beach", "Hermosa Beach", "Encino", "Sherman Oaks", "Studio City",
         "North Hollywood", "Van Nuys", "Topanga", "Altadena", "El Segundo", "Playa del Rey"]
NBHDS += [f"Neighbourhood {i:03d}" for i in range(1, 86)]
GROUP = lambda n: ("Other Cities" if n in {"Santa Monica", "Malibu", "Long Beach", "Pasadena",
                   "West Hollywood", "Beverly Hills", "Culver City", "Burbank", "Glendale",
                   "Inglewood", "Torrance", "Redondo Beach", "Manhattan Beach",
                   "Hermosa Beach", "El Segundo"} else
                   "Unincorporated Areas" if n in {"Topanga", "Altadena"} else "City of Los Angeles")
AMENITIES = (["Wifi", "Kitchen", "Smoke alarm", "Essentials", "Hair dryer", "Hangers", "Iron",
              "Air conditioning", "Heating", "Dedicated workspace", "Free parking on premises",
              "Washer", "Dryer", "TV", "Refrigerator", "Microwave", "Coffee maker",
              "Hot water", "Carbon monoxide alarm", "Self check-in", "Pool", "Hot tub",
              "Gym", "EV charger", "Ocean view", "Pets allowed", "BBQ grill", "Patio or balcony",
              "Crib", "Fireplace", "Private pool", "Sauna", "Piano"] +
             [f"Brand {i} shampoo" for i in range(60)] + ["Café espresso machine"])
AMEN_P = np.array([0.97, 0.9, 0.95, 0.9, 0.8, 0.8, 0.7, 0.75, 0.8, 0.6, 0.45, 0.5, 0.5,
                   0.7, 0.75, 0.7, 0.6, 0.8, 0.85, 0.55, 0.06, 0.08, 0.05, 0.03, 0.04, 0.2,
                   0.15, 0.4, 0.1, 0.1, 0.02, 0.01, 0.005] + [0.01] * 60 + [0.005])
ROOM = ["Entire home/apt", "Private room", "Shared room", "Hotel room"]
ROOM_P = [0.68, 0.29, 0.02, 0.01]
WEIRD = ['Great place!\nWould stay again.', 'Host said "welcome" \\ tabs\there',
         'Línea con acentos — y emoji 🌴', 'Short.', 'Clean, quiet,\r\nclose to the beach.']


def money(x):
    return f"${x:,.2f}"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="data/synthetic_raw")
    ap.add_argument("--listings", type=int, default=6000)
    ap.add_argument("--days", type=int, default=365)
    ap.add_argument("--snapshot", default="2026-09-14")
    ap.add_argument("--blank-calendar-price", action="store_true",
                    help="mimic recent snapshots whose calendar price column is empty")
    ap.add_argument("--seed", type=int, default=551)
    a = ap.parse_args()
    rng = np.random.default_rng(a.seed)
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    snap = date.fromisoformat(a.snapshot)
    n = a.listings

    # neighbourhoods.csv (deliberately omits the last name)
    with open(out / "neighbourhoods.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["neighbourhood_group", "neighbourhood"])
        for nb in NBHDS[:-1]:
            w.writerow([GROUP(nb), nb])

    # listings
    zipf = 1 / np.arange(1, len(NBHDS) + 1) ** 0.9
    nb_idx = rng.choice(len(NBHDS), n, p=zipf / zipf.sum())
    ids = (rng.choice(10**6, n, replace=False) + 10**6).astype(np.int64)
    ids[n // 2:] = ids[n // 2:] + 1_100_000_000_000_000_000  # 18-19 digit modern IDs
    # ~55% of listings belong to single-listing hosts; the rest to a long tail
    # of multi-listing operators (Zipf), so host concentration (Q3) is realistic.
    pro_hosts = max(20, n // 40)
    pro = np.minimum(rng.zipf(1.3, n), pro_hosts) - 1
    single = np.arange(n) + pro_hosts
    host_rank = np.where(rng.random(n) < 0.55, single, pro)
    host_ids = (host_rank * 7919 + 1_000_003).astype(np.int64)
    room = rng.choice(ROOM, n, p=ROOM_P)
    base = {"Entire home/apt": 220, "Private room": 95, "Shared room": 45, "Hotel room": 180}
    price = np.array([base[r] for r in room]) * rng.lognormal(0, 0.55, n)
    price[nb_idx < 7] *= 1.4
    listing_price = {}
    cols = ["id", "listing_url", "name", "host_id", "host_name", "host_since",
            "host_is_superhost", "host_listings_count", "neighbourhood_cleansed",
            "neighbourhood_group_cleansed", "latitude", "longitude", "property_type",
            "room_type", "accommodates", "bedrooms", "amenities", "price",
            "review_scores_rating", "license", "number_of_reviews"]
    with gzip.open(out / "listings.csv.gz", "wt", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(cols)
        for i in range(n):
            nb = NBHDS[nb_idx[i]]
            amen = [AMENITIES[j] for j in np.nonzero(rng.random(len(AMENITIES)) < AMEN_P)[0]]
            if rng.random() < 0.02:
                amen += ["WiFi", "wifi "]  # case/space duplicates of "Wifi"
            if rng.random() < 0.01:
                amen += ["Cafe espresso machine"]  # accent duplicate
            hs = snap - timedelta(days=int(rng.integers(30, 5000)))
            score = "" if rng.random() < 0.18 else f"{min(5, rng.normal(4.75, 0.25)):.2f}"
            lic = "" if rng.random() < 0.45 else f"HSR{rng.integers(10**5, 10**6)}"
            p = None if rng.random() < 0.03 else round(float(price[i]), 2)
            listing_price[int(ids[i])] = p if p is not None else round(float(price[i]), 2)
            w.writerow([ids[i], f"https://www.airbnb.com/rooms/{ids[i]}",
                        f"Cozy {room[i].split()[0].lower()} in {nb}",
                        host_ids[i], "Host", hs.isoformat(),
                        rng.choice(["t", "f", ""], p=[0.3, 0.65, 0.05]),
                        int(rng.integers(1, 40)), nb, GROUP(nb),
                        f"{34.0 + rng.random() * 0.3:.6f}", f"{-118.5 + rng.random() * 0.4:.6f}",
                        "Entire rental unit", room[i], int(rng.integers(1, 9)),
                        "" if rng.random() < 0.1 else int(rng.integers(0, 6)),
                        json.dumps(amen, ensure_ascii=True),
                        "" if p is None else money(p), score, lic, int(rng.integers(0, 300))])
        # invalid row: unknown room type -> should be dropped by cleaning
        w.writerow([999, "", "bad", 1, "", "", "", "", "Venice", "", "", "", "", "Castle", 1,
                    1, "[]", "$10.00", "", "", 0])

    # calendar: one row per listing per day
    with gzip.open(out / "calendar.csv.gz", "wt", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["listing_id", "date", "available", "price", "adjusted_price",
                    "minimum_nights", "maximum_nights"])
        days = [snap + timedelta(d) for d in range(a.days)]
        busy = np.array([0.35 + 0.25 * (d.weekday() >= 4) + 0.3 * (d.month == 12 and d.day >= 24)
                         + 0.1 * (d.month in (6, 7, 8)) for d in days])
        for i in range(n):
            lid = int(ids[i])
            avail = rng.random(a.days) > np.clip(busy + rng.normal(0, 0.15), 0, 0.97)
            mult = 1 + 0.2 * np.array([d.weekday() >= 4 for d in days]) + 0.5 * (busy > 0.8)
            mn = int(rng.choice([1, 2, 3, 30]))
            for d, av, m in zip(days, avail, mult):
                pr = "" if a.blank_calendar_price else money(listing_price[lid] * m)
                w.writerow([lid, d.isoformat(), "t" if av else "f", pr, "", mn, 365])
        for d in days[:30]:  # orphan rows -> dropped
            w.writerow([42, d.isoformat(), "t", "$1.00", "", 1, 365])

    # reviews
    with gzip.open(out / "reviews.csv.gz", "wt", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["listing_id", "id", "date", "reviewer_id", "reviewer_name", "comments"])
        rid = 10_000_000
        for i in range(n):
            for _ in range(int(rng.negative_binomial(1, 0.03))):
                rid += int(rng.integers(1, 50))
                d = snap - timedelta(days=int(rng.exponential(500)))
                w.writerow([ids[i], rid, d.isoformat(), int(rng.integers(1, 10**9)),
                            "Reviewer Name", rng.choice(WEIRD) + "<br/>Thanks!"])
        w.writerow([42, 1, "2025-01-01", 5, "x", "orphan"])
    print(f"Synthetic Inside-Airbnb-format files written to {out}")


if __name__ == "__main__":
    main()
