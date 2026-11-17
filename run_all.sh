#!/usr/bin/env bash
# Full reproducible build: download -> clean -> load -> experiments -> MongoDB.
# Usage: ./run_all.sh            (real Inside Airbnb snapshot)
#        SYNTHETIC=1 ./run_all.sh (synthetic data, for testing only)
set -euo pipefail
cd "$(dirname "$0")"

if [[ "${SYNTHETIC:-0}" == "1" ]]; then
  export RAW_DIR=data/synthetic_raw
  python tools/make_synthetic.py --out "$RAW_DIR" --listings "${LISTINGS:-6000}" --blank-calendar-price
  python pipeline/01_download.py --manifest-only
else
  python pipeline/01_download.py
fi
python pipeline/02_clean.py
python pipeline/03_load_mysql.py
python experiments/run_experiments.py --runs "${RUNS:-5}"
if python -c "import pymongo,config; pymongo.MongoClient(config.MONGO_URI, serverSelectionTimeoutMS=2000).admin.command('ping')" 2>/dev/null; then
  python mongo/load_mongo.py
  python -m mongo.compare
else
  echo "MongoDB not running - skipping mongo/ steps"
fi
echo "Done. Start the app with: streamlit run app/streamlit_app.py"
