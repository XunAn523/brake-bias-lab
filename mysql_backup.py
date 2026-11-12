"""Versioned MySQL storage for complete SQLite snapshots (optional dependency)."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import ssl
import tempfile
import uuid

CHUNK_SIZE = 256 * 1024
MAX_BACKUP_BYTES = 128 * 1024 * 1024
BACKUP_FORMAT = 1


class BackupError(Exception):
    """Safe to display: messages never include passwords or raw SQL."""


@dataclass(frozen=True)
class MySQLSettings:
    host: str = "localhost"
    port: int = 3306
    database: str = "brake_bias_lab"
    user: str = ""
    password: str = field(default="", repr=False)
    tls: bool = True
    ca_file: str = ""

    def validate(self):
        for label, value in (("服务器", self.host), ("数据库名", self.database), ("用户名", self.user)):
            if not isinstance(value, str) or not value.strip() or any(ord(c) < 32 for c in value):
                raise BackupError(f"请填写有效的{label}。")
        if type(self.port) is not int or not 1 <= self.port <= 65535:
            raise BackupError("端口必须为 1～65535 的整数。")
        if type(self.tls) is not bool:
            raise BackupError("TLS 选项无效。")
        if self.ca_file and not self.tls:
            raise BackupError("选择 CA 证书时请启用 TLS。")
        if self.ca_file and not Path(self.ca_file).is_file():
            raise BackupError("CA 证书文件不存在。")

    def public_dict(self):
        return {key: value for key, value in asdict(self).items() if key != "password"}


def save_settings(path: Path, settings: MySQLSettings):
    settings.validate()
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    _atomic_write(path, json.dumps(settings.public_dict(), ensure_ascii=False, indent=2).encode("utf-8"))


def load_settings(path: Path) -> MySQLSettings:
    if not Path(path).exists():
        return MySQLSettings()
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
        # Ignore unknown keys and never load a password, even from an edited file.
        if not isinstance(payload, dict):
            raise ValueError()
        settings = MySQLSettings(**{key: payload[key] for key in MySQLSettings().public_dict() if key in payload})
        settings.validate()
        return settings
    except (ValueError, TypeError, BackupError):
        raise BackupError("MySQL 连接配置无效，请重新填写并保存。") from None


def _atomic_write(path: Path, data: bytes):
    with tempfile.NamedTemporaryFile(dir=path.parent, prefix=".bbl_", delete=False) as stream:
        temporary = Path(stream.name)
        try:
            stream.write(data)
        except BaseException:
            stream.close()
            temporary.unlink(missing_ok=True)
            raise
    try:
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def inspect_snapshot(path: Path) -> tuple[int, int]:
    """Read-only integrity/schema check; do not recalculate or discard any rows."""
    try:
        connection = sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True)
        try:
            if connection.execute("PRAGMA quick_check").fetchall() != [("ok",)]:
                raise BackupError("SQLite 备份完整性检查失败。")
            required = {
                "combinations": {"id", "created_at", "updated_at", "spec_json", "result_json"},
                "analysis_profiles": {"record_id", "profile_json", "updated_at"},
            }
            for table, columns in required.items():
                actual = {row[1] for row in connection.execute(f"PRAGMA table_info({table})")}
                if not columns <= actual:
                    raise BackupError("备份不是支持的 Brake Bias Lab 数据库。")
            return (connection.execute("SELECT count(*) FROM combinations").fetchone()[0],
                    connection.execute("SELECT count(*) FROM analysis_profiles").fetchone()[0])
        finally:
            connection.close()
    except sqlite3.Error:
        raise BackupError("SQLite 备份无法读取或已经损坏。") from None


@dataclass(frozen=True)
class Snapshot:
    data: bytes = field(repr=False)
    sha256: str
    record_count: int
    profile_count: int

    @classmethod
    def from_file(cls, path: Path):
        size = Path(path).stat().st_size
        if not 0 < size <= MAX_BACKUP_BYTES:
            raise BackupError("单次备份大小须为 1 字节～128 MiB。")
        records, profiles = inspect_snapshot(path)
        data = Path(path).read_bytes()
        return cls(data, hashlib.sha256(data).hexdigest(), records, profiles)


SCHEMA = (
    """CREATE TABLE IF NOT EXISTS bbl_backups (
        backup_id CHAR(32) CHARACTER SET ascii COLLATE ascii_bin PRIMARY KEY,
        backup_name VARCHAR(120) NOT NULL, created_at VARCHAR(40) NOT NULL,
        app_version VARCHAR(32) NOT NULL, format_version INT NOT NULL,
        byte_count BIGINT NOT NULL, chunk_count INT NOT NULL,
        sha256 CHAR(64) CHARACTER SET ascii NOT NULL,
        record_count BIGINT NOT NULL, profile_count BIGINT NOT NULL
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",
    """CREATE TABLE IF NOT EXISTS bbl_backup_chunks (
        backup_id CHAR(32) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
        chunk_no INT NOT NULL, payload MEDIUMBLOB NOT NULL,
        PRIMARY KEY (backup_id, chunk_no),
        FOREIGN KEY (backup_id) REFERENCES bbl_backups(backup_id) ON DELETE CASCADE
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",
)
METADATA_COLUMNS = ("backup_id", "backup_name", "created_at", "app_version", "format_version",
                    "byte_count", "chunk_count", "sha256", "record_count", "profile_count")


def _friendly_error(exc):
    if isinstance(exc, BackupError):
        return exc
    code = exc.args[0] if exc.args and type(exc.args[0]) is int else None
    messages = {
        1045: "MySQL 登录失败，请核对用户名和密码。",
        1049: "MySQL 数据库不存在，请先创建指定数据库。",
        1044: "MySQL 用户没有访问此数据库的权限。",
        1142: "MySQL 用户权限不足，需要 SELECT、INSERT 和首次建表的 CREATE、REFERENCES 权限。",
        1146: "尚未找到备份表，请先完成首次备份。",
        1153: "MySQL 数据包上限过小，请检查 max_allowed_packet（建议至少 1 MiB）。",
        2003: "无法连接 MySQL，请检查服务器、端口及网络。",
        2006: "MySQL 连接中断，本次结果未确认，请刷新备份列表核对。",
        2013: "MySQL 连接中断，本次结果未确认，请刷新备份列表核对。",
        2026: "MySQL TLS 连接失败，请核对服务器证书、主机名及 CA 文件。",
    }
    if isinstance(exc, (ssl.SSLError, ssl.CertificateError)):
        return BackupError("MySQL TLS 证书校验失败，请核对主机名及 CA 证书。")
    return BackupError(messages.get(code, "MySQL 操作未完成，请检查连接、数据库权限、表结构及服务器状态。")
                       + (f"（错误码 {code}）" if code is not None else ""))


class MySQLBackupClient:
    def __init__(self, settings: MySQLSettings, connect_factory=None):
        settings.validate()
        self.settings, self.connect_factory = settings, connect_factory

    def _connect(self):
        factory = self.connect_factory
        if factory is None:
            try:
                import pymysql
            except ImportError:
                raise BackupError("缺少 MySQL 依赖，请安装 requirements.txt，或使用包含 MySQL 功能的新版 EXE。") from None
            factory = pymysql.connect
        options = dict(host=self.settings.host.strip(), port=self.settings.port,
                       database=self.settings.database.strip(), user=self.settings.user.strip(),
                       password=self.settings.password, charset="utf8mb4", autocommit=False,
                       connect_timeout=10, read_timeout=30, write_timeout=30, local_infile=False)
        if self.connect_factory is None:
            # Stream payload rows so a damaged server table cannot exhaust memory.
            options["cursorclass"] = pymysql.cursors.SSCursor
        if self.settings.tls:
            context = ssl.create_default_context(cafile=self.settings.ca_file or None)
            context.minimum_version = ssl.TLSVersion.TLSv1_2
            options["ssl"] = context
        else:
            options["ssl_disabled"] = True
        return factory(**options)

    def _run(self, operation):
        connection = None
        try:
            connection = self._connect()
            return operation(connection)
        except Exception as exc:
            if connection is not None:
                try:
                    connection.rollback()
                except Exception:
                    pass
            raise _friendly_error(exc) from None
        finally:
            if connection is not None:
                try:
                    connection.close()
                except Exception:
                    pass

    def test_connection(self):
        def operation(connection):
            with connection.cursor() as cursor:
                cursor.execute("SELECT 1")
                cursor.fetchone()
            return "连接成功。备份时将使用当前指定数据库。"
        return self._run(operation)

    def upload(self, snapshot: Snapshot, name: str, app_version: str) -> dict:
        name = name.strip()
        if not name or len(name) > 120 or any(ord(c) < 32 for c in name):
            raise BackupError("备份名称需为 1～120 个字符，不能包含控制字符。")
        if (not 0 < len(snapshot.data) <= MAX_BACKUP_BYTES
                or hashlib.sha256(snapshot.data).hexdigest() != snapshot.sha256):
            raise BackupError("待上传快照大小或校验和无效。")
        metadata = dict(zip(METADATA_COLUMNS, (
            uuid.uuid4().hex, name, datetime.now(timezone.utc).isoformat(timespec="microseconds"),
            app_version, BACKUP_FORMAT, len(snapshot.data),
            (len(snapshot.data) + CHUNK_SIZE - 1) // CHUNK_SIZE,
            snapshot.sha256, snapshot.record_count, snapshot.profile_count,
        )))
        def operation(connection):
            with connection.cursor() as cursor:
                # MySQL DDL auto-commits. Complete it before beginning snapshot writes.
                engine_query = ("SELECT TABLE_NAME,ENGINE FROM information_schema.TABLES "
                                "WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME IN (%s,%s)")
                tables = ("bbl_backups", "bbl_backup_chunks")
                cursor.execute(engine_query, tables)
                engines = dict(cursor.fetchall())
                if any(str(engine).upper() != "INNODB" for engine in engines.values()):
                    raise BackupError("MySQL 备份表必须使用 InnoDB，现有表不支持事务，请联系数据库管理员。")
                for table, statement in zip(tables, SCHEMA):
                    if table not in engines:
                        cursor.execute(statement)
                connection.commit()
                cursor.execute(engine_query, tables)
                engines = dict(cursor.fetchall())
                if any(str(engines.get(table, "")).upper() != "INNODB"
                       for table in tables):
                    raise BackupError("MySQL 备份表必须使用 InnoDB，现有表不支持事务，请联系数据库管理员。")
                connection.begin()
                cursor.execute("INSERT INTO bbl_backups (" + ",".join(METADATA_COLUMNS)
                               + ") VALUES (" + ",".join(["%s"] * len(METADATA_COLUMNS)) + ")",
                               tuple(metadata.values()))
                for index, start in enumerate(range(0, len(snapshot.data), CHUNK_SIZE)):
                    cursor.execute("INSERT INTO bbl_backup_chunks (backup_id,chunk_no,payload) VALUES (%s,%s,%s)",
                                   (metadata["backup_id"], index, snapshot.data[start:start + CHUNK_SIZE]))
                connection.commit()
            return metadata
        return self._run(operation)

    def list_backups(self) -> list[dict]:
        def operation(connection):
            with connection.cursor() as cursor:
                cursor.execute("SELECT " + ",".join(METADATA_COLUMNS)
                               + " FROM bbl_backups ORDER BY created_at DESC,backup_id DESC LIMIT 200")
                return [dict(zip(METADATA_COLUMNS, row)) for row in cursor.fetchall()]
        return self._run(operation)

    def download(self, backup_id: str) -> bytes:
        if not isinstance(backup_id, str) or not re.fullmatch(r"[0-9a-f]{32}", backup_id):
            raise BackupError("备份编号无效。")
        def operation(connection):
            with connection.cursor() as cursor:
                cursor.execute("SELECT " + ",".join(METADATA_COLUMNS)
                               + " FROM bbl_backups WHERE backup_id=%s LIMIT 1", (backup_id,))
                # SSCursor must consume EOF before the next SQL statement.
                rows = cursor.fetchall()
                if not rows:
                    raise BackupError("该备份已不存在，请刷新列表。")
                metadata = dict(zip(METADATA_COLUMNS, rows[0]))
                size, chunks = metadata["byte_count"], metadata["chunk_count"]
                if (metadata["format_version"] != BACKUP_FORMAT or type(size) is not int
                        or not 0 < size <= MAX_BACKUP_BYTES or type(chunks) is not int
                        or chunks != (size + CHUNK_SIZE - 1) // CHUNK_SIZE):
                    raise BackupError("远端备份格式或大小不受支持。")
                cursor.execute("SELECT chunk_no,payload FROM bbl_backup_chunks WHERE backup_id=%s ORDER BY chunk_no", (backup_id,))
                parts, total = [], 0
                # Consume incrementally; enforce bounds before joining the payload.
                for index in range(chunks):
                    chunk = cursor.fetchone()
                    if chunk is None or chunk[0] != index or not isinstance(chunk[1], (bytes, bytearray)):
                        raise BackupError("远端备份分块缺失或顺序无效。")
                    total += len(chunk[1])
                    if len(chunk[1]) > CHUNK_SIZE or total > size:
                        raise BackupError("远端备份分块大小无效。")
                    parts.append(chunk[1])
                if cursor.fetchone() is not None:
                    raise BackupError("远端备份包含多余分块。")
                data = b"".join(parts)
                if len(data) != size or hashlib.sha256(data).hexdigest() != metadata["sha256"]:
                    raise BackupError("远端备份校验失败，不能用于恢复。")
                with tempfile.TemporaryDirectory(prefix="bbl_verify_") as folder:
                    path = Path(folder) / "snapshot.sqlite3"
                    path.write_bytes(data)
                    counts = inspect_snapshot(path)
                if counts != (metadata["record_count"], metadata["profile_count"]):
                    raise BackupError("远端备份记录数量与元数据不一致。")
                return data
        return self._run(operation)


def save_download(path: Path, data: bytes, live_database: Path):
    """Downloaded snapshots never overwrite the open local database."""
    path = Path(path)
    if path.resolve() == Path(live_database).resolve() or (path.exists() and path.samefile(live_database)):
        raise BackupError("请另存为恢复文件，不能覆盖正在使用的数据库。")
    _atomic_write(path, data)
