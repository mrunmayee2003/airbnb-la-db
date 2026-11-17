"""Inside Airbnb Los Angeles - DSCI 551 demo application.

Every number on screen comes from a live parameterized query against MySQL
(or MongoDB on the MongoDB page). Run:  streamlit run app/streamlit_app.py
"""
import csv
import inspect
import threading
import json
import sys
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

import altair as alt
import pandas as pd
import pymysql
import streamlit as st

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import config  # noqa: E402
import workload as W  # noqa: E402

st.set_page_config(page_title="Inside Airbnb LA - DSCI 551", layout="wide")
BAR = "#3b6fb6"  # one hue: every chart here is a single series
# Full-width argument differs by Streamlit version (width="stretch" is newer).
STRETCH = ({"width": "stretch"} if "width" in inspect.signature(st.altair_chart).parameters
           else {"use_container_width": True})


@st.cache_resource
def _local():
    return threading.local()


def conn():
    """One MySQL connection per script-run thread: overlapping reruns never share one."""
    tls = _local()
    c = getattr(tls, "c", None)
    if c is None or not c.open:
        c = tls.c = config.mysql_conn()
    return c


def reset_conn():
    c = getattr(_local(), "c", None)
    _local().c = None
    try:
        c.close()
    except Exception:  # noqa: BLE001
        pass


def floats(df):
    """MySQL DECIMAL arrives as Python Decimal; charts and tooltips want plain numbers."""
    for c in df.columns:
        nonnull = df[c].dropna()
        if len(nonnull) and isinstance(nonnull.iloc[0], Decimal):
            df[c] = pd.to_numeric(df[c], errors="coerce")
    return df


def run(qid, **params):
    try:
        return floats(W.run(conn(), qid, params))
    except (pymysql.err.InternalError, pymysql.err.OperationalError, pymysql.err.InterfaceError):
        reset_conn()          # broken/out-of-sync connection -> retry once on a fresh one
        return floats(W.run(conn(), qid, params))


def bar(df, x, y, title, horizontal=True, n=20):
    d = df.head(n)
    enc = dict(tooltip=list(d.columns))
    if horizontal:
        ch = alt.Chart(d).mark_bar(color=BAR, cornerRadiusEnd=4).encode(
            x=alt.X(f"{y}:Q", title=y.replace("_", " ")),
            y=alt.Y(f"{x}:N", sort="-x", title=None), **enc)
    else:
        ch = alt.Chart(d).mark_bar(color=BAR, cornerRadiusEnd=4).encode(
            x=alt.X(f"{x}:N", sort="-y", title=None),
            y=alt.Y(f"{y}:Q", title=y.replace("_", " ")), **enc)
    st.altair_chart(ch.properties(title=title, height=max(240, 22 * len(d))), **STRETCH)


def show_sql(qid, params):
    with st.expander("SQL executed"):
        st.code(W.render(qid, params), language="sql")


# ------------------------------------------------------------------ setup
try:
    try:
        nb_df = W.neighbourhoods(conn())
    except Exception:  # noqa: BLE001  stale connection from an interrupted run
        reset_conn()
        nb_df = W.neighbourhoods(conn())
    lo, hi = W.calendar_range(conn())
except Exception as e:  # noqa: BLE001
    st.error(f"Cannot reach MySQL ({e}). Check config.py / MYSQL_* environment variables.")
    st.stop()
NBHDS = nb_df["name"].tolist()
default_nb = NBHDS.index("Venice") if "Venice" in NBHDS else 0

page = st.sidebar.radio("Page", ["Neighbourhood explorer", "Stay finder",
                                 "Host & listing rankings", "Query lab", "MongoDB"])
st.sidebar.caption(f"Snapshot {config.SNAPSHOT_DATE} · calendar {lo} → {hi}")
st.sidebar.caption("Data: Inside Airbnb (insideairbnb.com), CC BY 4.0. Not affiliated with Airbnb. "
                   "Calendar flags mean *marked available/unavailable*, not bookings.")

# ------------------------------------------------------------------ pages
if page == "Neighbourhood explorer":
    st.header("Neighbourhood explorer")
    st.subheader("Q2 · Which neighbourhoods have the highest share of entire-home listings?")
    m = st.slider("Minimum listings in neighbourhood", 1, 500, 50)
    p = {"min_listings": m, "limit": 25}
    df = run("Q2", **p)
    c1, c2 = st.columns([3, 2])
    with c1:
        bar(df, "neighbourhood", "pct_entire_home", "Entire-home share (%)")
    with c2:
        st.dataframe(df, hide_index=True, **STRETCH)
    show_sql("Q2", p)

    st.subheader("Q4 · How many listings are marked available on a date, by neighbourhood?")
    d = st.date_input("Night", date(2026, 12, 31), min_value=lo, max_value=hi)
    p = {"date": d.isoformat()}
    df = run("Q4", **p)
    st.metric("Listings marked available citywide", f"{int(df['listings_marked_available'].sum()):,}")
    bar(df, "neighbourhood", "listings_marked_available", f"Top 20 neighbourhoods, {d}")
    show_sql("Q4", p)

elif page == "Stay finder":
    st.header("Stay finder")
    c1, c2, c3 = st.columns(3)
    nb = c1.selectbox("Neighbourhood", NBHDS, index=default_nb)
    night = c2.date_input("Night", min(hi, date(2026, 10, 16)), min_value=lo, max_value=hi)
    guests = c3.number_input("Guests", 1, 16, 2)
    st.subheader("Q5 · Listings marked available that night, cheapest first")
    p = {"date": night.isoformat(), "nbhd": nb, "guests": int(guests), "limit": 25}
    stays = run("Q5", **p)
    st.dataframe(stays, hide_index=True, **STRETCH)
    show_sql("Q5", p)

    st.subheader("Q1 · 90-day calendar for a listing")
    ids = stays["listing_id"].astype(str).tolist()
    lid = st.selectbox("Listing", ids) if ids else st.text_input("Listing id")
    if lid:
        p = {"listing_id": int(lid), "start": night.isoformat()}
        cal = run("Q1", **p)
        if len(cal):
            cal["status"] = cal["available"].map({1: "marked available", 0: "marked unavailable"})
            ch = alt.Chart(cal).mark_line(color=BAR, strokeWidth=2).encode(
                x=alt.X("cal_date:T", title=None), y=alt.Y("price:Q", title="nightly price ($)"),
                tooltip=["cal_date:T", "price:Q", "status:N", "minimum_nights:Q"])
            pts = alt.Chart(cal).mark_point(filled=True, size=40).encode(
                x="cal_date:T", y="price:Q",
                shape=alt.Shape("status:N", title=None),
                color=alt.Color("status:N", title=None,
                                scale=alt.Scale(range=[BAR, "#9aa3ad"])),
                tooltip=["cal_date:T", "price:Q", "status:N"])
            st.altair_chart((ch + pts).properties(height=260), **STRETCH)
            st.caption(f"{int(cal['available'].sum())} of {len(cal)} nights marked available")
        show_sql("Q1", p)

    st.subheader("Q7 · Highly rated listings with a selected amenity")
    am = W.amenities(conn())
    c1, c2 = st.columns(2)
    names = am["amenity_name"].tolist()
    amen = c1.selectbox("Amenity", names, index=names.index("Pool") if "Pool" in names else 0)
    score = c2.slider("Minimum review score", 0.0, 5.0, 4.8, 0.05)
    p = {"amenity": amen, "nbhd": nb, "min_score": Decimal(f"{score:.2f}"), "limit": 50}
    st.dataframe(run("Q7", **p), hide_index=True, **STRETCH)
    show_sql("Q7", p)

    st.subheader("Simulated admin update · change one calendar night (local DB only)")
    st.caption("Updates the local project database only; the raw files are untouched and the "
               "database can be rebuilt from them.")
    with st.form("upd"):
        c1, c2, c3, c4 = st.columns(4)
        u_lid = c1.text_input("Listing id", lid or "")
        u_date = c2.date_input("Night ", night, min_value=lo, max_value=hi)
        u_price = c3.number_input("New price", 0.0, 99999.0, 150.0, 5.0)
        u_av = c4.selectbox("Status", ["marked available", "marked unavailable"])
        if st.form_submit_button("Apply update") and u_lid:
            key = {"listing_id": int(u_lid), "date": u_date.isoformat()}
            sel = "SELECT cal_date, available, price FROM calendar WHERE listing_id=%(listing_id)s AND cal_date=%(date)s"
            before = W.run(conn(), sel, key)
            with conn().cursor() as cur:
                n = cur.execute(W.UPDATE_CALENDAR, {**key, "price": Decimal(f"{u_price:.2f}"),
                                                    "available": int(u_av == "marked available")})
            after = W.run(conn(), sel, key)
            st.write(f"Rows changed: {n}")
            st.write("Before", before, "After", after)

elif page == "Host & listing rankings":
    st.header("Host & listing rankings")
    st.subheader("Q3 · Which 20 hosts have the most listings in the LA snapshot?")
    df = run("Q3", limit=20)
    df["host"] = df["host_id"].astype(str)
    c1, c2 = st.columns([3, 2])
    with c1:
        bar(df, "host", "la_listings", "Listings per host (loaded LA data)")
    with c2:
        st.dataframe(df.drop(columns="host"), hide_index=True, **STRETCH)
        st.caption("`source_listings_count` is Inside Airbnb's own figure, kept only for comparison; "
                   "it may cover activity outside this snapshot.")
    show_sql("Q3", {"limit": 20})

    st.subheader("Q6 · Most-reviewed listings in the 12 months before the snapshot")
    p = {"snapshot": config.SNAPSHOT_DATE, "limit": 25}
    st.dataframe(run("Q6", **p), hide_index=True, **STRETCH)
    show_sql("Q6", p)
    st.subheader("Q6b · Licensing among listings reviewed in the last 12 months")
    p = {"snapshot": config.SNAPSHOT_DATE}
    st.dataframe(run("Q6b", **p), hide_index=True, **STRETCH)
    show_sql("Q6b", p)

elif page == "Query lab":
    st.header("Query lab")
    st.subheader("Run any workload query with EXPLAIN ANALYZE")
    qid = st.selectbox("Query", list(W.QUERIES), format_func=lambda k: f"{k} · {W.QUERIES[k].title}")
    q = W.QUERIES[qid]
    st.markdown(f"**Question:** {q.question}  \n**Operations:** {q.operations}")
    params = dict(q.defaults)
    if qid == "Q1":
        params["listing_id"] = int(W.run(conn(), "SELECT listing_id FROM listing LIMIT 1").iloc[0, 0])
    with st.expander("Parameters", expanded=False):
        for k, v in list(params.items()):
            params[k] = type(v)(st.text_input(k, str(v))) if v is not None else v
    st.code(W.render(qid, params), language="sql")
    if st.button("Run + EXPLAIN ANALYZE"):
        c1, c2 = st.columns([2, 3])
        c1.dataframe(run(qid, **params), hide_index=True, **STRETCH)
        c2.code(W.explain(conn(), qid, params), language="text")

    st.subheader("Measured physical-design experiments (P1–P4)")
    summ = config.RESULTS_DIR / "summary.csv"
    if not summ.exists():
        st.info("Run `python experiments/run_experiments.py` to populate results/summary.csv.")
    else:
        res = pd.DataFrame(list(csv.DictReader(open(summ))))
        exp = st.selectbox("Experiment", [e for e in ["P1", "P2", "P3", "P3S", "P4", "Q3", "W"]
                                          if e in set(res["experiment"])])
        sub = res[res["experiment"] == exp].copy()
        sub["median_ms"] = pd.to_numeric(sub["median_ms"])
        variant = st.selectbox("Variant", sub["variant"].unique())
        v = sub[sub["variant"] == variant]
        bar(v, "config", "median_ms", f"{exp} · {variant} · median of runs 2–N (ms)")
        st.dataframe(v[["config", "query_cost", "access", "filesort", "plan_ops", "run1_ms",
                        "median_ms", "rows", "Handler_read_key", "Handler_read_next",
                        "Handler_read_rnd_next", "Sort_merge_passes", "bp_read_requests",
                        "bp_disk_reads"]], hide_index=True, **STRETCH)
        plans = sorted((config.RESULTS_DIR / "experiments" / "plans").glob(f"{exp}__*.txt"))
        pick = st.selectbox("Captured plan", [p.name for p in plans])
        if pick:
            st.code((config.RESULTS_DIR / "experiments" / "plans" / pick).read_text(), language="sql")

else:  # MongoDB
    st.header("MongoDB · listing-centred document model")
    try:
        from pymongo import MongoClient
        cli = MongoClient(config.MONGO_URI, serverSelectionTimeoutMS=2000)
        coll = cli[config.MONGO_DB]["listings"]
        n = coll.estimated_document_count()
        st.caption(f"{n:,} listing documents in {config.MONGO_DB}.listings")
        nb = st.selectbox("Neighbourhood", NBHDS, index=default_nb)
        am = st.text_input("Amenity (array match, multikey index)", "Pool")
        filt = {"neighbourhood.name": nb, "amenities": am}
        st.code(f"db.listings.find({json.dumps(filt)}).sort({{review_score: -1, price: 1}}).limit(20)",
                language="javascript")
        docs = list(coll.find(filt, {"recent_reviews": 0}).sort([("review_score", -1), ("price", 1)]).limit(20))
        st.dataframe(pd.DataFrame([{"_id": str(d["_id"]), "name": d.get("name"),
                                    "review_score": d.get("review_score"), "price": d.get("price")}
                                   for d in docs]), hide_index=True, **STRETCH)
        if docs:
            with st.expander("One full document (recent_reviews truncated to 2)"):
                d = coll.find_one({"_id": docs[0]["_id"]})
                d["recent_reviews"] = d["recent_reviews"][:2]
                st.json(json.loads(json.dumps(d, default=str)))
    except Exception as e:  # noqa: BLE001
        st.warning(f"MongoDB not reachable at {config.MONGO_URI} ({type(e).__name__}). "
                   "Start mongod and run `python mongo/load_mongo.py`.")
    cmp_file = config.RESULTS_DIR / "mongo_comparison.json"
    if cmp_file.exists():
        st.subheader("MySQL vs MongoDB comparison (from mongo/compare.py)")
        st.json(json.loads(cmp_file.read_text()))