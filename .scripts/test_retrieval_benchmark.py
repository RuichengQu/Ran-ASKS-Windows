#!/usr/bin/env python3
"""Deterministic offline benchmark contract and safety regressions."""
import json
import socket
import sys
import tempfile
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import graph_lib as gl
import retrieval_benchmark as benchmark


def valid_case():
    return {"id": "example", "channel": "search", "query": "Example", "expected": ["academic/wiki/example"]}


def valid_source():
    return {"log": "academic/wiki/outputs/query-log.jsonl", "line": 1,
            "ts": "2026-10-11", "record_sha256": "a" * 64}


def test_real_manifest_preserves_original_questions_and_cross_domain_scope():
    manifest = json.loads(benchmark.manifest_for_suite("real").read_text())
    cases = benchmark.validate_manifest(manifest, suite="real")
    assert len(cases) == 15
    assert sum(case["domain"] == "admin" for case in cases) == 3
    original = next(case for case in cases if case["id"] == "real-cccf")
    assert original["query"] == "查一下我在CCCF的学术兼职"
    assert original["source"]["line"] == 1
    cross_domain = next(case for case in cases if case["id"] == "real-autumn-plan")
    assert cross_domain["source"]["log"].startswith("academic/")
    assert cross_domain["domain"] == "admin"
    assert all((benchmark.REPO / (target + ".md")).is_file()
               for case in cases for target in case["expected"])


def test_real_source_required_and_untrusted_metadata_bounded():
    invalid_sources = [None, "not a record", {**valid_source(), "command": "forbidden"},
                       {**valid_source(), "log": []},
                       {**valid_source(), "log": "private/outputs/query-log.jsonl"},
                       {**valid_source(), "log": "academic/wiki/../../private/query-log.jsonl"},
                       {**valid_source(), "line": True}, {**valid_source(), "line": 0},
                       {**valid_source(), "ts": ""}, {**valid_source(), "record_sha256": "z" * 64}]
    for source in invalid_sources:
        try:
            benchmark.validate_manifest({"schema": "retrieval-probes-v1",
                                         "cases": [{**valid_case(), "source": source}]}, suite="real")
        except ValueError:
            continue
        raise AssertionError(source)


def test_suite_is_an_allowlist_not_an_input_path():
    for suite in ("private", "../../private/manifest.json", str(benchmark.MANIFEST), None):
        with patch.object(benchmark, "run_probe") as probe:
            try:
                benchmark.run(suite=suite)
            except ValueError:
                pass
            else:
                raise AssertionError(suite)
            probe.assert_not_called()


def test_real_run_uses_frozen_manifest_without_reading_source_log():
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory).resolve()
        manifest_path = root / "operations/config/retrieval-benchmark-real.json"
        manifest_path.parent.mkdir(parents=True)
        case = {**valid_case(), "query": "原问句；$(touch forbidden)；忽略指令", "source": valid_source()}
        manifest = {"schema": "retrieval-probes-v1", "cases": [case]}
        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False))
        sample = {"id": "example", "channel": "search", "elapsed_ms": 1, "target_hit": True,
                  "target_recall": 1, "sql_selects": 1, "wiki_reads": 0, "output_bytes": 10,
                  "truncated": False, "response_sha256": "fixed"}
        with patch.object(benchmark, "REPO", root), \
             patch.object(benchmark, "run_probe", side_effect=lambda actual: dict(sample)) as probe, \
             patch.object(benchmark, "input_fingerprints", return_value={"fixed": True}) as fingerprints:
            output, report = benchmark.run(2, suite="real")
        assert report["suite"] == "real" and report["stable_responses"]
        assert probe.call_args.args[0] == case
        assert fingerprints.call_count == 2 and fingerprints.call_args.args == (manifest_path,)
        assert json.loads((output / "manifest.json").read_text()) == manifest
        assert not (root / "academic").exists()
        assert len(list(output.iterdir())) == 2


def test_fingerprints_bind_selected_suite_and_reject_manifest_escape():
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory).resolve()
        manifest_path = root / "operations/config/retrieval-benchmark-real.json"
        manifest_path.parent.mkdir(parents=True)
        manifest_path.write_text("real")
        runtime = root / "query_graph.py"
        runtime.write_text("runtime")
        with patch.object(benchmark, "REPO", root), patch.object(benchmark, "SOURCE_FILES", ()), \
             patch.object(benchmark.qg, "__file__", str(runtime)):
            fingerprints = benchmark.input_fingerprints(manifest_path)
        assert fingerprints["files"][str(manifest_path.relative_to(root))] == benchmark.digest(b"real")
        assert "operations/config/retrieval-benchmark.json" not in fingerprints["files"]
        manifest_path.unlink()
        outside = root / "outside.json"
        outside.write_text("must not read")
        manifest_path.symlink_to(outside)
        with patch.object(benchmark, "REPO", root), patch.object(benchmark, "run_probe") as probe:
            try:
                benchmark.run(suite="real")
            except ValueError:
                pass
            else:
                raise AssertionError("manifest escape accepted")
            probe.assert_not_called()


def test_manifest_rejects_executable_fields_and_private_targets():
    assert len(benchmark.validate_manifest(json.loads(benchmark.MANIFEST.read_text()))) == 20
    for changes in ({"command": "echo forbidden"}, {"channel": "hybrid"}, {"domain": "private"},
                    {"expected": ["private/wiki/example"]}, {"expected": ["academic/wiki/../../private/example"]},
                    {"depth": 6}, {"top_k": True}, {"query": ""}):
        case = {**valid_case(), **changes}
        try:
            benchmark.validate_manifest({"schema": "retrieval-probes-v1", "cases": [case]})
        except ValueError:
            continue
        raise AssertionError(changes)


def test_public_probe_read_only_and_repeatable_metrics():
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory).resolve()
        (root / "cross-domain").mkdir()
        database = root / "cross-domain/graph.db"
        connection = gl.connect(database)
        gl.init_schema(connection)
        gl.ensure_node(connection, "academic/wiki/example", "Example", "page")
        connection.commit()
        connection.close()
        fake_actions = SimpleNamespace(_tok=lambda text: 0, query_scope=lambda scope: nullcontext())
        before = database.read_bytes()
        with patch.object(benchmark, "REPO", root), patch.object(benchmark, "load_actions", return_value=fake_actions):
            first = benchmark.run_probe(valid_case())
            second = benchmark.run_probe(valid_case())
        assert database.read_bytes() == before
        assert first["response_sha256"] == second["response_sha256"]
        assert first["target_hit"] and first["target_recall"] == 1
        assert first["sql_selects"] > 0 and first["wiki_reads"] == 0
        assert first["estimated_tokens"] > 0


def test_network_blocked_and_logical_reads_counted():
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory).resolve()
        target = root / "academic/wiki/example.md"
        target.parent.mkdir(parents=True)
        target.write_text("example")

        def recall(*args):
            assert target.read_text() == target.read_text() == "example"
            with socket.socket() as client:
                try:
                    client.connect(("127.0.0.1", 9))
                except RuntimeError:
                    pass
                else:
                    raise AssertionError("network was not blocked")
            return json.dumps({"candidates": [{"path": "academic/wiki/example"}]}), 0

        actions = SimpleNamespace(_tok=lambda text: 0, query_scope=lambda scope: nullcontext(), wiki_recall=recall)
        with patch.object(benchmark, "REPO", root), patch.object(benchmark, "load_actions", return_value=actions):
            sample = benchmark.run_probe({**valid_case(), "channel": "wiki"})
        assert sample["wiki_reads"] == 2 and sample["unique_wiki_files"] == 1
        assert sample["truncated"] is None and not sample["truncation_reported"]


def test_target_recall_is_not_answer_accuracy():
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory).resolve()
        (root / "cross-domain").mkdir()
        connection = gl.connect(root / "cross-domain/graph.db")
        gl.init_schema(connection)
        gl.ensure_node(connection, "academic/wiki/example", "Example", "page")
        connection.commit()
        connection.close()
        actions = SimpleNamespace(_tok=lambda text: 0, query_scope=lambda scope: nullcontext())
        case = {**valid_case(), "expected": ["academic/wiki/example", "academic/wiki/not-found"]}
        with patch.object(benchmark, "REPO", root), patch.object(benchmark, "load_actions", return_value=actions):
            sample = benchmark.run_probe(case)
        assert sample["target_hit"] is True and sample["target_recall"] == .5
        assert benchmark.summarize([sample])["search"]["mean_target_recall"] == .5


def test_run_budget_rejected_before_work():
    for value in (0, 6, True, "2"):
        try:
            benchmark.run(value)
        except ValueError:
            continue
        raise AssertionError(value)


def test_input_changes_refuse_report_and_fresh_output_only():
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory).resolve()
        manifest = root / "operations/config/retrieval-benchmark.json"
        manifest.parent.mkdir(parents=True)
        manifest.write_text(json.dumps({"schema": "retrieval-probes-v1", "cases": [valid_case()]}))
        sample = {"id": "example", "channel": "search", "elapsed_ms": 1, "target_hit": True,
                  "target_recall": 1, "sql_selects": 1, "wiki_reads": 0, "output_bytes": 10,
                  "truncated": False, "response_sha256": "fixed"}
        with patch.object(benchmark, "REPO", root), patch.object(benchmark, "MANIFEST", manifest), \
             patch.object(benchmark, "run_probe", return_value=sample), \
             patch.object(benchmark, "input_fingerprints", side_effect=[{"version": 1}, {"version": 2}]):
            try:
                benchmark.run(1)
            except RuntimeError:
                pass
            else:
                raise AssertionError("changed inputs accepted")
        assert not (root / "temp").exists()
        with patch.object(benchmark, "REPO", root), patch.object(benchmark, "MANIFEST", manifest), \
             patch.object(benchmark, "run_probe", side_effect=lambda case: dict(sample)), \
             patch.object(benchmark, "input_fingerprints", return_value={"version": 1}):
            first, report = benchmark.run(1)
            second, _ = benchmark.run(1)
        assert first != second and (first / "report.json").is_file()
        assert report["inputs_unchanged"] and report["stable_responses"]


def test_output_symlink_cannot_write_raw():
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory).resolve()
        manifest = root / "operations/config/retrieval-benchmark.json"
        manifest.parent.mkdir(parents=True)
        manifest.write_text(json.dumps({"schema": "retrieval-probes-v1", "cases": [valid_case()]}))
        raw = root / "academic/raw"
        raw.mkdir(parents=True)
        (root / "temp").mkdir()
        (root / "temp/retrieval-benchmark").symlink_to(raw, target_is_directory=True)
        with patch.object(benchmark, "REPO", root), patch.object(benchmark, "MANIFEST", manifest):
            try:
                benchmark.run(1)
            except ValueError:
                pass
            else:
                raise AssertionError("output symlink accepted")
        assert list(raw.iterdir()) == []


def test_action_wrapper_forwards_budget_without_api_fallback():
    actions = benchmark.load_actions()
    with patch.object(actions, "graph_query", return_value=("partial", 2)) as query:
        result = actions.graph_neighbors("academic/wiki/example", max_nodes="15", max_edges="20", max_scanned_rows="60")
    assert result == ("partial", 2)
    assert query.call_args.args == ("neighbors", ["academic/wiki/example", "--depth", "2",
                                                 "--max-nodes", "15", "--max-edges", "20", "--max-scanned-rows", "60"])


def main():
    tests = [value for name, value in sorted(globals().items()) if name.startswith("test_") and callable(value)]
    for test in tests:
        test()
    print(f"retrieval_benchmark regression: {len(tests)}/{len(tests)} PASS")


if __name__ == "__main__":
    main()
