"""Synthetic catalog tests and real host-CLI wiring checks."""
from __future__ import annotations

import copy
import hashlib
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import yaml
import code_library


REPO = Path(__file__).resolve().parent.parent


class CatalogTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.catalog = self.root / "computing/code-library/graph.yaml"
        self.catalog.parent.mkdir(parents=True)
        self.root_patch = patch.object(code_library, "REPO", self.root)
        self.path_patch = patch.object(code_library, "CATALOG_PATH", self.catalog)
        self.root_patch.start()
        self.path_patch.start()
        self.data = self.fixture()
        self.save()

    def tearDown(self):
        self.path_patch.stop()
        self.root_patch.stop()
        self.temporary.cleanup()

    def source(self, kind, name, path, locator, content):
        target = self.root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        return {"kind": kind, "title": name, "path": path, "locator": locator,
                "sha256": hashlib.sha256(target.read_bytes()).hexdigest()}

    def fixture(self):
        evidence = {
            "raw.first": self.source("raw", "Synthetic Paper Alpha", "academic/raw/alpha/paper.md", "Method",
                                     "# Synthetic Alpha\n\n## Method\n\nAlpha convention.\n"),
            "raw.second": self.source("raw", "Synthetic Paper Beta", "academic/raw/beta/paper.md", "Method",
                                      "# Synthetic Beta\n\n## Method\n\nBeta convention.\n"),
            "code.kernel": self.source("code", "Shared synthetic kernel", "computing/code-library/src/kernel.py",
                                       "py:kernel", "raise AssertionError('must never execute')\n\ndef kernel(value):\n    return value\n"),
        }
        nodes = {
            "method.shared": {"kind": "method", "title": "Shared method", "summary": "Synthetic mathematical method",
                              "aliases": ["公共方法"], "status": "available", "evidence": ["raw.first", "raw.second"]},
            "implementation.shared": {"kind": "implementation", "title": "Shared kernel", "summary": "Two source contexts",
                                      "aliases": ["kernel"], "status": "available", "evidence": ["code.kernel"]},
        }
        relations = [
            {"source": "implementation.shared", "predicate": "implements", "target": "method.shared",
             "status": "documented", "scope": "Alpha assumptions", "evidence": ["raw.first", "code.kernel"]},
            {"source": "implementation.shared", "predicate": "implements", "target": "method.shared",
             "status": "documented", "scope": "Beta assumptions", "evidence": ["raw.second", "code.kernel"]},
        ]
        return {"schema": "scientific-code-library-v1", "scope": "public_working_catalog",
                "nodes": nodes, "relations": relations, "evidence": evidence}

    def save(self):
        self.catalog.write_text(yaml.safe_dump(self.data, allow_unicode=True), encoding="utf-8")

    def test_valid_catalog_is_not_scientific_verification(self):
        result = code_library.validate_catalog(code_library.load_catalog())
        self.assertTrue(result["ok"])
        self.assertEqual(result["semantic_verification"], "not_performed")
        self.assertFalse(result["execution_performed"])
        self.assertEqual(result["sources"], [])

    def test_one_implementation_retains_distinct_source_scopes(self):
        result = code_library.context(self.data, "implementation.shared")
        self.assertEqual(len(result["nodes"]), 2)
        self.assertEqual(len(result["relations"]), 2)
        self.assertEqual({item["scope"] for item in result["relations"]}, {"Alpha assumptions", "Beta assumptions"})

    def test_source_titles_and_aliases_are_searchable(self):
        self.assertEqual(code_library.search(self.data, "Synthetic Paper Beta")["candidates"][0]["node_id"], "method.shared")
        self.assertEqual(code_library.search(self.data, "公共方法")["candidates"][0]["node_id"], "method.shared")

    def test_empty_search_and_invalid_limits_rejected(self):
        for query, topk in [("", 8), ("method", 0), ("method", 21)]:
            with self.subTest(query=query, topk=topk), self.assertRaises(code_library.CatalogError):
                code_library.search(self.data, query, topk)
        with self.assertRaises(code_library.CatalogError):
            code_library.context(self.data, "method.shared", depth=3)

    def test_context_budget_reports_truncation(self):
        result = code_library.context(self.data, "implementation.shared", depth=2, limit=1)
        self.assertTrue(result["truncated"])
        self.assertEqual(len(result["nodes"]), 1)

    def test_relation_budget_is_independent_of_node_budget(self):
        original = self.data["relations"][0]
        self.data["relations"] = [{**original, "scope": f"scope {position}"} for position in range(70)]
        result = code_library.context(self.data, "implementation.shared")
        self.assertEqual(len(result["relations"]), 64)
        self.assertTrue(result["truncated"])

    def test_exact_source_read_and_citation_roles(self):
        raw = code_library.read_evidence(self.data, "raw.first")
        self.assertTrue(raw["ok"])
        self.assertEqual(raw["sources"], ["academic/raw/alpha/paper.md#Method"])
        self.assertNotIn("# Synthetic Alpha", raw["evidence"]["content"])
        code = code_library.read_evidence(self.data, "code.kernel")
        self.assertTrue(code["ok"])
        self.assertNotIn("must never execute", code["evidence"]["content"])
        self.assertEqual(code["sources"], [])
        self.assertEqual(code["evidence"]["evidence_role"], "implementation_context")

    def test_modified_code_is_stale_and_not_read(self):
        path = self.root / self.data["evidence"]["code.kernel"]["path"]
        path.write_text("def kernel(value):\n    return value + 1\n", encoding="utf-8")
        result = code_library.read_evidence(self.data, "code.kernel")
        self.assertFalse(result["ok"])
        self.assertEqual(result["evidence"]["status"], "stale")
        self.assertNotIn("content", result["evidence"])
        self.assertEqual(code_library.search(self.data, "kernel")["candidates"][0]["availability"], "needs_review")

    def test_source_staleness_does_not_spread_to_other_relation(self):
        path = self.root / self.data["evidence"]["raw.first"]["path"]
        path.write_text("## Method\n\nChanged Alpha.\n", encoding="utf-8")
        result = code_library.context(self.data, "implementation.shared")
        states = {item["scope"]: item["evidence_status"] for item in result["relations"]}
        self.assertEqual(states, {"Alpha assumptions": "needs_review", "Beta assumptions": "current"})

    def test_missing_source_is_reported(self):
        (self.root / self.data["evidence"]["code.kernel"]["path"]).unlink()
        self.assertFalse(code_library.validate_catalog(self.data)["ok"])

    def test_bad_or_ambiguous_python_locator_rejected(self):
        text = "class First:\n    def same(self):\n        pass\nclass Second:\n    def same(self):\n        pass\n"
        with self.assertRaises(code_library.CatalogError):
            code_library.python_excerpt(text, "py:same")
        excerpt, _, _ = code_library.python_excerpt(text, "py:Second.same")
        self.assertIn("def same", excerpt)

    def test_unknown_evidence_does_not_allow_arbitrary_path(self):
        with self.assertRaises(code_library.CatalogError):
            code_library.read_evidence(self.data, "../../.env")

    def test_private_temp_wiki_environment_and_traversal_paths_rejected(self):
        for path in ["private/codes/kernel.py", "temp/kernel.py", "academic/wiki/paper.md", ".env",
                     "computing/code-library/../../.env", "/etc/passwd"]:
            changed = copy.deepcopy(self.data)
            changed["evidence"]["code.kernel"]["path"] = path
            with self.subTest(path=path), self.assertRaises(code_library.CatalogError):
                code_library.validate_structure(changed)

    def test_source_and_catalog_symlinks_rejected(self):
        source = self.root / self.data["evidence"]["code.kernel"]["path"]
        source.unlink()
        source.symlink_to(self.root / "academic/raw/alpha/paper.md")
        with self.assertRaises(code_library.CatalogError):
            code_library.validate_structure(self.data)
        source.unlink()
        self.catalog.unlink()
        self.catalog.symlink_to(self.root / "academic/raw/alpha/paper.md")
        with self.assertRaises(code_library.CatalogError):
            code_library.load_catalog()

    def test_missing_hash_unknown_fields_and_untyped_data_rejected(self):
        for field, value in [("sha256", ""), ("kind", ["code"]), ("locator", "全篇")]:
            changed = copy.deepcopy(self.data)
            changed["evidence"]["code.kernel"][field] = value
            with self.subTest(field=field), self.assertRaises(code_library.CatalogError):
                code_library.validate_structure(changed)
        changed = copy.deepcopy(self.data)
        changed["nodes"]["method.shared"]["kind"] = {}
        with self.assertRaises(code_library.CatalogError):
            code_library.validate_structure(changed)
        changed = copy.deepcopy(self.data)
        changed["nodes"]["method.shared"]["command"] = "execute me"
        with self.assertRaises(code_library.CatalogError):
            code_library.validate_structure(changed)

    def test_dangling_wrong_type_duplicate_and_unproven_relations_rejected(self):
        mutations = [("target", "method.missing"), ("predicate", "checks"), ("status", "verified"), ("evidence", [])]
        for field, value in mutations:
            changed = copy.deepcopy(self.data)
            changed["relations"][0][field] = value
            with self.subTest(field=field), self.assertRaises(code_library.CatalogError):
                code_library.validate_structure(changed)
        changed = copy.deepcopy(self.data)
        changed["relations"].append(copy.deepcopy(changed["relations"][0]))
        with self.assertRaises(code_library.CatalogError):
            code_library.validate_structure(changed)

    def test_duplicate_yaml_keys_aliases_and_arbitrary_catalog_rejected(self):
        self.catalog.write_text("schema: one\nschema: two\n", encoding="utf-8")
        with self.assertRaises(code_library.CatalogError):
            code_library.load_catalog()
        self.catalog.write_text("value: &anchor []\nother: *anchor\n", encoding="utf-8")
        with self.assertRaises(code_library.CatalogError):
            code_library.load_catalog()
        with patch.object(code_library, "CATALOG_PATH", self.root / "other.yaml"), self.assertRaises(code_library.CatalogError):
            code_library.load_catalog()

    def test_excerpt_budget_refuses_without_partial_content(self):
        path = self.root / "academic/raw/alpha/paper.md"
        path.write_text("## Method\n\n" + "long text " * 1000, encoding="utf-8")
        self.data["evidence"]["raw.first"]["sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
        result = code_library.read_evidence(self.data, "raw.first")
        self.assertFalse(result["ok"])
        self.assertEqual(result["sources"], [])
        self.assertNotIn("content", result["evidence"])

    def test_hash_is_not_automatically_refreshed(self):
        before = self.catalog.read_bytes()
        code_library.validate_catalog(self.data)
        code_library.search(self.data, "kernel")
        code_library.context(self.data, "method.shared")
        code_library.read_evidence(self.data, "code.kernel")
        self.assertEqual(self.catalog.read_bytes(), before)


class HostIntegrationTests(unittest.TestCase):
    def run_json(self, *arguments):
        completed = subprocess.run([sys.executable, *arguments], cwd=REPO, capture_output=True, text=True)
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
        return json.loads(completed.stdout)

    def test_real_catalog_validates_and_uses_real_paper(self):
        result = self.run_json(".scripts/code_library.py", "validate")
        self.assertTrue(result["ok"])
        result = self.run_json(".scripts/wg.py", "code-library", "search", "Tensorizing Neural Networks")
        self.assertTrue(result["ok"])
        self.assertTrue(result["result"]["candidates"])
        self.assertFalse(result["result"]["execution_performed"])

    def test_host_can_read_code_without_claiming_raw_source(self):
        result = self.run_json(".scripts/wg.py", "code-library", "read", "code.tt_matvec")
        self.assertIn("def tt_matvec", result["result"]["evidence"]["content"])
        self.assertEqual(result["sources"], [])

    def test_public_query_routes_code_evidence_private_does_not(self):
        for stage in ["start", "evidence"]:
            public = subprocess.run([sys.executable, ".scripts/route.py", "--task", "query", "--query-stage", stage],
                                    cwd=REPO, capture_output=True, text=True, check=True)
            self.assertIn("## 科研代码证据", public.stdout)
            private = subprocess.run([sys.executable, ".scripts/route.py", "--task", "query", "--query-stage", stage,
                                      "--subproject", "private"], cwd=REPO, capture_output=True, text=True, check=True)
            self.assertNotIn("## 科研代码证据", private.stdout)

    def test_registry_exposes_read_only_agent_function(self):
        import function_registry
        function = function_registry.show("science.code_library")
        self.assertEqual(function["backends"], ["agent"])
        self.assertEqual(function["effects"], ["read"])


if __name__ == "__main__":
    unittest.main()
