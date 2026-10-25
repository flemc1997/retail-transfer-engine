"""Save a checked weekly export: back up, replace the week, then delete the export file.

The order matters. The backup is taken before anything changes; the export file is
deleted only once the save has committed, so a failed save leaves both the master and
the export untouched and the upload can simply be retried.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from transfer_engine.export import ParsedExport
from transfer_engine.settings import Settings
from transfer_engine.storage import backup_database, connect, replace_week


@dataclass(frozen=True)
class UploadResult:
    """What a successful upload did, for the confirmation message."""

    rows_deleted: int
    rows_inserted: int
    categories_changed: int
    norms_refreshed: bool
    backup_path: Path


def save_upload(settings: Settings, parsed: ParsedExport, export_path: Path) -> UploadResult:
    """Steps 3-5 of the agreed weekly upload. Call with no other connection open."""
    backup_path = backup_database(
        settings.db_path, settings.backup_dir, settings.upload.backups_to_keep
    )
    with connect(settings.db_path) as con:
        saved = replace_week(con, parsed.history, parsed.norms)
    export_path.unlink()
    return UploadResult(backup_path=backup_path, **saved)
