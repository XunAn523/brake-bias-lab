"""Opt-in integration tests against a real MySQL server.

Set BBL_MYSQL_TEST=1 and BBL_MYSQL_USER/BBL_MYSQL_PASSWORD to run.
Each run creates and removes only its own randomly named bbl_it_* database.
"""
import os
from pathlib import Path
import re
import tempfile
import unittest
import uuid

from brake_calculator import HistoryStore, VehicleSpec
from mysql_backup import (BackupError, CHUNK_SIZE, MySQLBackupClient, MySQLSettings,
                          Snapshot, save_download)
from test_brake_calculator import full_sample, sample


@unittest.skipUnless(os.environ.get("BBL_MYSQL_TEST") == "1", "Real MySQL tests are opt-in")
class RealMySQLTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import pymysql
        cls.pymysql = pymysql
        cls.database = "bbl_it_" + uuid.uuid4().hex
        cls.settings = MySQLSettings(
            host=os.environ.get("BBL_MYSQL_HOST", "127.0.0.1"),
            port=int(os.environ.get("BBL_MYSQL_PORT", "3306")),
            database=cls.database, user=os.environ.get("BBL_MYSQL_USER", "root"),
            password=os.environ.get("BBL_MYSQL_PASSWORD", ""), tls=False,
        )
        # This suite is deliberately restricted to the explicitly requested local server.
        if cls.settings.host not in ("127.0.0.1", "localhost", "::1"):
            raise RuntimeError("Integration tests require a loopback MySQL server")
        cls.admin_options = dict(host=cls.settings.host, port=cls.settings.port,
                                 user=cls.settings.user, password=cls.settings.password,
                                 charset="utf8mb4", connect_timeout=5,
                                 read_timeout=15, write_timeout=15, autocommit=True,
                                 ssl_disabled=True)
        connection = pymysql.connect(**cls.admin_options)
        try:
            with connection.cursor() as cursor:
                cursor.execute("SELECT VERSION()")
                cls.server_version = cursor.fetchone()[0]
                # Generated exclusively from hex UUIDs, never from configuration input.
                cursor.execute(f"CREATE DATABASE `{cls.database}` CHARACTER SET utf8mb4")
        finally:
            connection.close()
        cls.addClassCleanup(cls.drop_database)

    @classmethod
    def drop_database(cls):
        if not re.fullmatch(r"bbl_it_[0-9a-f]{32}", cls.database):
            raise RuntimeError("Refusing to remove an unexpected database name")
        connection = cls.pymysql.connect(**cls.admin_options)
        try:
            with connection.cursor() as cursor:
                cursor.execute(f"DROP DATABASE `{cls.database}`")
        finally:
            connection.close()

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="bbl_mysql_it_")
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name)
        self.store = HistoryStore(self.folder / "live.sqlite3")
        self.addCleanup(self.store.close)
        self.client = MySQLBackupClient(self.settings)
        self.record_id = self.store.save(sample())
        self.store.save(full_sample())
        self.store.save_analysis_profile(self.record_id, VehicleSpec(45, 2400, 500, [0, 0.6, 1]))
        # Force multiple network chunks and preserve a row the UI cannot decode.
        with self.store.connection:
            self.store.connection.execute("INSERT INTO combinations VALUES (?,?,?,?,?)",
                                          ("opaque", "bad", "bad", "原始数据" * CHUNK_SIZE, "{}"))
        path = self.folder / "snapshot.sqlite3"
        self.store.backup(path)
        self.snapshot = Snapshot.from_file(path)
        self.assertGreater(len(self.snapshot.data), CHUNK_SIZE)

    def connect(self):
        return self.pymysql.connect(**dict(self.admin_options, database=self.database, autocommit=False))

    def test_connection_and_real_multichunk_restore(self):
        self.assertIn("成功", self.client.test_connection())
        before = self.store.path.read_bytes()
        metadata = self.client.upload(self.snapshot, "中文实测 'quoted' 记录", "1.2.0")
        self.assertEqual(metadata["record_count"], 3)
        self.assertEqual(metadata["profile_count"], 1)
        self.assertTrue(any(row["backup_id"] == metadata["backup_id"] for row in self.client.list_backups()))
        data = self.client.download(metadata["backup_id"])
        self.assertEqual(data, self.snapshot.data)
        path = self.folder / "restore.sqlite3"
        save_download(path, data, self.store.path)
        restored = HistoryStore(path)
        try:
            self.assertEqual(restored.analysis_profile(self.record_id), self.store.analysis_profile(self.record_id))
            self.assertEqual(restored.connection.execute("SELECT * FROM combinations ORDER BY id").fetchall(),
                             self.store.connection.execute("SELECT * FROM combinations ORDER BY id").fetchall())
            self.assertEqual(restored.connection.execute("PRAGMA integrity_check").fetchone(), ("ok",))
        finally:
            restored.close()
        self.assertEqual(before, self.store.path.read_bytes())

    def test_repeated_backup_names_are_independent(self):
        first = self.client.upload(self.snapshot, "同名备份", "1.2.0")
        second = self.client.upload(self.snapshot, "同名备份", "1.2.0")
        self.assertNotEqual(first["backup_id"], second["backup_id"])
        ids = {row["backup_id"] for row in self.client.list_backups()}
        self.assertTrue({first["backup_id"], second["backup_id"]} <= ids)
        self.assertEqual(self.client.download(first["backup_id"]), self.snapshot.data)

    def test_real_mysql_transaction_rolls_back_partial_upload(self):
        self.client.upload(self.snapshot, "回滚前备份", "1.2.0")
        before = {row["backup_id"] for row in self.client.list_backups()}
        owner = self
        class FailingCursor:
            def __init__(self, cursor):
                self.cursor = cursor
            def __enter__(self):
                self.cursor.__enter__()
                return self
            def __exit__(self, *args):
                return self.cursor.__exit__(*args)
            def __getattr__(self, name):
                return getattr(self.cursor, name)
            def execute(self, sql, args=()):
                if sql.startswith("INSERT INTO bbl_backup_chunks") and args[1] == 1:
                    raise RuntimeError("Injected network failure after first chunk")
                return self.cursor.execute(sql, args)
        class FailingConnection:
            def __init__(self, connection):
                self.connection = connection
            def __getattr__(self, name):
                return getattr(self.connection, name)
            def cursor(self):
                return FailingCursor(self.connection.cursor())
        client = MySQLBackupClient(self.settings,
                                   lambda **options: FailingConnection(owner.pymysql.connect(**options)))
        with self.assertRaises(BackupError):
            client.upload(self.snapshot, "应回滚的备份", "1.2.0")
        self.assertEqual(before, {row["backup_id"] for row in self.client.list_backups()})
        connection = self.connect()
        try:
            with connection.cursor() as cursor:
                cursor.execute("SELECT COUNT(*) FROM bbl_backup_chunks c LEFT JOIN bbl_backups b "
                               "ON c.backup_id=b.backup_id WHERE b.backup_id IS NULL")
                self.assertEqual(cursor.fetchone()[0], 0)
        finally:
            connection.close()

    def test_corrupted_remote_snapshot_is_rejected(self):
        metadata = self.client.upload(self.snapshot, "损坏检测备份", "1.2.0")
        connection = self.connect()
        try:
            with connection.cursor() as cursor:
                cursor.execute("UPDATE bbl_backup_chunks SET payload=%s WHERE backup_id=%s AND chunk_no=0",
                               (b"corrupted", metadata["backup_id"]))
            connection.commit()
        finally:
            connection.close()
        with self.assertRaisesRegex(BackupError, "校验"):
            self.client.download(metadata["backup_id"])

    def test_saved_tables_are_innodb_and_no_orphan_chunks(self):
        self.client.upload(self.snapshot, "存储引擎检查", "1.2.0")
        connection = self.connect()
        try:
            with connection.cursor() as cursor:
                cursor.execute("SELECT TABLE_NAME,ENGINE FROM information_schema.TABLES "
                               "WHERE TABLE_SCHEMA=%s", (self.database,))
                engines = dict(cursor.fetchall())
                self.assertEqual(set(engines), {"bbl_backups", "bbl_backup_chunks"})
                self.assertTrue(all(engine.upper() == "INNODB" for engine in engines.values()))
        finally:
            connection.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
