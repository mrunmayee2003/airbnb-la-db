"""Shared configuration. Override any value with an environment variable."""
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent

# ---- Source snapshot (pinned for the whole project) ----
SNAPSHOT_DATE = os.getenv("SNAPSHOT_DATE", "2026-09-14")
BASE_URL = os.getenv(
    "BASE_URL",
    f"https://data.insideairbnb.com/united-states/ca/los-angeles/{SNAPSHOT_DATE}",
)
SOURCE_FILES = {
    "listings.csv.gz": f"{BASE_URL}/data/listings.csv.gz",
    "calendar.csv.gz": f"{BASE_URL}/data/calendar.csv.gz",
    "reviews.csv.gz": f"{BASE_URL}/data/reviews.csv.gz",
    "neighbourhoods.csv": f"{BASE_URL}/visualisations/neighbourhoods.csv",
}

# ---- Local folders ----
RAW_DIR = Path(os.getenv("RAW_DIR", ROOT / "data" / "raw"))
CLEAN_DIR = Path(os.getenv("CLEAN_DIR", ROOT / "data" / "clean"))
RESULTS_DIR = Path(os.getenv("RESULTS_DIR", ROOT / "results"))

# ---- MySQL ----
MYSQL = dict(
    host=os.getenv("MYSQL_HOST", "127.0.0.1"),
    port=int(os.getenv("MYSQL_PORT", "3306")),
    user=os.getenv("MYSQL_USER", "dsci"),
    password=os.getenv("MYSQL_PASSWORD", "dsci551"),
    database=os.getenv("MYSQL_DB", "airbnb_la"),
)

# ---- MongoDB ----
MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")
MONGO_DB = os.getenv("MONGO_DB", "airbnb_la")
MONGO_RECENT_REVIEWS = 20  # cap on embedded reviews per listing document


def mysql_conn(database=True, **extra):
    """Return a PyMySQL connection (local_infile enabled for LOAD DATA)."""
    import pymysql

    kw = dict(MYSQL)
    if not database:
        kw.pop("database")
    kw.update(local_infile=True, charset="utf8mb4", autocommit=True)
    kw.update(extra)
    return pymysql.connect(**kw)
