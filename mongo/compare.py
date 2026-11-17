"""MongoDB vs MySQL - the three representative operations from the proposal,
plus an update-consistency illustration.

  1. Listing detail page        Mongo: one find_one         MySQL: 3 queries (joins)
  2. Amenity search             Mongo: multikey array index  MySQL: junction join (Q7)
  3. Avg price by neighbourhood Mongo: aggregation pipeline  MySQL: join + GROUP BY
  4. Host attribute update      Mongo: update_many on N docs MySQL: 1 row (no-op values)

Each operation: 1 warm-up + 5 timed runs, median reported. For a real MongoDB
server the winning plan and docs/keys examined come from explain().
Writes results/mongo_comparison.json.
Usage: python mongo/compare.py [--nbhd Venice --amenity Pool]
"""
import argparse
import json
import statistics
import sys
import time
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import config  # noqa: E402
import workload as W  # noqa: E402
from mongo.load_mongo import get_collection  # noqa: E402


def timed(fn, runs=5):
    fn()  # warm-up
    times, out = [], None
    for _ in range(runs):
        t0 = time.perf_counter()
        out = fn()
        times.append((time.perf_counter() - t0) * 1000)
    return round(statistics.median(times), 3), out


def mongo_explain(coll, filt, sort=None, limit=0):
    try:
        cur = coll.find(filt)
        if sort:
            cur = cur.sort(sort)
        if limit:
            cur = cur.limit(limit)
        ex = cur.explain()
        es = ex.get("executionStats", {})
        stage = ex["queryPlanner"]["winningPlan"]
        stages = []
        while stage:
            stages.append(stage.get("stage") + (f"[{stage['indexName']}]" if "indexName" in stage else ""))
            stage = stage.get("inputStage")
        return {"winning_plan": " > ".join(stages), "docs_examined": es.get("totalDocsExamined"),
                "keys_examined": es.get("totalKeysExamined"), "returned": es.get("nReturned")}
    except Exception as e:  # mongomock has no explain
        return {"explain": f"unavailable ({type(e).__name__})"}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--nbhd", default="Venice")
    ap.add_argument("--amenity", default="Pool")
    ap.add_argument("--min-score", default="4.8")
    ap.add_argument("--mock", action="store_true")
    a = ap.parse_args()

    coll = get_collection(a.mock)
    if a.mock and coll.estimated_document_count() == 0:
        from mongo.load_mongo import load
        load(coll)
    conn = config.mysql_conn()
    res = {}

    # pick a listing that has reviews and amenities
    lid = W.run(conn, """SELECT l.listing_id FROM listing l JOIN review r USING (listing_id)
                         GROUP BY l.listing_id ORDER BY COUNT(*) DESC LIMIT 1""").iloc[0, 0]
    lid = int(lid)

    # 1. listing detail
    def mysql_detail():
        d = W.run(conn, """SELECT l.*, h.host_since, h.is_superhost, n.name AS neighbourhood, n.group_name
                           FROM listing l JOIN host h USING (host_id) JOIN neighbourhood n USING (neighbourhood_id)
                           WHERE l.listing_id = %(id)s""", {"id": lid})
        am = W.run(conn, """SELECT a.amenity_name FROM listing_amenity la JOIN amenity a USING (amenity_id)
                            WHERE la.listing_id = %(id)s""", {"id": lid})
        rv = W.run(conn, """SELECT review_id, review_date, reviewer_id, comments FROM review
                            WHERE listing_id = %(id)s ORDER BY review_date DESC LIMIT 20""", {"id": lid})
        return len(d), len(am), len(rv)
    my_ms, my_out = timed(mysql_detail)
    mg_ms, doc = timed(lambda: coll.find_one({"_id": lid}))
    res["1_listing_detail"] = {
        "listing_id": lid,
        "mysql": {"median_ms": my_ms, "round_trips": 3, "rows": dict(zip(["listing", "amenities", "reviews"], my_out))},
        "mongo": {"median_ms": mg_ms, "round_trips": 1, "amenities": len(doc["amenities"]),
                  "embedded_reviews": len(doc["recent_reviews"]),
                  "plan": mongo_explain(coll, {"_id": lid})}}

    # 2. amenity search
    score = Decimal(a.min_score)
    my_ms, my_df = timed(lambda: W.run(conn, "Q7", {"amenity": a.amenity, "nbhd": a.nbhd, "min_score": score}))
    filt = {"amenities": a.amenity, "neighbourhood.name": a.nbhd, "review_score": {"$gte": float(score)}}
    sort = [("review_score", -1), ("price", 1)]
    mg_ms, mg_docs = timed(lambda: list(coll.find(filt, {"name": 1, "review_score": 1, "price": 1})
                                        .sort(sort).limit(50)))
    res["2_amenity_search"] = {
        "params": {"amenity": a.amenity, "nbhd": a.nbhd, "min_score": a.min_score},
        "mysql": {"median_ms": my_ms, "rows": len(my_df)},
        "mongo": {"median_ms": mg_ms, "rows": len(mg_docs), "plan": mongo_explain(coll, filt, sort, 50)},
        "same_listings": sorted(map(int, my_df["listing_id"])) == sorted(d["_id"] for d in mg_docs)}

    # 3. average price by neighbourhood
    sql = """SELECT n.name AS neighbourhood, COUNT(*) AS listings, ROUND(AVG(l.price), 2) AS avg_price
             FROM listing l JOIN neighbourhood n USING (neighbourhood_id)
             WHERE l.price IS NOT NULL GROUP BY n.neighbourhood_id ORDER BY avg_price DESC"""
    my_ms, my_df = timed(lambda: W.run(conn, sql))
    pipeline = [{"$match": {"price": {"$ne": None}}},
                {"$group": {"_id": "$neighbourhood.name", "listings": {"$sum": 1},
                            "avg_price": {"$avg": "$price"}}},
                {"$sort": {"avg_price": -1}}]
    mg_ms, mg_rows = timed(lambda: list(coll.aggregate(pipeline)))
    top_match = (my_df.iloc[0]["neighbourhood"] == mg_rows[0]["_id"]) if len(mg_rows) else False
    res["3_avg_price_by_neighbourhood"] = {
        "mysql": {"median_ms": my_ms, "groups": len(my_df)},
        "mongo": {"median_ms": mg_ms, "groups": len(mg_rows), "pipeline": pipeline},
        "same_top_neighbourhood": bool(top_match)}

    # 4. update consistency: host attributes are duplicated in every listing document
    host = W.run(conn, """SELECT host_id, is_superhost, COUNT(*) AS n FROM listing JOIN host USING (host_id)
                          GROUP BY host_id ORDER BY n DESC LIMIT 1""").iloc[0]
    hid, sup = int(host["host_id"]), host["is_superhost"]
    with conn.cursor() as cur:   # value set to itself -> no data change
        cur.execute("UPDATE host SET is_superhost = is_superhost WHERE host_id = %s", (hid,))
        mysql_rows = cur.rowcount
    cur_flag = None if sup is None or sup != sup else bool(sup)
    r = coll.update_many({"host.host_id": hid}, {"$set": {"host.is_superhost": cur_flag}})
    res["4_update_host_attribute"] = {
        "host_id": hid, "mysql_rows_matched": 1 if mysql_rows == 0 else mysql_rows,
        "mongo_documents_matched": r.matched_count,
        "note": "Same no-op value written; shows how many copies must stay consistent."}

    config.RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    out = config.RESULTS_DIR / "mongo_comparison.json"
    out.write_text(json.dumps(res, indent=2, default=str))
    for k, v in res.items():
        print(k, json.dumps(v, default=str)[:300])
    print(f"Wrote {out}")


if __name__ == "__main__":
    main()
