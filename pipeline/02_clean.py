"""Step 2 - Clean and transform raw Inside Airbnb files into load-ready TSV files.

Output (data/clean/): one tab-separated file per table in MySQL's default
LOAD DATA format (\\N = NULL, backslash-escaped tabs/newlines), plus
clean_manifest.json with row counts, dropped rows and decisions taken.

Transformations (proposal section C):
  * currency strings -> DECIMAL values; t/f -> 1/0
  * neighbourhood labels normalized; NULL kept for missing rating/price/bedrooms/license
  * amenity JSON arrays -> deduplicated amenity table + listing_amenity junction
  * reviewer names removed (reviewer_id kept)
  * calendar/review rows whose listing is not in listings are dropped (FK integrity)

Usage: python pipeline/02_clean.py [--calendar-price auto|source|listing]
"""
import argparse
import csv
import json
import re
import sys
import unicodedata
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import config  # noqa: E402

CHUNK = 1_000_000
ROOM_TYPES = {"Entire home/apt", "Private room", "Shared room", "Hotel room"}
CTRL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
report = {"decisions": {}, "rows": {}, "dropped": {}}


# ---------------------------------------------------------------- helpers
def clean_text(s: pd.Series, maxlen=None) -> pd.Series:
    s = s.astype("string").str.replace(CTRL, "", regex=True).str.strip()
    s = s.mask(s == "")
    if maxlen:
        s = s.str.slice(0, maxlen)
    return s


def money(s: pd.Series) -> pd.Series:
    s = s.astype("string").str.replace(r"[$,\s]", "", regex=True)
    v = pd.to_numeric(s, errors="coerce")
    return v.where((v >= 0) & (v < 1_000_000))  # DECIMAL(8,2) range


def tf(s: pd.Series) -> pd.Series:
    return s.map({"t": "1", "f": "0", True: "1", False: "0"}).astype("object")


def int_str(s: pd.Series, lo=0, hi=None) -> pd.Series:
    v = pd.to_numeric(s, errors="coerce")
    ok = v.notna() & (v >= lo) & ((v <= hi) if hi is not None else True)
    return v.where(ok).map(lambda x: str(int(x)), na_action="ignore")


def dec_str(v: pd.Series, nd: int) -> pd.Series:
    return v.map(lambda x: f"{x:.{nd}f}", na_action="ignore")


def date_str(s: pd.Series) -> pd.Series:
    d = pd.to_datetime(s, errors="coerce", format="%Y-%m-%d")
    return d.dt.strftime("%Y-%m-%d").where(d.notna())


def id_ok(s: pd.Series) -> pd.Series:
    """IDs stay strings end-to-end so 18-digit values never pass through float."""
    return s.astype("string").str.strip().str.fullmatch(r"\d{1,20}").fillna(False)


def write_tsv(df: pd.DataFrame, path: Path, append=False):
    out = df.copy()
    for c in out.columns:
        col = out[c].astype("object")
        mask = col.notna()
        col[mask] = (col[mask].astype(str)
                     .str.replace("\\", "\\\\", regex=False)
                     .str.replace("\t", "\\t", regex=False)
                     .str.replace("\n", "\\n", regex=False)
                     .str.replace("\r", "\\r", regex=False))
        out[c] = col
    out.to_csv(path, sep="\t", header=False, index=False, na_rep="\\N",
               quoting=csv.QUOTE_NONE, quotechar="\x01", lineterminator="\n",
               mode="a" if append else "w", encoding="utf-8")


def norm_label(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip()


def collation_key(s: str) -> str:
    """Approximate utf8mb4_0900_ai_ci equality (accent + case insensitive)."""
    s = unicodedata.normalize("NFKD", s)
    s = "".join(ch for ch in s if not unicodedata.combining(ch))
    return re.sub(r"\s+", " ", s).strip().casefold()


# ---------------------------------------------------------------- steps
def clean_neighbourhoods(listings: pd.DataFrame) -> dict:
    src = pd.read_csv(config.RAW_DIR / "neighbourhoods.csv", dtype=str)
    src["neighbourhood"] = src["neighbourhood"].map(norm_label)
    groups = dict(zip(src["neighbourhood"], src["neighbourhood_group"].map(
        lambda g: norm_label(g) if isinstance(g, str) else None)))
    # neighbourhoods that appear in listings but not in the reference file
    extra = (listings[["nb", "nb_group"]].dropna(subset=["nb"])
             .drop_duplicates("nb"))
    added = 0
    for nb, g in extra.itertuples(index=False):
        if nb not in groups:
            groups[nb] = g if isinstance(g, str) else None
            added += 1
    # dedupe under case/accent-insensitive collation
    seen, names = set(), []
    for n in sorted(groups):
        k = collation_key(n)
        if k not in seen:
            seen.add(k)
            names.append(n)
    ids = {n: i + 1 for i, n in enumerate(names)}
    key_to_id = {collation_key(n): i for n, i in ids.items()}
    df = pd.DataFrame({"neighbourhood_id": [str(ids[n]) for n in names],
                       "name": [n[:100] for n in names],
                       "group_name": [groups[n][:60] if groups[n] else None for n in names]})
    write_tsv(df, config.CLEAN_DIR / "neighbourhood.tsv")
    report["rows"]["neighbourhood"] = len(df)
    report["decisions"]["neighbourhoods_added_from_listings"] = added
    return key_to_id


def clean_listings():
    want = ["id", "name", "host_id", "host_since", "host_is_superhost",
            "host_listings_count", "neighbourhood_cleansed",
            "neighbourhood_group_cleansed", "latitude", "longitude", "room_type",
            "accommodates", "bedrooms", "price", "review_scores_rating",
            "license", "amenities"]
    raw = pd.read_csv(config.RAW_DIR / "listings.csv.gz", dtype=str,
                      usecols=lambda c: c in want, keep_default_na=False,
                      na_values=[""])
    for c in want:
        if c not in raw.columns:
            raw[c] = pd.NA
            report["decisions"].setdefault("missing_source_columns", []).append(c)
    n0 = len(raw)

    raw["nb"] = raw["neighbourhood_cleansed"].map(
        lambda x: norm_label(x) if isinstance(x, str) else None)
    raw["nb_group"] = raw["neighbourhood_group_cleansed"]
    ok = (id_ok(raw["id"]) & id_ok(raw["host_id"]) & raw["nb"].notna()
          & raw["room_type"].isin(ROOM_TYPES))
    report["dropped"]["listings_invalid_id_nbhd_or_room_type"] = int((~ok).sum())
    raw = raw[ok].drop_duplicates("id")
    report["dropped"]["listings_duplicate_id"] = int(n0 - report["dropped"]["listings_invalid_id_nbhd_or_room_type"] - len(raw))

    nb_ids = clean_neighbourhoods(raw)

    # ---- host (one row per host_id; attributes repeat on each listing in source)
    h = raw.drop_duplicates("host_id")
    host = pd.DataFrame({
        "host_id": h["host_id"].str.strip(),
        "host_since": date_str(h["host_since"]),
        "is_superhost": tf(h["host_is_superhost"]),
        "source_listings_count": int_str(h["host_listings_count"], 0, 65535),
    })
    write_tsv(host, config.CLEAN_DIR / "host.tsv")
    report["rows"]["host"] = len(host)

    # ---- listing
    lat = pd.to_numeric(raw["latitude"], errors="coerce")
    lon = pd.to_numeric(raw["longitude"], errors="coerce")
    score = pd.to_numeric(raw["review_scores_rating"], errors="coerce")
    score = score.where((score >= 0) & (score <= 5))
    price = money(raw["price"])
    listing = pd.DataFrame({
        "listing_id": raw["id"].str.strip(),
        "host_id": raw["host_id"].str.strip(),
        "neighbourhood_id": raw["nb"].map(lambda n: str(nb_ids[collation_key(n)])),
        "name": clean_text(raw["name"], 255),
        "room_type": raw["room_type"],
        "latitude": dec_str(lat.where(lat.between(-90, 90)), 6),
        "longitude": dec_str(lon.where(lon.between(-180, 180)), 6),
        "accommodates": int_str(raw["accommodates"], 0, 255),
        "bedrooms": int_str(raw["bedrooms"], 0, 255),
        "price": dec_str(price, 2),
        "review_score": dec_str(score, 2),
        "license": clean_text(raw["license"], 100),
    })
    write_tsv(listing, config.CLEAN_DIR / "listing.tsv")
    report["rows"]["listing"] = len(listing)
    report["decisions"]["listing_price_null"] = int(price.isna().sum())
    report["decisions"]["listing_review_score_null"] = int(score.isna().sum())

    # ---- amenities (packed JSON array -> amenity + listing_amenity)
    amen_ids, display, pairs, bad = {}, [], [], 0
    for lid, arr in zip(listing["listing_id"], raw["amenities"]):
        if not isinstance(arr, str) or not arr.strip():
            continue
        try:
            items = json.loads(arr)
        except json.JSONDecodeError:
            bad += 1
            continue
        seen = set()
        for a in items:
            if not isinstance(a, str):
                continue
            a = CTRL.sub("", norm_label(a))[:150]
            if not a:
                continue
            k = collation_key(a)
            if k not in amen_ids:
                amen_ids[k] = len(display) + 1
                display.append(a)
            aid = amen_ids[k]
            if aid not in seen:
                seen.add(aid)
                pairs.append((lid, str(aid)))
    write_tsv(pd.DataFrame({"amenity_id": [str(i + 1) for i in range(len(display))],
                            "amenity_name": display}),
              config.CLEAN_DIR / "amenity.tsv")
    write_tsv(pd.DataFrame(pairs, columns=["listing_id", "amenity_id"]),
              config.CLEAN_DIR / "listing_amenity.tsv")
    report["rows"]["amenity"] = len(display)
    report["rows"]["listing_amenity"] = len(pairs)
    report["dropped"]["listings_unparseable_amenities"] = bad

    return set(listing["listing_id"]), dict(zip(listing["listing_id"], price))


def clean_calendar(valid_ids: set, listing_price: dict, price_mode: str):
    path = config.CLEAN_DIR / "calendar.tsv"
    path.unlink(missing_ok=True)
    total = kept = orphans = dups = 0
    fill = None
    want = ["listing_id", "date", "available", "price", "minimum_nights"]
    reader = pd.read_csv(config.RAW_DIR / "calendar.csv.gz", dtype=str,
                         usecols=lambda c: c in want,
                         keep_default_na=False, na_values=[""], chunksize=CHUNK)
    for i, ch in enumerate(reader):
        missing = [c for c in want if c not in ch.columns]
        if i == 0 and missing:
            report["decisions"]["calendar_missing_source_columns"] = missing
            if {"listing_id", "date", "available"} & set(missing):
                sys.exit(f"calendar.csv.gz lacks required columns: {missing}")
        for c in missing:
            ch[c] = pd.NA
        total += len(ch)
        src_price = money(ch["price"])
        if fill is None:  # decide once, from the first chunk
            null_frac = float(src_price.isna().mean()) if len(ch) else 1.0
            fill = (price_mode == "listing" or
                    (price_mode == "auto" and null_frac > 0.95))
            report["decisions"]["calendar_price_null_fraction_first_chunk"] = round(null_frac, 4)
            report["decisions"]["calendar_price_source"] = (
                "listing-level price copied into calendar.price (source calendar prices blank)"
                if fill else "source calendar price")
        lid = ch["listing_id"].astype("string").str.strip()
        in_fk = lid.isin(valid_ids)
        orphans += int((~in_fk).sum())
        df = pd.DataFrame({
            "listing_id": lid,
            "cal_date": date_str(ch["date"]),
            "available": tf(ch["available"].str.strip()),
            "price": (lid.map(listing_price) if fill else src_price),
            "minimum_nights": int_str(ch["minimum_nights"], 0, 65535),
        })[in_fk]
        df = df.dropna(subset=["cal_date", "available"])
        before = len(df)
        df = df.drop_duplicates(["listing_id", "cal_date"])
        dups += before - len(df)
        df["price"] = dec_str(pd.to_numeric(df["price"]), 2)
        write_tsv(df, path, append=True)
        kept += len(df)
        print(f"    calendar chunk {i + 1}: {kept:,} rows written")
    report["rows"]["calendar"] = kept
    report["dropped"]["calendar_orphan_listing"] = orphans
    report["dropped"]["calendar_bad_or_duplicate"] = total - orphans - kept


def clean_reviews(valid_ids: set):
    path = config.CLEAN_DIR / "review.tsv"
    path.unlink(missing_ok=True)
    kept = orphans = bad = 0
    want = ["listing_id", "id", "date", "reviewer_id", "comments"]
    reader = pd.read_csv(config.RAW_DIR / "reviews.csv.gz", dtype=str,
                         usecols=lambda c: c in want,
                         keep_default_na=False, na_values=[""], chunksize=CHUNK)
    seen = set()
    for ch in reader:
        for c in want:
            if c not in ch.columns:
                if c in ("listing_id", "id", "date"):
                    sys.exit(f"reviews.csv.gz lacks required column {c} - is it the detailed reviews file?")
                ch[c] = pd.NA
        lid = ch["listing_id"].astype("string").str.strip()
        in_fk = lid.isin(valid_ids)
        orphans += int((~in_fk).sum())
        df = pd.DataFrame({
            "review_id": ch["id"].astype("string").str.strip(),
            "listing_id": lid,
            "review_date": date_str(ch["date"]),
            "reviewer_id": ch["reviewer_id"].where(id_ok(ch["reviewer_id"])),
            "comments": clean_text(ch["comments"].str.replace(r"<br\s*/?>", "\n", regex=True)),
        })[in_fk]
        good = id_ok(df["review_id"]) & df["review_date"].notna() & ~df["review_id"].isin(seen)
        bad += int((~good).sum())
        df = df[good].drop_duplicates("review_id")
        seen.update(df["review_id"])
        write_tsv(df, path, append=True)
        kept += len(df)
    report["rows"]["review"] = kept
    report["dropped"]["review_orphan_listing"] = orphans
    report["dropped"]["review_bad_or_duplicate"] = bad


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--calendar-price", choices=["auto", "source", "listing"], default="auto",
                    help="auto: copy listing price into calendar if source calendar prices are blank")
    args = ap.parse_args()
    config.CLEAN_DIR.mkdir(parents=True, exist_ok=True)

    print("Cleaning listings, hosts, neighbourhoods, amenities ...")
    valid_ids, listing_price = clean_listings()
    print("Cleaning calendar (chunked) ...")
    clean_calendar(valid_ids, listing_price, args.calendar_price)
    print("Cleaning reviews (chunked) ...")
    clean_reviews(valid_ids)

    out = config.CLEAN_DIR / "clean_manifest.json"
    out.write_text(json.dumps(report, indent=2))
    print(json.dumps(report, indent=2))
    print(f"Wrote {out}")


if __name__ == "__main__":
    main()
