import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

PROBE = Path(__file__).with_name("probe.py")
REPO_ROOT = Path(__file__).resolve().parents[4]


def run_probe(mode: str, db_path: Path) -> dict[str, Any]:
    env = os.environ.copy()
    env.update(
        {
            "DB_ENGINE": "sqlite",
            "DB_NAME": str(db_path),
            "DJANGO_SETTINGS_MODULE": "config.settings",
            "AUTO_MIGRATE": "True",
            "REDIS_CACHE_URL": "",
        }
    )
    completed = subprocess.run(
        [sys.executable, str(PROBE), mode, str(db_path)],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    return json.loads(completed.stdout.strip().splitlines()[-1])


@pytest.mark.integration
def test_migration_0002_upgrades_existing_accounts(tmp_path):
    result = run_probe("migration", tmp_path / "legacy.sqlite3")

    assert result["rows"] == [
        ["alice", "alice@one.com", ""],
        ["alice2", "alice@two.com", ""],
        ["bob", "bob@x.com", ""],
    ]
    assert result["columns"]["username"]["notnull"] is True
    assert result["columns"]["email"]["notnull"] is True
    assert result["columns"]["lastname"]["notnull"] is True
    assert ["username"] in result["unique_indexes"]
    assert ["email"] in result["unique_indexes"]


@pytest.mark.integration
def test_wsgi_startup_creates_account_and_blacklist_tables(tmp_path):
    result = run_probe("startup", tmp_path / "startup.sqlite3")

    assert "accounts_user" in result["tables"]
    assert "token_blacklist_outstandingtoken" in result["tables"]
    assert "username" in result["account_columns"]
    assert "lastname" in result["account_columns"]
    assert "last_name" not in result["account_columns"]
