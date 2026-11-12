"""Tk backup dialog; all MySQL I/O runs outside the Tk/SQLite thread."""
from datetime import datetime
from pathlib import Path
import queue
import sqlite3
import tempfile
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from mysql_backup import (BackupError, MySQLBackupClient, MySQLSettings, Snapshot,
                          load_settings, save_download, save_settings)


class MySQLBackupDialog:
    def __init__(self, app, config_path: Path, app_version: str):
        self.app, self.config_path, self.app_version = app, config_path, app_version
        self.busy, self.closed = False, False
        self.events = queue.Queue()
        self.rows = []
        self.controls = []
        self.window = tk.Toplevel(app.root)
        self.window.title("MySQL 数据备份")
        self.window.geometry("960x690")
        self.window.minsize(850, 620)
        self.window.transient(app.root)
        self.window.protocol("WM_DELETE_WINDOW", self.close)
        note = "填写 MySQL 连接信息；服务器需预先创建数据库。密码不保存到文件。"
        try:
            settings = load_settings(config_path)
        except (BackupError, OSError) as exc:
            settings = MySQLSettings()
            note = str(exc)
        self.host = tk.StringVar(value=settings.host)
        self.port = tk.StringVar(value=str(settings.port))
        self.database = tk.StringVar(value=settings.database)
        self.user = tk.StringVar(value=settings.user)
        self.password = tk.StringVar()
        self.tls = tk.BooleanVar(value=settings.tls)
        self.ca_file = tk.StringVar(value=settings.ca_file)
        self.backup_name = tk.StringVar(value="刹车记录_" + datetime.now().strftime("%Y%m%d_%H%M%S"))
        self.note = tk.StringVar(value=note)
        frame = ttk.Frame(self.window, padding=16)
        frame.pack(fill="both", expand=True)
        ttk.Label(frame, text="MySQL 数据备份", style="Percent.TLabel").pack(anchor="w")
        ttk.Label(frame, text="每次保存独立的完整数据库快照，包含全部组合及 g 值车辆参数。",
                  style="Muted.TLabel").pack(anchor="w", pady=(4, 10))
        form = ttk.Frame(frame)
        form.pack(fill="x")
        for col in (1, 3):
            form.columnconfigure(col, weight=1)
        for row, items in enumerate([
            [("服务器", self.host), ("端口", self.port)],
            [("数据库名", self.database), ("用户名", self.user)],
            [("密码（本次使用）", self.password), ("备份名称", self.backup_name)],
        ]):
            for column, (label, variable) in enumerate(items):
                ttk.Label(form, text=label).grid(row=row, column=column * 2, sticky="w", padx=(0, 6), pady=5)
                entry = ttk.Entry(form, textvariable=variable, show="●" if variable is self.password else "")
                entry.grid(row=row, column=column * 2 + 1, sticky="ew", padx=(0, 12), pady=5)
                self.controls.append(entry)
        tls = ttk.Checkbutton(form, text="启用 TLS，校验证书及服务器主机名", variable=self.tls)
        tls.grid(row=3, column=0, columnspan=4, sticky="w", pady=5)
        self.controls.append(tls)
        ttk.Label(form, text="CA 证书（可选）").grid(row=4, column=0, sticky="w")
        ca = ttk.Entry(form, textvariable=self.ca_file)
        ca.grid(row=4, column=1, columnspan=2, sticky="ew", pady=5)
        self.controls.append(ca)
        browse = ttk.Button(form, text="选择证书", command=self.choose_ca)
        browse.grid(row=4, column=3, sticky="e", padx=(0, 12))
        self.controls.append(browse)
        actions = ttk.Frame(frame)
        actions.pack(fill="x", pady=10)
        for text, command in (("保存连接配置", self.save_config), ("测试连接", self.test_connection),
                              ("备份到 MySQL", self.upload), ("刷新备份列表", self.refresh),
                              ("下载所选恢复文件", self.download)):
            button = ttk.Button(actions, text=text, command=command)
            button.pack(side="left", padx=(0, 7))
            self.controls.append(button)
        self.progress = ttk.Progressbar(frame, mode="indeterminate")
        self.progress.pack(fill="x", pady=(0, 8))
        ttk.Label(frame, textvariable=self.note, wraplength=900, justify="left").pack(anchor="w", pady=(0, 8))
        table = ttk.Frame(frame)
        table.pack(fill="both", expand=True)
        columns = (("name", "备份名称", 220), ("created", "备份时间 UTC", 205),
                   ("records", "组合数", 70), ("profiles", "车辆参数数", 85),
                   ("size", "大小 KiB", 85), ("version", "程序版本", 80), ("id", "编号", 90))
        self.tree = ttk.Treeview(table, columns=[c[0] for c in columns], show="headings", selectmode="browse")
        for key, text, width in columns:
            self.tree.heading(key, text=text)
            self.tree.column(key, width=width, minwidth=60, anchor="w" if key == "name" else "center")
        self.tree.grid(row=0, column=0, sticky="nsew")
        table.rowconfigure(0, weight=1)
        table.columnconfigure(0, weight=1)
        yscroll = ttk.Scrollbar(table, command=self.tree.yview)
        yscroll.grid(row=0, column=1, sticky="ns")
        xscroll = ttk.Scrollbar(table, orient="horizontal", command=self.tree.xview)
        xscroll.grid(row=1, column=0, sticky="ew")
        self.tree.configure(yscrollcommand=yscroll.set, xscrollcommand=xscroll.set)
        ttk.Label(frame, text="下载会校验 SHA-256 和 SQLite 完整性，另存为 .sqlite3 文件。恢复时关闭所有计算器窗口，\n"
                  "保留当前 data 目录副本，再将恢复文件复制为 data/brake_history.sqlite3。密码不会写入配置或日志。\n"
                  "非 TLS 仅适用于你确认可信的本地连接。列表显示最近 200 次备份，备份不受主窗口筛选影响。",
                  style="Muted.TLabel", wraplength=900).pack(anchor="w", pady=(10, 0))
        self.poll_id = self.window.after(100, self.poll)

    def collect(self):
        try:
            port = int(self.port.get())
        except ValueError:
            raise BackupError("端口必须为 1～65535 的整数。") from None
        settings = MySQLSettings(self.host.get(), port, self.database.get(), self.user.get(),
                                 self.password.get(), self.tls.get(), self.ca_file.get().strip())
        settings.validate()
        return settings

    def client(self):
        try:
            return MySQLBackupClient(self.collect())
        except BackupError as exc:
            self.note.set(str(exc))
            return None

    def choose_ca(self):
        path = filedialog.askopenfilename(title="选择 CA 证书", filetypes=[("证书", "*.pem *.crt"), ("所有文件", "*.*")], parent=self.window)
        if path:
            self.ca_file.set(path)

    def save_config(self):
        try:
            save_settings(self.config_path, self.collect())
        except (BackupError, OSError) as exc:
            self.note.set(f"未保存配置：{exc}")
            return
        self.note.set("连接配置已保存；未保存密码，下次打开时需重新输入密码。")

    def start(self, label, operation, completed):
        if self.busy:
            return
        self.busy = True
        self.note.set(label)
        self.progress.start(12)
        for control in self.controls:
            control.configure(state="disabled")
        def worker():
            try:
                self.events.put((True, operation(), completed))
            except Exception as exc:
                # Do not expose driver/SQL exception text (could contain credentials).
                safe = str(exc) if isinstance(exc, BackupError) else "操作未完成，请检查本地文件权限或 MySQL 状态。"
                self.events.put((False, safe, completed))
        threading.Thread(target=worker, name="mysql-backup", daemon=True).start()

    def poll(self):
        if self.closed:
            return
        try:
            success, value, completed = self.events.get_nowait()
        except queue.Empty:
            pass
        else:
            self.busy = False
            self.progress.stop()
            for control in self.controls:
                control.configure(state="normal")
            if success:
                completed(value)
            else:
                self.note.set(value)
        self.poll_id = self.window.after(100, self.poll)

    def test_connection(self):
        client = self.client()
        if client:
            self.start("正在连接 MySQL…", client.test_connection, self.note.set)

    def upload(self):
        client = self.client()
        if client is None:
            return
        name = self.backup_name.get().strip()
        if not name or len(name) > 120 or any(ord(c) < 32 for c in name):
            self.note.set("备份名称需为 1～120 个字符，不能包含控制字符。")
            return
        try:
            # SQLite belongs to the UI thread. Pass only immutable bytes to the worker.
            with tempfile.TemporaryDirectory(prefix="bbl_upload_") as folder:
                path = Path(folder) / "snapshot.sqlite3"
                self.app.store.backup(path)
                snapshot = Snapshot.from_file(path)
        except (BackupError, OSError, sqlite3.Error) as exc:
            self.note.set(f"无法生成本地快照：{exc}")
            return
        def completed(metadata):
            self.show_rows([metadata] + [r for r in self.rows if r["backup_id"] != metadata["backup_id"]])
            text = f"MySQL 备份成功：{metadata['record_count']} 条组合、{metadata['profile_count']} 条车辆参数；编号 {metadata['backup_id'][:8]}。"
            self.note.set(text)
            self.app.status.set(text)
        self.start("正在上传完整快照，请等待完成后关闭程序…",
                   lambda: client.upload(snapshot, name, self.app_version), completed)

    def show_rows(self, rows):
        self.rows = rows[:200]
        self.tree.delete(*self.tree.get_children())
        for row in self.rows:
            self.tree.insert("", "end", iid=row["backup_id"], values=(
                row["backup_name"], row["created_at"][:19].replace("T", " "),
                row["record_count"], row["profile_count"], f"{row['byte_count'] / 1024:.1f}",
                row["app_version"], row["backup_id"][:8],
            ))
        if self.rows:
            self.tree.selection_set(self.rows[0]["backup_id"])

    def refresh(self):
        client = self.client()
        if client:
            def completed(rows):
                self.show_rows(rows)
                self.note.set(f"已载入最近 {len(rows)} 次备份。")
            self.start("正在读取备份列表…", client.list_backups, completed)

    def download(self):
        selected = self.tree.selection()
        if not selected:
            self.note.set("请先刷新列表并选择一个备份。")
            return
        client = self.client()
        if client is None:
            return
        backup_id = selected[0]
        path = filedialog.asksaveasfilename(
            title="下载 MySQL 备份为恢复文件", defaultextension=".sqlite3",
            filetypes=[("SQLite 恢复文件", "*.sqlite3")],
            initialfile=f"刹车记录恢复_{backup_id[:8]}.sqlite3", parent=self.window,
        )
        if not path:
            return
        def operation():
            save_download(Path(path), client.download(backup_id), self.app.store.path)
            return path
        self.start("正在下载并校验备份…", operation,
                   lambda saved: self.note.set(f"已下载并校验恢复文件：{saved}。当前本地记录未改变。"))

    def close(self):
        if self.busy:
            self.note.set("任务仍在进行，请等待完成后关闭，避免中断备份。")
            return
        self.closed = True
        self.password.set("")
        self.window.after_cancel(self.poll_id)
        self.window.destroy()
