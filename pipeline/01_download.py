"""Step 1 - Download the pinned Inside Airbnb LA snapshot and record provenance.

Raw files are never modified. A manifest with source URL, snapshot date,
SHA-256 checksum, byte size and raw row count is written to data/raw/manifest.json.

Usage:  python pipeline/01_download.py            # download missing files
        python pipeline/01_download.py --force    # re-download everything
        python pipeline/01_download.py --manifest-only   # files already in data/raw
"""
import argparse
import csv
import gzip
import hashlib
import json
import sys
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import config  # noqa: E402

csv.field_size_limit(2**31 - 1)


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def count_rows(path: Path) -> int:
    """Count CSV records (not physical lines: review comments contain newlines)."""
    opener = gzip.open if path.suffix == ".gz" else open
    with opener(path, "rt", encoding="utf-8", newline="") as f:
        return sum(1 for _ in csv.reader(f)) - 1  # minus header


def download(url: str, dest: Path):
    print(f"  downloading {url}")
    req = urllib.request.Request(url, headers={"User-Agent": "DSCI551-course-project"})
    with urllib.request.urlopen(req) as r, open(dest, "wb") as out:
        while chunk := r.read(1 << 20):
            out.write(chunk)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--manifest-only", action="store_true")
    args = ap.parse_args()

    config.RAW_DIR.mkdir(parents=True, exist_ok=True)
    manifest = {"snapshot_date": config.SNAPSHOT_DATE,
                "license": "Inside Airbnb, CC BY 4.0 (attribution required)",
                "created_utc": datetime.now(timezone.utc).isoformat(),
                "files": {}}
    for name, url in config.SOURCE_FILES.items():
        dest = config.RAW_DIR / name
        if not args.manifest_only and (args.force or not dest.exists()):
            download(url, dest)
        if not dest.exists():
            sys.exit(f"Missing {dest}. Download it manually from {url}")
        print(f"  checksumming + counting {name}")
        manifest["files"][name] = {
            "url": url,
            "bytes": dest.stat().st_size,
            "sha256": sha256(dest),
            "raw_rows": count_rows(dest),
        }
        print(f"    {manifest['files'][name]['raw_rows']:,} rows")

    out = config.RAW_DIR / "manifest.json"
    out.write_text(json.dumps(manifest, indent=2))
    print(f"Wrote {out}")


if __name__ == "__main__":
    main()
