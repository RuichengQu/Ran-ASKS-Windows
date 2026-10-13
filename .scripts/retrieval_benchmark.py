#!/usr/bin/env python3
"""Offline public navigation probes; engineering measurements, not answer evaluation."""
import argparse
import hashlib
import importlib
import json
import math
import socket
import statistics
import sys
import time
import uuid
from contextlib import ExitStack
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import graph_lib as gl
import query_graph as qg

REPO = Path(__file__).resolve().parent.parent
MANIFEST = REPO / "operations/config/retrieval-benchmark.json"
DOMAINS = {"academic", "admin", "teaching", "business"}
SOURCE_FILES = (
    ".scripts/retrieval_benchmark.py", ".scripts/query_graph.py",
    ".scripts/query_actions.py", ".scripts/graph_lib.py",
    ".scripts/hub_semantics.py", ".scripts/wiki_locator.py",
    "operations/config/graph-schema.yaml",
)


def digest(content):
    return hashlib.sha256(content).hexdigest()


def public_locator(value):
    parts = Path(value).parts
    return (len(parts) >= 3 and parts[0] in DOMAINS and parts[1] in {"wiki", "raw"}
            and ".." not in parts and not Path(value).is_absolute())


def manifest_for_suite(suite):
    if suite == "curated":
        return MANIFEST
    if suite == "real":
        return REPO / "operations/config/retrieval-benchmark-real.json"
    raise ValueError("unsupported suite")


def validate_manifest(manifest, suite="curated"):
    manifest_for_suite(suite)
    if manifest.get("schema") != "retrieval-probes-v1":
        raise ValueError("unsupported manifest schema")
    cases = manifest.get("cases", [])
    if not isinstance(cases, list) or not 1 <= len(cases) <= 40:
        raise ValueError("expected 1–40 fixed probes")
    identifiers = set()
    for case in cases:
        if set(case) - {"id", "channel", "query", "domain", "expected", "depth", "top_k", "source"}:
            raise ValueError("unknown case field")
        if not isinstance(case.get("id"), str) or case["id"] in identifiers:
            raise ValueError("missing or duplicate case id")
        identifiers.add(case["id"])
        if case.get("channel") not in {"wiki", "search", "neighbors"}:
            raise ValueError("unsupported channel")
        if not isinstance(case.get("query"), str) or not 1 <= len(case["query"]) <= 300:
            raise ValueError("invalid query")
        if case.get("domain", "academic") not in DOMAINS:
            raise ValueError("public domain required")
        expected = case.get("expected")
        if not isinstance(expected, list) or not 1 <= len(expected) <= 20:
            raise ValueError("expected navigation targets required")
        if not all(isinstance(value, str) and public_locator(value) for value in expected):
            raise ValueError("expected target must be a public navigation locator")
        if case["channel"] == "neighbors" and not public_locator(case["query"]):
            raise ValueError("neighbors requires a public navigation locator")
        for key, lower, upper in (("depth", 0, 5), ("top_k", 1, 20)):
            value = case.get(key, 2 if key == "depth" else 8)
            if type(value) is not int or not lower <= value <= upper:
                raise ValueError(f"invalid {key}")
        source = case.get("source")
        if suite == "real" and source is None:
            raise ValueError("historical probe source required")
        if source is not None:
            if not isinstance(source, dict) or set(source) != {"log", "line", "ts", "record_sha256"}:
                raise ValueError("invalid historical source fields")
            if (not isinstance(source["log"], str)
                    or source["log"] not in {f"{domain}/wiki/outputs/query-log.jsonl" for domain in DOMAINS}):
                raise ValueError("public query log locator required")
            if type(source["line"]) is not int or source["line"] < 1:
                raise ValueError("invalid source line")
            if not isinstance(source["ts"], str) or not 1 <= len(source["ts"]) <= 80:
                raise ValueError("invalid source timestamp")
            fingerprint = source["record_sha256"]
            if (not isinstance(fingerprint, str) or len(fingerprint) != 64
                    or any(character not in "0123456789abcdef" for character in fingerprint)):
                raise ValueError("invalid source hash")
    return cases


def input_fingerprints(manifest_path=None):
    manifest_path = MANIFEST if manifest_path is None else manifest_path
    files = {str(manifest_path.relative_to(REPO)): digest(manifest_path.read_bytes())}
    for relative in SOURCE_FILES:
        files[relative] = digest((REPO / relative).read_bytes())
    for suffix in ("", "-wal"):
        target = REPO / f"cross-domain/graph.db{suffix}"
        files[str(target.relative_to(REPO))] = digest(target.read_bytes()) if target.exists() else None
    wiki = {}
    for domain in sorted(DOMAINS):
        for target in sorted((REPO / domain / "wiki").rglob("*.md")):
            if target.name in {"index.md", "log.md"}:
                continue
            if not target.resolve().is_relative_to(REPO / domain / "wiki"):
                raise ValueError("Wiki symlink escapes domain")
            wiki[str(target.relative_to(REPO))] = digest(target.read_bytes())
    files["wiki_corpus"] = digest(json.dumps(wiki, sort_keys=True).encode())
    return {"files": files, "wiki_files": len(wiki), "wiki": wiki,
            "runtime_query_graph": {"file": str(Path(qg.__file__).relative_to(REPO)),
                                    "sha256": digest(Path(qg.__file__).read_bytes())}}


def estimated_tokens(text):
    return math.ceil(len(text.encode("utf-8")) / 4)


def load_actions():
    missing = object()
    previous = sys.modules.get("tiktoken", missing)
    sys.modules["tiktoken"] = None
    try:
        return importlib.import_module("query_actions")
    finally:
        if previous is missing:
            sys.modules.pop("tiktoken", None)
        else:
            sys.modules["tiktoken"] = previous


def run_probe(case):
    counters = {"sql_selects": 0, "file_reads": 0, "wiki_reads": 0}
    wiki_files = set()
    original_read = Path.read_text
    original_connect = gl.connect
    connections = []

    def read_text(target, *args, **kwargs):
        counters["file_reads"] += 1
        resolved = target.resolve()
        relative = resolved.relative_to(REPO.resolve())
        if relative.parts[0] == "private":
            raise ValueError("private reads forbidden")
        if len(relative.parts) > 1 and relative.parts[1] == "wiki":
            counters["wiki_reads"] += 1
            wiki_files.add(str(relative))
        return original_read(target, *args, **kwargs)

    def trace(statement):
        if statement.lstrip().upper().startswith(("SELECT", "WITH")):
            counters["sql_selects"] += 1

    def connect(db_path=None, *, read_only=False):
        if not read_only or Path(db_path or gl.GRAPH_DB).resolve() != (REPO / "cross-domain/graph.db").resolve():
            raise ValueError("only read-only public graph connections allowed")
        connection = original_connect(db_path, read_only=True)
        connection.set_trace_callback(trace)
        connections.append(connection)
        return connection

    with ExitStack() as stack:
        stack.enter_context(patch.object(socket.socket, "connect", side_effect=RuntimeError("offline probe")))
        stack.enter_context(patch.object(socket.socket, "connect_ex", side_effect=RuntimeError("offline probe")))
        stack.enter_context(patch.object(socket, "create_connection", side_effect=RuntimeError("offline probe")))
        actions = load_actions()
        stack.enter_context(patch.object(actions, "_tok", estimated_tokens))
        stack.enter_context(patch.object(Path, "read_text", read_text))
        stack.enter_context(patch.object(gl, "connect", connect))
        started = time.perf_counter()
        try:
            with actions.query_scope("public"):
                if case["channel"] == "wiki":
                    text, _ = actions.wiki_recall(case["query"], case.get("domain", "academic"), str(case.get("top_k", 8)))
                    result = json.loads(text)
                    candidates = [item["path"] for item in result["candidates"]]
                else:
                    connection = connect(REPO / "cross-domain/graph.db", read_only=True)
                    if case["channel"] == "search":
                        result = qg.search_nodes(connection, case["query"], top_k=case.get("top_k", 8))
                    else:
                        result = qg.neighbors_bfs(connection, case["query"], depth=case.get("depth", 2), top_k=case.get("top_k", 8))
                    if "error" in result:
                        raise ValueError(result["error"])
                    if case["channel"] == "neighbors":
                        candidates = sorted({endpoint for edge in result["edges"]
                                             for endpoint in (edge["subject"], edge["object"])} - {case["query"]})
                    else:
                        candidates = [item["path"] for item in result["nodes"]]
                    text = json.dumps(result, ensure_ascii=False, indent=2)
            elapsed_ms = (time.perf_counter() - started) * 1000
        finally:
            for connection in connections:
                connection.close()
    expected = set(case["expected"])
    covered = sorted(expected.intersection(candidates))
    return {
        "id": case["id"], "channel": case["channel"], "elapsed_ms": round(elapsed_ms, 3),
        **counters, "unique_wiki_files": len(wiki_files),
        "output_bytes": len(text.encode("utf-8")), "estimated_tokens": estimated_tokens(text),
        "candidate_count": len(candidates), "candidates": candidates,
        "node_list_count": len(result.get("nodes", [])),
        "expected": sorted(expected), "covered": covered,
        "target_hit": bool(covered), "target_recall": len(covered) / len(expected),
        "truncated": result.get("truncated"), "truncation_reported": "truncated" in result,
        "truncation_reasons": result.get("truncation_reasons", []),
        "work": result.get("work"), "response_sha256": digest(text.encode("utf-8")),
    }


def summarize(samples):
    channels = {}
    for channel in sorted({sample["channel"] for sample in samples}):
        group = [sample for sample in samples if sample["channel"] == channel]
        elapsed = sorted(sample["elapsed_ms"] for sample in group)
        channels[channel] = {
            "samples": len(group), "target_hit_rate": statistics.mean(sample["target_hit"] for sample in group),
            "mean_target_recall": statistics.mean(sample["target_recall"] for sample in group),
            "median_ms": statistics.median(elapsed), "p95_ms": elapsed[math.ceil(len(elapsed) * .95) - 1],
            "mean_sql_selects": statistics.mean(sample["sql_selects"] for sample in group),
            "mean_wiki_reads": statistics.mean(sample["wiki_reads"] for sample in group),
            "mean_output_bytes": statistics.mean(sample["output_bytes"] for sample in group),
            "truncated_samples": sum(sample["truncated"] is True for sample in group),
            "unknown_truncation_samples": sum(sample.get("truncated") is None for sample in group),
        }
    return channels


def run(repeats=2, suite="curated"):
    if type(repeats) is not int or not 1 <= repeats <= 5:
        raise ValueError("repeats must be 1–5")
    manifest_path = manifest_for_suite(suite)
    if not manifest_path.resolve().is_relative_to(REPO / "operations/config"):
        raise ValueError("manifest symlink escapes config directory")
    output_root = REPO / "temp/retrieval-benchmark"
    if output_root.resolve() != output_root:
        raise ValueError("benchmark output symlinks forbidden")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    cases = validate_manifest(manifest, suite=suite)
    before = input_fingerprints(manifest_path)
    samples = []
    for repetition in range(repeats):
        for case in cases:
            sample = run_probe(case)
            sample["pass"] = repetition + 1
            sample["phase"] = "first_pass" if repetition == 0 else "same_process_repeat"
            samples.append(sample)
    after = input_fingerprints(manifest_path)
    if before != after:
        raise RuntimeError("inputs changed during benchmark; refusing comparable report")
    report = {
        "schema": "retrieval-baseline-v1", "created_at": datetime.now(timezone.utc).isoformat(),
        "input_fingerprints": before, "inputs_unchanged": True, "repeats": repeats, "suite": suite,
        "limitations": [
            ("curated navigation probes, not historical traffic or scientific answer gold" if suite == "curated"
             else "reviewed historical questions, selective navigation targets; not representative traffic or answer gold"),
            "Wiki lexical recall / graph search / neighbors only; no hybrid embedding or Agent end-to-end evaluation",
            "first pass is not a controlled cold-cache measurement; imports excluded, instrumented timing",
            "tokens estimated as UTF-8 bytes/4; logical reads and SQL statements are not physical I/O",
            "target recall is fixed navigation target arrival, not Raw evidence verification",
        ],
        "summary": {phase: summarize([sample for sample in samples if sample["phase"] == phase])
                    for phase in ("first_pass", "same_process_repeat")},
        "samples": samples,
        "stable_responses": all(len({sample["response_sha256"] for sample in samples if sample["id"] == case["id"]}) == 1 for case in cases),
    }
    directory = output_root / (datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ-") + uuid.uuid4().hex[:8])
    directory.mkdir(parents=True, exist_ok=False)
    (directory / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (directory / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return directory, report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repeats", type=int, default=2)
    parser.add_argument("--suite", choices=("curated", "real"), default="curated")
    args = parser.parse_args()
    try:
        directory, report = run(args.repeats, suite=args.suite)
    except (ValueError, RuntimeError, OSError) as error:
        parser.exit(1, f"benchmark failed: {error}\n")
    print(json.dumps({"report": str(directory / "report.json"), "summary": report["summary"], "stable_responses": report["stable_responses"]}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
