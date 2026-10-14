#!/usr/bin/env python3
"""query_graph.py 搜索、来源详情、关系与时态的纯代码回归测试。"""
import importlib.util
import json
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import graph_lib as gl

SCRIPT = Path(__file__).with_name("query_graph.py")
spec = importlib.util.spec_from_file_location("query_graph", SCRIPT)
module = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(module)


def make_db(directory):
    conn = sqlite3.connect(Path(directory) / "graph.db")
    conn.row_factory = sqlite3.Row
    gl.init_schema(conn)
    gl.ensure_node(conn, "page-a", "A", "page")
    gl.ensure_node(conn, "page-b", "B", "page")
    return conn


def add_fact(conn, fid=None, subject="page-a", predicate="政策", obj="page-b", valid_from=None, valid_until=None, superseded_by=None, source="raw/policy.md"):
    keys = ["subject", "predicate", "object", "valid_from", "valid_until", "superseded_by", "source"]
    values = [subject, predicate, obj, valid_from, valid_until, superseded_by, source]
    if fid is not None:
        keys.insert(0, "id")
        values.insert(0, fid)
    conn.execute(f"INSERT INTO temporal_facts ({','.join(keys)}) VALUES ({','.join(['?']*len(keys))})", values)
    return conn.execute("SELECT last_insert_rowid()").fetchone()[0]


def test_point_in_time_filters_by_validity():
    with tempfile.TemporaryDirectory() as directory:
        conn = make_db(directory)
        add_fact(conn, valid_from="2024-01-01", valid_until="2024-06-30", source="raw/old.md")
        add_fact(conn, valid_from="2024-07-01", valid_until=None, source="raw/current.md")
        conn.commit()
        before = module.temporal_at(conn, "2024-03-15")
        after = module.temporal_at(conn, "2024-08-01")
        conn.close()
        assert before["count"] == 1
        assert before["facts"][0]["source"] == "raw/old.md"
        assert after["count"] == 1
        assert after["facts"][0]["source"] == "raw/current.md"


def test_superseded_fact_returns_only_for_old_snapshot():
    with tempfile.TemporaryDirectory() as directory:
        conn = make_db(directory)
        successor_id = add_fact(conn, valid_from="2024-07-01", valid_until=None, source="raw/new.md")
        add_fact(conn, valid_from="2024-01-01", valid_until=None, superseded_by=successor_id, source="raw/old.md")
        conn.commit()
        old_result = module.temporal_at(conn, "2024-03-15")
        new_result = module.temporal_at(conn, "2024-08-01")
        conn.close()
        assert old_result["count"] == 1
        assert old_result["facts"][0]["source"] == "raw/old.md"
        assert new_result["count"] == 1
        assert new_result["facts"][0]["source"] == "raw/new.md"


def test_filters_subject_object_predicate():
    with tempfile.TemporaryDirectory() as directory:
        conn = make_db(directory)
        add_fact(conn, predicate="属于", valid_from="2024-01-01")
        add_fact(conn, predicate="负责", valid_from="2024-01-01")
        conn.commit()
        result = module.temporal_at(conn, "2024-06-01", subject="page-a", obj="page-b", predicate="属于")
        conn.close()
        assert result["count"] == 1
        assert result["facts"][0]["predicate"] == "属于"


def test_invalid_date_returns_error():
    with tempfile.TemporaryDirectory() as directory:
        conn = make_db(directory)
        result = module.temporal_at(conn, "not-a-date")
        conn.close()
        assert set(result) == {"error"}


def test_node_info_exposes_description_and_raw_located_glosses():
    with tempfile.TemporaryDirectory() as directory:
        conn = make_db(directory)
        gl.ensure_node(
            conn, "concept", "Concept", "entity", entity_subtype="keyword"
        )
        gl.add_node_gloss(
            conn, "concept", "academic/wiki/papers/example",
            "academic/raw/references/example/paper.md#L55", "局部概念说明",
        )
        result = module.node_info(conn, "concept")
        conn.close()
    assert result["entity_subtype"] == "keyword"
    assert result["description"] == "局部概念说明"
    assert result["glosses"] == [{
        "origin_page": "academic/wiki/papers/example",
        "source": "academic/raw/references/example/paper.md#L55",
        "description": "局部概念说明",
        "is_primary": 1,
    }]


def test_relations_profile_filters_derived_predicate_families():
    with tempfile.TemporaryDirectory() as directory:
        conn = make_db(directory)
        gl.ensure_node(conn, "raw-a", "Raw A", "raw")
        gl.ensure_node(conn, "hub-a", "Hub A", "hub")
        gl.ensure_node(conn, "author-a", "Author A", "entity", entity_subtype="person")
        conn.executemany(
            "INSERT INTO edges (subject,predicate,object,confidence) VALUES (?,?,?,?)",
            [
                ("page-a", "来源", "raw-a", "可追溯"),
                ("page-a", "核心方法", "page-b", "可追溯"),
                ("page-a", "主要研究", "hub-a", "可追溯"),
                ("author-a", "作者", "page-a", "可追溯"),
            ],
        )
        result = module.relations(conn, "page-a", profile="explanation")
        conn.close()
    assert {edge["family"] for edge in result["edges"]} == {"evidence", "semantic"}
    assert {edge["predicate"] for edge in result["edges"]} == {"来源", "核心方法"}


def test_search_ranks_all_exact_candidates_before_limit_and_keeps_ambiguity():
    with tempfile.TemporaryDirectory() as directory:
        conn = make_db(directory)
        for index in range(60):
            gl.ensure_node(conn, f"noise-{index:02}", f"a decomposition {index}", "entity")
        gl.ensure_node(conn, "MPO", "Matrix product operator", "entity")
        gl.ensure_node(conn, "z-title", "mpo", "page")
        gl.ensure_node(conn, "z-alias", "Another meaning", "entity")
        gl.insert_aliases(conn, "z-alias", ["MPO", "MPO alternative", "inside MPO"])
        gl.ensure_node(conn, "prefix", "MPO method", "entity")
        before = conn.total_changes
        result = module.search_nodes(conn, "MPO")
        assert conn.total_changes == before
        assert [node["path"] for node in result["nodes"][:4]] == [
            "MPO", "z-alias", "z-title", "prefix",
        ]
        assert len({node["path"] for node in result["nodes"]}) == 50
        assert result["count"] == 50 and result["truncated"] is True
        assert result["nodes"] == module.search_nodes(conn, "mpo")["nodes"]
        text = module.fmt_text(result, "search")
        assert "仍有更多匹配" in text and "当前展示前 20 个" in text
        conn.close()


def test_search_literal_patterns_granularity_and_exact_limit():
    with tempfile.TemporaryDirectory() as directory:
        conn = make_db(directory)
        gl.ensure_node(conn, "name_1", "Percent 20%", "entity", entity_subtype="keyword")
        gl.ensure_node(conn, "nameA1", "Percent 200", "entity", entity_subtype="proposition")
        gl.insert_aliases(conn, "name_1", [r"path\part", "Shared"])
        gl.insert_aliases(conn, "nameA1", ["Shared"])
        for term in ("_", "%", "\\"):
            result = module.search_nodes(conn, term)
            assert [node["path"] for node in result["nodes"]] == ["name_1"], term
        result = module.search_nodes(conn, "Shared", granularity="keyword", top_k=1)
        assert [node["path"] for node in result["nodes"]] == ["name_1"]
        assert result["truncated"] is False
        result = module.search_nodes(conn, "Shared", top_k=1)
        assert result["count"] == 1 and result["truncated"] is True
        result = module.search_nodes(conn, "Shared", top_k=2)
        assert result["count"] == 2 and result["truncated"] is False
        empty = module.search_nodes(conn, "not-in-graph")
        assert empty["nodes"] == [] and empty["truncated"] is False
        conn.close()


def test_search_cli_honors_top_k():
    with tempfile.TemporaryDirectory() as directory:
        conn = make_db(directory)
        for index in range(4):
            gl.ensure_node(conn, f"term-{index}", f"Term {index}", "entity")
        conn.commit()
        conn.close()
        result = subprocess.run(
            [sys.executable, str(SCRIPT), "search", "Term", "--top-k", "2", "--json",
             "--db", str(Path(directory) / "graph.db")],
            check=True, capture_output=True, text=True,
        )
        payload = json.loads(result.stdout)
        assert payload["count"] == payload["limit"] == 2
        assert payload["truncated"] is True


def make_star(conn, count):
    for index in range(count):
        target = f"leaf-{index:03}"
        gl.ensure_node(conn, target, target, "page")
        conn.execute("INSERT INTO edges(subject,predicate,object,confidence) VALUES (?,?,?,?)",
                     ("page-a", "核心方法", target, "可追溯"))


def assert_endpoints(result):
    endpoints = {endpoint for edge in result["edges"] for endpoint in (edge["subject"], edge["object"])} - {result["start"]}
    assert {node["path"] for node in result["nodes"]} == endpoints


def test_neighbors_default_node_budget_and_determinism():
    with tempfile.TemporaryDirectory() as directory:
        conn = make_db(directory)
        make_star(conn, 120)
        before = conn.total_changes
        result = module.neighbors_bfs(conn, "page-a")
        assert result == module.neighbors_bfs(conn, "page-a")
        assert conn.total_changes == before
        assert len(result["nodes"]) == len(result["edges"]) == 100
        assert result["traversal_truncated"] and result["truncated"]
        assert result["truncation_reasons"] == ["max_nodes"]
        assert "page-a" in result["remaining_frontier"]
        assert "部分结果" in module.fmt_text(result, "neighbors")
        assert_endpoints(result)
        conn.close()


def test_neighbors_top_k_only_caps_output_and_batches_details():
    with tempfile.TemporaryDirectory() as directory:
        conn = make_db(directory)
        make_star(conn, 100)
        selects = []
        conn.set_trace_callback(lambda statement: selects.append(statement) if statement.startswith("SELECT") else None)
        result = module.neighbors_bfs(conn, "page-a", top_k=3)
        assert result["work"]["discovered_nodes"] == 100
        assert len(result["nodes"]) == len(result["edges"]) == 3
        assert result["truncation_reasons"] == ["output_top_k"]
        assert not result["traversal_truncated"] and result["remaining_frontier"] == []
        assert len(selects) == 106, len(selects)
        assert_endpoints(result)
        conn.close()


def test_neighbors_each_budget_and_exact_boundary():
    with tempfile.TemporaryDirectory() as directory:
        conn = make_db(directory)
        make_star(conn, 5)
        for options, reason in (({"max_nodes": 4}, "max_nodes"), ({"max_edges": 4}, "max_edges"),
                                ({"max_scanned_rows": 4}, "max_scanned_rows")):
            result = module.neighbors_bfs(conn, "page-a", depth=1, **options)
            assert reason in result["truncation_reasons"]
            assert len(result["edges"]) == 4
            assert_endpoints(result)
        exact = module.neighbors_bfs(conn, "page-a", depth=1, max_nodes=5, max_edges=5, max_scanned_rows=5)
        assert not exact["truncated"]
        assert exact["work"]["fetched_rows"] == 5
        cut = module.neighbors_bfs(conn, "page-a", depth=1, max_scanned_rows=4)
        assert cut["work"]["scanned_rows"] == 4 and cut["work"]["fetched_rows"] == 5
        conn.close()


def test_neighbors_cycles_metadata_families_hubs_and_similarity():
    with tempfile.TemporaryDirectory() as directory:
        conn = make_db(directory)
        gl.ensure_node(conn, "hub", "Hub", "hub")
        gl.ensure_node(conn, "raw", "Raw", "raw")
        gl.ensure_node(conn, "page-c", "C", "page")
        gl.insert_aliases(conn, "page-b", ["alias"])
        gl.add_node_gloss(conn, "page-b", "page-a", "academic/raw/example.md#L1", "gloss")
        conn.executemany("INSERT INTO edges(subject,predicate,object,confidence,score) VALUES (?,?,?,?,?)", [
            ("page-a", "核心方法", "page-b", "可追溯", None),
            ("page-b", "核心方法", "page-a", "推断", None),
            ("page-a", "来源", "raw", "可追溯", None),
            ("page-a", "相似", "hub", "推断", .99),
            ("page-a", "相似", "page-c", "推断", .8),
        ])
        result = module.neighbors_bfs(conn, "page-a")
        assert {node["path"] for node in result["nodes"]} == {"page-b", "raw"}
        assert len(result["edges"]) == 3 and not result["truncated"]
        assert next(node for node in result["nodes"] if node["path"] == "page-b") == module.node_info(conn, "page-b")
        included = module.neighbors_bfs(conn, "page-a", depth=1, include_hub=True)
        assert "hub" in {node["path"] for node in included["nodes"]}
        unlimited_similar = module.neighbors_bfs(conn, "page-a", depth=1, similar_topk=-1)
        assert "page-c" in {node["path"] for node in unlimited_similar["nodes"]}
        evidence = module.neighbors_bfs(conn, "page-a", families="evidence")
        assert {edge["predicate"] for edge in evidence["edges"]} == {"来源"}
        assert_endpoints(result)
        conn.close()


def test_neighbors_invalid_limits_and_zero_depth():
    with tempfile.TemporaryDirectory() as directory:
        conn = make_db(directory)
        for options in ({"depth": 6}, {"depth": -1}, {"max_nodes": 0}, {"max_edges": 3001},
                        {"max_scanned_rows": 10001}, {"max_nodes": True}, {"top_k": 0}):
            assert "error" in module.neighbors_bfs(conn, "page-a", **options)
        result = module.neighbors_bfs(conn, "page-a", depth=0)
        assert result["nodes"] == result["edges"] == [] and not result["truncated"]
        assert module.neighbors_bfs(conn, "missing") == {"error": "节点不存在: missing"}
        conn.close()


def test_neighbors_same_tier_prefers_direct_hop_over_path_tie():
    with tempfile.TemporaryDirectory() as directory:
        conn = make_db(directory)
        gl.ensure_node(conn, "z-direct", "Direct", "page")
        gl.ensure_node(conn, "a-indirect", "Indirect", "page")
        conn.executemany("INSERT INTO edges(subject,predicate,object,confidence) VALUES (?,?,?,?)", [
            ("page-a", "核心方法", "z-direct", "可追溯"),
            ("a-indirect", "核心方法", "z-direct", "可追溯"),
        ])
        result = module.neighbors_bfs(conn, "page-a", depth=2, top_k=1)
        assert result["edges"][0]["subject"] == "page-a"
        assert_endpoints(result)
        conn.close()


def test_neighbors_cli_exposes_budgets():
    with tempfile.TemporaryDirectory() as directory:
        conn = make_db(directory)
        make_star(conn, 5)
        conn.commit()
        conn.close()
        process = subprocess.run([sys.executable, str(SCRIPT), "neighbors", "page-a", "--depth", "1",
                                  "--max-nodes", "2", "--json", "--db", str(Path(directory) / "graph.db")],
                                 check=True, capture_output=True, text=True)
        result = json.loads(process.stdout)
        assert result["budget"]["max_nodes"] == 2
        assert result["truncation_reasons"] == ["max_nodes"]


def test_deprecated_search_filter_is_before_limit_and_preserves_history():
    with tempfile.TemporaryDirectory() as directory:
        conn = make_db(directory)
        gl.ensure_node(conn, "academic/wiki/papers/a-old", "Shared", "page", status="deprecated")
        gl.ensure_node(conn, "academic/wiki/papers/z-current", "Shared overview", "page")
        result = module.search_nodes(conn, "Shared", top_k=1)
        assert [node["path"] for node in result["nodes"]] == ["academic/wiki/papers/z-current"]
        assert result["count"] == 1 and result["truncated"] is False
        history = module.search_nodes(conn, "Shared", top_k=1, include_deprecated=True)
        assert history["nodes"][0]["path"] == "academic/wiki/papers/a-old"
        assert history["nodes"][0]["material_role"] == "historical_navigation"
        assert history["nodes"][0]["default_page_candidate"] is False
        assert history["truncated"] is True
        assert module.search_nodes(conn, "Shared", include_deprecated="false")["count"] == 1
        conn.close()


def test_search_only_filters_deprecated_pages_not_entities_or_raw():
    with tempfile.TemporaryDirectory() as directory:
        conn = make_db(directory)
        gl.ensure_node(conn, "old-concept", "Shared", "entity", status="deprecated")
        gl.ensure_node(conn, "old-raw", "Shared", "raw", status="deprecated")
        gl.ensure_node(conn, "academic/wiki/people/old", "Shared", "people", status="deprecated")
        assert {node["path"] for node in module.search_nodes(conn, "Shared")["nodes"]} == {"old-concept", "old-raw"}
        for value in (None, 1, "yes", [], {}):
            try:
                module.search_nodes(conn, "Shared", include_deprecated=value)
            except ValueError:
                pass
            else:
                raise AssertionError(f"invalid history flag accepted: {value!r}")
        conn.close()


def test_deprecated_bridge_is_navigation_only_and_current_neighbor_survives():
    with tempfile.TemporaryDirectory() as directory:
        conn = make_db(directory)
        old = "academic/wiki/papers/old"
        current = "academic/wiki/papers/current"
        gl.ensure_node(conn, old, "Old", "page", status="deprecated")
        gl.ensure_node(conn, current, "Current", "page")
        conn.execute("INSERT INTO edges(subject,predicate,object) VALUES (?,?,?)", ("page-a", "关联", old))
        conn.execute("INSERT INTO edges(subject,predicate,object) VALUES (?,?,?)", (old, "关联", current))
        result = module.neighbors_bfs(conn, "page-a", depth=2)
        nodes = {item["path"]: item for item in result["nodes"]}
        assert nodes[old]["material_role"] == "historical_navigation"
        assert nodes[old]["default_page_candidate"] is False
        assert nodes[current]["default_page_candidate"] is True
        assert current in result["version_context"]["eligible_page_paths"]
        assert old not in result["version_context"]["eligible_page_paths"]
        assert result["version_context"]["historical_page_paths"] == [old]
        assert len(result["edges"]) == 2
        assert module.path_exists(conn, "page-a", current)["connected"] is True
        explicit = module.neighbors_bfs(conn, old, depth=0)
        assert explicit["version_context"]["historical_page_paths"] == [old]
        assert module.node_info(conn, old)["default_page_candidate"] is False
        conn.close()


def test_relations_and_hub_navigation_expose_old_input_without_deleting_edges():
    with tempfile.TemporaryDirectory() as directory:
        conn = make_db(directory)
        old = "academic/wiki/papers/old"
        gl.ensure_node(conn, old, "Old", "page", status="deprecated")
        gl.ensure_node(conn, "hub", "Hub", "hub")
        conn.execute("INSERT INTO edges(subject,predicate,object) VALUES (?,?,?)", (old, "主要研究", "hub"))
        result = module.relations(conn, "hub")
        assert len(result["edges"]) == 1
        assert result["version_context"]["historical_page_paths"] == [old]
        assert result["version_context"]["eligible_page_paths"] == []
        hub = module.hub_of(conn, old)
        assert hub["hubs"][0]["path"] == "hub"
        assert hub["version_context"]["historical_page_paths"] == [old]
        conn.close()


def test_search_cli_explicit_history_flag():
    with tempfile.TemporaryDirectory() as directory:
        conn = make_db(directory)
        gl.ensure_node(conn, "academic/wiki/papers/old", "UniqueOldLabel", "page", status="deprecated")
        conn.commit()
        conn.close()
        command = [sys.executable, str(SCRIPT), "search", "UniqueOldLabel", "--db", str(Path(directory) / "graph.db"), "--json"]
        default = json.loads(subprocess.run(command, capture_output=True, text=True, check=True).stdout)
        history = json.loads(subprocess.run([*command, "--include-deprecated"], capture_output=True, text=True, check=True).stdout)
        assert default["count"] == 0
        assert history["count"] == 1 and history["include_deprecated"] is True


def main():
    tests = [value for name, value in sorted(globals().items()) if name.startswith("test_") and callable(value)]
    for test in tests:
        test()
    print(f"query_graph regression: {len(tests)}/{len(tests)} PASS")


if __name__ == "__main__":
    main()
