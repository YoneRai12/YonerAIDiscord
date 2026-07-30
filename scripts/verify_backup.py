from __future__ import annotations

import argparse
import sqlite3
from dataclasses import dataclass
from pathlib import Path


_REQUIRED_TABLES = frozenset(
    {
        "audit_log",
        "capability_override",
        "evolution_proposal",
        "module_override",
        "permission_override",
        "schema_migrations",
    }
)


@dataclass(frozen=True, slots=True)
class BackupVerification:
    quick_check: tuple[str, ...]
    schema_versions: tuple[int, ...]
    tables: frozenset[str]

    @property
    def valid(self) -> bool:
        return self.quick_check == ("ok",) and bool(self.schema_versions) and _REQUIRED_TABLES <= self.tables


def verify_backup(path: Path) -> BackupVerification:
    source = Path(path)
    if source.is_symlink() or not source.is_file():
        raise ValueError("backup must be a regular non-symlink file")
    uri = source.resolve().as_uri() + "?mode=ro&immutable=1"
    try:
        with sqlite3.connect(uri, uri=True) as connection:
            quick_check = tuple(str(row[0]) for row in connection.execute("PRAGMA quick_check"))
            tables = frozenset(
                str(row[0]) for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
            )
            versions = tuple(
                int(row[0]) for row in connection.execute("SELECT version FROM schema_migrations ORDER BY version")
            )
    except sqlite3.Error as exc:
        raise ValueError("backup is not a readable suite database") from exc
    return BackupVerification(quick_check, versions, tables)


def main() -> int:
    parser = argparse.ArgumentParser(description="SQLite backupをread-onlyで復元前検査")
    parser.add_argument("backup", type=Path)
    args = parser.parse_args()
    try:
        report = verify_backup(args.backup)
    except ValueError:
        print("backup verification: FAILED")
        return 1
    if not report.valid:
        print("backup verification: FAILED")
        return 1
    print(f"backup verification: OK ({args.backup.name}, schema={report.schema_versions[-1]})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
