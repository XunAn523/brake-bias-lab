"""Snapshot/transaction tests with a SQLite-backed DB-API transport double.

This exercises payload round trips and rollback, not MySQL server compatibility.
"""
from dataclasses import replace
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import ssl
import tempfile
import unittest
from unittest.mock import patch

from brake_calculator import HistoryStore, VehicleSpec
from mysql_backup import (BackupError, CHUNK_SIZE, MySQLBackupClient, MySQLSettings,
                          Snapshot, load_settings, save_download, save_settings)
from test_brake_calculator import sample


class TransportCursor:
    def __init__(self, transport):
        self.transport = transport
        self.cursor = transport.connection.cursor()
        self.rows = None

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.cursor.close()

    def execute(self, sql, args=()):
        if "information_schema.TABLES" in sql:
            existing = {r[0] for r in self.transport.connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            self.rows = [(table, self.transport.server.engine) for table in args if table in existing]
            return
        self.rows = None
        if self.transport.server.reject_ddl and sql.startswith("CREATE TABLE"):
            raise RuntimeError("CREATE permission denied")
        if self.transport.server.fail_chunks and sql.startswith("INSERT INTO bbl_backup_chunks"):
            raise RuntimeError("password=must-not-appear")
        sql = sql.replace(" CHARACTER SET ascii COLLATE ascii_bin", "").replace(" CHARACTER SET ascii", "")
        sql = sql.replace(" ENGINE=InnoDB DEFAULT CHARSET=utf8mb4", "")
        self.cursor.execute(sql.replace("%s", "?"), args)

    def fetchone(self):
        if self.rows is not None:
            return self.rows.pop(0) if self.rows else None
        return self.cursor.fetchone()

    def fetchall(self):
        if self.rows is not None:
            rows, self.rows = self.rows, []
            return rows
        return self.cursor.fetchall()


class Transport:
    def __init__(self, server):
        self.server = server
        self.connection = sqlite3.connect(server.path)
        self.connection.execute("PRAGMA foreign_keys=ON")
        self.rolled_back = self.closed = False

    def cursor(self):
        return TransportCursor(self)

    def begin(self):
        self.connection.execute("BEGIN")

    def commit(self):
        self.connection.commit()

    def rollback(self):
        self.rolled_back = True
        self.connection.rollback()

    def close(self):
        self.closed = True
        self.connection.close()


class Server:
    def __init__(self, path):
        self.path, self.fail_chunks, self.engine = path, False, "InnoDB"
        self.reject_ddl = False
        self.latest = self.options = None

    def connect(self, **options):
        self.options = options
        self.latest = Transport(self)
        return self.latest


class MySQLBackupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.folder = Path(self.temp.name)
        self.store = HistoryStore(self.folder / "live.sqlite3")
        self.record_id = self.store.save(sample())
        self.store.save_analysis_profile(self.record_id, VehicleSpec(45, 2400, 500, [0, 1]))
        self.server = Server(self.folder / "mysql_transport.sqlite3")
        self.settings = MySQLSettings(user="backup", password="secret", tls=False)
        self.client = MySQLBackupClient(self.settings, self.server.connect)

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def snapshot(self):
        path = self.folder / "snapshot.sqlite3"
        self.store.backup(path)
        return Snapshot.from_file(path)

    def test_settings_never_persist_or_reload_password(self):
        path = self.folder / "config.json"
        save_settings(path, self.settings)
        self.assertNotIn("secret", path.read_text())
        self.assertNotIn("password", json.loads(path.read_text()))
        self.assertNotIn("secret", repr(self.settings))
        payload = json.loads(path.read_text())
        payload["password"] = "should-be-ignored"
        path.write_text(json.dumps(payload))
        self.assertEqual(load_settings(path).password, "")
        for setting in (replace(self.settings, port=0), replace(self.settings, port=True),
                        replace(self.settings, user=""), replace(self.settings, host="\n"),
                        replace(self.settings, ca_file="missing.pem")):
            with self.subTest(setting=setting), self.assertRaises(BackupError):
                setting.validate()

    def test_connection_test_has_no_schema_side_effects_and_tls_verifies_identity(self):
        client = MySQLBackupClient(replace(self.settings, tls=True), self.server.connect)
        self.assertIn("成功", client.test_connection())
        context = self.server.options["ssl"]
        self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)
        self.assertTrue(context.check_hostname)
        self.assertGreaterEqual(context.minimum_version, ssl.TLSVersion.TLSv1_2)
        self.assertFalse(self.server.options["local_infile"])
        self.assertTrue(self.server.latest.closed)
        with sqlite3.connect(self.server.path) as connection:
            self.assertEqual(connection.execute("SELECT name FROM sqlite_master").fetchall(), [])

    def test_complete_multichunk_round_trip_preserves_all_rows_and_repeated_names(self):
        # Even a row the UI cannot parse must survive a full database backup.
        with self.store.connection:
            self.store.connection.execute("INSERT INTO combinations VALUES (?,?,?,?,?)",
                                          ("corrupt", "bad", "bad", "x" * (CHUNK_SIZE + 100), "{}"))
        snapshot = self.snapshot()
        self.assertGreater(len(snapshot.data), CHUNK_SIZE)
        name = "中文备份'); DROP TABLE bbl_backups;--"
        first = self.client.upload(snapshot, name, "1.2.0")
        # After initial schema creation, SELECT/INSERT is enough for future backups.
        self.server.reject_ddl = True
        second = self.client.upload(snapshot, name, "1.2.0")
        self.assertNotEqual(first["backup_id"], second["backup_id"])
        self.assertEqual(first["record_count"], 2)
        self.assertEqual(first["profile_count"], 1)
        self.assertEqual(len(self.client.list_backups()), 2)
        self.assertEqual(self.client.download(first["backup_id"]), snapshot.data)
        self.assertTrue(self.server.latest.closed)
        output = self.folder / "restored.sqlite3"
        save_download(output, self.client.download(second["backup_id"]), self.store.path)
        restored = HistoryStore(output)
        try:
            self.assertEqual(restored.analysis_profile(self.record_id), self.store.analysis_profile(self.record_id))
            self.assertEqual(restored.connection.execute("SELECT * FROM combinations ORDER BY id").fetchall(),
                             self.store.connection.execute("SELECT * FROM combinations ORDER BY id").fetchall())
        finally:
            restored.close()

    def test_failed_upload_rolls_back_and_does_not_damage_previous_backups(self):
        snapshot = self.snapshot()
        first = self.client.upload(snapshot, "first", "1.2.0")
        self.server.fail_chunks = True
        with self.assertRaises(BackupError) as context:
            self.client.upload(snapshot, "failed", "1.2.0")
        self.assertNotIn("must-not-appear", str(context.exception))
        self.assertTrue(self.server.latest.rolled_back)
        self.assertTrue(self.server.latest.closed)
        self.assertEqual(len(self.client.list_backups()), 1)
        self.assertEqual(self.client.download(first["backup_id"]), snapshot.data)

    def test_nontransactional_existing_tables_are_rejected(self):
        self.client.upload(self.snapshot(), "valid", "1.2.0")
        self.server.engine = "MyISAM"
        with self.assertRaisesRegex(BackupError, "InnoDB"):
            self.client.upload(self.snapshot(), "first", "1.2.0")
        self.assertEqual(len(self.client.list_backups()), 1)

    def test_modified_missing_or_incompatible_backup_is_rejected(self):
        metadata = self.client.upload(self.snapshot(), "first", "1.2.0")
        backup_id = metadata["backup_id"]
        with sqlite3.connect(self.server.path) as connection:
            connection.execute("UPDATE bbl_backup_chunks SET payload=? WHERE backup_id=?", (b"bad", backup_id))
        with self.assertRaisesRegex(BackupError, "校验"):
            self.client.download(backup_id)
        with sqlite3.connect(self.server.path) as connection:
            connection.execute("DELETE FROM bbl_backup_chunks WHERE backup_id=?", (backup_id,))
        with self.assertRaisesRegex(BackupError, "缺失"):
            self.client.download(backup_id)
        with sqlite3.connect(self.server.path) as connection:
            connection.execute("UPDATE bbl_backups SET format_version=99 WHERE backup_id=?", (backup_id,))
        with self.assertRaisesRegex(BackupError, "格式"):
            self.client.download(backup_id)
        with self.assertRaises(BackupError):
            self.client.download("' OR 1=1")

    def test_sha_correct_but_non_sqlite_payload_is_rejected_on_download(self):
        data = b"not a SQLite file"
        metadata = self.client.upload(Snapshot(data, hashlib.sha256(data).hexdigest(), 1, 1), "bad", "1.2.0")
        with self.assertRaisesRegex(BackupError, "SQLite"):
            self.client.download(metadata["backup_id"])

    def test_download_never_replaces_live_database_and_atomic_failure_preserves_file(self):
        snapshot = self.snapshot()
        before = self.store.path.read_bytes()
        with self.assertRaises(BackupError):
            save_download(self.store.path, snapshot.data, self.store.path)
        self.assertEqual(self.store.path.read_bytes(), before)
        path = self.folder / "existing.sqlite3"
        path.write_bytes(b"previous backup")
        with patch("mysql_backup.os.replace", side_effect=OSError("disk error")), self.assertRaises(OSError):
            save_download(path, snapshot.data, self.store.path)
        self.assertEqual(path.read_bytes(), b"previous backup")
        self.assertFalse(list(self.folder.glob(".bbl_*")))

    def test_invalid_names_and_snapshot_hash_are_rejected_before_connection(self):
        snapshot = self.snapshot()
        for name in ("", "x" * 121, "a\nb"):
            with self.assertRaises(BackupError):
                self.client.upload(snapshot, name, "1.2.0")
        with self.assertRaises(BackupError):
            self.client.upload(replace(snapshot, sha256="0" * 64), "bad", "1.2.0")
        self.assertIsNone(self.server.latest)


if __name__ == "__main__":
    unittest.main(verbosity=2)
