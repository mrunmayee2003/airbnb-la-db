# Inside Airbnb Los Angeles: DSCI 551 Fall 2026 Course Project

Team: Mrunmayee Khilare · Priya Chakka · Isabella Linarez

A MySQL database built from the Inside Airbnb Los Angeles snapshot (2026-09-14), with a
reproducible Python load pipeline, a 7-question query workload, four physical-design
experiments (P1–P4) measured with EXPLAIN / EXPLAIN ANALYZE, a modest MongoDB
listing-document comparison, and a simple Streamlit app that runs live queries.

```
Inside Airbnb CSVs ─▶ 01_download ─▶ 02_clean ─▶ 03_load_mysql ─▶ workload.py (Q1–Q7)
   (raw, checksummed)  (manifest)   (TSV + log)  (schema, validation)      │
                                                        ├─▶ experiments/run_experiments.py ─▶ results/
                                                        ├─▶ mongo/load_mongo.py + compare.py
                                                        └─▶ app/streamlit_app.py (live queries)
```

## Layout

| Path | What it is | Owner (proposal) |
|---|---|---|
| `config.py` | Snapshot URL, folders, MySQL/Mongo credentials (env-var overridable) | all |
| `pipeline/01_download.py` | Download the 4 files; SHA-256, byte size, raw row counts → `data/raw/manifest.json` | Mrunmayee |
| `pipeline/02_clean.py` | Currency → DECIMAL, t/f → 0/1, neighbourhood normalization, amenity JSON → `amenity` + `listing_amenity`, drop reviewer names, FK filtering, calendar-price fallback → `data/clean/*.tsv` + `clean_manifest.json` | Mrunmayee |
| `pipeline/03_load_mysql.py` | Create schema, `LOAD DATA LOCAL INFILE` in FK order, count + orphan validation, table/index sizes → `results/load_log.json` | Mrunmayee |
| `sql/schema.sql` | 7-table baseline schema (PKs, FKs, UNIQUE, CHECK, reverse junction index) | Mrunmayee |
| `workload.py` | Q1–Q7 (+Q4b, Q6b, Q7b) and the simulated update; shared by app and experiments | Priya |
| `experiments/run_experiments.py` | W, P1, P2, P3, P3S, P4, Q3 with the full measurement protocol | Priya (+ Mrunmayee P2, Isabella P4) |
| `mongo/` | Listing documents built from MySQL; 3 comparison operations + update-consistency demo | Isabella |
| `app/streamlit_app.py` | 5 pages: explorer, stay finder, rankings, query lab, MongoDB | Isabella |
| `tools/make_synthetic.py` | Synthetic data in the exact Inside Airbnb format, **for testing only** | – |

## Setup (each laptop)

1. **MySQL 8.0+.** In `my.cnf` / `my.ini` under `[mysqld]`, then restart MySQL:
   ```ini
   local_infile = 1
   innodb_buffer_pool_size = 2G   # calendar + indexes should fit; default 128M makes every run I/O-bound
   ```
   Create the project user once (as root):
   ```sql
   CREATE USER 'dsci'@'localhost' IDENTIFIED BY 'dsci551';
   GRANT ALL ON *.* TO 'dsci'@'localhost';   -- needs RELOAD for FLUSH STATUS and mysql.innodb_index_stats
   ```
   Different credentials? `export MYSQL_USER=... MYSQL_PASSWORD=...` (see `config.py`).
2. **Python 3.10+:** `pip install -r requirements.txt`
3. **MongoDB (Isabella's machine at minimum):** MongoDB Community 7/8, `mongod` on `localhost:27017`.

## Run

```bash
python pipeline/01_download.py          # ~100-200 MB download
python pipeline/02_clean.py             # prints decisions + dropped-row counts
python pipeline/03_load_mysql.py        # must end with VALIDATION PASSED
python experiments/run_experiments.py   # all experiments, 5 runs each; --only P1,P4 for a subset
python mongo/load_mongo.py && python -m mongo.compare
streamlit run app/streamlit_app.py
```
Or everything at once: `./run_all.sh`. To dry-run with synthetic data: `SYNTHETIC=1 ./run_all.sh`.

If `01_download.py` can't fetch a file (the URL pattern changed or a newer snapshot replaced it),
download the 4 files by hand from https://insideairbnb.com/get-the-data/ into `data/raw/`
and run `python pipeline/01_download.py --manifest-only`.

## Experiments (what each one compares)

Protocol for every configuration: change the physical design → `ANALYZE TABLE` →
`EXPLAIN FORMAT=JSON` (cost, access type, key, filesort/temp) → 5× `EXPLAIN ANALYZE`
(run 1 kept separately, **median of runs 2–5** reported) with session `Handler_read_*`,
`Sort_*`, tmp-table counters and global buffer-pool logical (`read_requests`) / physical
(`reads`) page reads per run. Index sizes come from `mysql.innodb_index_stats`.

| ID | Query | Configurations | Workload conditions |
|---|---|---|---|
| W | all Q1–Q7 | baseline | default parameters (plans for report §D) |
| P1 | available nights in a date window, `ORDER BY price LIMIT 100` | none · `(cal_date)` · `(price)` · `(cal_date, price)` · `(price, cal_date)` | window = 1, 7, 30, 90 nights |
| P2 | Q4b (one neighbourhood) and Q4 (all) | none · `(cal_date)` · `(cal_date, available)` · hinted hash join | New Year's Eve vs a comparison Tuesday |
| P3 | all available calendar rows `ORDER BY price` | none · `(price)` · `(available, price)` | no LIMIT vs LIMIT 20 |
| P3S | same, no index | `sort_buffer_size` 32K / 256K / 4M / 64M → `Sort_merge_passes` | external-sort evidence (stretch goal) |
| P4 | Q7 (Venice) and Q7b (citywide) | junction PK + reverse `(amenity_id, listing_id)` vs PK only | most common amenity vs `Pool` |
| Q3 | top 20 hosts | baseline | supporting aggregation analysis |

Outputs: `results/summary.csv` (one row per config × variant: cost, access path, filesort,
plan operators, run-1 and median ms, handler/sort/buffer-pool counters, index MB),
`results/experiments/<ID>.json` (everything, including every run), and
`results/experiments/plans/*.txt` (SQL + estimated tree + EXPLAIN ANALYZE, which can be pasted into the report).

Options: `--p3-window month` if the full-table sort is too slow on a laptop (the proposal's fallback),
`--p1-limit 0` to drop the LIMIT in P1, `--nbhd`, `--rare-amenity`, `--compare-date`.
The script **always restores the baseline design** at the end (drops `xp_*` indexes, restores the reverse index + FK).

## Design notes worth knowing for the demo / report

- **Calendar prices:** recent Inside Airbnb snapshots often leave `calendar.price` blank. `02_clean.py --calendar-price auto`
  detects this (>95% blank in the first 1M rows), copies the listing-level price into `calendar.price`, and records it in
  `clean_manifest.json → decisions.calendar_price_source`. Say so in the report (the proposal's planned mitigation).
  P1/P3 test index and sort behaviour, not price accuracy.
- **"Marked available"** ≠ booked. The UI and queries use that wording throughout.
- **P4 gotcha:** `idx_amenity_listing` also backs the `amenity_id` foreign key, so InnoDB refuses to drop it while the FK
  exists. The runner drops the FK first and re-adds both afterwards. Also worth saying in the report: an InnoDB secondary
  index on `(amenity_id)` alone would physically store `(amenity_id, listing_id)` anyway, because the PK columns are appended.
- **InnoDB secondary indexes carry the PK** `(listing_id, cal_date)`, so `INDEX(cal_date, available)` is *covering* for Q4
  and `INDEX(available, price)` is covering for P3. That explains several "no table lookup" plans.
- **Hash join:** MySQL 8 has nested-loop variants and hash join but no sort-merge join. P2's hinted configuration
  (`NO_INDEX` hints) forces the hash-join plan so it can be measured against the index nested-loop plan MySQL picks itself.
- **EXPLAIN ANALYZE overhead:** it times every iterator, so its absolute times run a bit higher than plain execution.
  For small-result queries the runner also records plain client wall-clock (`median_exec_ms`). Compare configurations with the same metric.
- **Load speed:** FK and unique checks are off during `LOAD DATA`; integrity is then verified with orphan queries (all must be 0).
- IDs are kept as strings during cleaning, so 18–19-digit listing IDs never pass through float.

## Attribution

Data: Inside Airbnb, http://insideairbnb.com/ (CC BY 4.0). Analytical classroom project, not affiliated with Airbnb.
