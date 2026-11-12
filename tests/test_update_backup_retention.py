import os
from pathlib import Path

import update_guard


def test_update_backup_retention_preserves_pending_recovery_point(tmp_path):
    recovery = Path(tmp_path)
    backups = []
    for i in range(12):
        path = recovery / ("before-update-%02d.sqlite3" % i)
        path.write_bytes(b"verified-backup")
        os.utime(path, (1000 + i, 1000 + i))
        backups.append(path)
    unrelated = recovery / "manual-backup.sqlite3"
    unrelated.write_bytes(b"manual")
    protected = backups[0]  # Oldest file, but still referenced by pending update state.

    removed = update_guard.prune_database_backups(
        str(recovery), keep=10, protected=[str(protected)])

    assert removed == 2
    remaining = sorted(recovery.glob("before-update-*.sqlite3"))
    assert len(remaining) == 10
    assert protected.exists()
    assert unrelated.exists()
