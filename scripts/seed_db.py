"""One-off: build retail.duckdb from the seed files in $RETAIL_DATA_DIR/seed/.

Run once, from the repo root:

    python scripts/seed_db.py

Refuses to run if the database already has history, so it can't double-load.
"""

from transfer_engine.settings import load_settings
from transfer_engine.storage import connect, seed_from_csv


def main() -> None:
    settings = load_settings()
    print(f"Seeding {settings.db_path} from {settings.seed_dir} ...")
    with connect(settings.db_path) as con:
        counts = seed_from_csv(con, settings.seed_dir)
    for table, rows in counts.items():
        print(f"  {table}: {rows:,} rows")


if __name__ == "__main__":
    main()
