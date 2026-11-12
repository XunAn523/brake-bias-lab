"""Chinese desktop brake torque calculator; optional MySQL snapshot backups."""
from __future__ import annotations

import csv
import ctypes
import json
import logging
import math
import os
import re
import sqlite3
import sys
import tempfile
import uuid
from dataclasses import asdict, dataclass, replace
from datetime import datetime
from pathlib import Path
import tkinter as tk
from tkinter import filedialog, messagebox, simpledialog, ttk
from mysql_backup_ui import MySQLBackupDialog

# A one-file executable extracts bundled modules to a temporary directory.
# Keep user records beside the EXE, never in that temporary extraction folder.
APP_DIR = Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parent
DEFAULT_DB = APP_DIR / "data" / "brake_history.sqlite3"
MODES = {
    "diameter": "盘径估算",
    "radius": "有效半径",
    "full": "完整参数",
}
KINDS = {"fixed": "对向固定卡钳", "floating": "单侧活塞浮动卡钳"}
APP_VERSION = "1.2.1"


@dataclass
class PistonGroup:
    diameter_mm: float
    count: int


@dataclass
class AxleSpec:
    kind: str
    groups: list[PistonGroup]
    disc_mm: float
    effective_radius_mm: float | None = None
    mu: float | None = None
    pressure_bar: float | None = None


@dataclass
class SetupSpec:
    name: str
    mode: str
    front: AxleSpec
    rear: AxleSpec


@dataclass
class VehicleSpec:
    front_static_percent: float
    wheelbase_mm: float
    cg_height_mm: float
    g_values: list[float]
    front_tyre_radius_mm: float | None = None
    rear_tyre_radius_mm: float | None = None


def nonnegative(value, label: str) -> float:
    try:
        number = float(value)
    except (ValueError, TypeError, OverflowError):
        raise ValueError(f"{label}必须填写有效数字。") from None
    if not math.isfinite(number) or number < 0:
        raise ValueError(f"{label}必须是大于或等于 0 的有限数值。")
    return number


def parse_g_values(text: str) -> list[float]:
    tokens = [x for x in re.split(r"[,，;；\s]+", text.strip()) if x]
    if not tokens or len(tokens) > 100:
        raise ValueError("请填写 1～100 个 g 值，用逗号分隔，例如 0,0.2,0.4,0.6,0.8,1.0,1.2。")
    return sorted({nonnegative(token, "减速度 g 值") for token in tokens})


def deceleration_analysis(torque_ratio: float, vehicle: VehicleSpec) -> list[dict]:
    """Level-road, quasi-static axle loads; force splits assume equal tyre adhesion.

    ideal front fraction = static front fraction + (deceleration/g) * h/L.
    Hardware ground force ratio = torque ratio * rear tyre radius/front tyre radius.
    Neither formula models ABS/EBD, aerodynamic loads or available tyre grip.
    """
    ratio = positive(torque_ratio, "硬件前后力矩比")
    front_static = positive(vehicle.front_static_percent, "前轴静态配重百分比")
    if front_static >= 100:
        raise ValueError("前轴静态配重必须大于 0% 且小于 100%。")
    wheelbase = positive(vehicle.wheelbase_mm, "轴距")
    height = nonnegative(vehicle.cg_height_mm, "重心离地高度")
    if not isinstance(vehicle.g_values, (list, tuple)) or not 1 <= len(vehicle.g_values) <= 100:
        raise ValueError("需要 1～100 个减速度 g 值。")
    g_values = sorted({nonnegative(g, "减速度 g 值") for g in vehicle.g_values})
    front_radius, rear_radius = vehicle.front_tyre_radius_mm, vehicle.rear_tyre_radius_mm
    if (front_radius is None) != (rear_radius is None):
        raise ValueError("前后轮胎滚动半径需同时填写，或同时留空（假设前后相同）。")
    if front_radius is not None:
        front_radius = positive(front_radius, "前轮胎滚动半径")
        rear_radius = positive(rear_radius, "后轮胎滚动半径")
        ratio *= rear_radius / front_radius
    if not math.isfinite(ratio) or ratio <= 0:
        raise ValueError("地面制动力比例超出计算范围，请检查轮胎半径。")
    hardware_front = 100 / (1 + 1 / ratio) if ratio >= 1 else 100 * ratio / (1 + ratio)
    rows = []
    for g_value in g_values:
        front_load = front_static / 100 + g_value * (height / wheelbase)
        valid = math.isfinite(front_load) and 0 < front_load < 1
        ideal_front = front_load * 100 if valid else None
        rows.append({
            "g": g_value,
            "ideal_front_percent": ideal_front,
            "ideal_rear_percent": 100 - ideal_front if valid else None,
            "hardware_front_percent": hardware_front,
            "hardware_rear_percent": 100 - hardware_front,
            "front_difference_pp": hardware_front - ideal_front if valid else None,
            "valid": valid,
            "status": ("0g静态参考" if g_value == 0 else "理论参考") if valid else "后轴载荷≤0，超出模型",
        })
    return rows


def positive(value, label: str) -> float:
    try:
        number = float(value)
    except (ValueError, TypeError, OverflowError):
        raise ValueError(f"{label}必须填写有效数字。") from None
    if not math.isfinite(number) or number <= 0:
        raise ValueError(f"{label}必须是大于 0 的有限数值。")
    return number


def validate_name(name: str) -> str:
    if not isinstance(name, str) or not name.strip():
        raise ValueError("请填写组合名称。")
    name = name.strip()
    if len(name) > 120 or any(ord(c) < 32 for c in name):
        raise ValueError("组合名称最多 120 个字符，不能包含换行等控制字符。")
    return name


def axle_result(spec: AxleSpec, mode: str, label: str) -> dict:
    if mode not in MODES:
        raise ValueError("计算模式无效。")
    if spec.kind not in KINDS:
        raise ValueError(f"{label}卡钳类型无效。")
    disc = positive(spec.disc_mm, f"{label}刹车盘外径")
    if not spec.groups:
        raise ValueError(f"请至少填写一组{label}活塞直径和数量。")
    total_area = 0.0
    total_count = 0
    for group in spec.groups:
        diameter = positive(group.diameter_mm, f"{label}活塞直径")
        if type(group.count) is not int or group.count <= 0:
            raise ValueError(f"{label}活塞数量必须是正整数。")
        if spec.kind == "fixed" and group.count % 2:
            raise ValueError(
                f"{label}对向卡钳的每种直径数量必须为偶数，"
                "并按两侧合计填写，例如 30mm × 2。仅支持两侧活塞对称的固定卡钳。"
            )
        try:
            total_area += math.pi * (diameter / 2) ** 2 * group.count
        except OverflowError:
            raise ValueError(f"{label}活塞尺寸过大。") from None
        total_count += group.count
    area = total_area / 2 if spec.kind == "fixed" else total_area
    radius = None
    if mode in ("radius", "full"):
        radius = positive(spec.effective_radius_mm, f"{label}有效作用半径")
        if radius >= disc / 2:
            raise ValueError(f"{label}有效作用半径必须小于刹车盘外径的一半（{disc / 2:g} mm）。")
    wheel_torque = None
    if mode == "diameter":
        # This is only a ratio factor; the disc outer radius is NOT an effective radius.
        factor = area * disc
    elif mode == "radius":
        factor = area * radius
    else:
        mu = positive(spec.mu, f"{label}刹车片摩擦系数")
        pressure = positive(spec.pressure_bar, f"{label}卡钳油压")
        # bar -> N/mm²: x0.1; mm -> m: x0.001.
        wheel_torque = 2 * (pressure * 0.1) * mu * area * (radius * 0.001)
        factor = wheel_torque
    if not all(math.isfinite(x) and x > 0 for x in (total_area, area, factor)):
        raise ValueError(f"{label}参数超出计算范围，请检查尺寸和数量。")
    if wheel_torque is not None and not math.isfinite(wheel_torque * 2):
        raise ValueError(f"{label}力矩超出计算范围。")
    return {
        "piston_count": total_count,
        "total_area_mm2": total_area,
        "effective_area_mm2": area,
        "effective_radius_mm": radius,
        "factor": factor,
        "wheel_torque_nm": wheel_torque,
        "axle_torque_nm": None if wheel_torque is None else wheel_torque * 2,
    }


def calculate(spec: SetupSpec) -> dict:
    validate_name(spec.name)
    if spec.mode not in MODES:
        raise ValueError("计算模式无效。")
    front = axle_result(spec.front, spec.mode, "前轮")
    rear = axle_result(spec.rear, spec.mode, "后轮")
    ratio = front["factor"] / rear["factor"]
    if not math.isfinite(ratio) or ratio <= 0:
        raise ValueError("前后比例超出计算范围，请检查输入。")
    # Stable normalization, even for very unequal positive inputs.
    scale = max(front["factor"], rear["factor"])
    f = front["factor"] / scale
    r = rear["factor"] / scale
    front_percent = f / (f + r) * 100
    return {
        "formula_version": 1,
        "front": front,
        "rear": rear,
        "ratio": ratio,
        "front_percent": front_percent,
        "rear_percent": 100 - front_percent,
    }


def spec_from_dict(payload: dict) -> SetupSpec:
    def axle(data):
        return AxleSpec(
            kind=data["kind"],
            groups=[PistonGroup(**g) for g in data["groups"]],
            disc_mm=data["disc_mm"],
            effective_radius_mm=data.get("effective_radius_mm"),
            mu=data.get("mu"),
            pressure_bar=data.get("pressure_bar"),
        )
    return SetupSpec(
        name=payload["name"], mode=payload["mode"],
        front=axle(payload["front"]), rear=axle(payload["rear"]),
    )


def solve_rear_disc(spec: SetupSpec, target_front_percent: float,
                    min_disc_mm: float, max_disc_mm: float) -> dict:
    """Hold the front assembly fixed and solve for the rear disc.

    Diameter mode retains the proportional-radius estimate. Radius/full modes
    assume a constant rear outer-edge offset D/2-r, i.e. the same pad geometry
    moves radially with disc size. This is a sizing model, not a fitment check.
    """
    baseline = calculate(spec)
    target = positive(target_front_percent, "目标前轴力矩占比")
    if target >= 100:
        raise ValueError("目标前轴力矩占比必须大于 0% 且小于 100%。")
    minimum = positive(min_disc_mm, "后盘下限")
    maximum = positive(max_disc_mm, "后盘上限")
    if minimum > maximum:
        raise ValueError("后盘下限不能大于上限。")
    if maximum > spec.front.disc_mm:
        raise ValueError("后盘上限不能超过固定前盘的外径。")
    offset = 0.0 if spec.mode == "diameter" else spec.rear.disc_mm / 2 - spec.rear.effective_radius_mm
    if spec.mode != "diameter" and minimum / 2 <= offset:
        raise ValueError(f"在当前摩擦半径偏距假设下，后盘下限必须大于 {2 * offset:g} mm。")
    target_ratio = target / (100 - target)
    variable = spec.rear.disc_mm if spec.mode == "diameter" else spec.rear.effective_radius_mm
    required_variable = positive(variable * (baseline["ratio"] / target_ratio), "反算尺寸")
    required_disc = required_variable if spec.mode == "diameter" else 2 * (required_variable + offset)
    required_disc = positive(required_disc, "反算后盘外径")

    def candidate(diameter):
        radius = None if spec.mode == "diameter" else diameter / 2 - offset
        rear = replace(spec.rear, disc_mm=diameter, effective_radius_mm=radius)
        candidate_spec = replace(spec, rear=rear)
        result = calculate(candidate_spec)
        return {"spec": candidate_spec, "result": result,
                "difference_pp": result["front_percent"] - target}

    # Include exact solution when feasible and usable bounds even if it is not.
    tolerance = 1e-9
    feasible = minimum - tolerance <= required_disc <= maximum + tolerance
    closest_disc = min(maximum, max(minimum, required_disc))
    diameters = {minimum, maximum, closest_disc}
    diameters.update(d for d in (260, 270, 275, 280, 288, 295, 300, 304, 310,
                               315, 318, 320, 325, 328, 330, 332, 340, 345,
                               348, 350, 355, 356, 360, 365, 370, 380, 390,
                               400, 410, 420) if minimum <= d <= maximum)
    candidates = [candidate(d) for d in sorted(diameters)]
    candidates.sort(key=lambda item: abs(item["difference_pp"]))
    lower_percent = candidate(maximum)["result"]["front_percent"]
    upper_percent = candidate(minimum)["result"]["front_percent"]
    return {"target_front_percent": target, "required_disc_mm": required_disc,
            "required_radius_mm": None if spec.mode == "diameter" else required_variable,
            "rear_edge_offset_mm": offset, "feasible": feasible,
            "min_front_percent": lower_percent, "max_front_percent": upper_percent,
            "candidates": candidates}


def group_text(axle: AxleSpec) -> str:
    return " / ".join(f"{g.diameter_mm:g}×{g.count}" for g in axle.groups)


def timestamp() -> str:
    return datetime.now().astimezone().isoformat(timespec="microseconds")


class HistoryStore:
    """SQLite transactions preserve saved records across crashes and multiple windows."""

    def __init__(self, path: Path = DEFAULT_DB):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(str(self.path), timeout=5)
        try:
            self.connection.execute("PRAGMA foreign_keys=ON")
            self.connection.execute("""
                CREATE TABLE IF NOT EXISTS combinations (
                    id TEXT PRIMARY KEY,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    spec_json TEXT NOT NULL,
                    result_json TEXT NOT NULL
                )
            """)
            self.connection.execute("""
                CREATE TABLE IF NOT EXISTS analysis_profiles (
                    record_id TEXT PRIMARY KEY REFERENCES combinations(id) ON DELETE CASCADE,
                    profile_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
            """)
            self.connection.commit()
        except Exception:
            self.connection.close()
            raise

    def save(self, spec: SetupSpec, record_id: str | None = None) -> str:
        spec.name = validate_name(spec.name)
        result = calculate(spec)
        now = timestamp()
        payload = json.dumps(asdict(spec), ensure_ascii=False, allow_nan=False)
        outcome = json.dumps(result, ensure_ascii=False, allow_nan=False)
        with self.connection:
            if record_id:
                cursor = self.connection.execute(
                    "UPDATE combinations SET updated_at=?, spec_json=?, result_json=? WHERE id=?",
                    (now, payload, outcome, record_id),
                )
                if cursor.rowcount != 1:
                    raise ValueError("该记录已被删除，请另存为新记录。")
            else:
                record_id = uuid.uuid4().hex
                self.connection.execute(
                    "INSERT INTO combinations VALUES (?,?,?,?,?)",
                    (record_id, now, now, payload, outcome),
                )
        return record_id

    def records(self) -> tuple[list[dict], list[str]]:
        records, errors = [], []
        rows = self.connection.execute(
            "SELECT id, created_at, updated_at, spec_json FROM combinations ORDER BY updated_at DESC"
        )
        for record_id, created, updated, payload in rows:
            try:
                # Validate timestamps here, so a malformed row cannot break the UI.
                datetime.fromisoformat(created).astimezone()
                datetime.fromisoformat(updated).astimezone()
                spec = spec_from_dict(json.loads(payload))
                result = calculate(spec)
                records.append({
                    "id": record_id, "created_at": created, "updated_at": updated,
                    "spec": spec, "result": result,
                })
            except (ValueError, TypeError, KeyError, AttributeError, OverflowError, RecursionError, OSError) as exc:
                errors.append(f"{record_id[:8]}：{exc}")
        return records, errors

    def backup(self, destination: Path):
        """Create a consistent live snapshot, replacing the target only on success."""
        destination = Path(destination)
        if (destination.resolve() == self.path.resolve()
                or (destination.exists() and destination.samefile(self.path))):
            raise ValueError("备份位置不能是当前正在使用的数据库。")
        with tempfile.NamedTemporaryFile(dir=destination.parent, prefix=".brake_backup_",
                                         suffix=".sqlite3", delete=False) as stream:
            temporary = Path(stream.name)
        try:
            connection = sqlite3.connect(temporary)
            try:
                self.connection.backup(connection)
            finally:
                connection.close()
            os.replace(temporary, destination)
        finally:
            temporary.unlink(missing_ok=True)

    def rename(self, record_id: str, name: str):
        row = self.connection.execute(
            "SELECT spec_json FROM combinations WHERE id=?", (record_id,)
        ).fetchone()
        if row is None:
            raise ValueError("该记录已被删除。")
        spec = spec_from_dict(json.loads(row[0]))
        spec.name = validate_name(name)
        self.save(spec, record_id)

    def delete(self, record_id: str):
        with self.connection:
            cursor = self.connection.execute("DELETE FROM combinations WHERE id=?", (record_id,))
            if cursor.rowcount != 1:
                raise ValueError("该记录已被删除。")

    def save_analysis_profile(self, record_id: str, vehicle: VehicleSpec):
        deceleration_analysis(1.0, vehicle)
        payload = json.dumps(asdict(vehicle), ensure_ascii=False, allow_nan=False)
        with self.connection:
            if not self.connection.execute("SELECT 1 FROM combinations WHERE id=?", (record_id,)).fetchone():
                raise ValueError("该组合记录已被删除，请刷新列表。")
            self.connection.execute(
                """INSERT INTO analysis_profiles VALUES (?,?,?)
                   ON CONFLICT(record_id) DO UPDATE
                   SET profile_json=excluded.profile_json, updated_at=excluded.updated_at""",
                (record_id, payload, timestamp()),
            )

    def analysis_profile(self, record_id: str | None = None) -> VehicleSpec | None:
        if record_id is None:
            row = self.connection.execute(
                "SELECT profile_json FROM analysis_profiles ORDER BY updated_at DESC LIMIT 1"
            ).fetchone()
        else:
            row = self.connection.execute(
                "SELECT profile_json FROM analysis_profiles WHERE record_id=?", (record_id,)
            ).fetchone()
        if row is None:
            return None
        vehicle = VehicleSpec(**json.loads(row[0]))
        deceleration_analysis(1.0, vehicle)
        return vehicle

    def close(self):
        self.connection.close()


def detail_text(record: dict) -> str:
    spec, result = record["spec"], record["result"]
    lines = [
        f"组合名称：{spec.name}",
        f"记录编号：{record['id']}",
        f"创建时间：{record['created_at']}",
        f"更新时间：{record['updated_at']}",
        f"计算模式：{MODES[spec.mode]}",
        f"前后力矩比：{result['ratio']:.6f} : 1",
        f"前轴占比：{result['front_percent']:.4f}%    后轴占比：{result['rear_percent']:.4f}%",
        "",
    ]
    for key, label in (("front", "前轮"), ("rear", "后轮")):
        axle, values = getattr(spec, key), result[key]
        lines.extend([
            f"{label}：{KINDS[axle.kind]}",
            f"活塞：{group_text(axle)} mm，共 {values['piston_count']} 个（单只卡钳）",
            f"刹车盘外径：{axle.disc_mm:g} mm",
            f"全部液压活塞面积：{values['total_area_mm2']:.3f} mm²",
            f"有效液压面积 A：{values['effective_area_mm2']:.3f} mm²",
        ])
        if spec.mode in ("radius", "full"):
            lines.append(f"有效作用半径：{axle.effective_radius_mm:g} mm")
        if spec.mode == "full":
            lines.extend([
                f"摩擦系数 μ：{axle.mu:g}    卡钳油压：{axle.pressure_bar:g} bar",
                f"单轮理论力矩：{values['wheel_torque_nm']:.3f} N·m",
                f"整轴理论力矩：{values['axle_torque_nm']:.3f} N·m",
            ])
        lines.append("")
    lines.extend([
        "计算口径：左右配置一致，每轴两个车轮；对向固定卡钳两侧活塞对称。",
        "A：对向固定卡钳全部活塞面积÷2；浮动卡钳为全部液压活塞面积。",
        "盘径估算：前后油压、摩擦系数相同，假设有效半径之比等于盘外径之比。",
        "有效半径：使用填写的半径，仍假设前后同压、同摩擦，不输出绝对力矩。",
        "完整参数：单轮 T=2×P×μ×A×r；bar转N/mm²乘0.1，mm转m乘0.001。",
        "这是硬件理论力矩分配，不代表考虑轴荷转移、轮胎及ABS/EBD后的车辆实际分配。",
    ])
    return "\n".join(lines)


CSV_HEADERS = [
    "记录编号", "组合名称", "创建时间", "更新时间", "计算模式", "前后力矩比_前比后",
    "前轴占比_%", "后轴占比_%",
]
for _label in ("前", "后"):
    CSV_HEADERS.extend([
        f"{_label}卡钳类型", f"{_label}活塞配置_mm×数量", f"{_label}活塞总数",
        f"{_label}盘外径_mm", f"{_label}全部活塞面积_mm2", f"{_label}有效液压面积_mm2",
        f"{_label}有效半径_mm", f"{_label}摩擦系数", f"{_label}油压_bar",
        f"{_label}单轮力矩_Nm", f"{_label}整轴力矩_Nm",
    ])


def csv_cell(value):
    if value is None:
        return ""
    if isinstance(value, str) and value.lstrip().startswith(("=", "+", "-", "@")):
        return "'" + value
    return value


def export_csv(path: Path, records: list[dict]):
    with Path(path).open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.writer(stream)
        writer.writerow(CSV_HEADERS)
        for record in records:
            spec, result = record["spec"], record["result"]
            row = [
                record["id"], spec.name, record["created_at"], record["updated_at"],
                MODES[spec.mode], result["ratio"], result["front_percent"], result["rear_percent"],
            ]
            for key in ("front", "rear"):
                axle, values = getattr(spec, key), result[key]
                row.extend([
                    KINDS[axle.kind], group_text(axle), values["piston_count"], axle.disc_mm,
                    values["total_area_mm2"], values["effective_area_mm2"],
                    values["effective_radius_mm"],
                    axle.mu if spec.mode == "full" else None,
                    axle.pressure_bar if spec.mode == "full" else None,
                    values["wheel_torque_nm"], values["axle_torque_nm"],
                ])
            writer.writerow([csv_cell(x) for x in row])


class AxleEditor(ttk.LabelFrame):
    def __init__(self, parent, label: str, changed):
        super().__init__(parent, text=label, padding=10)
        self.changed = changed
        self.rows = []
        self.kind = tk.StringVar(value=KINDS["fixed"])
        self.disc = tk.StringVar()
        self.radius = tk.StringVar()
        self.mu = tk.StringVar()
        self.pressure = tk.StringVar()
        self.count_text = tk.StringVar(value="共 0 个活塞（单只卡钳）")
        self.columnconfigure(1, weight=1)
        ttk.Label(self, text="卡钳类型").grid(row=0, column=0, sticky="w", pady=3)
        ttk.Combobox(
            self, textvariable=self.kind, values=list(KINDS.values()), state="readonly", width=24
        ).grid(row=0, column=1, columnspan=2, sticky="ew", padx=(8, 0))
        ttk.Label(self, text="盘外径 mm").grid(row=1, column=0, sticky="w", pady=4)
        ttk.Entry(self, textvariable=self.disc).grid(row=1, column=1, columnspan=2, sticky="ew", padx=(8, 0))
        self.groups_frame = ttk.Frame(self)
        self.groups_frame.grid(row=2, column=0, columnspan=3, sticky="ew", pady=5)
        self.groups_frame.columnconfigure(0, weight=1)
        self.groups_frame.columnconfigure(1, weight=1)
        ttk.Label(self.groups_frame, text="活塞直径 mm").grid(row=0, column=0, sticky="w")
        ttk.Label(self.groups_frame, text="该直径的总数量").grid(row=0, column=1, sticky="w")
        actions = ttk.Frame(self)
        actions.grid(row=3, column=0, columnspan=3, sticky="ew")
        ttk.Button(actions, text="＋ 添加直径组", command=self.add_group).pack(side="left")
        ttk.Label(actions, textvariable=self.count_text, style="Muted.TLabel").pack(side="left", padx=8)
        self.advanced_entries = {}
        for row, (key, text, var) in enumerate([
            ("radius", "有效半径 mm", self.radius),
            ("mu", "摩擦系数 μ", self.mu),
            ("pressure", "卡钳油压 bar", self.pressure),
        ], start=4):
            ttk.Label(self, text=text).grid(row=row, column=0, sticky="w", pady=4)
            entry = ttk.Entry(self, textvariable=var)
            entry.grid(row=row, column=1, columnspan=2, sticky="ew", padx=(8, 0))
            self.advanced_entries[key] = entry
        for var in (self.kind, self.disc, self.radius, self.mu, self.pressure):
            var.trace_add("write", self._changed)
        self.add_group()

    def _changed(self, *_):
        try:
            count = sum(int(item["count"].get()) for item in self.rows if item["count"].get().strip())
            self.count_text.set(f"共 {count} 个活塞（单只卡钳）")
        except ValueError:
            self.count_text.set("数量需为正整数")
        self.changed()

    def add_group(self, diameter="", count=""):
        row = len(self.rows) + 1
        dvar, nvar = tk.StringVar(value=diameter), tk.StringVar(value=count)
        item = {"diameter": dvar, "count": nvar}
        widgets = [
            ttk.Entry(self.groups_frame, textvariable=dvar, width=14),
            ttk.Entry(self.groups_frame, textvariable=nvar, width=12),
            ttk.Button(self.groups_frame, text="移除", width=5, command=lambda: self.remove_group(item)),
        ]
        item["widgets"] = widgets
        self.rows.append(item)
        for col, widget in enumerate(widgets):
            widget.grid(row=row, column=col, sticky="ew", padx=(0, 5), pady=3)
        for var in (dvar, nvar):
            var.trace_add("write", self._changed)
        self._changed()

    def remove_group(self, item):
        if len(self.rows) == 1:
            item["diameter"].set("")
            item["count"].set("")
            return
        for widget in item["widgets"]:
            widget.destroy()
        self.rows.remove(item)
        for row, group in enumerate(self.rows, start=1):
            for widget in group["widgets"]:
                widget.grid_configure(row=row)
        self._changed()

    def mode_changed(self, mode: str):
        self.advanced_entries["radius"].configure(state="normal" if mode != "diameter" else "disabled")
        for key in ("mu", "pressure"):
            self.advanced_entries[key].configure(state="normal" if mode == "full" else "disabled")

    def collect(self, mode: str, label: str) -> AxleSpec:
        groups = {}
        for item in self.rows:
            diameter, count = item["diameter"].get().strip(), item["count"].get().strip()
            if not diameter and not count:
                continue
            d = positive(diameter, f"{label}活塞直径")
            try:
                n = int(count)
            except (ValueError, TypeError):
                raise ValueError(f"{label}活塞数量必须是正整数。") from None
            if n <= 0:
                raise ValueError(f"{label}活塞数量必须是正整数。")
            groups[d] = groups.get(d, 0) + n
        kind = next(key for key, text in KINDS.items() if text == self.kind.get())
        return AxleSpec(
            kind=kind, groups=[PistonGroup(d, n) for d, n in groups.items()],
            disc_mm=positive(self.disc.get(), f"{label}盘外径"),
            effective_radius_mm=positive(self.radius.get(), f"{label}有效半径") if mode != "diameter" else None,
            mu=positive(self.mu.get(), f"{label}摩擦系数") if mode == "full" else None,
            pressure_bar=positive(self.pressure.get(), f"{label}油压") if mode == "full" else None,
        )

    def load(self, spec: AxleSpec):
        self.kind.set(KINDS[spec.kind])
        for item in self.rows:
            for widget in item["widgets"]:
                widget.destroy()
        self.rows.clear()
        for group in spec.groups:
            self.add_group(f"{group.diameter_mm:g}", str(group.count))
        if not self.rows:
            self.add_group()
        for var, value in (
            (self.disc, spec.disc_mm), (self.radius, spec.effective_radius_mm),
            (self.mu, spec.mu), (self.pressure, spec.pressure_bar),
        ):
            var.set("" if value is None else f"{value:g}")
        self._changed()


class GAnalysisPanel(ttk.Frame):
    def __init__(self, parent, store: HistoryStore, status):
        super().__init__(parent, padding=10)
        self.store, self.status = store, status
        self.record = None
        self.rows = []
        self.vehicle = None
        self.loading = False
        self.name_text = tk.StringVar(value="先点击下方一条历史记录")
        self.hardware_text = tk.StringVar(value="所选组合的硬件力矩比例将在这里显示。")
        self.note_text = tk.StringVar(value="首次使用请填写车辆参数，再点击“计算并保存参数”。")
        self.front_static = tk.StringVar()
        self.wheelbase = tk.StringVar()
        self.cg_height = tk.StringVar()
        self.front_tyre = tk.StringVar()
        self.rear_tyre = tk.StringVar()
        self.g_values = tk.StringVar(value="0,0.2,0.4,0.6,0.8,1.0,1.2")
        self.columnconfigure(0, weight=1)
        ttk.Label(self, textvariable=self.name_text, style="Percent.TLabel", wraplength=820).grid(
            row=0, column=0, sticky="w"
        )
        ttk.Label(self, textvariable=self.hardware_text, wraplength=840).grid(
            row=1, column=0, sticky="w", pady=(3, 5)
        )
        form = ttk.Frame(self)
        form.grid(row=2, column=0, sticky="ew")
        for col in (1, 3, 5):
            form.columnconfigure(col, weight=1)
        for row, items in enumerate([
            [("前轴静态配重 %", self.front_static), ("轴距 mm", self.wheelbase), ("重心高度 mm", self.cg_height)],
            [("前轮滚动半径 mm", self.front_tyre), ("后轮滚动半径 mm", self.rear_tyre)],
        ]):
            for col, (label, variable) in enumerate(items):
                ttk.Label(form, text=label).grid(row=row, column=col * 2, sticky="w", pady=3, padx=(0, 4))
                ttk.Entry(form, textvariable=variable, width=8).grid(
                    row=row, column=col * 2 + 1, sticky="ew", padx=(0, 9), pady=3
                )
        ttk.Label(form, text="轮胎半径同时留空＝假设前后相同", style="Muted.TLabel").grid(
            row=1, column=4, columnspan=2, sticky="w"
        )
        controls = ttk.Frame(self)
        controls.grid(row=3, column=0, sticky="ew", pady=5)
        ttk.Label(controls, text="减速度 g").pack(side="left")
        ttk.Entry(controls, textvariable=self.g_values, width=28).pack(side="left", fill="x", expand=True, padx=6)
        ttk.Button(controls, text="计算并保存参数", command=lambda: self.run(save=True)).pack(side="left")
        ttk.Button(controls, text="示例车辆", command=self.demo_vehicle).pack(side="left", padx=5)
        ttk.Button(controls, text="导出 g 值表", command=self.export).pack(side="left")
        table_frame = ttk.Frame(self)
        table_frame.grid(row=4, column=0, sticky="nsew")
        self.rowconfigure(4, weight=1)
        columns = [
            ("g", "减速度 g", 72),
            ("ideal_front", "理想前轴 %", 98), ("ideal_rear", "理想后轴 %", 98),
            ("hardware_front", "硬件前轴 %", 98), ("hardware_rear", "硬件后轴 %", 98),
            ("difference", "前轴差值 pp", 105), ("status", "模型状态", 160),
        ]
        self.tree = ttk.Treeview(
            table_frame, columns=[x[0] for x in columns], show="headings", height=6, selectmode="browse"
        )
        for key, label, width in columns:
            self.tree.heading(key, text=label)
            self.tree.column(key, width=width, minwidth=60, anchor="center")
        scroll = ttk.Scrollbar(table_frame, orient="vertical", command=self.tree.yview)
        xscroll = ttk.Scrollbar(table_frame, orient="horizontal", command=self.tree.xview)
        self.tree.configure(yscrollcommand=scroll.set, xscrollcommand=xscroll.set)
        self.tree.grid(row=0, column=0, sticky="nsew")
        scroll.grid(row=0, column=1, sticky="ns")
        xscroll.grid(row=1, column=0, sticky="ew")
        table_frame.columnconfigure(0, weight=1)
        table_frame.rowconfigure(0, weight=1)
        ttk.Label(self, textvariable=self.note_text, style="Muted.TLabel", wraplength=840).grid(
            row=5, column=0, sticky="w", pady=(4, 0)
        )
        ttk.Label(
            self,
            text="表内均为地面制动力比例；差值＝硬件前轴－理想前轴（百分点）。\n"
                 "理想分配按动态轴荷计算，假设平路、前后轮胎附着系数相同，不模拟 ABS/EBD 或空气下压力。",
            style="Muted.TLabel", wraplength=840,
        ).grid(row=6, column=0, sticky="w", pady=(3, 0))
        for variable in (
            self.front_static, self.wheelbase, self.cg_height, self.front_tyre, self.rear_tyre, self.g_values,
        ):
            variable.trace_add("write", self.input_changed)

    def input_changed(self, *_):
        if not self.loading:
            if self.run(silent=True) is not None:
                self.note_text.set("参数已修改，尚未保存到此记录；切换组合前请点击“计算并保存参数”。")

    def collect(self):
        front_tyre, rear_tyre = self.front_tyre.get().strip(), self.rear_tyre.get().strip()
        return VehicleSpec(
            front_static_percent=positive(self.front_static.get(), "前轴静态配重"),
            wheelbase_mm=positive(self.wheelbase.get(), "轴距"),
            cg_height_mm=nonnegative(self.cg_height.get(), "重心高度"),
            g_values=parse_g_values(self.g_values.get()),
            front_tyre_radius_mm=positive(front_tyre, "前轮胎滚动半径") if front_tyre else None,
            rear_tyre_radius_mm=positive(rear_tyre, "后轮胎滚动半径") if rear_tyre else None,
        )

    def load_vehicle(self, vehicle):
        self.loading = True
        try:
            for variable, value in (
                (self.front_static, vehicle.front_static_percent if vehicle else None),
                (self.wheelbase, vehicle.wheelbase_mm if vehicle else None),
                (self.cg_height, vehicle.cg_height_mm if vehicle else None),
                (self.front_tyre, vehicle.front_tyre_radius_mm if vehicle else None),
                (self.rear_tyre, vehicle.rear_tyre_radius_mm if vehicle else None),
            ):
                variable.set("" if value is None else f"{value:g}")
            self.g_values.set(",".join(f"{g:g}" for g in vehicle.g_values) if vehicle else "0,0.2,0.4,0.6,0.8,1.0,1.2")
        finally:
            self.loading = False

    def set_record(self, record):
        same_record = record and self.record and record["id"] == self.record["id"]
        self.record = record
        if record is None:
            self.name_text.set("先点击下方一条历史记录")
            self.hardware_text.set("所选组合的硬件力矩比例将在这里显示。")
            self.note_text.set("车辆参数可以随每条组合记录保存。")
            self.clear_rows()
            return
        result = record["result"]
        self.name_text.set(f"{record['spec'].name}  ·  {record['id'][:8]}")
        self.hardware_text.set(
            f"硬件力矩比 {result['ratio']:.4f}:1 ｜ 前 {result['front_percent']:.2f}% / 后 {result['rear_percent']:.2f}%"
            f" ｜ {MODES[record['spec'].mode]}；油压比、摩擦系数固定时不随 g 改变"
        )
        if same_record:
            self.run(silent=True)
            return
        try:
            vehicle = self.store.analysis_profile(record["id"])
            origin = "已载入此记录保存的车辆参数。"
            if vehicle is None:
                vehicle = self.store.analysis_profile()
                origin = "借用最近保存的车辆参数；核对后可保存到此记录。"
            self.load_vehicle(vehicle)
            if vehicle is None:
                self.clear_rows()
                self.note_text.set("请填写前轴静态配重、轴距和重心高度；不预设任何车型数据。")
            elif self.run(silent=True) is not None:
                self.note_text.set(origin)
        except (ValueError, KeyError, TypeError, sqlite3.Error) as exc:
            self.load_vehicle(None)
            self.clear_rows()
            self.note_text.set(f"车辆参数读取失败：{exc}。原数据保留，可重新填写并保存。")

    def clear_rows(self):
        self.tree.delete(*self.tree.get_children())
        self.rows, self.vehicle = [], None

    def run(self, save=False, silent=False):
        self.clear_rows()
        if self.record is None:
            self.note_text.set("请先点击下方一条历史记录。")
            if not silent:
                messagebox.showinfo("请选择记录", "请先在历史列表中选择一个刹车组合。", parent=self)
            return None
        try:
            vehicle = self.collect()
            rows = deceleration_analysis(self.record["result"]["ratio"], vehicle)
            if save:
                self.store.save_analysis_profile(self.record["id"], vehicle)
        except (ValueError, OverflowError, sqlite3.Error, OSError) as exc:
            self.note_text.set(f"待补充或修正：{exc}")
            if not silent:
                messagebox.showerror("g 值分析未完成", str(exc), parent=self)
            return None
        self.rows, self.vehicle = rows, vehicle
        for row in rows:
            def pct(key):
                return "—" if row[key] is None else f"{row[key]:.2f}"
            self.tree.insert("", "end", values=(
                f"{row['g']:g}", pct("ideal_front_percent"), pct("ideal_rear_percent"),
                pct("hardware_front_percent"), pct("hardware_rear_percent"),
                "—" if row["front_difference_pp"] is None else f"{row['front_difference_pp']:+.2f}",
                row["status"],
            ))
        self.note_text.set("已保存此组合的车辆参数；下次点击记录会自动显示。" if save else "已按当前车辆参数计算。")
        if save:
            self.status.set(f"已保存“{self.record['spec'].name}”的车辆参数及 g 值列表。")
        return rows

    def demo_vehicle(self):
        self.load_vehicle(VehicleSpec(45, 2400, 500, [0, 0.2, 0.4, 0.6, 0.8, 1, 1.2]))
        self.run(silent=True)
        self.note_text.set("仅演示：前轴45%、轴距2400mm、重心高500mm，不代表任何车型，尚未保存。")

    def export(self):
        if self.run() is None:
            return
        path = filedialog.asksaveasfilename(
            title="导出 g 值分析", defaultextension=".csv", filetypes=[("CSV 文件", "*.csv")],
            initialfile="刹车g值分析_" + datetime.now().strftime("%Y%m%d_%H%M%S") + ".csv",
            initialdir=str(APP_DIR), parent=self,
        )
        if not path:
            return
        try:
            with Path(path).open("w", encoding="utf-8-sig", newline="") as stream:
                writer = csv.writer(stream)
                writer.writerow([
                    "记录编号", "组合名称", "硬件计算模式", "硬件前后力矩比",
                    "前轴静态配重_%", "轴距_mm", "重心高度_mm",
                    "前轮胎滚动半径_mm_空值表示同半径假设", "后轮胎滚动半径_mm",
                    "减速度_g", "理想前轴地面制动力_%", "理想后轴地面制动力_%",
                    "硬件前轴地面制动力_%", "硬件后轴地面制动力_%",
                    "前轴差值_百分点", "模型状态",
                ])
                for row in self.rows:
                    writer.writerow([csv_cell(x) for x in [
                        self.record["id"], self.record["spec"].name, MODES[self.record["spec"].mode],
                        self.record["result"]["ratio"], self.vehicle.front_static_percent,
                        self.vehicle.wheelbase_mm, self.vehicle.cg_height_mm,
                        self.vehicle.front_tyre_radius_mm, self.vehicle.rear_tyre_radius_mm,
                        row["g"], row["ideal_front_percent"], row["ideal_rear_percent"],
                        row["hardware_front_percent"], row["hardware_rear_percent"],
                        row["front_difference_pp"], row["status"],
                    ]])
        except (OSError, csv.Error) as exc:
            messagebox.showerror("导出失败", str(exc), parent=self)
            return
        self.status.set(f"已导出 {len(self.rows)} 个 g 值的分析：{path}")


class DiscSizingDialog:
    """A snapshot-based sizing workspace; applying changes only the left draft."""

    def __init__(self, app, source: SetupSpec):
        self.app, self.source = app, source
        self.candidates = []
        self.window = tk.Toplevel(app.root)
        self.window.title("固定前盘 · 按目标比例反算后盘")
        self.window.geometry("940x650")
        self.window.minsize(800, 580)
        self.window.transient(app.root)
        frame = ttk.Frame(self.window, padding=16)
        frame.pack(fill="both", expand=True)
        ttk.Label(frame, text=f"{source.name} ｜ 固定前盘 {source.front.disc_mm:g} mm ｜ {MODES[source.mode]}",
                  style="Percent.TLabel", wraplength=880).pack(anchor="w")
        ttk.Label(frame, text="先在左侧确定前盘及卡钳，再反算后盘；目标填写硬件前轴力矩百分比。",
                  style="Muted.TLabel").pack(anchor="w", pady=(5, 12))
        fields = ttk.Frame(frame)
        fields.pack(fill="x")
        self.target = tk.StringVar(value=f"{calculate(source)['front_percent']:.10g}")
        self.minimum = tk.StringVar(value=f"{min(280, source.front.disc_mm):g}")
        self.maximum = tk.StringVar(value=f"{source.front.disc_mm:g}")
        for label, variable in (("目标前轴 %", self.target), ("后盘下限 mm", self.minimum),
                                ("后盘上限 mm", self.maximum)):
            ttk.Label(fields, text=label).pack(side="left")
            ttk.Entry(fields, textvariable=variable, width=10).pack(side="left", padx=(5, 12))
        controls = ttk.Frame(frame)
        controls.pack(fill="x", pady=10)
        ttk.Button(controls, text="计算候选盘径", command=self.calculate).pack(side="left")
        ttk.Button(controls, text="用右侧记录的比例", command=self.use_record_target).pack(side="left", padx=8)
        self.summary = tk.StringVar(value="")
        ttk.Label(frame, textvariable=self.summary, wraplength=880, justify="left").pack(anchor="w", pady=(0, 10))
        table = ttk.Frame(frame)
        table.pack(fill="both", expand=True)
        columns = (("disc", "后盘外径 mm", 125), ("radius", "后有效半径 mm", 135),
                   ("ratio", "前 / 后力矩比", 130), ("front", "前轴 %", 100),
                   ("rear", "后轴 %", 100), ("difference", "前轴差值 pp", 130))
        self.tree = ttk.Treeview(table, columns=[c[0] for c in columns], show="headings", selectmode="browse")
        for key, text, width in columns:
            self.tree.heading(key, text=text)
            self.tree.column(key, width=width, minwidth=80, anchor="center")
        self.tree.pack(side="left", fill="both", expand=True)
        scroll = ttk.Scrollbar(table, command=self.tree.yview)
        scroll.pack(side="right", fill="y")
        self.tree.configure(yscrollcommand=scroll.set)
        self.apply_button = ttk.Button(frame, text="将所选盘径填入左侧并试算", command=self.apply, state="disabled")
        self.apply_button.pack(anchor="w", pady=10)
        assumption = ("盘径估算：继续假设前后有效半径之比等于盘外径之比。" if source.mode == "diameter" else
                      "有效半径/完整参数：保持后盘 D/2−r 不变，随盘径改变推算后有效半径；摩擦系数及油压保持不变。\n"
                      "这里的 D/2−r 是盘外缘到有效作用半径的距离，不是合头安装偏距。")
        ttk.Label(frame, text=assumption + "\n候选尺寸仅用于计算，不代表现货或可安装尺寸。无法达到目标时显示所需尺寸及可达范围。\n"
                  "列表按目标差值绝对值排序；填入仅改变左侧，保存后才会更新历史记录。",
                  wraplength=880, style="Muted.TLabel", justify="left").pack(anchor="w")
        for variable in (self.target, self.minimum, self.maximum):
            variable.trace_add("write", self.invalidate)
        self.calculate()

    def invalidate(self, *_):
        self.candidates = []
        self.tree.delete(*self.tree.get_children())
        self.apply_button.configure(state="disabled")
        self.summary.set("参数已修改，请重新计算候选盘径。")

    def use_record_target(self):
        record = next((r for r in self.app.all_records if r["id"] == self.app.viewed_id), None)
        if record is None:
            messagebox.showinfo("请选择记录", "先在主窗口选中一条历史记录。", parent=self.window)
            return
        self.target.set(f"{record['result']['front_percent']:.12g}")
        self.calculate()

    def calculate(self):
        self.invalidate()
        try:
            solution = solve_rear_disc(self.source, self.target.get(), self.minimum.get(), self.maximum.get())
        except (ValueError, OverflowError) as exc:
            self.summary.set(f"请修正：{exc}")
            return None
        self.candidates = solution["candidates"]
        state = "目标在设定范围内可达" if solution["feasible"] else "目标在设定范围内不可达；需调整卡钳面积或其他参数"
        self.summary.set(
            f"{state}\n目标前轴 {solution['target_front_percent']:.4f}% → 所需后盘 {solution['required_disc_mm']:.4f} mm\n"
            f"本范围可达前轴 {solution['min_front_percent']:.4f}%～{solution['max_front_percent']:.4f}%；"
            "前盘固定，后盘不得大于前盘。"
        )
        for i, item in enumerate(self.candidates):
            spec, result = item["spec"], item["result"]
            radius = "—" if spec.mode == "diameter" else f"{spec.rear.effective_radius_mm:.4f}"
            self.tree.insert("", "end", iid=str(i), values=(
                f"{spec.rear.disc_mm:.4f}", radius, f"{result['ratio']:.6f}",
                f"{result['front_percent']:.4f}", f"{result['rear_percent']:.4f}", f"{item['difference_pp']:+.4f}",
            ))
        self.tree.selection_set("0")
        self.apply_button.configure(state="normal")
        return solution

    def apply(self):
        selected = self.tree.selection()
        if not self.candidates or not selected:
            return
        item = self.candidates[int(selected[0])]
        try:
            self.app.apply_disc_candidate(self.source, item["spec"])
        except (ValueError, OverflowError) as exc:
            messagebox.showerror("无法填入", str(exc), parent=self.window)
            return
        self.window.destroy()


class BrakeCalculatorApp:
    def __init__(self, root: tk.Tk, store: HistoryStore):
        self.root, self.store = root, store
        self.mysql_dialog = None
        self.loaded_id = None
        self.viewed_id = None
        self.all_records = []
        self.visible_records = []
        self.sort_column, self.sort_reverse = "updated_at", True
        self.form_loading = True
        self.has_result = False
        self.name = tk.StringVar()
        self.mode = tk.StringVar(value=MODES["diameter"])
        self.search = tk.StringVar()
        self.status = tk.StringVar(value="填写参数后计算；历史记录仅保存在本机。")
        self.editing = tk.StringVar(value="新组合")
        self.draft_text = tk.StringVar(value="填写左侧参数后点击“试算”。")
        self.input_assumption = tk.StringVar()
        self.record_text = tk.StringVar(value="请在下方选择历史记录")
        self.ratio_text = tk.StringVar(value="前后力矩比  —")
        self.percent_text = tk.StringVar(value="前轴 —     后轴 —")
        self.extra_text = tk.StringVar(value="右侧显示所选记录的已保存结果；左侧编辑和试算独立进行。")
        self.assumption_text = tk.StringVar()
        self.history_count = tk.StringVar()
        root.title(f"Brake Bias Lab · 刹车力矩计算器 {APP_VERSION}")
        root.geometry("1440x960")
        root.minsize(1080, 720)
        style = ttk.Style(root)
        style.theme_use("clam")
        style.configure(".", font=("Microsoft YaHei UI", 10))
        style.configure("TFrame", background="#f3f5f8")
        style.configure("TLabel", background="#f3f5f8")
        style.configure("TLabelframe", background="#f3f5f8")
        style.configure("TLabelframe.Label", background="#f3f5f8", font=("Microsoft YaHei UI", 11, "bold"))
        style.configure("TButton", padding=(9, 5))
        style.configure("Muted.TLabel", foreground="#5f6d7e", font=("Microsoft YaHei UI", 9))
        style.configure("Title.TLabel", font=("Microsoft YaHei UI", 20, "bold"), foreground="#152a45")
        style.configure("Result.TLabel", font=("Microsoft YaHei UI", 23, "bold"), foreground="#1764b0")
        style.configure("Percent.TLabel", font=("Microsoft YaHei UI", 16, "bold"), foreground="#152a45")
        style.configure("Treeview", rowheight=31, font=("Microsoft YaHei UI", 9))
        style.configure("Treeview.Heading", font=("Microsoft YaHei UI", 9, "bold"), padding=5)
        style.map("Treeview", background=[("selected", "#d5e7fa")], foreground=[("selected", "#122e4b")])
        top = ttk.Frame(root, padding=(18, 12))
        top.pack(fill="x")
        ttk.Label(top, text="刹车力矩计算器", style="Title.TLabel").pack(side="left")
        ttk.Label(top, text="组合命名 · 理论力矩 · 本地历史", style="Muted.TLabel").pack(side="left", padx=18)
        ttk.Button(top, text="公式与填写说明", command=self.show_help).pack(side="right")
        ttk.Button(top, text="备份全部记录", command=self.backup_history).pack(side="right", padx=8)
        ttk.Button(top, text="MySQL 备份", command=self.open_mysql_backup).pack(side="right")
        body = ttk.Panedwindow(root, orient="horizontal")
        body.pack(fill="both", expand=True, padx=14, pady=(0, 8))
        left = ttk.Frame(body)
        right = ttk.Frame(body)
        body.add(left, weight=0)
        body.add(right, weight=1)
        # Scroll only the input pane so adding many piston groups stays usable.
        self.input_canvas = tk.Canvas(left, width=395, background="#f3f5f8", highlightthickness=0)
        left_scroll = ttk.Scrollbar(left, orient="vertical", command=self.input_canvas.yview)
        self.input_canvas.configure(yscrollcommand=left_scroll.set)
        left_scroll.pack(side="right", fill="y")
        self.input_canvas.pack(side="left", fill="both", expand=True)
        form = ttk.Frame(self.input_canvas, padding=(1, 0, 10, 12))
        self.form_window = self.input_canvas.create_window((0, 0), window=form, anchor="nw")
        form.bind("<Configure>", lambda e: self.input_canvas.configure(scrollregion=self.input_canvas.bbox("all")))
        self.input_canvas.bind("<Configure>", lambda e: self.input_canvas.itemconfigure(self.form_window, width=e.width))
        root.bind_all("<MouseWheel>", self._mousewheel, add="+")
        ttk.Label(form, text="组合名称").pack(anchor="w")
        ttk.Entry(form, textvariable=self.name).pack(fill="x", pady=(4, 8))
        ttk.Label(form, textvariable=self.editing, style="Muted.TLabel", wraplength=365).pack(anchor="w")
        ttk.Label(form, text="计算模式").pack(anchor="w", pady=(8, 3))
        ttk.Combobox(
            form, textvariable=self.mode, values=list(MODES.values()), state="readonly"
        ).pack(fill="x")
        ttk.Label(
            form, text="对向卡钳填两侧活塞总数；不同直径分组填写。\n例：六活塞可填 30×2、34×2、38×2。",
            style="Muted.TLabel", wraplength=370,
        ).pack(anchor="w", pady=8)
        self.front_editor = AxleEditor(form, "前轮 · 单只卡钳", self.mark_dirty)
        self.front_editor.pack(fill="x", pady=(0, 10))
        self.rear_editor = AxleEditor(form, "后轮 · 单只卡钳", self.mark_dirty)
        self.rear_editor.pack(fill="x", pady=(0, 10))
        draft_box = ttk.LabelFrame(form, text="左侧输入 · 试算结果", padding=9)
        draft_box.pack(fill="x", pady=(0, 10))
        ttk.Label(draft_box, textvariable=self.draft_text, wraplength=355, justify="left").pack(anchor="w")
        ttk.Label(
            draft_box, textvariable=self.input_assumption, style="Muted.TLabel", wraplength=355
        ).pack(anchor="w", pady=(5, 0))
        actions = ttk.Frame(form)
        actions.pack(fill="x")
        ttk.Button(actions, text="试算", command=self.compute).pack(side="left")
        ttk.Button(actions, text="计算并另存新记录", command=lambda: self.save(False)).pack(side="left", padx=5)
        self.update_button = ttk.Button(form, text="计算并更新载入记录", command=lambda: self.save(True))
        self.update_button.pack(fill="x", pady=6)
        ttk.Button(form, text="固定前盘 · 目标比例反算后盘", command=self.open_disc_sizing).pack(fill="x", pady=(0, 8))
        secondary = ttk.Frame(form)
        secondary.pack(fill="x")
        ttk.Button(secondary, text="新建 / 清空", command=self.new_form).pack(side="left")
        ttk.Button(secondary, text="载入演示参数", command=self.load_demo).pack(side="left", padx=5)
        ttk.Label(form, text="演示参数不代表任何车型，不会自动保存。", style="Muted.TLabel").pack(anchor="w", pady=8)
        self.output_tabs = ttk.Notebook(right, height=470)
        self.output_tabs.pack(fill="x", padx=(10, 0))
        self.result_tab = ttk.Frame(self.output_tabs)
        self.g_tab = ttk.Frame(self.output_tabs)
        self.output_tabs.add(self.result_tab, text="所选记录 · 计算结果")
        self.output_tabs.add(self.g_tab, text="所选记录 · g 值分析")
        result_box = ttk.LabelFrame(self.result_tab, text="所选历史记录 · 已保存结果", padding=14)
        result_box.pack(fill="both", expand=True)
        ttk.Label(result_box, textvariable=self.record_text, style="Percent.TLabel", wraplength=840).pack(
            anchor="w", pady=(0, 9)
        )
        ttk.Label(result_box, textvariable=self.ratio_text, style="Result.TLabel").pack(anchor="w")
        ttk.Label(result_box, textvariable=self.percent_text, style="Percent.TLabel").pack(anchor="w", pady=(3, 8))
        self.bar = tk.Canvas(result_box, height=32, background="#e0e5ed", highlightthickness=0)
        self.bar.pack(fill="x", pady=(0, 8))
        self.bar.bind("<Configure>", lambda e: self.draw_bar())
        self.bar_percent = None
        ttk.Label(result_box, textvariable=self.extra_text, justify="left", wraplength=860).pack(anchor="w")
        ttk.Label(
            result_box, textvariable=self.assumption_text, style="Muted.TLabel", wraplength=860, justify="left"
        ).pack(anchor="w", pady=(7, 0))
        self.g_panel = GAnalysisPanel(self.g_tab, store, self.status)
        self.g_panel.pack(fill="both", expand=True)
        history = ttk.LabelFrame(right, text="已保存的组合", padding=10)
        history.pack(fill="both", expand=True, padx=(10, 0), pady=(12, 0))
        searchbar = ttk.Frame(history)
        searchbar.pack(fill="x", pady=(0, 8))
        ttk.Label(searchbar, text="名称 / 盘径 / 编号").pack(side="left")
        ttk.Entry(searchbar, textvariable=self.search, width=25).pack(side="left", padx=7)
        ttk.Button(searchbar, text="清除", command=lambda: self.search.set("")).pack(side="left")
        ttk.Button(searchbar, text="刷新", command=self.refresh_history).pack(side="left", padx=5)
        ttk.Label(searchbar, textvariable=self.history_count, style="Muted.TLabel").pack(side="right")
        table_frame = ttk.Frame(history)
        table_frame.pack(fill="both", expand=True)
        columns = [
            ("name", "组合名称", 185), ("mode", "计算模式", 90),
            ("front_groups", "前活塞 mm×数量", 145), ("rear_groups", "后活塞 mm×数量", 145),
            ("discs", "前 / 后盘 mm", 110), ("ratio", "前后力矩比", 110),
            ("front_percent", "前轴 %", 82), ("rear_percent", "后轴 %", 82),
            ("front_area", "前有效面积 mm²", 130), ("rear_area", "后有效面积 mm²", 130),
            ("updated_at", "更新时间", 165), ("id", "记录编号", 95),
        ]
        self.columns = columns
        self.tree = ttk.Treeview(table_frame, columns=[x[0] for x in columns], show="headings", selectmode="browse")
        for key, label, width in columns:
            self.tree.heading(key, text=label, command=lambda k=key: self.sort_history(k))
            self.tree.column(key, width=width, minwidth=65, anchor="w" if key in ("name", "front_groups", "rear_groups") else "center")
        yscroll = ttk.Scrollbar(table_frame, orient="vertical", command=self.tree.yview)
        xscroll = ttk.Scrollbar(table_frame, orient="horizontal", command=self.tree.xview)
        self.tree.configure(yscrollcommand=yscroll.set, xscrollcommand=xscroll.set)
        self.tree.grid(row=0, column=0, sticky="nsew")
        yscroll.grid(row=0, column=1, sticky="ns")
        xscroll.grid(row=1, column=0, sticky="ew")
        table_frame.rowconfigure(0, weight=1)
        table_frame.columnconfigure(0, weight=1)
        self.tree.bind("<Double-1>", self._double_click)
        self.tree.bind("<<TreeviewSelect>>", self.history_selection_changed)
        self.tree.bind("<ButtonRelease-1>", self.history_clicked)
        buttons = ttk.Frame(history)
        buttons.pack(fill="x", pady=(9, 0))
        for text, command in [
            ("载入编辑", self.load_selected), ("查看详情", self.show_details),
            ("重命名", self.rename_selected), ("删除", self.delete_selected),
            ("导出当前列表 CSV", self.export_visible),
        ]:
            ttk.Button(buttons, text=text, command=command).pack(side="left", padx=(0, 5))
        ttk.Label(
            history, text="单击记录更新右侧结果及 g 值分析，保留当前页签；双击载入左侧编辑。",
            style="Muted.TLabel", wraplength=870,
        ).pack(anchor="w", pady=(7, 0))
        footer = ttk.Frame(root, padding=(16, 3, 16, 9))
        footer.pack(fill="x")
        ttk.Label(footer, textvariable=self.status, style="Muted.TLabel", wraplength=1380).pack(anchor="w")
        self.name.trace_add("write", lambda *_: self.mark_dirty())
        self.mode.trace_add("write", self._mode_changed)
        self.search.trace_add("write", lambda *_: self.render_history())
        self.form_loading = False
        self._mode_changed()
        self.update_button.configure(state="disabled")
        self.refresh_history()
        root.report_callback_exception = self.report_exception
        root.protocol("WM_DELETE_WINDOW", self.close)

    def open_mysql_backup(self):
        if self.mysql_dialog and not self.mysql_dialog.closed:
            self.mysql_dialog.window.lift()
            return self.mysql_dialog
        self.mysql_dialog = MySQLBackupDialog(self, self.store.path.parent / "mysql_connection.json", APP_VERSION)
        return self.mysql_dialog

    def close(self):
        if self.mysql_dialog and not self.mysql_dialog.closed:
            if self.mysql_dialog.busy:
                self.mysql_dialog.note.set("MySQL 任务仍在进行，请等待完成后关闭程序。")
                self.mysql_dialog.window.lift()
                return
            self.mysql_dialog.close()
        self.root.destroy()

    def _mousewheel(self, event):
        widget = event.widget
        if str(widget).startswith(str(self.input_canvas)):
            self.input_canvas.yview_scroll(-int(event.delta / 120), "units")

    def mode_key(self):
        return next(key for key, value in MODES.items() if value == self.mode.get())

    def _mode_changed(self, *_):
        mode = self.mode_key()
        self.front_editor.mode_changed(mode)
        self.rear_editor.mode_changed(mode)
        self.input_assumption.set(self.assumptions(mode))
        self.mark_dirty()

    @staticmethod
    def assumptions(mode):
        return {
            "diameter": "估算：前后同压、同摩擦；有效半径比近似等于盘外径比。仅输出比例。",
            "radius": "使用填写的有效半径；假设前后同压、同摩擦。仅输出比例。",
            "full": "使用填写的有效半径、摩擦系数和卡钳油压，计算理论比例及绝对力矩。",
        }[mode] + "\n左右配置一致；硬件力矩结果不包含轴荷转移及 ABS/EBD 的影响。"

    def mark_dirty(self):
        if self.form_loading or not self.has_result:
            return
        self.has_result = False
        self.draft_text.set("左侧参数已修改，请重新试算。\n右侧仍显示所选记录的已保存结果。")

    def collect(self):
        mode = self.mode_key()
        return SetupSpec(
            name=validate_name(self.name.get()), mode=mode,
            front=self.front_editor.collect(mode, "前轮"),
            rear=self.rear_editor.collect(mode, "后轮"),
        )

    def present(self, spec: SetupSpec, result: dict):
        """Render a saved record only. Draft calculations use present_draft()."""
        self.ratio_text.set(f"前后力矩比   {result['ratio']:.4f} : 1")
        self.percent_text.set(f"前轴 {result['front_percent']:.2f}%     后轴 {result['rear_percent']:.2f}%")
        front, rear = result["front"], result["rear"]
        lines = [
            f"前轮：{KINDS[spec.front.kind]} · {group_text(spec.front)} mm · 盘径 {spec.front.disc_mm:g} mm",
            f"后轮：{KINDS[spec.rear.kind]} · {group_text(spec.rear)} mm · 盘径 {spec.rear.disc_mm:g} mm",
            f"有效液压面积 A：前 {front['effective_area_mm2']:.2f} mm²  /  后 {rear['effective_area_mm2']:.2f} mm²",
            f"活塞总数：前 {front['piston_count']} 个  /  后 {rear['piston_count']} 个（各为单只卡钳）",
        ]
        if spec.mode == "full":
            lines.extend([
                f"单轮理论力矩：前 {front['wheel_torque_nm']:.2f} N·m  /  后 {rear['wheel_torque_nm']:.2f} N·m",
                f"整轴理论力矩：前 {front['axle_torque_nm']:.2f} N·m  /  后 {rear['axle_torque_nm']:.2f} N·m",
            ])
        else:
            lines.append("绝对力矩 N·m：本模式不计算。")
        self.extra_text.set("\n".join(lines))
        self.assumption_text.set(self.assumptions(spec.mode))
        self.bar_percent = result["front_percent"]
        self.draw_bar()

    def present_draft(self, spec: SetupSpec, result: dict):
        self.has_result = True
        lines = [
            f"{spec.name} · {MODES[spec.mode]}",
            f"前后力矩比 {result['ratio']:.4f} : 1",
            f"前轴 {result['front_percent']:.2f}% / 后轴 {result['rear_percent']:.2f}%",
            f"有效面积 A：前 {result['front']['effective_area_mm2']:.2f} / "
            f"后 {result['rear']['effective_area_mm2']:.2f} mm²",
        ]
        if spec.mode == "full":
            lines.extend([
                f"单轮力矩：前 {result['front']['wheel_torque_nm']:.2f} / "
                f"后 {result['rear']['wheel_torque_nm']:.2f} N·m",
                f"整轴力矩：前 {result['front']['axle_torque_nm']:.2f} / "
                f"后 {result['rear']['axle_torque_nm']:.2f} N·m",
            ])
        self.draft_text.set("\n".join(lines))

    def clear_record_result(self):
        self.viewed_id = None
        self.record_text.set("请在下方选择历史记录")
        self.ratio_text.set("前后力矩比  —")
        self.percent_text.set("前轴 —     后轴 —")
        self.extra_text.set("右侧显示所选记录的已保存结果；左侧编辑和试算独立进行。")
        self.assumption_text.set("")
        self.bar_percent = None
        self.draw_bar()

    def draw_bar(self):
        self.bar.delete("all")
        percent = getattr(self, "bar_percent", None)
        if percent is None:
            return
        width, height = max(self.bar.winfo_width(), 1), 32
        split = width * percent / 100
        self.bar.create_rectangle(0, 0, split, height, fill="#2876bd", outline="")
        self.bar.create_rectangle(split, 0, width, height, fill="#ce7731", outline="")
        if split > 90:
            self.bar.create_text(split / 2, 16, text=f"前 {percent:.1f}%", fill="white", font=("Microsoft YaHei UI", 10, "bold"))
        if width - split > 90:
            self.bar.create_text((split + width) / 2, 16, text=f"后 {100-percent:.1f}%", fill="white", font=("Microsoft YaHei UI", 10, "bold"))

    def compute(self):
        try:
            spec = self.collect()
            result = calculate(spec)
        except (ValueError, OverflowError) as exc:
            messagebox.showerror("参数有误", str(exc), parent=self.root)
            return None
        self.present_draft(spec, result)
        self.status.set(f"左侧已试算“{spec.name}” · {MODES[spec.mode]}；右侧继续显示所选历史记录。")
        return spec, result

    def open_disc_sizing(self):
        try:
            spec = self.collect()
            calculate(spec)
        except (ValueError, OverflowError) as exc:
            messagebox.showerror("参数有误", str(exc), parent=self.root)
            return None
        return DiscSizingDialog(self, spec)

    def apply_disc_candidate(self, source: SetupSpec, candidate: SetupSpec):
        if self.collect() != source:
            raise ValueError("左侧参数已变更，请关闭此窗口，按最新参数重新反算。")
        calculate(candidate)
        self.rear_editor.disc.set(f"{candidate.rear.disc_mm:.12g}")
        if candidate.mode != "diameter":
            self.rear_editor.radius.set(f"{candidate.rear.effective_radius_mm:.12g}")
        self.compute()
        self.status.set("所选后盘已填入左侧并试算；尚未保存，右侧所选记录继续显示。")

    def save(self, update: bool):
        if update and not self.loaded_id:
            messagebox.showinfo("未载入记录", "请先从列表载入记录，或选择另存新记录。", parent=self.root)
            return
        outcome = self.compute()
        if outcome is None:
            return
        spec, _ = outcome
        try:
            record_id = self.store.save(spec, self.loaded_id if update else None)
        except (ValueError, sqlite3.Error, OSError) as exc:
            messagebox.showerror("保存失败", str(exc), parent=self.root)
            return
        self.loaded_id = record_id
        self.update_button.configure(state="normal")
        self.editing.set(f"已载入记录 {record_id[:8]} · 更新此记录或另存为新记录")
        self.search.set("")
        self.refresh_history(select_id=self.viewed_id or record_id)
        self.status.set(f"已{'更新' if update else '保存'}“{spec.name}” · 编号 {record_id[:8]} · 数据库：{self.store.path}")

    def load_spec(self, spec: SetupSpec, record_id=None):
        self.form_loading = True
        try:
            self.loaded_id = record_id
            self.name.set(spec.name)
            self.mode.set(MODES[spec.mode])
            self.front_editor.load(spec.front)
            self.rear_editor.load(spec.rear)
            self.update_button.configure(state="normal" if record_id else "disabled")
            self.editing.set(f"已载入记录 {record_id[:8]} · 更新此记录或另存为新记录" if record_id else "新组合")
        finally:
            self.form_loading = False
        self._mode_changed()
        self.input_canvas.yview_moveto(0)

    def new_form(self):
        self.load_spec(SetupSpec("", "diameter", AxleSpec("fixed", [], 0), AxleSpec("fixed", [], 0)))
        self.front_editor.disc.set("")
        self.rear_editor.disc.set("")
        self.has_result = False
        self.draft_text.set("填写左侧参数后点击“试算”。")
        self.status.set("已清空左侧输入；右侧所选记录继续显示。")

    def load_demo(self):
        self.load_spec(SetupSpec(
            "演示组合（非车型数据）", "diameter",
            AxleSpec("fixed", [PistonGroup(30, 6)], 350),
            AxleSpec("fixed", [PistonGroup(30, 4)], 330),
        ))
        self.compute()
        self.status.set("已载入演示：前 6×30mm / 350mm，后 4×30mm / 330mm。未保存到历史。")

    def refresh_history(self, select_id=None):
        try:
            self.all_records, errors = self.store.records()
        except (sqlite3.Error, OSError) as exc:
            messagebox.showerror("读取历史失败", str(exc), parent=self.root)
            return
        self.render_history(select_id)
        if errors:
            messagebox.showwarning(
                "部分记录无法读取",
                "以下记录仍保留在数据库中，未被覆盖或删除：\n" + "\n".join(errors[:8]),
                parent=self.root,
            )

    def row_values(self, record):
        spec, result = record["spec"], record["result"]
        return {
            "name": spec.name, "mode": MODES[spec.mode],
            "front_groups": group_text(spec.front), "rear_groups": group_text(spec.rear),
            "discs": f"{spec.front.disc_mm:g} / {spec.rear.disc_mm:g}",
            "ratio": result["ratio"], "front_percent": result["front_percent"],
            "rear_percent": result["rear_percent"],
            "front_area": result["front"]["effective_area_mm2"],
            "rear_area": result["rear"]["effective_area_mm2"],
            "updated_at": record["updated_at"], "id": record["id"],
        }

    def render_history(self, select_id=None):
        previous = self.tree.selection()
        select_id = select_id or (previous[0] if previous else None)
        terms = self.search.get().casefold().split()
        def matches(record):
            values = self.row_values(record)
            text = " ".join(str(values[key]) for key in
                            ("name", "mode", "front_groups", "rear_groups", "discs", "id")).casefold()
            return all(term in text for term in terms)
        self.visible_records = [r for r in self.all_records if matches(r)]
        def sort_key(record):
            if self.sort_column == "discs":
                return (record["spec"].front.disc_mm, record["spec"].rear.disc_mm)
            if self.sort_column == "updated_at":
                return datetime.fromisoformat(record["updated_at"]).astimezone()
            return self.row_values(record)[self.sort_column]
        self.visible_records.sort(
            key=sort_key, reverse=self.sort_reverse
        )
        self.tree.delete(*self.tree.get_children())
        for record in self.visible_records:
            values = self.row_values(record)
            values["ratio"] = f"{values['ratio']:.4f} : 1"
            for key in ("front_percent", "rear_percent", "front_area", "rear_area"):
                values[key] = f"{values[key]:.2f}"
            values["updated_at"] = datetime.fromisoformat(record["updated_at"]).astimezone().strftime("%Y-%m-%d %H:%M:%S")
            values["id"] = record["id"][:8]
            self.tree.insert("", "end", iid=record["id"], values=[values[key] for key, _, _ in self.columns])
        if select_id and self.tree.exists(select_id):
            self.tree.selection_set(select_id)
            self.tree.see(select_id)
        for key, label, _ in self.columns:
            arrow = (" ▼" if self.sort_reverse else " ▲") if key == self.sort_column else ""
            self.tree.heading(key, text=label + arrow)
        self.history_count.set(f"显示 {len(self.visible_records)} / 共 {len(self.all_records)} 条")
        self.history_selection_changed()

    def sort_history(self, column):
        self.sort_reverse = not self.sort_reverse if column == self.sort_column else False
        self.sort_column = column
        self.render_history()

    def selected(self):
        selection = self.tree.selection()
        if not selection:
            messagebox.showinfo("请选择记录", "请先在历史列表中选择一条记录。", parent=self.root)
            return None
        return next((r for r in self.all_records if r["id"] == selection[0]), None)

    def history_selection_changed(self, *_):
        selection = self.tree.selection()
        record = next((r for r in self.all_records if selection and r["id"] == selection[0]), None)
        if record is None and self.viewed_id:
            # Filtering the list does not dismiss the record being inspected.
            record = next((r for r in self.all_records if r["id"] == self.viewed_id), None)
        if record is None:
            self.clear_record_result()
        else:
            self.viewed_id = record["id"]
            self.record_text.set(f"{record['spec'].name}  ·  {record['id'][:8]}")
            self.present(record["spec"], record["result"])
        self.g_panel.set_record(record)

    def history_clicked(self, event):
        if self.tree.identify_region(event.x, event.y) in ("cell", "tree"):
            self.history_selection_changed()

    def _double_click(self, event):
        if self.tree.identify_region(event.x, event.y) in ("cell", "tree"):
            self.load_selected()

    def load_selected(self):
        record = self.selected()
        if record is None:
            return
        self.history_selection_changed()
        self.load_spec(record["spec"], record["id"])
        self.present_draft(record["spec"], record["result"])
        self.status.set(f"已载入“{record['spec'].name}” · 修改后可更新此记录，或另存新记录。")

    def text_window(self, title, content):
        window = tk.Toplevel(self.root)
        window.title(title)
        window.geometry("790x650")
        window.minsize(600, 400)
        frame = ttk.Frame(window, padding=12)
        frame.pack(fill="both", expand=True)
        text = tk.Text(frame, wrap="word", font=("Microsoft YaHei UI", 10), padx=12, pady=12)
        scroll = ttk.Scrollbar(frame, orient="vertical", command=text.yview)
        text.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y")
        text.pack(fill="both", expand=True)
        text.insert("1.0", content)
        text.configure(state="disabled")
        ttk.Button(window, text="关闭", command=window.destroy).pack(pady=(0, 10))
        window.transient(self.root)

    def show_details(self):
        record = self.selected()
        if record:
            self.text_window("组合详情", detail_text(record))

    def rename_selected(self):
        record = self.selected()
        if record is None:
            return
        name = simpledialog.askstring(
            "重命名", "填写新的组合名称：", initialvalue=record["spec"].name, parent=self.root
        )
        if name is None:
            return
        try:
            self.store.rename(record["id"], name)
        except (ValueError, sqlite3.Error, OSError) as exc:
            messagebox.showerror("重命名失败", str(exc), parent=self.root)
            return
        if self.loaded_id == record["id"] and self.name.get() == record["spec"].name:
            self.name.set(name.strip())
        self.refresh_history(select_id=record["id"])
        self.status.set(f"已重命名为“{name.strip()}”。")

    def delete_selected(self):
        record = self.selected()
        if record is None:
            return
        if not messagebox.askyesno(
            "删除记录", f"确定删除“{record['spec'].name}”？\n编号：{record['id'][:8]}\n删除后不能通过界面恢复。",
            parent=self.root,
        ):
            return
        try:
            self.store.delete(record["id"])
        except (ValueError, sqlite3.Error, OSError) as exc:
            messagebox.showerror("删除失败", str(exc), parent=self.root)
            return
        if self.loaded_id == record["id"]:
            self.loaded_id = None
            self.update_button.configure(state="disabled")
            self.editing.set("来源记录已删除；当前输入可以另存新记录")
        self.refresh_history()
        self.status.set(f"已删除“{record['spec'].name}”。")

    def export_visible(self):
        if not self.visible_records:
            messagebox.showinfo("暂无记录", "当前列表没有可导出的记录。", parent=self.root)
            return
        path = filedialog.asksaveasfilename(
            title="导出当前列表", defaultextension=".csv", filetypes=[("CSV 文件", "*.csv")],
            initialfile="刹车组合_" + datetime.now().strftime("%Y%m%d_%H%M%S") + ".csv",
            initialdir=str(APP_DIR), parent=self.root,
        )
        if not path:
            return
        try:
            export_csv(Path(path), self.visible_records)
        except (OSError, csv.Error) as exc:
            messagebox.showerror("导出失败", str(exc), parent=self.root)
            return
        self.status.set(f"已导出当前列表 {len(self.visible_records)} 条记录：{path}")

    def show_help(self):
        self.text_window("公式与填写说明", HELP_TEXT + f"\n\n当前数据库：{self.store.path}")

    def backup_history(self):
        path = filedialog.asksaveasfilename(
            title="备份全部组合及车辆参数", defaultextension=".sqlite3",
            filetypes=[("SQLite 数据库", "*.sqlite3")],
            initialfile="刹车记录备份_" + datetime.now().strftime("%Y%m%d_%H%M%S") + ".sqlite3",
            initialdir=str(APP_DIR), parent=self.root,
        )
        if not path:
            return
        try:
            self.store.backup(Path(path))
        except (ValueError, OSError, sqlite3.Error) as exc:
            messagebox.showerror("备份失败", str(exc), parent=self.root)
            return
        self.status.set(f"已备份全部组合及每条记录的 g 值车辆参数：{path}")

    def report_exception(self, exception, value, traceback):
        logging.error("GUI callback failed", exc_info=(exception, value, traceback))
        messagebox.showerror(
            "操作未完成", f"{value}\n请检查输入或重试。详情已写入 app_errors.log。", parent=self.root
        )


HELP_TEXT = """填写方式
1. 输入组合名称；同名组合可以多次保存，编号和时间不同。
2. 数量按单只卡钳填写，不是整根车轴的数量。
3. 对向固定卡钳：两侧合计，同一直径填写偶数。例如六活塞卡钳填 30×2、34×2、38×2。仅支持两侧活塞规格对称的常见卡钳。
4. 单侧活塞浮动卡钳：只填实际液压活塞。例如单活塞浮动卡钳填 54×1，程序包含另一侧刹车片的反作用力。
5. 多行填写相同直径时会自动合并数量。空白行会忽略。

三种计算模式
盘径估算：只需活塞及盘外径。前后油压、摩擦系数视为相同，并假设有效半径与盘外径成比例。用于快速估算比例；盘外径÷2不是真实有效半径，不能据此给出绝对力矩。
有效半径：补充前后有效作用半径，使用前后同压、同摩擦的假设计算比例，不给出绝对力矩。
完整参数：必须输入前后有效半径、刹车片摩擦系数及卡钳油压。每项均由用户填写，不自动猜测默认值。可计算理论绝对力矩。油压应是到达卡钳的油压。

公式与单位
单个活塞面积 = π×直径²÷4，直径单位mm，面积单位mm²。
有效液压面积A：对向卡钳全部活塞面积÷2；单侧活塞浮动卡钳为全部液压活塞面积。
单轮力矩 T = 2×P×μ×A×r。
P以bar输入，转换N/mm²时乘0.1；r以mm输入，转换m时乘0.001。
整轴力矩=单轮力矩×2，默认左右配置一致。
前后力矩比 K=前轮力矩÷后轮力矩。
前轴占比=K÷(1+K)，后轴占比=1÷(1+K)。
盘径估算模式：K≈(前有效面积×前盘外径)÷(后有效面积×后盘外径)。
有效半径模式：K=(前有效面积×前有效半径)÷(后有效面积×后有效半径)。

有效半径
优先使用卡钳/刹车片厂家针对相应盘径标注的数据。它是刹车片摩擦产生力矩的等效作用半径，不是盘外径的一半；也不是刹车片厚度或轮胎半径。
不同刹车片形状、安装位置及压力分布会影响有效半径。

历史记录
试算：仅更新左侧“左侧输入·试算结果”，不改变右侧当前查看的历史记录。
右侧“所选记录·计算结果”显示已保存记录的名称、编号、力矩、占比、面积及计算假设。
左侧改名称、改参数、切换模式、清空或载入演示，都不会改变右侧结果或g值分析。
单击历史记录会同时更新右侧两个页签的数据，并保留你正在查看的页签。
计算并另存新记录：每次创建新编号，即使名称相同也保留。
载入编辑：将选中的历史参数载入左侧；双击记录也可以。
计算并更新载入记录：覆盖载入编号对应的记录，保留创建时间并更新修改时间。如果右侧正在查看此记录，保存成功后才刷新。
另存新记录或更新另一条记录时，右侧继续显示原先选中的记录；没有选中记录时，首次保存后显示新记录。
选中列表其他行不会改变当前载入的编号；以左侧载入提示为准。
重命名、删除：作用于列表中选中的记录。
搜索支持名称、模式、活塞规格、盘径及完整/短编号；多个关键词用空格分隔，需同时匹配。盘径列按前盘、后盘数值排序。
导出CSV包含当前筛选列表的所有参数和结果，使用UTF-8 BOM，方便Excel打开。
SQLite数据库在data目录内自动保存。“备份全部记录”可在运行中生成一致的数据库快照，包含所有组合及其车辆参数。
恢复备份：先关闭所有计算器窗口，保留当前data目录副本，再把备份复制为data/brake_history.sqlite3。

MySQL备份
点击“MySQL备份”，填写服务器、端口、已存在的数据库名、用户名及密码。
测试连接后可“备份到MySQL”；每次生成独立完整SQLite快照，保留组合及g值参数，远端历史备份不会被覆盖。
“保存连接配置”只保存服务器、端口、库名、用户名与TLS/CA设置，密码只在本次窗口内使用。
默认启用TLS并校验证书及主机名；私有CA可选择证书文件。可信本地连接可关闭TLS。
“刷新备份列表”显示最近200次；选择后下载为.sqlite3恢复文件，会校验SHA-256和SQLite完整性，不覆盖当前数据库。
网络操作在后台执行，任务完成前请勿关闭程序。首次备份需CREATE、REFERENCES权限，之后需SELECT、INSERT。
本地数据库仍为主存储；MySQL不是实时同步，不会自动上传任何记录。

目标比例反算后盘
先在左侧确定卡钳和固定前盘，例如355mm，再点击“固定前盘·目标比例反算后盘”。
目标填写前轴硬件力矩百分比，也可取右侧所选记录的硬件比例；不自动选择所谓最佳比例。
设定后盘上下限，上限不得大于固定前盘；程序列出理论所需后盘及当前范围可达的前轴百分比。
盘径估算模式继续使用有效半径与外径成比例的假设。
有效半径/完整参数模式保持后盘外缘至有效作用半径的距离D/2-r不变，推算新盘对应半径；完整参数的摩擦系数和油压不变。
D/2-r不是合头安装偏距；这个模型需用实际刹车片/卡钳几何复核，不校验盘厚、摩擦环扫掠、卡钳允许盘径或安装空间。
候选尺寸按目标百分比差值排序，仅是计算尺寸，不表示有对应商品。目标不可达时保留所需尺寸，明确显示不可达。
选择候选后“填入左侧并试算”只修改左侧草稿，不写入历史，不影响右侧记录。修改目标或上下限后须重新计算。

结果范围
基础计算页只计算硬件理论力矩；g值分析页另行按车辆动态轴荷计算理想分配。两者都不预测实际抱死顺序或制动距离，不模拟ABS/EBD干预、温度及卡钳弹性。
不同摩擦系数应对应相同使用工况。完整参数结果仍是理论模型结果。

g值分析
单击历史记录后，右侧两个页签均显示该记录的数据。选择“所选记录 · g值分析”查看各g值。上行显示原组合的硬件力矩比；下表的理想/硬件百分比均为地面制动力分配。
前轴静态配重：例如前45%、后55%，填写45。可由水平地面轮荷称量或可靠车辆资料获得。
轴距：前后轮轴中心间距离，单位mm，不是轮距。
重心高度：车辆整体重心距地面高度，单位mm，不是离地间隙或车身高度。
不猜测车型参数。示例车辆按钮仅用于演示，不代表你的车，也不会自动保存。
减速度以g为单位填写，例如1表示1g；多个数用逗号分隔，程序自动去重并排序。
理想前轴占比 = 前轴静态配重占比 + 减速度g值×重心高度/轴距。
理想后轴占比 = 1 - 理想前轴占比。质量在计算比例时约掉，不需要输入。
假设水平路面、准静态纵向载荷转移、前后轮胎相同附着系数；忽略空气下压力及阻力造成的附加载荷转移。给定g值不代表车辆一定能达到该减速度。
0g行只表示静态轴荷参考，0g时制动力为0。后轴法向载荷到0或负值时标注“超出模型”，不输出虚假的负占比。
硬件油压比及刹车片摩擦系数固定时，硬件力矩比不随g变化。
前后轮胎滚动半径同时留空时假设相同；若不同请同时填写。
硬件前后地面制动力比 = 硬件前后力矩比×后轮胎滚动半径/前轮胎滚动半径。
表内“前轴差值pp” = 硬件前轴地面制动力百分比 - 理想前轴百分比。正值仅表示前轴占比较理想值高，不直接判断先抱死的车轴。
车辆参数编辑后实时重算；点击“计算并保存参数”绑定到选中记录。每条组合可保存不同车辆参数。
无专属车辆参数的记录会借用最近一次保存的车辆参数，并明确提示。请核对参数，再保存到该记录。
切换记录前请保存车辆参数，否则未保存的修改不会保留。删除组合时，同时删除其专属车辆参数。
“导出g值表”包含车辆参数、每个g值的理想比例、硬件参考比例及差值。

参考
Wilwood 有效活塞面积说明：
https://wilwood.com/PDF/DataSheets/ds1189.pdf
AP Racing 有效作用半径示例图：
https://apracing.com/cdn/drawings/1145-CP7410-1CD_06.pdf
Cambridge 车辆制动与动态轴荷：
https://www-mdp.eng.cam.ac.uk/web/library/enginfo/textbooks_dvd_only/DAN/brakes/vehicles/vehicles.html
"""


def main():
    if sys.platform == "win32":
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except (AttributeError, OSError):
            pass
    logging.basicConfig(
        filename=str(APP_DIR / "app_errors.log"), level=logging.ERROR,
        format="%(asctime)s %(levelname)s %(message)s", encoding="utf-8",
    )
    root = tk.Tk()
    root.withdraw()
    store = None
    try:
        store = HistoryStore()
        app = BrakeCalculatorApp(root, store)
    except Exception as exc:
        logging.exception("Startup failed")
        messagebox.showerror("启动失败", f"{exc}\n请检查程序目录和数据目录的读写权限。", parent=root)
        if store is not None:
            store.close()
        root.destroy()
        return 1
    root.deiconify()
    try:
        root.mainloop()
    finally:
        store.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
