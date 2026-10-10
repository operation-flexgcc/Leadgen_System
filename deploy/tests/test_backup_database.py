import importlib.util
import os
from pathlib import Path
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location("backup_database", Path(__file__).resolve().parents[1] / "venv/backup_database.py")
backup = importlib.util.module_from_spec(spec)
spec.loader.exec_module(backup)


class BackupEnvironmentTests(unittest.TestCase):
    def test_explicit_connection_settings_and_password_decoding(self):
        with patch.dict(os.environ, {"PGHOST": "wrong-host", "PGDATABASE": "wrong-db", "PGPASSWORD": "wrong-password", "PGSERVICE": "wrong-service"}):
            environment = backup.dump_environment("postgresql://app:p%40ss%3Aword@db.example.com:5433/leadgen?sslmode=require&connect_timeout=20")
        self.assertEqual(environment["PGHOST"], "db.example.com")
        self.assertEqual(environment["PGPORT"], "5433")
        self.assertEqual(environment["PGDATABASE"], "leadgen")
        self.assertEqual(environment["PGUSER"], "app")
        self.assertEqual(environment["PGPASSWORD"], "p@ss:word")
        self.assertEqual(environment["PGSSLMODE"], "require")
        self.assertEqual(environment["PGCONNECT_TIMEOUT"], "20")
        self.assertNotIn("PGSERVICE", environment)

    def test_sqlite_missing_and_malformed_urls_are_rejected_without_leaking_secrets(self):
        for value in ["", "sqlite:///db.sqlite3", "postgresql://test:private-secret@db.example.com/db?invalid_parameter=value"]:
            with self.subTest(value=value):
                try:
                    backup.dump_environment(value)
                except ValueError as error:
                    self.assertNotIn("private-secret", str(error))
                else:
                    self.fail("Invalid URL accepted")
