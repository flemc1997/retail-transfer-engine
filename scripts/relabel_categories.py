"""One-off: switch history from the legacy categories to Power BI's master categories.

Run once, from the repo root, BEFORE uploading that export (the upload deletes it):

    python scripts/relabel_categories.py ~/retail-data/week41.xlsx

1. Styles in the export get their master category in every week.
2. Older styles (no longer sold) get the master category their old category most
   often became among the styles in step 1.

Prints the translation so it can be reviewed. Takes a backup first. Reads the export
but doesn't delete it.
"""

import sys
from pathlib import Path

from transfer_engine.export import parse_export, read_export
from transfer_engine.settings import load_settings
from transfer_engine.storage import (
    backup_database,
    category_translation,
    connect,
    set_style_categories,
    translate_old_categories,
)


def main(export_path: Path) -> None:
    settings = load_settings()
    parsed = parse_export(read_export(export_path), settings.export)
    styles = parsed.history[["sty", "cat"]].drop_duplicates("sty")

    backup = backup_database(settings.db_path, settings.backup_dir, settings.upload.backups_to_keep)
    print(f"Backup: {backup}")

    with connect(settings.db_path) as con:
        translation = category_translation(con, styles)
        con.execute("BEGIN")
        try:
            from_export = set_style_categories(con, styles)
            from_translation = translate_old_categories(con, translation, styles["sty"].tolist())
            con.execute("COMMIT")
        except Exception:
            con.execute("ROLLBACK")
            raise
        remaining = con.execute(
            "SELECT dept, cat, count(DISTINCT sty) AS styles FROM history GROUP BY ALL ORDER BY ALL"
        ).df()

    changed = translation[translation["old_cat"] != translation["new_cat"]]
    print("\nOld category -> master category (styles behind the choice / behind the runner-up):")
    print(changed.to_string(index=False))
    ties = changed[changed["styles"] == changed["runner_up_styles"]]
    if len(ties):
        print(f"\nTies, decided alphabetically: {len(ties)}. Check these by hand.")
    print(f"\nRows relabelled from the export: {from_export:,}")
    print(f"Rows relabelled by translation:  {from_translation:,}")
    print("\nCategories now in history (dept, category, styles):")
    print(remaining.to_string(index=False))


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit("Usage: python scripts/relabel_categories.py <export.xlsx>")
    main(Path(sys.argv[1]).expanduser())
