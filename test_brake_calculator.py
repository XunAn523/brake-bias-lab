"""Engineering, persistence and GUI workflow checks; all records use temporary databases."""
import copy
import csv
import math
from pathlib import Path
import sqlite3
import tempfile
import tkinter as tk
import unittest
from unittest.mock import patch

from brake_calculator import (
    AxleSpec, BrakeCalculatorApp, HistoryStore, MODES, PistonGroup, SetupSpec, VehicleSpec,
    calculate, deceleration_analysis, detail_text, export_csv, parse_g_values, solve_rear_disc,
)


def sample(mode="diameter"):
    return SetupSpec(
        "Test combination", mode,
        AxleSpec("fixed", [PistonGroup(30, 6)], 350),
        AxleSpec("fixed", [PistonGroup(30, 4)], 330),
    )


def full_sample():
    return SetupSpec(
        "Full parameters", "full",
        AxleSpec("fixed", [PistonGroup(40, 2)], 300, 100, 0.5, 100),
        AxleSpec("floating", [PistonGroup(40, 1)], 300, 100, 0.5, 50),
    )


class EngineeringTests(unittest.TestCase):
    def test_known_diameter_ratio(self):
        result = calculate(sample())
        self.assertAlmostEqual(result["ratio"], 35 / 22)
        self.assertAlmostEqual(result["front_percent"], 100 * 35 / 57)
        self.assertAlmostEqual(result["front_percent"] + result["rear_percent"], 100)
        self.assertIsNone(result["front"]["wheel_torque_nm"])

    def test_fixed_and_floating_equivalent_area(self):
        spec = full_sample()
        spec.rear.pressure_bar = 100
        result = calculate(spec)
        self.assertAlmostEqual(result["ratio"], 1)
        self.assertAlmostEqual(result["front"]["effective_area_mm2"], 400 * math.pi)
        self.assertAlmostEqual(result["rear"]["effective_area_mm2"], 400 * math.pi)

    def test_torque_units_and_axle_multiplier(self):
        result = calculate(full_sample())
        self.assertAlmostEqual(result["front"]["wheel_torque_nm"], 400 * math.pi)
        self.assertAlmostEqual(result["front"]["axle_torque_nm"], 800 * math.pi)
        self.assertAlmostEqual(result["ratio"], 2)
        self.assertAlmostEqual(result["front_percent"], 200 / 3)

    def test_mu_and_pressure_both_affect_full_mode(self):
        spec = full_sample()
        spec.rear.mu = 0.25
        self.assertAlmostEqual(calculate(spec)["ratio"], 4)

    def test_effective_radius_replaces_disc_ratio(self):
        spec = sample("radius")
        spec.front.effective_radius_mm = 140
        spec.rear.effective_radius_mm = 110
        result = calculate(spec)
        self.assertAlmostEqual(result["ratio"], 6 * 140 / (4 * 110))
        self.assertIsNone(result["front"]["wheel_torque_nm"])

    def test_mixed_piston_sizes_use_sum_of_squares(self):
        spec = sample()
        spec.front.groups = [PistonGroup(30, 2), PistonGroup(34, 2), PistonGroup(38, 2)]
        area = math.pi / 4 * (30**2 + 34**2 + 38**2)
        self.assertAlmostEqual(calculate(spec)["front"]["effective_area_mm2"], area)

    def test_invalid_values_are_rejected(self):
        invalid = []
        for value in (0, -10, float("nan"), float("inf"), 1e300):
            spec = sample()
            spec.front.groups[0].diameter_mm = value
            invalid.append(spec)
        for count in (0, -2, 1.5, True, 3):
            spec = sample()
            spec.front.groups[0].count = count
            invalid.append(spec)
        spec = full_sample()
        spec.front.effective_radius_mm = 150
        invalid.append(spec)
        spec = full_sample()
        spec.rear.mu = None
        invalid.append(spec)
        spec = sample()
        spec.name = " "
        invalid.append(spec)
        for spec in invalid:
            with self.subTest(spec=spec), self.assertRaises(ValueError):
                calculate(spec)


class DiscSizingTests(unittest.TestCase):
    def test_diameter_solution_matches_independent_equation(self):
        spec = sample()
        spec.front.disc_mm = 355
        before = copy.deepcopy(spec)
        solution = solve_rear_disc(spec, 65, 280, 355)
        self.assertTrue(solution["feasible"])
        self.assertAlmostEqual(solution["required_disc_mm"], 355 * 1.5 * 35 / 65)
        best = solution["candidates"][0]
        self.assertAlmostEqual(best["result"]["front_percent"], 65)
        self.assertEqual(spec, before)
        for candidate in solution["candidates"]:
            self.assertEqual(candidate["spec"].front, before.front)
            self.assertLessEqual(candidate["spec"].rear.disc_mm, 355)

    def test_effective_radius_solution_keeps_rear_edge_offset(self):
        spec = sample("radius")
        spec.front.disc_mm, spec.front.effective_radius_mm = 355, 148.5
        spec.rear.disc_mm, spec.rear.effective_radius_mm = 350, 145
        solution = solve_rear_disc(spec, 62.5, 280, 355)
        self.assertAlmostEqual(solution["required_radius_mm"], 133.65)
        self.assertAlmostEqual(solution["required_disc_mm"], 327.3)
        self.assertAlmostEqual(solution["rear_edge_offset_mm"], 30)
        self.assertAlmostEqual(solution["candidates"][0]["result"]["front_percent"], 62.5)

    def test_full_mode_preserves_pressure_mu_and_floating_caliper(self):
        solution = solve_rear_disc(full_sample(), 70, 260, 300)
        best = solution["candidates"][0]
        self.assertAlmostEqual(solution["required_radius_mm"], 600 / 7)
        self.assertAlmostEqual(best["result"]["front_percent"], 70)
        self.assertEqual(best["spec"].rear.kind, "floating")
        self.assertEqual(best["spec"].rear.pressure_bar, 50)
        self.assertEqual(best["spec"].rear.mu, 0.5)

    def test_unreachable_target_reports_required_size_and_closest_bound(self):
        solution = solve_rear_disc(sample(), 50, 280, 350)
        self.assertFalse(solution["feasible"])
        self.assertAlmostEqual(solution["required_disc_mm"], 525)
        best = solution["candidates"][0]
        self.assertEqual(best["spec"].rear.disc_mm, 350)
        self.assertAlmostEqual(best["difference_pp"], 10)
        self.assertAlmostEqual(solution["min_front_percent"], 60)
        self.assertAlmostEqual(solution["max_front_percent"], 100 * 525 / 805)
        solution = solve_rear_disc(sample(), 90, 280, 350)
        self.assertFalse(solution["feasible"])
        self.assertEqual(solution["candidates"][0]["spec"].rear.disc_mm, 280)

    def test_invalid_limits_targets_and_radius_geometry(self):
        for target, minimum, maximum in ((0, 280, 350), (100, 280, 350),
                                         (float("nan"), 280, 350), (65, 350, 280),
                                         (65, 280, 355), (65, 0, 350)):
            with self.subTest(target=target, minimum=minimum, maximum=maximum), self.assertRaises(ValueError):
                solve_rear_disc(sample(), target, minimum, maximum)
        with self.assertRaises(ValueError):
            solve_rear_disc(full_sample(), 70, 100, 300)


class DecelerationTests(unittest.TestCase):
    def test_known_load_transfer_and_constant_hardware(self):
        vehicle = VehicleSpec(45, 2400, 500, [0, 0.6, 1, 1.2])
        rows = deceleration_analysis(35 / 22, vehicle)
        self.assertAlmostEqual(rows[0]["ideal_front_percent"], 45)
        self.assertAlmostEqual(rows[1]["ideal_front_percent"], 57.5)
        self.assertAlmostEqual(rows[2]["ideal_front_percent"], 65.83333333333333)
        self.assertAlmostEqual(rows[3]["ideal_front_percent"], 70)
        for row in rows:
            self.assertAlmostEqual(row["hardware_front_percent"], 100 * 35 / 57)
            self.assertAlmostEqual(row["ideal_front_percent"] + row["ideal_rear_percent"], 100)
        self.assertAlmostEqual(rows[2]["front_difference_pp"], 100 * 35 / 57 - 65.83333333333333)

    def test_tyre_radii_convert_torque_ratio_to_force_ratio(self):
        vehicle = VehicleSpec(50, 2500, 500, [0, 1], 400, 300)
        rows = deceleration_analysis(2, vehicle)
        self.assertAlmostEqual(rows[0]["hardware_front_percent"], 60)
        self.assertAlmostEqual(rows[1]["hardware_front_percent"], 60)

    def test_lift_boundary_does_not_produce_negative_percentages(self):
        vehicle = VehicleSpec(50, 2000, 1000, [0.9, 1, 1.1])
        rows = deceleration_analysis(2, vehicle)
        self.assertTrue(rows[0]["valid"])
        for row in rows[1:]:
            self.assertFalse(row["valid"])
            self.assertIsNone(row["ideal_front_percent"])
            self.assertIsNone(row["ideal_rear_percent"])
            self.assertIsNone(row["front_difference_pp"])

    def test_zero_cg_has_no_load_transfer(self):
        rows = deceleration_analysis(1, VehicleSpec(40, 2000, 0, [0, 1, 3]))
        self.assertEqual([r["ideal_front_percent"] for r in rows], [40, 40, 40])

    def test_g_list_parsing_and_validation(self):
        self.assertEqual(parse_g_values("1.2，0;0.6 0.6；1"), [0, 0.6, 1, 1.2])
        for text in ("", "-0.1,1", "nan", "inf", "abc", "1," * 101):
            with self.subTest(text=text), self.assertRaises(ValueError):
                parse_g_values(text)

    def test_invalid_vehicle_parameters(self):
        base = VehicleSpec(45, 2400, 500, [0, 1])
        cases = []
        for attribute, value in [
            ("front_static_percent", 100), ("front_static_percent", 0),
            ("wheelbase_mm", 0), ("cg_height_mm", -1), ("g_values", [-1]),
            ("g_values", [float("nan")]), ("g_values", []),
            ("front_tyre_radius_mm", 300),
        ]:
            vehicle = copy.deepcopy(base)
            setattr(vehicle, attribute, value)
            cases.append(vehicle)
        for vehicle in cases:
            with self.subTest(vehicle=vehicle), self.assertRaises(ValueError):
                deceleration_analysis(2, vehicle)


class StorageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "history.sqlite3"
        self.store = HistoryStore(self.path)

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def test_same_names_survive_restart_and_update(self):
        spec = sample()
        first = self.store.save(spec)
        second = self.store.save(spec)
        self.assertNotEqual(first, second)
        original = next(r for r in self.store.records()[0] if r["id"] == first)
        self.store.close()
        self.store = HistoryStore(self.path)
        self.assertEqual(len(self.store.records()[0]), 2)
        spec.front.disc_mm = 380
        self.store.save(spec, first)
        updated = next(r for r in self.store.records()[0] if r["id"] == first)
        self.assertEqual(updated["created_at"], original["created_at"])
        self.assertAlmostEqual(updated["result"]["ratio"], 6 * 380 / (4 * 330))
        self.store.rename(first, "中文新名称")
        renamed = next(r for r in self.store.records()[0] if r["id"] == first)
        self.assertEqual(renamed["spec"].name, "中文新名称")
        self.assertIn("中文新名称", detail_text(renamed))
        self.store.delete(first)
        with self.assertRaises(ValueError):
            self.store.save(spec, first)
        self.assertEqual(len(self.store.records()[0]), 1)

    def test_csv_contains_full_parameters_and_no_invented_torque(self):
        spec = sample()
        spec.name = "=1+1"
        self.store.save(spec)
        self.store.save(full_sample())
        records, errors = self.store.records()
        self.assertFalse(errors)
        output = Path(self.temp.name) / "export.csv"
        export_csv(output, records)
        self.assertTrue(output.read_bytes().startswith(b"\xef\xbb\xbf"))
        with output.open(encoding="utf-8-sig", newline="") as stream:
            rows = list(csv.DictReader(stream))
        basic = next(r for r in rows if r["组合名称"] == "'=1+1")
        full = next(r for r in rows if r["组合名称"] == "Full parameters")
        self.assertEqual(basic["前单轮力矩_Nm"], "")
        self.assertAlmostEqual(float(full["前单轮力矩_Nm"]), 400 * math.pi)
        self.assertEqual(full["前油压_bar"], "100")

    def test_corrupt_record_is_preserved_and_reported(self):
        self.store.save(sample())
        with self.store.connection:
            self.store.connection.execute(
                "INSERT INTO combinations VALUES (?,?,?,?,?)",
                ("broken", "bad", "bad", "{broken JSON", "{}"),
            )
        records, errors = self.store.records()
        self.assertEqual(len(records), 1)
        self.assertEqual(len(errors), 1)
        self.assertEqual(self.store.connection.execute("SELECT count(*) FROM combinations").fetchone()[0], 2)

    def test_analysis_profile_persistence_and_cascade(self):
        first, second = self.store.save(sample()), self.store.save(full_sample())
        before = self.store.connection.execute(
            "SELECT spec_json,result_json FROM combinations WHERE id=?", (first,)
        ).fetchone()
        vehicle = VehicleSpec(45, 2400, 500, [0, 0.6, 1])
        self.store.save_analysis_profile(first, vehicle)
        self.assertEqual(self.store.analysis_profile(first), vehicle)
        self.assertIsNone(self.store.analysis_profile(second))
        self.assertEqual(self.store.analysis_profile(), vehicle)
        self.store.close()
        self.store = HistoryStore(self.path)
        self.assertEqual(self.store.analysis_profile(first), vehicle)
        after = self.store.connection.execute(
            "SELECT spec_json,result_json FROM combinations WHERE id=?", (first,)
        ).fetchone()
        self.assertEqual(before, after)
        self.store.rename(first, "Renamed with profile")
        self.assertEqual(self.store.analysis_profile(first), vehicle)
        self.store.delete(first)
        self.assertIsNone(self.store.analysis_profile(first))
        self.assertIsNone(self.store.analysis_profile())

    def test_upgrade_old_database_without_modifying_records(self):
        legacy_path = Path(self.temp.name) / "legacy.sqlite3"
        with sqlite3.connect(legacy_path) as legacy:
            legacy.execute("""CREATE TABLE combinations (
                id TEXT PRIMARY KEY, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                spec_json TEXT NOT NULL, result_json TEXT NOT NULL)""")
            row = ("legacy", "2026-10-23T12:00:00+08:00", "2026-10-23T12:00:00+08:00",
                   '{"name":"legacy"}', '{"ratio":1}')
            legacy.execute("INSERT INTO combinations VALUES (?,?,?,?,?)", row)
        upgraded = HistoryStore(legacy_path)
        try:
            self.assertEqual(upgraded.connection.execute("SELECT * FROM combinations").fetchone(), row)
            self.assertIsNone(upgraded.analysis_profile())
        finally:
            upgraded.close()

    def test_live_backup_preserves_records_profiles_and_identifiers(self):
        record_id = self.store.save(sample())
        vehicle = VehicleSpec(45, 2400, 500, [0, 0.6, 1])
        self.store.save_analysis_profile(record_id, vehicle)
        output = Path(self.temp.name) / "backup.sqlite3"
        self.store.backup(output)
        restored = HistoryStore(output)
        try:
            self.assertEqual(restored.records(), self.store.records())
            self.assertEqual(restored.analysis_profile(record_id), vehicle)
            self.assertEqual(restored.connection.execute("PRAGMA integrity_check").fetchone()[0], "ok")
            self.store.delete(record_id)
            self.assertEqual(len(restored.records()[0]), 1)
        finally:
            restored.close()

    def test_failed_backup_preserves_destination_and_cleans_temporary_file(self):
        output = Path(self.temp.name) / "backup.sqlite3"
        output.write_bytes(b"existing backup")
        with patch("brake_calculator.os.replace", side_effect=OSError("disk error")), self.assertRaises(OSError):
            self.store.backup(output)
        self.assertEqual(output.read_bytes(), b"existing backup")
        self.assertFalse(list(Path(self.temp.name).glob(".brake_backup_*")))
        with self.assertRaises(ValueError):
            self.store.backup(self.path)

    def test_invalid_timestamp_is_reported_without_losing_other_records(self):
        valid_id = self.store.save(sample())
        invalid_id = self.store.save(sample())
        with self.store.connection:
            self.store.connection.execute("UPDATE combinations SET updated_at=? WHERE id=?", ("bad date", invalid_id))
        records, errors = self.store.records()
        self.assertEqual([r["id"] for r in records], [valid_id])
        self.assertEqual(len(errors), 1)
        self.assertEqual(self.store.connection.execute("SELECT count(*) FROM combinations").fetchone()[0], 2)


class GuiWorkflowTests(unittest.TestCase):
    def test_sizing_dialog_applies_only_to_left_draft_and_invalidates_old_results(self):
        with tempfile.TemporaryDirectory() as temp:
            root = tk.Tk()
            root.withdraw()
            store = HistoryStore(Path(temp) / "sizing.sqlite3")
            try:
                record_id = store.save(sample())
                app = BrakeCalculatorApp(root, store)
                app.tree.selection_set(record_id)
                app.load_selected()
                before = (app.viewed_id, app.ratio_text.get(), store.records())
                dialog = app.open_disc_sizing()
                dialog.target.set("65")
                self.assertFalse(dialog.candidates)
                solution = dialog.calculate()
                self.assertTrue(solution["feasible"])
                dialog.apply()
                self.assertAlmostEqual(app.collect().rear.disc_mm, 350 * 1.5 * 35 / 65)
                self.assertEqual((app.viewed_id, app.ratio_text.get(), store.records()), before)
                dialog = app.open_disc_sizing()
                source = dialog.source
                candidate = dialog.candidates[0]["spec"]
                app.front_editor.disc.set("355")
                with self.assertRaises(ValueError):
                    app.apply_disc_candidate(source, candidate)
                dialog.window.destroy()
                app.load_spec(full_sample())
                dialog = app.open_disc_sizing()
                dialog.target.set("70")
                dialog.minimum.set("260")
                dialog.calculate()
                dialog.apply()
                self.assertAlmostEqual(calculate(app.collect())["front_percent"], 70)
                self.assertEqual((app.viewed_id, app.ratio_text.get(), store.records()), before)
            finally:
                root.update_idletasks()
                root.destroy()
                store.close()

    def test_numeric_disc_sort_multi_term_search_and_backup_button(self):
        with tempfile.TemporaryDirectory() as temp:
            root = tk.Tk()
            root.withdraw()
            store = HistoryStore(Path(temp) / "sorting.sqlite3")
            try:
                ids = []
                for diameter in (1000, 350, 90):
                    spec = sample()
                    spec.name = "AP street"
                    spec.front.disc_mm = diameter
                    ids.append(store.save(spec))
                app = BrakeCalculatorApp(root, store)
                app.sort_history("discs")
                self.assertEqual([r["spec"].front.disc_mm for r in app.visible_records], [90, 350, 1000])
                app.search.set("AP 350")
                self.assertEqual([r["id"] for r in app.visible_records], [ids[1]])
                app.search.set(ids[0][:8])
                self.assertEqual([r["id"] for r in app.visible_records], [ids[0]])
                output = Path(temp) / "from_button.sqlite3"
                with patch("brake_calculator.filedialog.asksaveasfilename", return_value=str(output)):
                    app.backup_history()
                restored = HistoryStore(output)
                try:
                    self.assertEqual(len(restored.records()[0]), 3)
                finally:
                    restored.close()
            finally:
                root.update_idletasks()
                root.destroy()
                store.close()

    def test_left_edits_calculation_and_clear_never_change_selected_results(self):
        with tempfile.TemporaryDirectory() as temp:
            root = tk.Tk()
            root.withdraw()
            store = HistoryStore(Path(temp) / "independent_view.sqlite3")
            try:
                record_id = store.save(sample())
                store.save_analysis_profile(record_id, VehicleSpec(45, 2400, 500, [0, 0.6, 1]))
                app = BrakeCalculatorApp(root, store)
                app.tree.selection_set(record_id)
                app.history_selection_changed()
                app.load_selected()
                root.update()

                def right_snapshot():
                    return (
                        app.viewed_id, app.record_text.get(), app.ratio_text.get(),
                        app.percent_text.get(), app.extra_text.get(), app.assumption_text.get(),
                        app.bar_percent, copy.deepcopy(app.g_panel.rows),
                        app.g_panel.record["id"], app.output_tabs.select(),
                    )

                for tab in (app.result_tab, app.g_tab):
                    app.output_tabs.select(tab)
                    before = right_snapshot()
                    app.name.set("Unsaved left edit")
                    app.front_editor.disc.set("380")
                    app.front_editor.rows[0]["diameter"].set("32")
                    app.front_editor.add_group("36", "2")
                    app.compute()
                    self.assertIn("Unsaved left edit", app.draft_text.get())
                    self.assertEqual(right_snapshot(), before)
                    app.load_spec(full_sample())
                    app.compute()
                    self.assertIn("1256.64", app.draft_text.get())
                    self.assertEqual(right_snapshot(), before)
                    app.new_form()
                    self.assertEqual(right_snapshot(), before)
                    app.load_demo()
                    self.assertEqual(right_snapshot(), before)
                    root.update()
                    self.assertEqual(right_snapshot(), before)
                # Filtering must not remove the record currently being inspected.
                before = right_snapshot()
                app.search.set("does not match")
                self.assertEqual(len(app.visible_records), 0)
                self.assertEqual(right_snapshot(), before)
            finally:
                root.update_idletasks()
                root.destroy()
                store.close()

    def test_saving_updates_selected_record_only_after_persistence(self):
        with tempfile.TemporaryDirectory() as temp:
            root = tk.Tk()
            root.withdraw()
            store = HistoryStore(Path(temp) / "saved_view.sqlite3")
            try:
                record_id = store.save(sample())
                app = BrakeCalculatorApp(root, store)
                app.tree.selection_set(record_id)
                app.load_selected()
                before = app.ratio_text.get()
                app.front_editor.disc.set("380")
                app.compute()
                self.assertEqual(app.ratio_text.get(), before)
                app.save(True)
                self.assertEqual(app.viewed_id, record_id)
                self.assertIn(f"{6 * 380 / (4 * 330):.4f}", app.ratio_text.get())
                updated = app.ratio_text.get()
                app.load_spec(full_sample())
                app.save(False)
                self.assertNotEqual(app.loaded_id, record_id)
                self.assertEqual(app.viewed_id, record_id)
                self.assertEqual(app.ratio_text.get(), updated)
            finally:
                root.update_idletasks()
                root.destroy()
                store.close()

    def test_update_loaded_record_does_not_switch_record_being_viewed(self):
        with tempfile.TemporaryDirectory() as temp:
            root = tk.Tk()
            root.withdraw()
            store = HistoryStore(Path(temp) / "two_records.sqlite3")
            try:
                first = store.save(sample())
                second = store.save(full_sample())
                app = BrakeCalculatorApp(root, store)
                app.tree.selection_set(first)
                app.load_selected()
                app.tree.selection_set(second)
                app.history_selection_changed()
                before = (app.record_text.get(), app.ratio_text.get(), app.extra_text.get())
                self.assertEqual(app.loaded_id, first)
                self.assertEqual(app.viewed_id, second)
                app.front_editor.disc.set("380")
                app.save(True)
                self.assertEqual(app.loaded_id, first)
                self.assertEqual(app.viewed_id, second)
                self.assertEqual((app.record_text.get(), app.ratio_text.get(), app.extra_text.get()), before)
                root.update()
                self.assertEqual(app.viewed_id, second)
                with patch("brake_calculator.messagebox.askyesno", return_value=True):
                    app.delete_selected()
                self.assertIsNone(app.viewed_id)
                self.assertIsNone(app.g_panel.record)
                self.assertIsNone(app.bar_percent)
            finally:
                root.update_idletasks()
                root.destroy()
                store.close()

    def test_selected_record_controls_analysis_without_changing_editor(self):
        with tempfile.TemporaryDirectory() as temp:
            root = tk.Tk()
            root.withdraw()
            store = HistoryStore(Path(temp) / "analysis_gui.sqlite3")
            try:
                first = store.save(sample())
                second = store.save(full_sample())
                store.save_analysis_profile(first, VehicleSpec(45, 2400, 500, [0, 0.6, 1]))
                store.save_analysis_profile(second, VehicleSpec(60, 3000, 600, [0, 1]))
                app = BrakeCalculatorApp(root, store)
                root.update()
                panel = app.g_panel
                with patch("brake_calculator.messagebox.showerror") as error:
                    app.tree.selection_set(first)
                    app.history_selection_changed()
                    self.assertEqual(panel.record["id"], first)
                    self.assertEqual(len(panel.rows), 3)
                    self.assertAlmostEqual(panel.rows[2]["ideal_front_percent"], 65.83333333333333)
                    self.assertEqual(app.name.get(), "")
                    panel.front_static.set("48")
                    self.assertAlmostEqual(panel.rows[0]["ideal_front_percent"], 48)
                    panel.run(save=True)
                    app.tree.selection_set(second)
                    app.history_selection_changed()
                    self.assertEqual(panel.front_static.get(), "60")
                    self.assertEqual(len(panel.rows), 2)
                    app.tree.selection_set(first)
                    app.history_selection_changed()
                    self.assertEqual(panel.front_static.get(), "48")
                    panel.wheelbase.set("")
                    self.assertEqual(panel.rows, [])
                    self.assertEqual(len(panel.tree.get_children()), 0)
                    panel.wheelbase.set("2400")
                    output = Path(temp) / "g.csv"
                    with patch("brake_calculator.filedialog.asksaveasfilename", return_value=str(output)):
                        panel.export()
                    with output.open(encoding="utf-8-sig", newline="") as stream:
                        rows = list(csv.DictReader(stream))
                    self.assertEqual(len(rows), 3)
                    self.assertEqual(float(rows[0]["前轴静态配重_%"]), 48)
                    self.assertEqual(float(rows[0]["理想前轴地面制动力_%"]), 48)
                    self.assertFalse(error.called, error.call_args_list)
            finally:
                root.update_idletasks()
                root.destroy()
                store.close()

    def test_compute_save_reload_filter_update_rename_export_delete(self):
        with tempfile.TemporaryDirectory() as temp:
            root = tk.Tk()
            root.withdraw()
            store = HistoryStore(Path(temp) / "gui.sqlite3")
            try:
                app = BrakeCalculatorApp(root, store)
                with patch("brake_calculator.messagebox.showerror") as error:
                    app.load_demo()
                    root.update()
                    self.assertIn("1.5909", app.draft_text.get())
                    app.save(False)
                    first = app.loaded_id
                    app.save(False)
                    second = app.loaded_id
                    self.assertNotEqual(first, second)
                    self.assertEqual(len(app.all_records), 2)
                    app.front_editor.disc.set("380")
                    self.assertFalse(app.has_result)
                    app.save(True)
                    self.assertEqual(len(app.all_records), 2)
                    app.tree.selection_set(first)
                    app.load_selected()
                    self.assertEqual(app.front_editor.disc.get(), "350")
                    self.assertEqual(app.loaded_id, first)
                    with patch("brake_calculator.simpledialog.askstring", return_value="Renamed"):
                        app.rename_selected()
                    app.search.set("renamed")
                    self.assertEqual(len(app.visible_records), 1)
                    app.sort_history("front_percent")
                    export = Path(temp) / "visible.csv"
                    with patch("brake_calculator.filedialog.asksaveasfilename", return_value=str(export)):
                        app.export_visible()
                    with export.open(encoding="utf-8-sig", newline="") as stream:
                        self.assertEqual(len(list(csv.reader(stream))), 2)
                    with patch("brake_calculator.messagebox.askyesno", return_value=True):
                        app.delete_selected()
                    self.assertIsNone(app.loaded_id)
                    app.search.set("")
                    self.assertEqual(len(app.all_records), 1)
                    app.new_form()
                    self.assertEqual(app.name.get(), "")
                    self.assertEqual(app.front_editor.disc.get(), "")
                    spec = full_sample()
                    app.load_spec(spec)
                    app.compute()
                    self.assertIn("1256.64", app.draft_text.get())
                    self.assertFalse(error.called, error.call_args_list)
            finally:
                root.update_idletasks()
                root.destroy()
                store.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
