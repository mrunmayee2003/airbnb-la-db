"""The application query workload (proposal section E), shared by the Streamlit
app and the experiment runner so the SQL that is demoed is the SQL that is analyzed.

Parameters use PyMySQL's %(name)s style and are sent as values, never string-built.
"""
from dataclasses import dataclass, field
from decimal import Decimal

import pandas as pd

import config

SNAPSHOT = config.SNAPSHOT_DATE  # set SNAPSHOT_DATE env var if you use a different snapshot


@dataclass
class Query:
    qid: str
    title: str
    question: str
    operations: str
    sql: str
    defaults: dict = field(default_factory=dict)


QUERIES = {q.qid: q for q in [
    Query("Q1", "Listing calendar",
          "What are a selected listing's availability and nightly prices for the next 90 days?",
          "Equality + date-range filter on the clustered PK; ORDER BY date",
          """
SELECT cal_date, available, price, minimum_nights
FROM calendar
WHERE listing_id = %(listing_id)s
  AND cal_date BETWEEN %(start)s AND DATE_ADD(%(start)s, INTERVAL 89 DAY)
ORDER BY cal_date""",
          {"listing_id": None, "start": SNAPSHOT}),

    Query("Q2", "Neighbourhood mix",
          "Which neighbourhoods have the highest share of entire-home listings?",
          "Join; conditional aggregation; GROUP BY; HAVING; ORDER BY",
          """
SELECT n.name AS neighbourhood, n.group_name,
       COUNT(*)                                        AS listings,
       SUM(l.room_type = 'Entire home/apt')            AS entire_home,
       ROUND(100 * AVG(l.room_type = 'Entire home/apt'), 1) AS pct_entire_home
FROM listing l
JOIN neighbourhood n ON n.neighbourhood_id = l.neighbourhood_id
GROUP BY n.neighbourhood_id
HAVING COUNT(*) >= %(min_listings)s
ORDER BY pct_entire_home DESC, listings DESC
LIMIT %(limit)s""",
          {"min_listings": 50, "limit": 25}),

    Query("Q3", "Host concentration",
          "Which 20 hosts have the most listings in the loaded Los Angeles snapshot?",
          "Join; COUNT; GROUP BY (FK index on listing.host_id); ORDER BY count; LIMIT",
          """
SELECT h.host_id, h.host_since, h.is_superhost,
       COUNT(l.listing_id)      AS la_listings,
       h.source_listings_count  AS source_listings_count
FROM listing l
JOIN host h ON h.host_id = l.host_id
GROUP BY h.host_id
ORDER BY la_listings DESC
LIMIT %(limit)s""",
          {"limit": 20}),

    Query("Q4", "Date availability by neighbourhood (all)",
          "How many listings are marked available on a given date, by neighbourhood?",
          "Single-date filter on calendar; join; GROUP BY; ORDER BY",
          """
SELECT n.name AS neighbourhood, COUNT(*) AS listings_marked_available
FROM calendar c
JOIN listing l       ON l.listing_id = c.listing_id
JOIN neighbourhood n ON n.neighbourhood_id = l.neighbourhood_id
WHERE c.cal_date = %(date)s
  AND c.available = 1
GROUP BY n.name
ORDER BY listings_marked_available DESC""",
          {"date": "2026-12-31"}),

    Query("Q4b", "Date availability (one neighbourhood)",
          "How many listings in one neighbourhood are marked available on a given date?",
          "Selective neighbourhood filter; join order; index nested-loop via calendar PK",
          """
SELECT n.name AS neighbourhood, COUNT(*) AS listings_marked_available
FROM calendar c
JOIN listing l       ON l.listing_id = c.listing_id
JOIN neighbourhood n ON n.neighbourhood_id = l.neighbourhood_id
WHERE c.cal_date = %(date)s
  AND c.available = 1
  AND n.name = %(nbhd)s
GROUP BY n.name""",
          {"date": "2026-12-31", "nbhd": "Venice"}),

    Query("Q5", "Stay finder",
          "Which listings in a neighbourhood are marked available on a selected night, cheapest first?",
          "Date + neighbourhood + capacity filters; 3-way join; ORDER BY price; LIMIT",
          """
SELECT l.listing_id, l.name, l.room_type, l.accommodates, l.review_score,
       c.price AS nightly_price, c.minimum_nights
FROM neighbourhood n
JOIN listing l  ON l.neighbourhood_id = n.neighbourhood_id
JOIN calendar c ON c.listing_id = l.listing_id AND c.cal_date = %(date)s
WHERE n.name = %(nbhd)s
  AND c.available = 1
  AND l.accommodates >= %(guests)s
ORDER BY c.price IS NULL, c.price
LIMIT %(limit)s""",
          {"date": "2026-10-16", "nbhd": "Venice", "guests": 2, "limit": 25}),

    Query("Q6", "Most-reviewed listings (12 months)",
          "Which listings received the most reviews in the 12 months before the snapshot, and are they licensed?",
          "Date-range filter on review; join; GROUP BY; ORDER BY count; NULL predicate",
          """
SELECT l.listing_id, l.name, n.name AS neighbourhood,
       COUNT(*)                AS reviews_12m,
       (l.license IS NOT NULL) AS has_license
FROM review r
JOIN listing l       ON l.listing_id = r.listing_id
JOIN neighbourhood n ON n.neighbourhood_id = l.neighbourhood_id
WHERE r.review_date >= DATE_SUB(%(snapshot)s, INTERVAL 12 MONTH)
  AND r.review_date <  %(snapshot)s
GROUP BY l.listing_id
ORDER BY reviews_12m DESC
LIMIT %(limit)s""",
          {"snapshot": SNAPSHOT, "limit": 25}),

    Query("Q6b", "Licensing among recently reviewed listings",
          "Of listings reviewed in the last 12 months, how many have a non-NULL license, by room type?",
          "Semi-join (EXISTS); IS NULL predicate; conditional aggregation",
          """
SELECT l.room_type,
       COUNT(*)                     AS reviewed_listings,
       SUM(l.license IS NOT NULL)   AS with_license,
       ROUND(100 * AVG(l.license IS NOT NULL), 1) AS pct_with_license
FROM listing l
WHERE EXISTS (SELECT 1 FROM review r
              WHERE r.listing_id = l.listing_id
                AND r.review_date >= DATE_SUB(%(snapshot)s, INTERVAL 12 MONTH)
                AND r.review_date <  %(snapshot)s)
GROUP BY l.room_type
ORDER BY reviewed_listings DESC""",
          {"snapshot": SNAPSHOT}),

    Query("Q7", "Amenity search",
          "Which highly rated listings in a neighbourhood provide a selected amenity?",
          "M:N join through listing_amenity; multiple predicates; ORDER BY rating, price",
          """
SELECT l.listing_id, l.name, l.room_type, l.review_score, l.price
FROM amenity a
JOIN listing_amenity la ON la.amenity_id = a.amenity_id
JOIN listing l          ON l.listing_id = la.listing_id
JOIN neighbourhood n    ON n.neighbourhood_id = l.neighbourhood_id
WHERE a.amenity_name = %(amenity)s
  AND n.name = %(nbhd)s
  AND l.review_score >= %(min_score)s
ORDER BY l.review_score DESC, l.price
LIMIT %(limit)s""",
          {"amenity": "Pool", "nbhd": "Venice", "min_score": Decimal("4.8"), "limit": 50}),

    Query("Q7b", "Amenity search (citywide)",
          "Which highly rated listings anywhere in LA provide a selected amenity?",
          "Same as Q7 without the neighbourhood predicate (selectivity now comes from the amenity)",
          """
SELECT l.listing_id, l.name, l.room_type, l.review_score, l.price
FROM amenity a
JOIN listing_amenity la ON la.amenity_id = a.amenity_id
JOIN listing l          ON l.listing_id = la.listing_id
WHERE a.amenity_name = %(amenity)s
  AND l.review_score >= %(min_score)s
ORDER BY l.review_score DESC, l.price
LIMIT %(limit)s""",
          {"amenity": "Pool", "min_score": Decimal("4.8"), "limit": 50}),
]}

# Simulated administrative update (local project DB only, never Airbnb).
UPDATE_CALENDAR = """
UPDATE calendar SET price = %(price)s, available = %(available)s
WHERE listing_id = %(listing_id)s AND cal_date = %(date)s"""


def run(conn, sql_or_qid, params=None) -> pd.DataFrame:
    sql = QUERIES[sql_or_qid].sql if sql_or_qid in QUERIES else sql_or_qid
    p = dict(QUERIES[sql_or_qid].defaults) if sql_or_qid in QUERIES else {}
    p.update(params or {})
    with conn.cursor() as cur:
        cur.execute(sql, p)
        cols = [d[0] for d in cur.description] if cur.description else []
        return pd.DataFrame(cur.fetchall(), columns=cols)


def explain(conn, qid, params=None, analyze=True) -> str:
    q = QUERIES[qid]
    p = dict(q.defaults)
    p.update(params or {})
    with conn.cursor() as cur:
        cur.execute(("EXPLAIN ANALYZE " if analyze else "EXPLAIN FORMAT=TREE ") + q.sql, p)
        return "\n".join(r[0] for r in cur.fetchall())


def render(qid, params=None) -> str:
    """SQL with literal parameter values, for display and for the report."""
    import pymysql.converters as conv
    q = QUERIES[qid]
    p = dict(q.defaults)
    p.update(params or {})
    return q.sql.strip() % {k: conv.escape_item(v, "utf8mb4") for k, v in p.items()}


# ---- small lookups used by the UI and experiments
def neighbourhoods(conn):
    return run(conn, """SELECT n.name, COUNT(l.listing_id) AS listings
                        FROM neighbourhood n LEFT JOIN listing l USING (neighbourhood_id)
                        GROUP BY n.neighbourhood_id ORDER BY listings DESC""")


def amenities(conn, top=300):
    return run(conn, f"""SELECT a.amenity_name, COUNT(*) AS listings
                         FROM listing_amenity la JOIN amenity a USING (amenity_id)
                         GROUP BY a.amenity_id ORDER BY listings DESC LIMIT {int(top)}""")


def calendar_range(conn):
    df = run(conn, "SELECT MIN(cal_date) AS lo, MAX(cal_date) AS hi FROM calendar")
    return df.iloc[0]["lo"], df.iloc[0]["hi"]
