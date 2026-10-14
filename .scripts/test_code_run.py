"""Real fixed-recipe execution and isolated provenance/query regressions."""
from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np
import yaml

import code_library
import code_run


REAL_REPO = Path(__file__).resolve().parent.parent


class RunTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.project = "synthetic-tt-check"
        for source_path in code_run.SOURCE_FILES:
            target = self.root / source_path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes((REAL_REPO / source_path).read_bytes())
        project_path = self.root / "projects" / self.project
        (project_path / "outputs").mkdir(parents=True)
        (project_path / "schema.yaml").write_text(yaml.safe_dump({"project": {"type": "research"},
                                                                  "compute": {"policy": "local", "backend": "local"}}), encoding="utf-8")
        self.catalog = self.root / "computing/code-library/graph.yaml"
        core_hash = code_run.digest(self.root / "computing/code-library/src/tensor_train.py")
        evidence = {name: {"kind": "code", "title": name, "path": "computing/code-library/src/tensor_train.py",
                           "locator": locator, "sha256": core_hash}
                    for name, locator in [("code.tt_matvec", "py:tt_matvec"), ("code.tt_vjp", "py:tt_vjp")]}
        self.data = {"schema": "scientific-code-library-v1", "scope": "public_working_catalog",
                     "nodes": {"implementation.vjp": {"kind": "implementation", "title": "Synthetic TT adjoint",
                              "summary": "Test context, not a scientific source", "aliases": [], "status": "available",
                              "evidence": ["code.tt_matvec", "code.tt_vjp"]}}, "relations": [], "evidence": evidence}
        self.save_catalog()
        self.patches = [patch.object(code_run, "REPO", self.root), patch.object(code_library, "REPO", self.root),
                        patch.object(code_library, "CATALOG_PATH", self.catalog)]
        for patcher in self.patches:
            patcher.start()

    def tearDown(self):
        for patcher in reversed(self.patches):
            patcher.stop()
        self.temporary.cleanup()

    def save_catalog(self):
        self.catalog.write_text(yaml.safe_dump(self.data), encoding="utf-8")

    def run_case(self, seed=41):
        result = code_run.run_validation(self.project, seed=seed)
        self.metadata_path = self.root / result["metadata_path"]
        return result

    def register_run(self):
        relative = self.metadata_path.relative_to(self.root).as_posix()
        self.data["evidence"]["run.actual"] = {"kind": "run", "title": "Synthetic actual TT check",
                                                "path": relative, "locator": "json:/validation",
                                                "sha256": code_run.digest(self.metadata_path)}
        self.data["nodes"]["validation.actual"] = {"kind": "validation", "title": "Actual local check",
                                                   "summary": "Not a paper experiment", "aliases": ["运行证据"],
                                                   "status": "available", "evidence": ["run.actual"]}
        self.data["relations"].append({"source": "validation.actual", "predicate": "checks", "target": "implementation.vjp",
                                      "status": "documented", "scope": "One fixed recipe, seed and dependency set",
                                      "evidence": ["run.actual"]})
        self.save_catalog()

    def test_real_recipe_archives_actual_inputs_results_and_sources(self):
        result = self.run_case()
        self.assertTrue(result["ok"], result)
        inspected = code_run.inspect_record(self.metadata_path)
        self.assertEqual(inspected["status"], "current")
        record = inspected["record"]
        self.assertEqual(len(record["validation"]["checks"]), 6)
        self.assertEqual(sum(check["element_count"] for check in record["validation"]["checks"]), 80)
        self.assertEqual(record["scientific_status"], "local_check_not_paper_reproduction")
        self.assertIn("results.npz", record["artifacts"])
        self.assertIn("snapshots/tensor_train.py", record["artifacts"])
        self.assertFalse(list(self.metadata_path.parent.rglob("*.pyc")))

    def test_same_seed_reproduces_numeric_results_without_overwrite(self):
        first = self.run_case()
        first_path = self.metadata_path.parent
        second = self.run_case()
        self.assertNotEqual(first["run_id"], second["run_id"])
        with np.load(first_path / "results.npz", allow_pickle=False) as earlier, np.load(self.metadata_path.parent / "results.npz", allow_pickle=False) as later:
            for name in earlier.files:
                np.testing.assert_array_equal(earlier[name], later[name])

    def test_query_reads_actual_record_without_executing_code(self):
        self.run_case()
        self.register_run()
        before = sorted(path.relative_to(self.root).as_posix() for path in self.root.rglob("*"))
        with patch.object(code_run, "execute_worker", side_effect=AssertionError("query executed")), \
                patch.object(importlib.util, "module_from_spec", side_effect=AssertionError("query imported experiment")):
            result = code_library.read_evidence(code_library.load_catalog(), "run.actual")
            context = code_library.context(self.data, "implementation.vjp")
        self.assertTrue(result["ok"])
        self.assertEqual(result["sources"], [])
        self.assertFalse(result["execution_performed"])
        self.assertEqual(result["evidence"]["evidence_role"], "local_run_record")
        self.assertEqual(result["evidence"]["recorded_validation_status"], "passed")
        self.assertTrue(json.loads(result["evidence"]["content"])["passed"])
        self.assertEqual(len(context["relations"]), 1)
        self.assertEqual(before, sorted(path.relative_to(self.root).as_posix() for path in self.root.rglob("*")))

    def test_artifact_tampering_is_rejected_by_query(self):
        self.run_case()
        self.register_run()
        (self.metadata_path.parent / "results.npz").write_bytes(b"tampered")
        with self.assertRaises(code_run.RunError):
            code_run.inspect_record(self.metadata_path)
        result = code_library.read_evidence(self.data, "run.actual")
        self.assertFalse(result["ok"])
        self.assertEqual(result["evidence"]["status"], "invalid_run_record")
        self.assertNotIn("content", result["evidence"])

    def test_current_code_change_preserves_history_but_rejects_current_validation(self):
        self.run_case()
        self.register_run()
        source = self.root / "computing/code-library/src/tensor_train.py"
        source.write_text(source.read_text() + "\n", encoding="utf-8")
        inspection = code_run.inspect_record(self.metadata_path)
        self.assertEqual(inspection["status"], "stale")
        self.assertEqual(inspection["snapshot_integrity"], "current")
        result = code_library.read_evidence(self.data, "run.actual")
        self.assertEqual(result["evidence"]["status"], "stale")
        self.assertNotIn("content", result["evidence"])
        self.assertEqual(code_run.inspect_record(self.metadata_path, check_current=False)["status"], "current")

    def test_dependency_version_change_is_stale(self):
        self.run_case()
        with patch.object(importlib.metadata, "version", return_value="changed-version"):
            inspected = code_run.inspect_record(self.metadata_path)
        self.assertEqual(inspected["status"], "stale")
        self.assertEqual(inspected["dependency_changes"], ["numpy"])

    def test_runner_version_change_is_stale(self):
        self.run_case()
        source = self.root / ".scripts/code_run.py"
        source.write_text(source.read_text() + "\n", encoding="utf-8")
        self.assertEqual(code_run.inspect_record(self.metadata_path)["status"], "stale")

    def test_worker_error_and_timeout_are_archived_without_success_results(self):
        outcomes = [subprocess.CompletedProcess([], 2, "", "synthetic failure"), subprocess.TimeoutExpired([], 1)]
        for outcome in outcomes:
            manager = patch.object(code_run, "execute_worker", side_effect=outcome) if isinstance(outcome, Exception) else patch.object(code_run, "execute_worker", return_value=outcome)
            with manager:
                result = self.run_case()
            expected = "timeout" if isinstance(outcome, Exception) else "error"
            self.assertFalse(result["ok"])
            self.assertEqual(result["status"], expected)
            record = code_run.inspect_record(self.metadata_path)["record"]
            self.assertEqual(record["validation"], {})
            self.assertFalse(record["execution_completed"])
            self.assertNotIn("results.npz", record["artifacts"])

    def test_deliberately_wrong_gradient_records_failed_checks(self):
        specification = importlib.util.spec_from_file_location("tt_negative_check", self.root / "computing/code-library/src/tensor_train.py")
        module = importlib.util.module_from_spec(specification)
        specification.loader.exec_module(module)
        original_vjp = module.tt_vjp

        def wrong_vjp(*arguments):
            result = original_vjp(*arguments)
            result["cores"][0].flat[0] += 1
            return result

        def local_worker(project, run_id, bindings, inputs_hash, timeout):
            directory = code_run.run_directory(project, run_id)
            with np.load(directory / "inputs.npz", allow_pickle=False) as archive:
                cores = [archive[f"core_{index}"].copy() for index in range(3)]
                vector, bias, target = (archive[name].copy() for name in ("vector", "bias", "target"))
            with patch.object(module, "tt_vjp", side_effect=wrong_vjp):
                validation, results = code_run.gradient_check(module, cores, vector, bias, target)
            code_run.write_json(directory / "metrics.json", validation)
            np.savez(directory / "results.npz", **results)
            return subprocess.CompletedProcess([], 0, json.dumps({"passed": validation["passed"]}), "")

        with patch.object(code_run, "execute_worker", side_effect=local_worker):
            result = self.run_case()
        self.assertEqual(result["status"], "failed")
        self.assertFalse(result["ok"])
        record = code_run.inspect_record(self.metadata_path)["record"]
        self.assertFalse(record["validation"]["checks"][0]["passed"])

    def test_preflight_stale_source_does_not_create_run(self):
        source = self.root / "computing/code-library/src/tensor_train.py"
        source.write_text(source.read_text() + "\n", encoding="utf-8")
        with patch.object(code_run, "execute_worker", side_effect=AssertionError("preflight executed")), self.assertRaises(code_run.RunError):
            self.run_case()
        self.assertFalse((self.root / f"projects/{self.project}/outputs/code-runs").exists())

    def test_nonlocal_policy_and_invalid_resource_parameters_rejected(self):
        for seed, timeout in [(-1, 60), (2 ** 32, 60), (41, 0), (41, 121), (41, float("nan"))]:
            with self.subTest(seed=seed, timeout=timeout), self.assertRaises(code_run.RunError):
                code_run.run_validation(self.project, seed=seed, timeout=timeout)
        schema = self.root / f"projects/{self.project}/schema.yaml"
        schema.write_text("project:\n  type: research\ncompute:\n  policy: ssh_gpu\n  backend: local\n", encoding="utf-8")
        with self.assertRaises(code_run.RunError):
            self.run_case()

    def test_symlinks_private_paths_and_arbitrary_metadata_rejected(self):
        for relative in ["private/outputs/run", "../outside", "/etc/passwd", "."]:
            with self.subTest(path=relative), self.assertRaises(code_run.RunError):
                code_run.repo_path(relative)
        with self.assertRaises(code_run.RunError):
            code_run.inspect_record(self.root / "computing/code-library/metadata.json")
        runs = self.root / f"projects/{self.project}/outputs/code-runs"
        runs.symlink_to(self.root / ".scripts", target_is_directory=True)
        with self.assertRaises(code_run.RunError):
            self.run_case()

    def test_run_evidence_cannot_expand_to_arbitrary_output_files(self):
        self.run_case()
        self.register_run()
        for field, value in [("locator", "json:/all"), ("path", "projects/other/outputs/arbitrary.json")]:
            changed = copy.deepcopy(self.data)
            changed["evidence"]["run.actual"][field] = value
            with self.subTest(field=field), self.assertRaises(code_library.CatalogError):
                code_library.validate_structure(changed)

    def test_unsupported_worker_shape_rejected_before_loading_array(self):
        self.run_case()
        record = code_run.inspect_record(self.metadata_path)["record"]
        directory = self.metadata_path.parent
        np.savez(directory / "inputs.npz", core_0=np.ones((999,)), core_1=np.ones(code_run.CORE_SHAPES[1]),
                 core_2=np.ones(code_run.CORE_SHAPES[2]), vector=np.ones(12), bias=np.ones(12), target=np.ones(12))
        packet = {"project": self.project, "run_id": record["run_id"], "source_bindings": record["source_bindings"],
                  "inputs_sha256": code_run.digest(directory / "inputs.npz")}
        with self.assertRaises(code_run.RunError):
            code_run.worker(packet)


if __name__ == "__main__":
    unittest.main()
