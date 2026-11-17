"""Step 3 - Create the schema and bulk-load the cleaned files into MySQL.

Loads in foreign-key dependency order with LOAD DATA LOCAL INFILE, then
validates: loaded row counts vs. clean_manifest.json, orphan checks for every
foreign key, and table/index sizes. Writes results/load_log.json.

Prerequisite (once, as an admin user):  SET GLOBAL local_infile = 1;
Usage: python pipeline/03_load_mysql.py
"""
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import config  # noqa: E402

LOAD_ORDER = [
    ("neighbourhood", "neighbourhood_id, name, group_name"),
    ("host", "host_id, host_since, is_superhost, source_listings_count"),
    ("listing", "listing_id, host_id, neighbourhood_id, name, room_type, latitude, "
                "longitude, accommodates, bedrooms, price, review_score, license"),
    ("amenity", "amenity_id, amenity_name"),
    ("listing_amenity", "listing_id, amenity_id"),
    ("calendar", "listing_id, cal_date, available, price, minimum_nights"),
    ("review", "review_id, listing_id, review_date, reviewer_id, comments"),
]

ORPHAN_CHECKS = {
    "listing.host_id": "SELECT COUNT(*) FROM listing l LEFT JOIN host h USING (host_id) WHERE h.host_id IS NULL",
    "listing.neighbourhood_id": "SELECT COUNT(*) FROM listing l LEFT JOIN neighbourhood n USING (neighbourhood_id) WHERE n.neighbourhood_id IS NULL",
    "calendar.listing_id": "SELECT COUNT(*) FROM calendar c LEFT JOIN listing l USING (listing_id) WHERE l.listing_id IS NULL",
    "review.listing_id": "SELECT COUNT(*) FROM review r LEFT JOIN listing l USING (listing_id) WHERE l.listing_id IS NULL",
    "listing_amenity.listing_id": "SELECT COUNT(*) FROM listing_amenity la LEFT JOIN listing l USING (listing_id) WHERE l.listing_id IS NULL",
    "listing_amenity.amenity_id": "SELECT COUNT(*) FROM listing_amenity la LEFT JOIN amenity a USING (amenity_id) WHERE a.amenity_id IS NULL",
}


def run_sql_file(cur, path: Path):
    sql = "\n".join(l for l in path.read_text().splitlines() if not l.strip().startswith("--"))
    for stmt in sql.split(";"):
        if stmt.strip():
            cur.execute(stmt)


def table_sizes(cur):
    cur.execute("""
        SELECT table_name, table_rows, data_length, index_length
        FROM information_schema.tables WHERE table_schema = DATABASE()
        ORDER BY data_length DESC""")
    tables = [dict(table=t, est_rows=r, data_mb=round(d / 2**20, 1), index_mb=round(i / 2**20, 1))
              for t, r, d, i in cur.fetchall()]
    cur.execute("""
        SELECT table_name, index_name, stat_value * @@innodb_page_size
        FROM mysql.innodb_index_stats
        WHERE database_name = DATABASE() AND stat_name = 'size'
        ORDER BY table_name, index_name""")
    indexes = [dict(table=t, index=i, mb=round(b / 2**20, 2)) for t, i, b in cur.fetchall()]
    return tables, indexes


def main():
    manifest = json.loads((config.CLEAN_DIR / "clean_manifest.json").read_text())
    conn = config.mysql_conn(database=False)
    cur = conn.cursor()
    cur.execute("SELECT @@local_infile, VERSION()")
    infile, version = cur.fetchone()
    print(f"MySQL {version}")
    if not infile:
        sys.exit("Server has local_infile=OFF. Run as admin: SET GLOBAL local_infile = 1;")

    print("Creating schema ...")
    run_sql_file(cur, config.ROOT / "sql" / "schema.sql")
    cur.execute(f"USE {config.MYSQL['database']}")
    cur.execute("SET SESSION foreign_key_checks = 0")
    cur.execute("SET SESSION unique_checks = 0")

    log = {"mysql_version": version, "tables": {}}
    for table, cols in LOAD_ORDER:
        path = (config.CLEAN_DIR / f"{table}.tsv").resolve()
        t0 = time.perf_counter()
        cur.execute(f"""
            LOAD DATA LOCAL INFILE '{path.as_posix()}' INTO TABLE {table}
            CHARACTER SET utf8mb4
            FIELDS TERMINATED BY '\\t' ESCAPED BY '\\\\'
            LINES TERMINATED BY '\\n' ({cols})""")
        secs = time.perf_counter() - t0
        cur.execute("SHOW COUNT(*) WARNINGS")
        warnings = cur.fetchone()[0]
        cur.execute(f"SELECT COUNT(*) FROM {table}")
        loaded = cur.fetchone()[0]
        expected = manifest["rows"][table]
        status = "OK" if loaded == expected else "MISMATCH"
        log["tables"][table] = dict(expected=expected, loaded=loaded, warnings=warnings,
                                    seconds=round(secs, 1), status=status)
        print(f"  {table:<16} {loaded:>12,} rows  {secs:7.1f}s  warnings={warnings}  {status}")
        if warnings:
            cur.execute("SHOW WARNINGS LIMIT 5")
            for w in cur.fetchall():
                print("     ", w)

    cur.execute("SET SESSION foreign_key_checks = 1")
    cur.execute("SET SESSION unique_checks = 1")

    print("Checking foreign keys (loaded with checks off) ...")
    log["orphans"] = {}
    for fk, q in ORPHAN_CHECKS.items():
        cur.execute(q)
        n = cur.fetchone()[0]
        log["orphans"][fk] = n
        print(f"  {fk:<28} orphans={n}")

    print("ANALYZE TABLE ...")
    for table, _ in LOAD_ORDER:
        cur.execute(f"ANALYZE TABLE {table}")
        cur.fetchall()
    log["table_sizes"], log["index_sizes"] = table_sizes(cur)
    for t in log["table_sizes"]:
        print(f"  {t['table']:<16} data={t['data_mb']:>8} MB  index={t['index_mb']:>7} MB")

    ok = all(v["status"] == "OK" for v in log["tables"].values()) and not any(log["orphans"].values())
    log["valid"] = ok
    config.RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    (config.RESULTS_DIR / "load_log.json").write_text(json.dumps(log, indent=2))
    print("VALIDATION PASSED" if ok else "VALIDATION FAILED - see results/load_log.json")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
