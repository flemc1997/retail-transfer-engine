"""Write a complete made-up data folder, so the app runs without the real data.

Run from the repo root:

    python scripts/make_synthetic_data.py                 # -> data/local/synthetic/
    python scripts/make_synthetic_data.py /some/folder --seed 3 --misbought-share 0.6

Writes seed/history.csv.gz, seed/norms.csv, config.json (a copy of the example config)
and the next week's Power BI-style export to try the upload with. Then follow the
printed steps to seed the database and start the app on it.
"""

import argparse
import shutil
from pathlib import Path

from transfer_engine.settings import DEFAULT_CONFIG_PATH, REPO_ROOT, load_config
from transfer_engine.synthetic import MISBOUGHT_SHARE, generate

DEFAULT_FOLDER = REPO_ROOT / "data" / "local" / "synthetic"  # gitignored


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("folder", nargs="?", type=Path, default=DEFAULT_FOLDER)
    parser.add_argument("--last-week", default="2026-42", help="last week in the seed history, as YYYY-WW")
    parser.add_argument("--weeks", type=int, default=95, help="weeks of history (over 52 gives last-year comparisons)")
    parser.add_argument("--seed", type=int, default=7, help="random seed: the same seed gives the same data")
    parser.add_argument("--misbought-share", type=float, default=MISBOUGHT_SHARE,
                        help="share of fashion lines with most stock where they sell least")
    args = parser.parse_args()

    folder: Path = args.folder.expanduser().resolve()
    if (folder / "retail.duckdb").exists():
        raise SystemExit(f"{folder} already has a database; pick an empty folder or delete it first.")
    yr, wk = (int(x) for x in args.last_week.split("-"))
    *_, mapping, _ = load_config(DEFAULT_CONFIG_PATH)

    history, norms, export = generate(mapping, (yr, wk), n_weeks=args.weeks, seed=args.seed,
                                      misbought_share=args.misbought_share)

    (folder / "seed").mkdir(parents=True, exist_ok=True)
    history.to_csv(folder / "seed" / "history.csv.gz", index=False)
    norms.to_csv(folder / "seed" / "norms.csv", index=False)
    shutil.copy(DEFAULT_CONFIG_PATH, folder / "config.json")
    export_week = export["Week Name Year"].iat[0].replace("Week ", "week").replace(", ", "-")
    export_path = folder / f"{export_week}.xlsx"
    export.to_excel(export_path, index=False, sheet_name="Export")

    print(f"Wrote {folder}")
    print(f"  seed/history.csv.gz  {len(history):,} rows, {history['sty'].nunique()} styles")
    print(f"  seed/norms.csv       {len(norms):,} rows")
    print(f"  {export_path.name:<20} next week's export, to try the upload")
    env = f"RETAIL_DATA_DIR={folder} RETAIL_CONFIG={folder / 'config.json'}"
    print("\nTo use it:")
    print(f"  {env} python scripts/seed_db.py")
    print(f"  {env} streamlit run app.py")


if __name__ == "__main__":
    main()
