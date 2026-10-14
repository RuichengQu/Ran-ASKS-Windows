#!/usr/bin/env python3
"""Offline synthetic-truth regression for image curve data extraction."""
from __future__ import annotations

import csv
import json
import math
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image, ImageDraw

import extract_data as data

REPO = Path(__file__).resolve().parent.parent


class ExtractDataTests(unittest.TestCase):
    def setUp(self):
        (REPO / "temp").mkdir(exist_ok=True)
        directory = tempfile.TemporaryDirectory(prefix="test-image-data-", dir=REPO / "temp")
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        repo_patch = patch.object(data, "REPO", self.root)
        repo_patch.start()
        self.addCleanup(repo_patch.stop)
        self.source = self.root / "curve.png"
        self.image = Image.new("RGB", (220, 200), "white")
        for column in range(20, 181):
            self.image.putpixel((column, round(150 - (column - 20) / 2)), (220, 30, 30))
        self.image.save(self.source)
        self.task_dir = self.root / "temp" / "task"
        self.task_counter = 0
        self.prepared = data.prepare(self.source, self.task_dir)
        self.spec_path = self.task_dir / "spec.json"
        self.spec = data.read_json(self.spec_path)
        self.spec.update({"chart_type": "cartesian_curve", "plot_bbox": [20, 20, 181, 171],
                          "axes": {"x": self.axis(20, 0, 180, 8, 100, 4),
                                   "y": self.axis(150, 0, 30, 6, 90, 3)},
                          "series": [{"id": "red", "rgb": [220, 30, 30], "color_tolerance": 0}],
                          "calibration_review": {"status": "confirmed", "reviewer": "test"}})
        self.save_spec()

    def axis(self, first_pixel, first_value, last_pixel, last_value, check_pixel, check_value, scale="linear"):
        return {"scale": scale, "unit": "test-unit",
                "ticks": [{"pixel": first_pixel, "value": first_value}, {"pixel": last_pixel, "value": last_value}],
                "checks": [{"pixel": check_pixel, "value": check_value}]}

    def save_spec(self):
        data.write_json(self.spec_path, self.spec)

    def rows(self, result):
        with Path(result["files"]["reference.csv"]).open(encoding="utf-8", newline="") as stream:
            return list(csv.DictReader(stream))

    def reprepare(self):
        self.image.save(self.source)
        self.task_counter += 1
        self.task_dir = self.root / "temp" / f"second-task-{self.task_counter}"
        data.prepare(self.source, self.task_dir)
        self.spec_path = self.task_dir / "spec.json"
        self.spec["source_sha256"] = data.digest(self.source.read_bytes())
        self.save_spec()

    def configure_scatter(self, points=None, *, hollow=False):
        self.image = Image.new("RGB", (220, 200), "white")
        points = points if points is not None else [(40, 120), (80, 100), (140, 50)]
        drawing = ImageDraw.Draw(self.image)
        radius = 3 if hollow else 2
        for x_pixel, y_pixel in points:
            bounds = (x_pixel - radius, y_pixel - radius, x_pixel + radius, y_pixel + radius)
            drawing.ellipse(bounds, outline=(220, 30, 30), fill=None if hollow else (220, 30, 30))
        self.spec["chart_type"] = "cartesian_scatter"
        self.spec["series"][0].update({"marker_shape": "circle", "marker_area_pixels": [5, 40], "marker_span_pixels": [3, 9]})
        self.reprepare()
        return points

    def quality(self, result):
        return data.read_json(Path(result["files"]["quality_records.json"]))

    def test_prepare_is_prompt_free_source_bound_agent_task(self):
        task = self.prepared["agent_task"]
        self.assertEqual(task["schema"], "agent-task-v1")
        self.assertEqual(task["outputs"][0]["path"], "temp/task/spec.json")
        self.assertNotIn("prompt", task)
        self.assertEqual(task["protocol"]["coordinate_space"], "EXIF-normalized-image-pixels")
        self.assertEqual(task["context"]["source_sha256"], data.digest(self.source.read_bytes()))

    def test_linear_truth_and_all_artifact_hashes(self):
        result = data.extract(self.spec_path)
        self.assertEqual(result["status"], "needs_review")
        self.assertEqual(result["workflow_status"], "completed")
        rows = self.rows(result)
        self.assertEqual(len(rows), 161)
        for row in rows:
            self.assertAlmostEqual(float(row["y"]), float(row["x"]) / 2, delta=0.026)
            self.assertEqual(row["sigma_y"], "")
            self.assertEqual(row["point_kind"], "raster_digitized")
        metadata = data.read_json(Path(result["files"]["reference.metadata.json"]))
        self.assertEqual(metadata["axes"]["y"]["unit"], "test-unit")
        self.assertEqual(metadata["overlay_review"], "pending")
        for name, expected in metadata["artifact_sha256"].items():
            self.assertEqual(data.digest(Path(result["files"][name]).read_bytes()), expected)
        with Image.open(result["files"]["digitization_overlay.png"]) as overlay:
            self.assertEqual(overlay.size, (161, 151))

    def test_log_y_truth(self):
        self.spec["axes"]["y"] = self.axis(150, 1, 30, 100, 90, 10, "log")
        self.save_spec()
        for row in self.rows(data.extract(self.spec_path)):
            expected = 10 ** (float(row["x"]) / 6)
            self.assertLess(abs(float(row["y"]) / expected - 1), 0.02)

    def test_log_x_and_log_log_truth(self):
        self.spec["axes"]["x"] = self.axis(20, 1, 180, 100, 100, 10, "log")
        self.save_spec()
        for row in self.rows(data.extract(self.spec_path)):
            self.assertAlmostEqual(float(row["y"]), 2 * math.log10(float(row["x"])), delta=0.026)
        self.spec["axes"]["y"] = self.axis(150, 1, 30, 100, 90, 10, "log")
        self.save_spec()
        result = data.extract(self.spec_path, self.root / "temp" / "log-log")
        for row in self.rows(result):
            self.assertLess(abs(float(row["y"]) / float(row["x"]) ** (2 / 3) - 1), 0.02)

    def test_dashed_curve_does_not_fill_gaps(self):
        for column in range(60, 80):
            for row in range(200):
                self.image.putpixel((column, row), (255, 255, 255))
        self.reprepare()
        result = data.extract(self.spec_path)
        self.assertEqual(result["series_summary"][0]["empty_columns"], 20)
        self.assertEqual(len(self.rows(result)), 141)
        self.assertFalse(any(60 <= int(row["x_pixel"]) < 80 for row in self.rows(result)))

    def test_multicolor_series(self):
        for column in range(20, 181):
            self.image.putpixel((column, 40), (30, 30, 220))
        self.reprepare()
        self.spec["series"].append({"id": "blue", "rgb": [30, 30, 220], "color_tolerance": 0})
        self.save_spec()
        result = data.extract(self.spec_path)
        self.assertEqual([summary["points"] for summary in result["series_summary"]], [161, 161])

    def test_disjoint_pixel_bands_are_not_guessed(self):
        for column in range(60, 80):
            self.image.putpixel((column, 30), (220, 30, 30))
        self.reprepare()
        result = data.extract(self.spec_path)
        self.assertEqual(result["series_summary"][0]["ambiguous_columns"], 20)
        self.assertEqual(len(self.rows(result)), 141)
        rejected = self.quality(result)["rejected_candidates"]
        self.assertEqual(len(rejected), 20)
        self.assertIn("multiple_bands", rejected[0]["reasons"])
        self.assertEqual(rejected[0]["pixel_bbox"][0], 60)
        accepted_ids = {row["point_id"] for row in self.rows(result)}
        self.assertFalse(any(record["candidate_id"] in accepted_ids for record in rejected))

    def test_overlapping_color_masks_fail_without_outputs(self):
        self.spec["series"].append({"id": "another", "rgb": [220, 30, 30], "color_tolerance": 0})
        self.save_spec()
        with self.assertRaisesRegex(ValueError, "足够"):
            data.extract(self.spec_path)
        self.assertFalse((self.task_dir / "result").exists())

    def test_missing_series_is_explicitly_partial(self):
        self.spec["series"].append({"id": "missing", "rgb": [0, 0, 255], "color_tolerance": 0})
        self.save_spec()
        result = data.extract(self.spec_path)
        self.assertEqual(result["series_summary"][1]["points"], 0)
        self.assertTrue(any("不完整" in warning for warning in result["warnings"]))

    def test_no_curve_fails(self):
        self.spec["series"][0]["rgb"] = [0, 0, 255]
        self.save_spec()
        with self.assertRaisesRegex(ValueError, "足够"):
            data.extract(self.spec_path)

    def test_source_and_preview_changes_fail(self):
        self.source.write_bytes(self.source.read_bytes() + b"changed")
        with self.assertRaisesRegex(ValueError, "原图 hash"):
            data.load_spec(self.spec_path)
        self.image.save(self.source)
        (self.task_dir / "normalized.png").write_bytes(b"changed")
        with self.assertRaisesRegex(ValueError, "规范化图片 hash"):
            data.load_spec(self.spec_path)

    def test_bad_log_tick_degenerate_axis_and_wrong_scale_fail(self):
        with self.assertRaisesRegex(ValueError, "必须为正"):
            data.calibrate(self.axis(20, 0, 180, 100, 100, 10, "log"), 220, "x")
        axis = self.axis(20, 1, 20, 2, 100, 10)
        with self.assertRaisesRegex(ValueError, "不能重复"):
            data.calibrate(axis, 220, "x")
        axis = self.axis(20, 1, 180, 100, 100, 10)
        with self.assertRaisesRegex(ValueError, "不一致"):
            data.calibrate(axis, 220, "x")

    def test_independent_check_cannot_be_a_calibration_tick(self):
        self.spec["axes"]["x"]["checks"] = [{"pixel": 20, "value": 0}]
        self.save_spec()
        with self.assertRaisesRegex(ValueError, "不能重复"):
            data.load_spec(self.spec_path)

    def test_unconfirmed_or_unsupported_specs_fail(self):
        self.spec["calibration_review"]["status"] = "pending"
        self.save_spec()
        with self.assertRaisesRegex(ValueError, "确认者|确认"):
            data.load_spec(self.spec_path)
        self.spec["calibration_review"]["status"] = "confirmed"
        self.spec["chart_type"] = "scatter"
        self.save_spec()
        with self.assertRaisesRegex(ValueError, "首版只支持"):
            data.load_spec(self.spec_path)

    def test_bad_bbox_nonfinite_and_formula_id_fail(self):
        self.spec["plot_bbox"] = [20, 20, 999, 171]
        self.save_spec()
        with self.assertRaises(ValueError):
            data.load_spec(self.spec_path)
        with self.assertRaises(ValueError):
            data.number(float("nan"), "number")
        self.spec["plot_bbox"] = [20, 20, 181, 171]
        self.spec["series"][0]["id"] = "=formula"
        self.save_spec()
        with self.assertRaisesRegex(ValueError, "公式"):
            data.load_spec(self.spec_path)

    def test_output_boundaries_and_no_overwrite(self):
        for destination in (self.root / "raw" / "out", self.root / "academic" / "wiki" / "out",
                            self.root / ".scripts" / "out", self.root.parent / "out"):
            with self.subTest(destination=destination), self.assertRaises(ValueError):
                data.extract(self.spec_path, destination)
        destination = self.root / "projects" / "demo" / "outputs" / "run"
        data.extract(self.spec_path, destination)
        with self.assertRaisesRegex(ValueError, "已存在"):
            data.extract(self.spec_path, destination)
        with self.assertRaisesRegex(ValueError, "已存在"):
            data.prepare(self.source, self.task_dir)

    def test_output_symlink_cannot_escape(self):
        raw = self.root / "raw"
        raw.mkdir()
        (self.root / "temp" / "alias").symlink_to(raw, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "不得写入"):
            data.extract(self.spec_path, self.root / "temp" / "alias" / "result")
        outside = self.root / "temp" / "outside"
        outside.symlink_to(self.root.parent, target_is_directory=True)
        with self.assertRaises(ValueError):
            data.extract(self.spec_path, outside / "result")

    def test_private_source_is_not_exported(self):
        private = self.root / "private"
        private.mkdir()
        source = private / "figure.png"
        self.image.save(source)
        with self.assertRaisesRegex(ValueError, "private"):
            data.prepare(source, self.root / "temp" / "private-task")

    def test_source_orientation_is_normalized(self):
        source = self.root / "rotated.jpg"
        image = Image.new("RGB", (80, 40), "white")
        exif = Image.Exif()
        exif[274] = 6
        image.save(source, exif=exif)
        destination = self.root / "temp" / "rotated"
        data.prepare(source, destination)
        manifest = data.read_json(destination / "source.json")
        self.assertEqual([manifest["width"], manifest["height"]], [40, 80])

    def test_result_can_replay_without_staging_directory(self):
        result = data.extract(self.spec_path, self.root / "projects" / "demo" / "outputs" / "first")
        (self.task_dir / "normalized.png").unlink()
        (self.task_dir / "source.json").unlink()
        replayed = data.extract(Path(result["files"]["spec.json"]), self.root / "temp" / "replay")
        for name in ("reference.csv", "digitization_overlay.png"):
            self.assertEqual(Path(result["files"][name]).read_bytes(), Path(replayed["files"][name]).read_bytes())

    def test_cli_and_wg_envelopes(self):
        command = [sys.executable, str(REPO / ".scripts" / "extract_data.py"), "check", "--spec", str(self.spec_path)]
        completed = subprocess.run(command, capture_output=True, text=True)
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
        self.assertEqual(json.loads(completed.stdout)["status"], "valid")
        command = [sys.executable, str(REPO / ".scripts" / "wg.py"), "extract-data", "check", "--spec", str(self.spec_path)]
        completed = subprocess.run(command, capture_output=True, text=True)
        result = json.loads(completed.stdout)
        self.assertTrue(result["ok"], completed.stdout)
        self.assertEqual(result["action"], "extract-data")
        self.assertEqual(result["sources"], [str(self.source)])
        self.spec["calibration_review"]["status"] = "pending"
        self.save_spec()
        completed = subprocess.run(command, capture_output=True, text=True)
        self.assertFalse(json.loads(completed.stdout)["ok"])

    def test_shared_kernel_never_calls_remote_ocr(self):
        with patch.object(data.image_ocr, "call_api", side_effect=AssertionError("remote call forbidden")):
            data.prepare(self.source, self.root / "temp" / "offline")
            data.extract(self.spec_path)

    def test_scatter_counts_markers_not_pixel_columns(self):
        points = self.configure_scatter()
        result = data.extract(self.spec_path)
        rows = self.rows(result)
        self.assertEqual(len(rows), len(points))
        self.assertEqual(len({row["point_id"] for row in rows}), len(points))
        for row, (x_pixel, y_pixel) in zip(rows, points):
            self.assertAlmostEqual(float(row["x"]), (x_pixel - 20) / 20)
            self.assertAlmostEqual(float(row["y"]), (150 - y_pixel) / 20)
            self.assertEqual(int(row["x_pixel_min"]), x_pixel - 2)
            self.assertEqual(int(row["x_pixel_max"]), x_pixel + 2)
            self.assertEqual(row["quality_flag"], "automatic")
        self.assertEqual(self.quality(result)["rejected_candidates"], [])
        metadata = data.read_json(Path(result["files"]["reference.metadata.json"]))
        self.assertEqual(metadata["chart_type"], "cartesian_scatter")
        self.assertEqual(metadata["sampling"]["kind"], "raster_markers")
        self.assertIn("scipy", metadata["runtime"])
        self.assertEqual(metadata["status"], "needs_review")
        for name, expected in metadata["artifact_sha256"].items():
            self.assertEqual(data.digest(Path(result["files"][name]).read_bytes()), expected)

    def test_scatter_log_log_truth(self):
        points = self.configure_scatter()
        self.spec["axes"]["x"] = self.axis(20, 1, 180, 100, 100, 10, "log")
        self.spec["axes"]["y"] = self.axis(150, 1, 30, 100, 90, 10, "log")
        self.save_spec()
        for row, (x_pixel, y_pixel) in zip(self.rows(data.extract(self.spec_path)), points):
            self.assertAlmostEqual(float(row["x"]), 10 ** ((x_pixel - 20) / 80))
            self.assertAlmostEqual(float(row["y"]), 10 ** ((150 - y_pixel) / 60))

    def test_hollow_markers_have_one_center_each(self):
        points = self.configure_scatter(hollow=True)
        rows = self.rows(data.extract(self.spec_path))
        self.assertEqual(len(rows), len(points))
        for row, (x_pixel, y_pixel) in zip(rows, points):
            self.assertEqual(float(row["x_pixel"]), x_pixel)
            self.assertEqual(float(row["y_pixel"]), y_pixel)

    def test_scatter_single_marker_is_valid(self):
        self.configure_scatter([(80, 100)])
        result = data.extract(self.spec_path)
        self.assertEqual(len(self.rows(result)), 1)
        self.assertFalse(any("不完整" in warning for warning in result["warnings"]))

    def test_scatter_noise_has_pixel_level_rejection_record(self):
        self.configure_scatter()
        self.image.putpixel((50, 70), (220, 30, 30))
        self.reprepare()
        result = data.extract(self.spec_path)
        self.assertEqual(len(self.rows(result)), 3)
        rejected = self.quality(result)["rejected_candidates"]
        self.assertEqual(len(rejected), 1)
        self.assertEqual(rejected[0]["pixel_bbox"], [50, 70, 51, 71])
        self.assertIn("marker_area_out_of_range", rejected[0]["reasons"])
        summary = result["series_summary"][0]
        self.assertEqual(summary["candidate_components"], 4)
        self.assertEqual(summary["rejected_components"], 1)
        self.assertEqual(summary["coverage"], 0.75)
        self.assertIn("not detection recall", summary["coverage_definition"])

    def test_scatter_clipped_edge_marker_is_not_assigned_a_biased_center(self):
        self.configure_scatter([(20, 100), (80, 100)])
        result = data.extract(self.spec_path)
        self.assertEqual(len(self.rows(result)), 1)
        rejected = self.quality(result)["rejected_candidates"]
        self.assertEqual(len(rejected), 1)
        self.assertIn("boundary_touch", rejected[0]["reasons"])

    def test_scatter_oversize_candidate_is_not_a_data_point(self):
        self.configure_scatter()
        ImageDraw.Draw(self.image).rectangle((30, 40, 50, 60), fill=(220, 30, 30))
        self.reprepare()
        result = data.extract(self.spec_path)
        self.assertEqual(len(self.rows(result)), 3)
        rejected = self.quality(result)["rejected_candidates"]
        self.assertEqual(len(rejected), 1)
        self.assertIn("marker_span_out_of_range", rejected[0]["reasons"])

    def test_scatter_touching_markers_exceeding_declared_size_are_not_split(self):
        self.configure_scatter([(40, 120), (45, 120), (80, 100)])
        result = data.extract(self.spec_path)
        self.assertEqual(len(self.rows(result)), 1)
        self.assertEqual(result["series_summary"][0]["candidate_components"], 2)
        self.assertIn("marker_span_out_of_range", self.quality(result)["rejected_candidates"][0]["reasons"])

    def test_scatter_multiple_colors_and_ambiguous_color_selection(self):
        self.configure_scatter()
        ImageDraw.Draw(self.image).ellipse((138, 78, 142, 82), fill=(30, 30, 220))
        self.reprepare()
        self.spec["series"].append(dict(self.spec["series"][0], id="blue", rgb=[30, 30, 220]))
        self.save_spec()
        result = data.extract(self.spec_path)
        self.assertEqual([summary["points"] for summary in result["series_summary"]], [3, 1])
        self.spec["series"].append(dict(self.spec["series"][0], id="ambiguous-red"))
        self.save_spec()
        result = data.extract(self.spec_path, self.root / "temp" / "ambiguous-colors")
        self.assertEqual([summary["points"] for summary in result["series_summary"]], [0, 1, 0])
        self.assertTrue(all("color_overlap" in record["reasons"] for record in self.quality(result)["rejected_candidates"]))

    def test_scatter_requires_explicit_valid_marker_geometry(self):
        self.configure_scatter()
        for field in ("marker_area_pixels", "marker_span_pixels"):
            saved = self.spec["series"][0].pop(field)
            self.save_spec()
            with self.assertRaisesRegex(ValueError, "散点标记"):
                data.load_spec(self.spec_path)
            self.spec["series"][0][field] = saved
        for value in ([0, 40], [40, 5], [1, 10001], [1, True]):
            with self.subTest(value=value), self.assertRaises(ValueError):
                data.integer_range(value, 10000, "marker")

    def test_scatter_component_budget_is_enforced(self):
        self.configure_scatter()
        with patch.object(data, "MAX_MARKER_COMPONENTS", 2), self.assertRaisesRegex(ValueError, "候选超过"):
            data.extract(self.spec_path)
        self.assertFalse((self.task_dir / "result").exists())

    def test_scatter_marker_shape_must_be_confirmed(self):
        self.configure_scatter()
        for shape in (None, "triangle", "square"):
            self.spec["series"][0]["marker_shape"] = shape
            self.save_spec()
            with self.subTest(shape=shape), self.assertRaisesRegex(ValueError, "marker_shape"):
                data.load_spec(self.spec_path)

    def test_scatter_results_and_ids_are_repeatable(self):
        self.configure_scatter()
        first = data.extract(self.spec_path)
        second = data.extract(self.spec_path, self.root / "temp" / "repeated-scatter")
        for name in ("reference.csv", "digitization_overlay.png", "quality_records.json"):
            self.assertEqual(Path(first["files"][name]).read_bytes(), Path(second["files"][name]).read_bytes())

    def test_scatter_kernel_never_calls_remote_ocr(self):
        self.configure_scatter()
        with patch.object(data.image_ocr, "call_api", side_effect=AssertionError("remote call forbidden")):
            data.extract(self.spec_path)


if __name__ == "__main__":
    unittest.main()
