"""MongoDB component - build one document per listing from the MySQL database.

Document shape (listing-centred; host + neighbourhood duplicated per listing,
amenities embedded as an array, at most 20 most recent reviews embedded):

{ _id: <listing_id>, name, room_type, accommodates, bedrooms, price, review_score, license,
  location: {type: "Point", coordinates: [lon, lat]},
  host: {host_id, host_since, is_superhost},
  neighbourhood: {name, group},
  amenities: ["Wifi", "Kitchen", ...],
  review_count: <total reviews in MySQL>,
  recent_reviews: [{review_id, date, reviewer_id, comments}, ...]  # capped at 20 }

MySQL stays the authoritative full review history.
Usage: python mongo/load_mongo.py [--subset 5000]
"""
import argparse
import sys
import time
from collections import defaultdict
from datetime import datetime, time as dtime
from pathlib import Path

import pymysql.cursors

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import config  # noqa: E402


def as_dt(d):
    return datetime.combine(d, dtime()) if d else None


def get_collection(mock=False):
    if mock:
        import mongomock
        return mongomock.MongoClient()[config.MONGO_DB]["listings"]
    from pymongo import MongoClient
    client = MongoClient(config.MONGO_URI, serverSelectionTimeoutMS=5000)
    client.admin.command("ping")
    return client[config.MONGO_DB]["listings"]


def build_documents(subset=None):
    conn = config.mysql_conn(cursorclass=pymysql.cursors.SSDictCursor)
    limit = f"LIMIT {int(subset)}" if subset else ""
    with conn.cursor() as cur:
        cur.execute(f"""
            SELECT l.*, h.host_since, h.is_superhost, n.name AS nb_name, n.group_name
            FROM listing l
            JOIN host h ON h.host_id = l.host_id
            JOIN neighbourhood n ON n.neighbourhood_id = l.neighbourhood_id
            ORDER BY l.listing_id {limit}""")
        listings = list(cur)
    ids = {r["listing_id"] for r in listings}

    amen = defaultdict(list)
    with conn.cursor() as cur:
        cur.execute("""SELECT la.listing_id, a.amenity_name FROM listing_amenity la
                       JOIN amenity a ON a.amenity_id = la.amenity_id""")
        for r in cur:
            if r["listing_id"] in ids:
                amen[r["listing_id"]].append(r["amenity_name"])

    reviews, counts = defaultdict(list), defaultdict(int)
    with conn.cursor() as cur:
        cur.execute("SELECT listing_id, COUNT(*) AS n FROM review GROUP BY listing_id")
        for r in cur:
            counts[r["listing_id"]] = r["n"]
    with conn.cursor() as cur:
        cur.execute(f"""
            SELECT listing_id, review_id, review_date, reviewer_id, comments FROM (
              SELECT r.*, ROW_NUMBER() OVER (PARTITION BY listing_id
                                             ORDER BY review_date DESC, review_id DESC) AS rn
              FROM review r) t
            WHERE rn <= {config.MONGO_RECENT_REVIEWS}""")
        for r in cur:
            if r["listing_id"] in ids:
                reviews[r["listing_id"]].append({
                    "review_id": r["review_id"], "date": as_dt(r["review_date"]),
                    "reviewer_id": r["reviewer_id"], "comments": r["comments"]})
    conn.close()

    f = lambda v: float(v) if v is not None else None
    for r in listings:
        lid = r["listing_id"]
        doc = {
            "_id": lid, "name": r["name"], "room_type": r["room_type"],
            "accommodates": r["accommodates"], "bedrooms": r["bedrooms"],
            "price": f(r["price"]), "review_score": f(r["review_score"]), "license": r["license"],
            "host": {"host_id": r["host_id"], "host_since": as_dt(r["host_since"]),
                     "is_superhost": None if r["is_superhost"] is None else bool(r["is_superhost"])},
            "neighbourhood": {"name": r["nb_name"], "group": r["group_name"]},
            "amenities": amen.get(lid, []),
            "review_count": counts.get(lid, 0),
            "recent_reviews": sorted(reviews.get(lid, []), key=lambda x: x["date"], reverse=True),
        }
        if r["latitude"] is not None and r["longitude"] is not None:
            doc["location"] = {"type": "Point", "coordinates": [f(r["longitude"]), f(r["latitude"])]}
        yield doc


def load(coll, subset=None, batch=2000):
    coll.drop()
    t0, buf, n = time.perf_counter(), [], 0
    for doc in build_documents(subset):
        buf.append(doc)
        if len(buf) >= batch:
            coll.insert_many(buf)
            n += len(buf)
            buf = []
    if buf:
        coll.insert_many(buf)
        n += len(buf)
    coll.create_index("amenities")                 # multikey index on the embedded array
    coll.create_index("neighbourhood.name")
    coll.create_index("host.host_id")
    print(f"Loaded {n:,} listing documents in {time.perf_counter() - t0:.1f}s")
    return n


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--subset", type=int, default=None, help="load only the first N listings")
    ap.add_argument("--mock", action="store_true", help="use in-memory mongomock (testing only)")
    a = ap.parse_args()
    coll = get_collection(a.mock)
    load(coll, a.subset)
    if not a.mock:
        stats = coll.database.command("collStats", "listings")
        print(f"collection size={stats['size'] / 2**20:.1f} MB, "
              f"avg doc={stats['avgObjSize']:.0f} B, indexes={stats['totalIndexSize'] / 2**20:.1f} MB")


if __name__ == "__main__":
    main()
