"""Physical-design experiments P1-P4 plus supporting plans (proposal section F).

Measurement protocol, applied to every (experiment, configuration, variant):
  * physical design changed with ALTER TABLE, then ANALYZE TABLE
  * EXPLAIN FORMAT=JSON  -> optimizer cost, access type, chosen key, filesort/temp flags
  * EXPLAIN FORMAT=TREE  -> estimated plan
  * N runs (default 5) of EXPLAIN ANALYZE; run 1 recorded separately, median of runs 2..N
  * per run: session Handler_read_* / Sort_* / tmp-table counters and global
    InnoDB buffer-pool logical reads (read_requests) and physical reads (reads)
  * plain execution wall-clock (client fetch) for queries with small results
  * index sizes from mysql.innodb_index_stats; index build time

Outputs: results/experiments/<EXP>.json, results/experiments/plans/*.txt,
         results/summary.csv, results/experiments/environment.json

Usage:
  python experiments/run_experiments.py                 # everything
  python experiments/run_experiments.py --only P1,P4    # subset (W, P1, P2, P3, P3S, P4, Q3)
  python experiments/run_experiments.py --runs 3 --p3-window month
The script always restores the baseline physical design when it finishes.
"""
import argparse
import csv
import json
import re
import statistics
import sys
import time
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import config  # noqa: E402
import workload as W  # noqa: E402

OUT = config.RESULTS_DIR / "experiments"
PLANS = OUT / "plans"
SESSION_COUNTERS = ["Handler_read_first", "Handler_read_key", "Handler_read_next",
                    "Handler_read_prev", "Handler_read_rnd", "Handler_read_rnd_next",
                    "Sort_merge_passes", "Sort_rows", "Sort_scan", "Sort_range",
                    "Created_tmp_tables", "Created_tmp_disk_tables", "Select_full_join",
                    "Select_scan", "Select_range"]
GLOBAL_COUNTERS = ["Innodb_buffer_pool_read_requests", "Innodb_buffer_pool_reads"]
SUMMARY_ROWS = []


# ------------------------------------------------------------ plumbing
def q(conn, sql, params=None):
    with conn.cursor() as cur:
        cur.execute(sql, params)
        return cur.fetchall() if cur.description else []


def status(conn, scope, names):
    rows = q(conn, f"SHOW {scope} STATUS WHERE Variable_name IN ({','.join(['%s'] * len(names))})", names)
    return {k: int(v) for k, v in rows}


def slug(s):
    return re.sub(r"[^A-Za-z0-9]+", "_", s).strip("_")[:80]


def plan_json_summary(plan):
    """Walk EXPLAIN FORMAT=JSON and pull out per-table access info + flags."""
    tables, flags = [], {"using_filesort": False, "using_temporary_table": False}

    def walk(node):
        if isinstance(node, dict):
            for k in flags:
                if node.get(k):
                    flags[k] = True
            t = node.get("table")
            if isinstance(t, dict) and "table_name" in t:
                tables.append({k: t.get(k) for k in (
                    "table_name", "access_type", "key", "used_key_parts", "key_length",
                    "rows_examined_per_scan", "rows_produced_per_join", "filtered",
                    "using_index", "attached_condition")})
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    walk(plan)
    cost = plan.get("query_block", {}).get("cost_info", {}).get("query_cost")
    return {"query_cost": float(cost) if cost else None, "tables": tables, **flags}


ACCESS_RE = re.compile(r"(Single-row covering index lookup|Single-row index lookup|Covering index lookup|"
                       r"Covering index range scan|Covering index skip scan|Covering index scan|Index range scan|"
                       r"Index lookup|Index scan|Index skip scan|Table scan|Constant row from) on (\w+)"
                       r"(?: using (\w+))?")
ACCESS_TYPE = {"Table scan": "ALL", "Index scan": "index", "Covering index scan": "index",
               "Index range scan": "range", "Covering index range scan": "range",
               "Index skip scan": "range", "Covering index skip scan": "range",
               "Index lookup": "ref", "Covering index lookup": "ref",
               "Single-row index lookup": "eq_ref", "Single-row covering index lookup": "eq_ref",
               "Constant row from": "const"}


def tree_summary(tree, plan_json):
    """Fallback for servers whose EXPLAIN JSON is not v1: read access paths from the tree plan."""
    tables = [{"table_name": m.group(2), "access_type": ACCESS_TYPE.get(m.group(1), m.group(1)),
               "key": m.group(3)} for m in ACCESS_RE.finditer(tree)]
    cost = None
    if isinstance(plan_json, dict):
        cost = plan_json.get("estimated_total_cost") or plan_json.get("query_cost")
    return {"tables": tables, "using_filesort": bool(re.search(r"->\s*Sort", tree)),
            "using_temporary_table": "temporary" in tree,
            "query_cost": float(cost) if cost else None}


PLAN_OPS = ["Table scan", "Index scan", "Index range scan", "Index lookup",
            "Single-row index lookup", "Covering index lookup", "Covering index range scan",
            "Covering index scan", "Index skip scan", "Sort", "Sort with limit", "Filesort",
            "Nested loop inner join", "Inner hash join", "Nested loop semijoin",
            "Hash semijoin", "Aggregate using temporary table", "Group aggregate",
            "Stream results", "Limit"]


def plan_ops(tree):
    found = []
    for line in tree.splitlines():
        body = line.strip().lstrip("-> ").strip()
        for op in sorted(PLAN_OPS, key=len, reverse=True):
            if body.startswith(op):
                idx = re.search(r" using (\S+)", body)
                found.append(op + (f"[{idx.group(1)}]" if idx and "index" in op.lower() else ""))
                break
    return " > ".join(found)


def root_actual(tree):
    m = re.search(r"actual time=([\d.e+-]+)\.\.([\d.e+-]+) rows=([\d.e+-]+) loops=(\d+)", tree)
    if not m:
        return None, None
    return float(m.group(2)), float(m.group(3))


def index_sizes(conn, table):
    rows = q(conn, """SELECT index_name, stat_value * @@innodb_page_size
                      FROM mysql.innodb_index_stats
                      WHERE database_name = DATABASE() AND table_name = %s AND stat_name = 'size'""",
             (table,))
    return {i: round(b / 2**20, 2) for i, b in rows}


def table_rows(conn):
    return {t: q(conn, f"SELECT COUNT(*) FROM {t}")[0][0]
            for t in ["neighbourhood", "host", "listing", "amenity", "listing_amenity",
                      "calendar", "review"]}


# ------------------------------------------------------------ physical design
def set_calendar_indexes(conn, wanted):
    """wanted: dict name -> column list. Drops other xp_* indexes, adds missing ones."""
    existing = {r[0] for r in q(conn, """SELECT DISTINCT index_name FROM information_schema.statistics
                                         WHERE table_schema = DATABASE() AND table_name = 'calendar'
                                           AND index_name LIKE 'xp\\_%%'""")}
    build = {}
    for name in existing - set(wanted):
        q(conn, f"ALTER TABLE calendar DROP INDEX {name}")
    for name, cols in wanted.items():
        if name not in existing:
            t0 = time.perf_counter()
            q(conn, f"ALTER TABLE calendar ADD INDEX {name} ({cols})")
            build[name] = round(time.perf_counter() - t0, 2)
    q(conn, "ANALYZE TABLE calendar")
    return build


def set_reverse_amenity_index(conn, present: bool):
    has = bool(q(conn, """SELECT 1 FROM information_schema.statistics WHERE table_schema = DATABASE()
                          AND table_name = 'listing_amenity' AND index_name = 'idx_amenity_listing' LIMIT 1"""))
    if present and not has:
        q(conn, "ALTER TABLE listing_amenity ADD INDEX idx_amenity_listing (amenity_id, listing_id)")
        q(conn, "SET foreign_key_checks = 0")
        q(conn, """ALTER TABLE listing_amenity ADD CONSTRAINT fk_la_amenity
                   FOREIGN KEY (amenity_id) REFERENCES amenity (amenity_id)""")
        q(conn, "SET foreign_key_checks = 1")
    elif not present and has:
        # The index also backs the amenity FK; InnoDB refuses to drop it while the FK exists.
        q(conn, "ALTER TABLE listing_amenity DROP FOREIGN KEY fk_la_amenity")
        q(conn, "ALTER TABLE listing_amenity DROP INDEX idx_amenity_listing")
    q(conn, "ANALYZE TABLE listing_amenity")


def restore_baseline(conn):
    set_calendar_indexes(conn, {})
    set_reverse_amenity_index(conn, True)
    q(conn, "SET SESSION sort_buffer_size = DEFAULT")


# ------------------------------------------------------------ measurement
def measure(conn, exp, config_name, variant, sql, params, runs, exec_too=True,
            physical=None, note=""):
    params = params or {}
    plan_json = json.loads(q(conn, "EXPLAIN FORMAT=JSON " + sql, params)[0][0])
    tree = "\n".join(r[0] for r in q(conn, "EXPLAIN FORMAT=TREE " + sql, params))
    rec = {"experiment": exp, "config": config_name, "variant": variant, "params": params,
           "note": note, "physical_design": physical or {}, "sql": sql.strip(),
           "estimate": plan_json_summary(plan_json), "plan_tree": tree, "runs": []}
    analyze_text = ""
    for i in range(runs):
        q(conn, "FLUSH STATUS")
        g0 = status(conn, "GLOBAL", GLOBAL_COUNTERS)
        t0 = time.perf_counter()
        analyze_text = "\n".join(r[0] for r in q(conn, "EXPLAIN ANALYZE " + sql, params))
        wall = (time.perf_counter() - t0) * 1000
        s = status(conn, "SESSION", SESSION_COUNTERS)
        g1 = status(conn, "GLOBAL", GLOBAL_COUNTERS)
        actual_ms, rows = root_actual(analyze_text)
        run = {"run": i + 1, "analyze_ms": actual_ms, "analyze_wall_ms": round(wall, 2),
               "rows": rows, **s,
               "bp_read_requests": g1["Innodb_buffer_pool_read_requests"] - g0["Innodb_buffer_pool_read_requests"],
               "bp_disk_reads": g1["Innodb_buffer_pool_reads"] - g0["Innodb_buffer_pool_reads"]}
        if exec_too:
            t0 = time.perf_counter()
            with conn.cursor() as cur:
                cur.execute(sql, params)
                cur.fetchall()
            run["exec_ms"] = round((time.perf_counter() - t0) * 1000, 2)
        rec["runs"].append(run)
    rec["explain_analyze"] = analyze_text
    warm = rec["runs"][1:] or rec["runs"]
    med = lambda k: (round(statistics.median([r[k] for r in warm if r.get(k) is not None]), 3)
                     if any(r.get(k) is not None for r in warm) else None)
    rec["summary"] = {"run1_ms": rec["runs"][0]["analyze_ms"], "median_ms": med("analyze_ms"),
                      "median_exec_ms": med("exec_ms"), "rows": rec["runs"][-1]["rows"],
                      **{k: med(k) for k in ["Handler_read_key", "Handler_read_next",
                                             "Handler_read_rnd_next", "Sort_rows",
                                             "Sort_merge_passes", "Created_tmp_disk_tables",
                                             "bp_read_requests", "bp_disk_reads"]}}
    name = f"{exp}__{slug(config_name)}__{slug(variant)}"
    (PLANS / f"{name}.txt").write_text(
        f"-- {exp} | config: {config_name} | variant: {variant}\n-- params: {params}\n"
        f"{sql.strip()}\n\n-- EXPLAIN FORMAT=TREE (estimated)\n{tree}\n\n"
        f"-- EXPLAIN ANALYZE (last run)\n{analyze_text}\n")
    if not rec["estimate"]["tables"]:
        rec["estimate"].update(tree_summary(tree, plan_json))
    e = rec["estimate"]
    SUMMARY_ROWS.append({
        "experiment": exp, "config": config_name, "variant": variant,
        "query_cost": e["query_cost"],
        "access": "; ".join(f"{t['table_name']}:{t['access_type']}({t['key'] or '-'})" for t in e["tables"]),
        "filesort": e["using_filesort"], "temp_table": e["using_temporary_table"],
        "plan_ops": plan_ops(analyze_text),
        **rec["summary"],
        "index_mb": json.dumps(physical.get("index_mb", {})) if physical else "",
    })
    s = rec["summary"]
    print(f"   {config_name:<34} {variant:<28} median={s['median_ms']}ms  run1={s['run1_ms']}ms  "
          f"rows={s['rows']}  filesort={e['using_filesort']}  {SUMMARY_ROWS[-1]['access']}")
    return rec


def save(exp, records, extra=None):
    (OUT / f"{exp}.json").write_text(json.dumps({"experiment": exp, **(extra or {}),
                                                 "records": records}, indent=2, default=str))


# ------------------------------------------------------------ experiments
def exp_workload(conn, a):
    """W: baseline plan for every workload query (report section D)."""
    print("W  - baseline plans for the whole workload")
    lid = q(conn, "SELECT listing_id FROM listing ORDER BY listing_id LIMIT 1")[0][0]
    recs = []
    for qid, query in W.QUERIES.items():
        p = dict(query.defaults)
        if qid == "Q1":
            p["listing_id"] = lid
        recs.append(measure(conn, "W", "baseline", qid, query.sql, p, a.runs))
    save("W", recs)


P1_CONFIGS = {
    "no secondary index": {},
    "INDEX(cal_date)": {"xp_date": "cal_date"},
    "INDEX(price)": {"xp_price": "price"},
    "INDEX(cal_date, price)": {"xp_date_price": "cal_date, price"},
    "INDEX(price, cal_date)": {"xp_price_date": "price, cal_date"},
}


def exp_p1(conn, a):
    print("P1 - date filtering + price ordering")
    limit = f"\nLIMIT {a.p1_limit}" if a.p1_limit else ""
    sql = f"""
SELECT listing_id, cal_date, price
FROM calendar
WHERE available = 1
  AND price IS NOT NULL
  AND cal_date BETWEEN %(lo)s AND %(hi)s
ORDER BY price{limit}"""
    start = date.fromisoformat(a.start)
    recs = []
    for cname, idx in P1_CONFIGS.items():
        build = set_calendar_indexes(conn, idx)
        phys = {"indexes": idx, "build_seconds": build, "index_mb": index_sizes(conn, "calendar")}
        for days in (1, 7, 30, 90):
            p = {"lo": start.isoformat(), "hi": (start + timedelta(days=days - 1)).isoformat()}
            sel = q(conn, "SELECT COUNT(*) FROM calendar WHERE available = 1 AND price IS NOT NULL AND cal_date BETWEEN %(lo)s AND %(hi)s", p)[0][0]
            recs.append(measure(conn, "P1", cname, f"{days} night window", sql, p, a.runs,
                                physical=phys, note=f"qualifying rows={sel}"))
    save("P1", recs)


def exp_p2(conn, a):
    print("P2 - date-based neighbourhood join")
    nye, tue = "2026-12-31", a.compare_date
    configs = {
        "no date index": ({}, ""),
        "INDEX(cal_date)": ({"xp_date": "cal_date"}, ""),
        "INDEX(cal_date, available)": ({"xp_date_avail": "cal_date, available"}, ""),
        "hinted hash join (no indexes)": ({}, "/*+ NO_INDEX(c) NO_INDEX(l) NO_INDEX(n) */ "),
    }
    recs = []
    sizes = table_rows(conn)
    for cname, (idx, hint) in configs.items():
        build = set_calendar_indexes(conn, idx)
        phys = {"indexes": idx, "build_seconds": build, "index_mb": index_sizes(conn, "calendar"),
                "hint": hint.strip()}
        for qid, label in (("Q4b", "one neighbourhood"), ("Q4", "all neighbourhoods")):
            for d in (nye, tue):
                p = dict(W.QUERIES[qid].defaults, date=d)
                if qid == "Q4b":
                    p["nbhd"] = a.nbhd
                sql = W.QUERIES[qid].sql.replace("SELECT", "SELECT " + hint, 1)
                recs.append(measure(conn, "P2", cname, f"{label} {d}", sql, p, a.runs, physical=phys))
    save("P2", recs, {"relation_sizes": sizes})


def exp_p3(conn, a):
    print(f"P3 - large sort (window: {a.p3_window})")
    where = "available = 1 AND price IS NOT NULL"
    params = {}
    if a.p3_window == "month":
        where += " AND cal_date BETWEEN %(lo)s AND %(hi)s"
        s = date.fromisoformat(a.start)
        params = {"lo": s.isoformat(), "hi": (s + timedelta(days=29)).isoformat()}
    base = f"SELECT listing_id, cal_date, price FROM calendar WHERE {where} ORDER BY price"
    configs = {
        "no secondary index": {},
        "INDEX(price)": {"xp_price": "price"},
        "INDEX(available, price)": {"xp_avail_price": "available, price"},
    }
    recs = []
    for cname, idx in configs.items():
        build = set_calendar_indexes(conn, idx)
        phys = {"indexes": idx, "build_seconds": build, "index_mb": index_sizes(conn, "calendar"),
                "window": a.p3_window}
        recs.append(measure(conn, "P3", cname, "no LIMIT", base, params, a.runs,
                            exec_too=False, physical=phys))
        recs.append(measure(conn, "P3", cname, "LIMIT 20", base + "\nLIMIT 20", params, a.runs,
                            physical=phys))
    save("P3", recs)


def exp_p3_sortbuffer(conn, a):
    """P3S (stretch): explicit filesort under different sort_buffer_size -> Sort_merge_passes."""
    print("P3S - sort_buffer_size vs external merge passes")
    set_calendar_indexes(conn, {})
    where, params = "available = 1 AND price IS NOT NULL", {}
    if a.p3_window == "month":
        s = date.fromisoformat(a.start)
        where += " AND cal_date BETWEEN %(lo)s AND %(hi)s"
        params = {"lo": s.isoformat(), "hi": (s + timedelta(days=29)).isoformat()}
    sql = f"SELECT listing_id, cal_date, price FROM calendar WHERE {where} ORDER BY price"
    recs = []
    for size in (32 * 1024, 256 * 1024, 4 * 2**20, 64 * 2**20):
        q(conn, f"SET SESSION sort_buffer_size = {size}")
        recs.append(measure(conn, "P3S", f"sort_buffer_size={size // 1024}K", "no LIMIT", sql,
                            params, a.runs, exec_too=False,
                            physical={"sort_buffer_size": size, "window": a.p3_window}))
    q(conn, "SET SESSION sort_buffer_size = DEFAULT")
    save("P3S", recs)


def exp_p4(conn, a):
    print("P4 - amenity access direction + selectivity")
    common = a.common_amenity or q(conn, """SELECT a.amenity_name FROM listing_amenity la
        JOIN amenity a USING (amenity_id) GROUP BY a.amenity_id ORDER BY COUNT(*) DESC LIMIT 1""")[0][0]
    rare = [x.strip() for x in a.rare_amenity.split(",") if x.strip()]
    amens = [common] + rare
    freq = dict(q(conn, f"""SELECT a.amenity_name, COUNT(*) FROM listing_amenity la JOIN amenity a
                           USING (amenity_id) WHERE a.amenity_name IN ({",".join(["%s"] * len(amens))})
                           GROUP BY a.amenity_name""", amens))
    total = q(conn, "SELECT COUNT(*) FROM listing")[0][0]
    recs = []
    for cname, present in (("PK(listing_id, amenity_id) + INDEX(amenity_id, listing_id)", True),
                           ("PK(listing_id, amenity_id) only", False)):
        set_reverse_amenity_index(conn, present)
        phys = {"reverse_index": present, "index_mb": index_sizes(conn, "listing_amenity")}
        for amen in amens:
            sel = f"{amen} ({freq.get(amen, 0)}/{total} listings)"
            for qid, scope in (("Q7", a.nbhd), ("Q7b", "citywide")):
                p = dict(W.QUERIES[qid].defaults, amenity=amen)
                if qid == "Q7":
                    p["nbhd"] = a.nbhd
                recs.append(measure(conn, "P4", cname, f"{scope} | {amen}", W.QUERIES[qid].sql, p,
                                    a.runs, physical=phys, note=sel))
    set_reverse_amenity_index(conn, True)
    save("P4", recs, {"amenity_frequency": freq, "listings": total})


def exp_q3(conn, a):
    print("Q3 - supporting aggregation analysis")
    recs = [measure(conn, "Q3", "baseline (FK index on listing.host_id)", "top 20 hosts",
                    W.QUERIES["Q3"].sql, W.QUERIES["Q3"].defaults, a.runs)]
    save("Q3", recs)


EXPERIMENTS = {"W": exp_workload, "P1": exp_p1, "P2": exp_p2, "P3": exp_p3,
               "P3S": exp_p3_sortbuffer, "P4": exp_p4, "Q3": exp_q3}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", default=",".join(EXPERIMENTS), help="comma list of " + ",".join(EXPERIMENTS))
    ap.add_argument("--runs", type=int, default=5)
    ap.add_argument("--start", default="2026-10-15", help="first night of P1/P3 windows")
    ap.add_argument("--compare-date", default="2027-01-12", help="comparison Tuesday for P2")
    ap.add_argument("--nbhd", default="Venice")
    ap.add_argument("--common-amenity", default=None, help="default: most frequent amenity")
    ap.add_argument("--rare-amenity", default="Pool,Private pool", help="comma list of less common amenities")
    ap.add_argument("--p1-limit", type=int, default=100, help="0 = no LIMIT in P1")
    ap.add_argument("--p3-window", choices=["full", "month"], default="full")
    a = ap.parse_args()

    OUT.mkdir(parents=True, exist_ok=True)
    PLANS.mkdir(parents=True, exist_ok=True)
    conn = config.mysql_conn(read_timeout=3600)
    try:  # MySQL 9+/26.x default to JSON format v2; the parser below reads v1
        q(conn, "SET SESSION explain_json_format_version = 1")
    except Exception:  # noqa: BLE001  (8.0 has no such variable and already uses v1)
        pass
    env = dict(q(conn, """SHOW VARIABLES WHERE Variable_name IN ('version', 'innodb_buffer_pool_size',
                          'sort_buffer_size', 'join_buffer_size', 'innodb_page_size', 'optimizer_switch',
                          'tmp_table_size', 'max_heap_table_size')"""))
    env["table_rows"] = table_rows(conn)
    env["runs_per_config"] = a.runs
    env["args"] = vars(a)
    (OUT / "environment.json").write_text(json.dumps(env, indent=2, default=str))
    print(f"MySQL {env['version']}  buffer pool={int(env['innodb_buffer_pool_size']) // 2**20} MB  "
          f"rows={env['table_rows']}")

    try:
        for name in [s.strip() for s in a.only.split(",") if s.strip()]:
            t0 = time.perf_counter()
            EXPERIMENTS[name](conn, a)
            print(f"   ({name} took {time.perf_counter() - t0:.0f}s)")
    finally:
        print("Restoring baseline physical design ...")
        restore_baseline(conn)
        if SUMMARY_ROWS:
            path = config.RESULTS_DIR / "summary.csv"
            new = not path.exists()
            existing = []
            if not new:  # replace rows of re-run experiments, keep the others
                with open(path) as f:
                    done = {r["experiment"] for r in SUMMARY_ROWS}
                    existing = [r for r in csv.DictReader(f) if r["experiment"] not in done]
            with open(path, "w", newline="") as f:
                w = csv.DictWriter(f, fieldnames=list(SUMMARY_ROWS[0]))
                w.writeheader()
                w.writerows(existing + SUMMARY_ROWS)
            print(f"Wrote {path}")


if __name__ == "__main__":
    main()