"""Subprocess probes for integration tests.

Kept outside the test module so each probe gets a clean Django process and an
isolated SQLite database.
"""

import json
import os
import sqlite3
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[4]


def prepare_database(db_path: str) -> None:
    os.environ["DB_NAME"] = db_path
    os.environ.setdefault("DB_USER", "probe")
    os.environ.setdefault("DB_PASSWORD", "probe")
    os.environ.setdefault("DJANGO_SECRET_KEY", "integration-probe-secret")
    os.environ.setdefault("DJANGO_DEBUG", "True")
    os.environ["DJANGO_SETTINGS_MODULE"] = "probe_settings"
    os.environ["REDIS_CACHE_URL"] = ""
    sys.path.insert(0, str(REPO_ROOT))
    sys.path.insert(0, str(Path(__file__).resolve().parent))


def migration_probe(db_path: str) -> dict[str, Any]:
    prepare_database(db_path)

    import django

    django.setup()

    from django.core.management import call_command

    call_command("migrate", "accounts", "0001", verbosity=0, interactive=False)

    connection = sqlite3.connect(db_path)
    connection.executemany(
        "INSERT INTO accounts_user "
        "(password, last_login, is_superuser, first_name, last_name, is_staff, "
        "is_active, date_joined, email) "
        "VALUES (?, NULL, 0, '', '', 0, 1, '2026-01-01 00:00:00', ?)",
        [
            ("hashed", "Alice@One.COM"),
            ("hashed", "alice@two.com"),
            ("hashed", "bob@x.com"),
        ],
    )
    connection.commit()
    connection.close()

    call_command("migrate", "accounts", "0002", verbosity=0, interactive=False)

    connection = sqlite3.connect(db_path)
    cursor = connection.cursor()
    columns = {
        row[1]: {"notnull": bool(row[3]), "pk": bool(row[5])}
        for row in cursor.execute("PRAGMA table_info(accounts_user)")
    }
    unique_indexes = []
    index_rows = list(cursor.execute("PRAGMA index_list(accounts_user)"))
    for index in index_rows:
        if index[2]:
            unique_indexes.append(
                [row[2] for row in cursor.execute(f'PRAGMA index_info("{index[1]}")')]
            )
    rows = [
        list(row)
        for row in cursor.execute(
            "SELECT username, email, lastname FROM accounts_user ORDER BY id"
        )
    ]
    connection.close()

    return {
        "columns": columns,
        "unique_indexes": unique_indexes,
        "rows": rows,
    }


def startup_probe(db_path: str) -> dict[str, Any]:
    prepare_database(db_path)
    os.environ["AUTO_MIGRATE"] = "True"

    import config.wsgi  # noqa: F401

    connection = sqlite3.connect(db_path)
    tables = sorted(
        row[0]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
    )
    account_columns = [
        row[1] for row in connection.execute("PRAGMA table_info(accounts_user)")
    ]
    connection.close()

    return {"tables": tables, "account_columns": account_columns}


def main() -> None:
    mode = sys.argv[1]
    db_path = sys.argv[2]
    if mode == "migration":
        result = migration_probe(db_path)
    elif mode == "startup":
        result = startup_probe(db_path)
    else:
        raise SystemExit(f"unknown probe mode: {mode}")
    print(json.dumps(result))


if __name__ == "__main__":
    main()
