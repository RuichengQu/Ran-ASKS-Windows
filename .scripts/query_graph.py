#!/usr/bin/env python3
"""query_graph.py — 图查询脚本接口(LLM 通过 exec 调用,非 MCP)

边语义:关联链非因果链。BFS=关联召回(查询时发现某实体相关的页/关系),
非 debug 爆炸半径。查询结果按 confidence 确定性排序(top-k 仅预算压力下)。

核心契约:图边是导航关系非答案。edge source/locator 可指向 Wiki section 或 Raw，
也可为空；通常先读取相邻 Wiki section 及其 raw_citations，再沿精确 Raw locator 核验。
图只回答"有关联什么"，事实答案最终必回 Raw。

子命令:
  node <path>                      节点详情(属性 + 来源)
  neighbors <node> [--depth N]      关联召回 BFS(默认 2 跳,按 confidence 排序)
  relations <node> [--predicate P]  某节点的关系边(可按谓词过滤)
  hub_of <page>                     查某页属于哪个 Hub(沿语义边反向)
  search <keyword> [--granularity G]  按字面关键词查节点(title/path/aliases)，完整匹配优先
                                    granularity: keyword=导航聚合, proposition=精确推理
  path_exists <from> <to>           两节点是否连通(辅助验证)
  temporal --at DATE [--subject S] [--object O] [--predicate P]
                                    查某时点的时态事实(不与普通 edges 混用)

选项:
  --json          输出 JSON(默认人类可读文本)
  --top-k N       限制返回边数；search 限制节点数(1–50，默认50)
  --similar-topk N 每节点相似边上限(动态K;0=排除第一轮,-1=全部,默认5)
  --include-hub   neighbors 含 Hub 节点(默认排除,省 token;显式查才返回)
  --granularity   search 按颗粒度过滤(keyword/proposition;对应查询意图)
  --db PATH       图数据库路径(默认 cross-domain/graph.db)

用法示例:
  query_graph.py neighbors "张明远" --depth 2
  query_graph.py hub_of "academic/wiki/papers/sarthi-2024-raptor"
  query_graph.py search "MPS"
"""
import argparse
import json
import sqlite3
import sys
from datetime import date
from pathlib import Path

import graph_lib as gl

GRAPH_DB = Path(__file__).resolve().parent.parent / "cross-domain" / "graph.db"

# confidence 排序权重(确定性,不存边属性):可追溯 > 推断 > 存疑
CONF_ORDER = {"可追溯": 0, "推断": 1, "存疑": 2, None: 3}
# status 排序:current 优先于 deprecated
STATUS_ORDER = {"current": 0, "active": 0, "deprecated": 1, "dormant": 2, "archived": 3}
NEIGHBOR_DEFAULTS = {"max_nodes": 100, "max_edges": 300, "max_scanned_rows": 1000}
NEIGHBOR_CEILINGS = {"max_nodes": 1000, "max_edges": 3000, "max_scanned_rows": 10000}
MAX_NEIGHBOR_DEPTH = 5


def connect(db_path):
    if not Path(db_path).exists():
        print(f"错误: 图数据库不存在 {db_path}。请通过受管摄入、同步或备份恢复该数据库", file=sys.stderr)
        sys.exit(1)
    return gl.connect(str(db_path), read_only=True)


def history_enabled(value):
    if isinstance(value, bool):
        return value
    if isinstance(value, str) and value.strip().lower() in {"true", "false"}:
        return value.strip().lower() == "true"
    raise ValueError("include_deprecated 必须是布尔值或 true/false 字符串")


def node_version(info):
    page = info.get("type") == "page" or "/wiki/" in info.get("path", "")
    historical = page and info.get("status") == "deprecated"
    return {"material_role": "historical_navigation" if historical else "page_navigation" if page else "graph_navigation",
            "default_page_candidate": page and not historical}


def version_context(nodes):
    entries = {item["path"]: item for item in nodes}
    eligible, historical = [], []
    for path, info in sorted(entries.items()):
        role = node_version(info)
        if role["default_page_candidate"]:
            eligible.append(path)
        elif role["material_role"] == "historical_navigation":
            historical.append(path)
    return {"result_role": "graph_navigation", "status_source": "graph",
            "eligible_page_paths": eligible, "historical_page_paths": historical,
            "next": "read Wiki lifecycle/superseded_by and exact Raw evidence; eligibility is not verified current validity"}


def query_version_context(conn, paths):
    entries = []
    ordered = sorted(set(paths))
    for offset in range(0, len(ordered), 250):
        batch = ordered[offset:offset + 250]
        placeholders = ",".join("?" for _ in batch)
        entries.extend(dict(row) for row in conn.execute(
            f"SELECT path,type,status FROM nodes WHERE path IN ({placeholders})", batch))
    return version_context(entries)


def node_info(conn, path):
    """节点详情，含可下钻 Raw 的逐来源概念说明。"""
    row = conn.execute("SELECT * FROM nodes WHERE path=?", (path,)).fetchone()
    if not row:
        return None
    aliases = [r["alias"] for r in conn.execute(
        "SELECT alias FROM aliases WHERE node_path=?", (path,))]
    glosses = [dict(r) for r in conn.execute(
        "SELECT origin_page,source,description,is_primary FROM node_glosses "
        "WHERE node_path=? ORDER BY is_primary DESC,recorded_at,origin_page,source",
        (path,),
    )]
    description_reviews = [dict(r) for r in conn.execute(
        "SELECT origin_page,source,status,reason,proposed_description,recorded_at "
        "FROM node_description_reviews WHERE node_path=? ORDER BY id",
        (path,),
    )]
    return {
        "path": row["path"],
        "title": row["title"],
        "type": row["type"],
        "entity_subtype": row["entity_subtype"],
        "source_type": row["source_type"],
        "date": row["date"],
        "status": row["status"],
        **node_version(dict(row)),
        "aliases": aliases,
        "description": row["description"] or "",
        "glosses": glosses,
        "description_reviews": description_reviews,
        "has_raw_source": bool(row["has_raw_source"]),
    }


# ADR-003: 相似边动态截断——依据当前节点的相似边 score 分布计算阈值
# 保留与 top score 差距 ≤ SIMILAR_SCORE_MARGIN 的边(分数接近=同档相似),
# 再按 similar_topk 上限裁剪。K 由数据涌现,非定死。
SIMILAR_SCORE_MARGIN = 0.03


def edge_record(row, family=""):
    """Expose the legacy ``source`` column as an optional edge locator."""
    edge = dict(row)
    edge["locator"] = str(edge.get("source") or "")
    if family:
        edge["family"] = family
    return edge


def _node_type(conn, path, cache):
    if path not in cache:
        row = conn.execute("SELECT type FROM nodes WHERE path=?", (path,)).fetchone()
        cache[path] = row[0] if row else ""
    return cache[path]


def _edge_record_with_family(conn, row, contract, type_cache):
    family = gl.predicate_family(
        row["predicate"],
        _node_type(conn, row["subject"], type_cache),
        _node_type(conn, row["object"], type_cache),
        contract,
    )
    return edge_record(row, family), family


def _filter_similar_edges(similar_rows, max_cap=5):
    """动态过滤相似边。max_cap 语义(多轮渐进式):
      max_cap=0   → 完全排除相似边(第一轮:纯知识边优先)
      max_cap=-1  → 不过滤(保留全部)
      max_cap=N>0 → 动态K:保留 score ≥ (top - margin) 的边,上限 N,下限 1

    K 由当前节点的相似边 score 分布决定(涌现):
      - 有明显高分赢家 → 只保留接近 top 的少数(如 iPEPS 7条保留1)
      - score 聚集平坦 → 保留更多(上限 max_cap)
    """
    if not similar_rows:
        return []
    if max_cap == 0:      # 第一轮:完全排除
        return []
    if max_cap < 0:       # -1:不过滤
        return similar_rows
    similar_rows.sort(key=lambda e: e.get("score") or 0.0, reverse=True)
    top_score = similar_rows[0].get("score") or 0.0
    cutoff = top_score - SIMILAR_SCORE_MARGIN
    kept = [e for e in similar_rows if (e.get("score") or 0.0) >= cutoff]
    kept = kept[:max_cap]
    return kept if kept else similar_rows[:1]


def _node_infos(conn, paths):
    """Fetch full legacy node details in bounded batches, without per-node queries."""
    result = []
    ordered = sorted(paths)
    for offset in range(0, len(ordered), 250):
        batch = ordered[offset:offset + 250]
        placeholders = ",".join("?" for _ in batch)
        nodes = {}
        for row in conn.execute(f"SELECT * FROM nodes WHERE path IN ({placeholders}) ORDER BY path", batch):
            nodes[row["path"]] = {
                key: row[key] for key in ("path", "title", "type", "entity_subtype", "source_type", "date", "status")
            }
            nodes[row["path"]].update({
                "description": row["description"] or "", "has_raw_source": bool(row["has_raw_source"]),
                "aliases": [], "glosses": [], "description_reviews": [],
                **node_version(dict(row)),
            })
        for row in conn.execute(f"SELECT node_path,alias FROM aliases WHERE node_path IN ({placeholders}) ORDER BY rowid", batch):
            nodes[row["node_path"]]["aliases"].append(row["alias"])
        for row in conn.execute(
            f"SELECT node_path,origin_page,source,description,is_primary FROM node_glosses WHERE node_path IN ({placeholders}) "
            "ORDER BY node_path,is_primary DESC,recorded_at,origin_page,source", batch,
        ):
            values = dict(row)
            nodes[values.pop("node_path")]["glosses"].append(values)
        for row in conn.execute(
            f"SELECT node_path,origin_page,source,status,reason,proposed_description,recorded_at "
            f"FROM node_description_reviews WHERE node_path IN ({placeholders}) ORDER BY node_path,id", batch,
        ):
            values = dict(row)
            nodes[values.pop("node_path")]["description_reviews"].append(values)
        result.extend(nodes.values())
    return result


def neighbors_bfs(conn, start, depth=2, top_k=None, include_hub=False, similar_topk=5,
                  profile="", families=None, max_nodes=100, max_edges=300,
                  max_scanned_rows=1000):
    """Read-only, deterministic association BFS with explicit partial-result disclosure."""
    budget = {"max_nodes": max_nodes, "max_edges": max_edges, "max_scanned_rows": max_scanned_rows}
    for key, value in budget.items():
        if type(value) is not int or not 1 <= value <= NEIGHBOR_CEILINGS[key]:
            return {"error": f"{key} 必须为 1–{NEIGHBOR_CEILINGS[key]} 的整数"}
    if type(depth) is not int or not 0 <= depth <= MAX_NEIGHBOR_DEPTH:
        return {"error": f"depth 必须为 0–{MAX_NEIGHBOR_DEPTH} 的整数"}
    if top_k is not None and (type(top_k) is not int or top_k < 1):
        return {"error": "top_k 必须为正整数"}
    start_info = conn.execute("SELECT path,type,status FROM nodes WHERE path=?", (start,)).fetchone()
    if start_info is None:
        return {"error": f"节点不存在: {start}"}
    visited = {start}
    frontier = {start}
    edges = {}
    edge_depths = {}
    statuses = {}
    reasons = []
    remaining_frontier = set()
    work = {"queried_nodes": 0, "scanned_rows": 0, "fetched_rows": 0}
    contract = gl.load_graph_contract()
    allowed_families = gl.traversal_families(profile, families, contract)
    for hop in range(depth):
        next_frontier = set()
        ordered_frontier = sorted(frontier)
        for position, node in enumerate(ordered_frontier):
            remaining = max_scanned_rows - work["scanned_rows"]
            rows = conn.execute(
                "SELECT e.*,s.type AS subject_type,o.type AS object_type,o.status AS object_status "
                "FROM edges e JOIN nodes s ON s.path=e.subject JOIN nodes o ON o.path=e.object "
                "WHERE e.subject=? OR e.object=? "
                "ORDER BY CASE WHEN e.predicate='相似' THEN 1 ELSE 0 END,"
                "CASE e.confidence WHEN '可追溯' THEN 0 WHEN '推断' THEN 1 WHEN '存疑' THEN 2 ELSE 3 END,"
                "CASE o.status WHEN 'current' THEN 0 WHEN 'active' THEN 0 WHEN 'deprecated' THEN 1 "
                "WHEN 'dormant' THEN 2 WHEN 'archived' THEN 3 ELSE 9 END,"
                "e.score DESC,e.subject,e.predicate,e.object,e.id LIMIT ?",
                (node, node, remaining + 1),
            ).fetchall()
            work["queried_nodes"] += 1
            work["fetched_rows"] += len(rows)
            scan_cut = len(rows) > remaining
            work["scanned_rows"] += min(len(rows), remaining)
            knowledge_rows = []
            similar_rows = []
            excluded_hubs = set()
            for row in rows[:remaining]:
                family = gl.predicate_family(row["predicate"], row["subject_type"], row["object_type"], contract)
                if allowed_families and family not in allowed_families:
                    continue
                other_type = row["object_type"] if row["subject"] == node else row["subject_type"]
                if not include_hub and other_type == "hub":
                    excluded_hubs.add(row["id"])
                statuses[row["object"]] = row["object_status"]
                record = edge_record({key: value for key, value in dict(row).items()
                                      if key not in {"subject_type", "object_type", "object_status"}}, family)
                if row["predicate"] == "相似":
                    similar_rows.append(record)
                else:
                    knowledge_rows.append(record)
            similar_rows = _filter_similar_edges(similar_rows, similar_topk)
            for record in knowledge_rows + similar_rows:
                if record["id"] in excluded_hubs:
                    continue
                key = (record["subject"], record["predicate"], record["object"])
                if key in edges:
                    continue
                other = record["object"] if record["subject"] == node else record["subject"]
                if len(edges) >= max_edges:
                    reasons.append("max_edges")
                    break
                if other not in visited and len(visited) - 1 >= max_nodes:
                    reasons.append("max_nodes")
                    break
                edges[key] = record
                edge_depths[key] = hop + 1
                if other not in visited:
                    visited.add(other)
                    next_frontier.add(other)
            if scan_cut:
                reasons.append("max_scanned_rows")
            if reasons:
                remaining_frontier = {node, *ordered_frontier[position + 1:]} | next_frontier
                break
        if reasons:
            break
        frontier = next_frontier
    traversal_truncated = bool(reasons)
    unique_edges = sorted(edges.values(), key=lambda edge: (
        1 if edge["predicate"] == "相似" else 0,
        CONF_ORDER.get(edge["confidence"], 3),
        STATUS_ORDER.get(statuses.get(edge["object"]), 9),
        edge_depths[(edge["subject"], edge["predicate"], edge["object"])],
        edge["subject"], edge["predicate"], edge["object"],
    ))
    if top_k is not None and len(unique_edges) > top_k:
        unique_edges = unique_edges[:top_k]
        reasons.append("output_top_k")
    node_paths = {endpoint for edge in unique_edges for endpoint in (edge["subject"], edge["object"])} - {start}
    nodes = _node_infos(conn, node_paths)
    work.update({"discovered_nodes": len(visited) - 1, "retained_edges": len(edges)})
    return {"start": start, "depth": depth, "profile": profile or None,
            "families": sorted(allowed_families), "nodes": nodes, "edges": unique_edges,
            "version_context": version_context([dict(start_info), *nodes]),
            "truncated": bool(reasons), "traversal_truncated": traversal_truncated,
            "truncation_reasons": sorted(set(reasons)), "budget": budget, "work": work,
            "remaining_frontier": sorted(remaining_frontier)}


def relations(conn, node, predicate=None, top_k=None, profile="", families=None):
    """某节点的所有关系边(可按谓词过滤)。"""
    if predicate:
        rows = conn.execute(
            "SELECT * FROM edges WHERE (subject=? OR object=?) AND predicate=? "
            "ORDER BY confidence",
            (node, node, predicate)
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM edges WHERE (subject=? OR object=?) "
            "ORDER BY confidence",
            (node, node)
        ).fetchall()
    contract = gl.load_graph_contract()
    allowed_families = gl.traversal_families(profile, families, contract)
    type_cache = {}
    edges = []
    for row in rows:
        record, family = _edge_record_with_family(conn, row, contract, type_cache)
        if allowed_families and family not in allowed_families:
            continue
        edges.append(record)
    edges.sort(key=lambda e: CONF_ORDER.get(e["confidence"], 3))
    if top_k:
        edges = edges[:top_k]
    return {"node": node, "predicate_filter": predicate, "profile": profile or None,
            "families": sorted(allowed_families), "edges": edges,
            "version_context": query_version_context(conn, {node, *(endpoint for edge in edges for endpoint in (edge["subject"], edge["object"]))})}


def temporal_at(conn, at_date, subject=None, obj=None, predicate=None, top_k=None):
    """查某时点 effective 的 temporal_facts。

    temporal_facts 与普通 edges 分表保存；默认不会被 neighbors/search 混入。
    effective 判定:
      - (valid_from IS NULL OR valid_from <= at)
      - (valid_until IS NULL OR valid_until >= at)
      - 未被后续事实取代：superseded_by IS NULL 或后续 valid_from > at
    """
    try:
        parsed = date.fromisoformat(at_date)
        at_value = parsed.isoformat()
    except ValueError:
        return {"error": f"无效日期: {at_date}，应使用 YYYY-MM-DD"}
    query = (
        "SELECT tf.* FROM temporal_facts tf "
        "LEFT JOIN temporal_facts succ ON succ.id = tf.superseded_by "
        "WHERE (tf.valid_from IS NULL OR tf.valid_from <= ?) "
        "AND (tf.valid_until IS NULL OR tf.valid_until >= ?) "
        "AND (tf.superseded_by IS NULL OR succ.valid_from IS NULL OR succ.valid_from > ?) "
    )
    params = [at_value, at_value, at_value]
    if subject:
        query += "AND tf.subject = ? "
        params.append(subject)
    if obj:
        query += "AND tf.object = ? "
        params.append(obj)
    if predicate:
        query += "AND tf.predicate = ? "
        params.append(predicate)
    query += "ORDER BY tf.valid_from IS NOT NULL, tf.valid_from, tf.id"
    if top_k:
        query += " LIMIT ?"
        params.append(top_k)
    rows = conn.execute(query, params).fetchall()
    return {
        "at": at_value,
        "subject": subject,
        "object": obj,
        "predicate": predicate,
        "count": len(rows),
        "facts": [dict(r) for r in rows],
    }


def hub_of(conn, page):
    """查某页所属 Hub(沿语义导航边反向查 type=hub 节点)。"""
    rows = conn.execute(
        "SELECT CASE WHEN e.subject=? THEN e.object ELSE e.subject END AS hub_path, "
        "e.predicate FROM edges e "
        "JOIN nodes n ON n.path=CASE WHEN e.subject=? THEN e.object ELSE e.subject END "
        "WHERE (e.subject=? OR e.object=?) AND n.type='hub'",
        (page, page, page, page)
    ).fetchall()
    hubs = []
    for r in rows:
        ni = node_info(conn, r["hub_path"])
        if ni:
            ni["predicate"] = r["predicate"]
            hubs.append(ni)
    return {"page": page, "hubs": hubs, "version_context": query_version_context(conn, [page, *(item["path"] for item in hubs)])}


def search_nodes(conn, keyword, granularity=None, top_k=None, include_deprecated=False):
    """按字面关键词查节点；完整匹配、前缀、子串在截断前排序。

    granularity: 按颗粒度过滤(对应查询意图)。
      - keyword  : 只返回概念节点(导航聚合类查询:哪些文献涉及某主题)
      - proposition : 只返回论断节点(精确推理类查询:谁验证/反对某 claim)
      - None : 不过滤(默认,返回所有颗粒度)
    """
    limit = 50 if top_k is None else max(1, min(int(top_k), 50))
    include_deprecated = history_enabled(include_deprecated)
    literal = keyword.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    kw = f"%{literal}%"
    prefix = f"{literal}%"
    query = (
        "SELECT n.path, n.title, n.type, n.status, n.entity_subtype, "
        "MIN(CASE "
        "WHEN n.title = ? COLLATE NOCASE OR n.path = ? COLLATE NOCASE "
        "OR a.alias = ? COLLATE NOCASE THEN 0 "
        "WHEN n.title LIKE ? ESCAPE '\\' OR n.path LIKE ? ESCAPE '\\' "
        "OR a.alias LIKE ? ESCAPE '\\' THEN 1 ELSE 2 END) AS match_priority "
        "FROM nodes n "
        "LEFT JOIN aliases a ON a.node_path = n.path "
        "WHERE (n.title LIKE ? ESCAPE '\\' OR n.path LIKE ? ESCAPE '\\' "
        "OR a.alias LIKE ? ESCAPE '\\') "
    )
    params = [keyword, keyword, keyword, prefix, prefix, prefix, kw, kw, kw]
    if not include_deprecated:
        query += "AND NOT (COALESCE(n.status,'')='deprecated' AND (n.type='page' OR n.path LIKE '%/wiki/%')) "
    if granularity:
        query += "AND n.entity_subtype = ? "
        params.append(granularity)
    query += "GROUP BY n.path ORDER BY match_priority, n.path LIMIT ?"
    params.append(limit + 1)
    rows = conn.execute(query, params).fetchall()
    results = []
    for r in rows[:limit]:
        results.append({
            "path": r["path"], "title": r["title"], "type": r["type"],
            "status": r["status"], "granularity": r["entity_subtype"] or "unspecified",
            **node_version(dict(r)),
        })
    return {
        "keyword": keyword, "granularity": granularity, "count": len(results),
        "limit": limit, "truncated": len(rows) > limit, "nodes": results,
        "include_deprecated": include_deprecated, "version_context": version_context(results),
    }


def path_exists(conn, frm, to):
    """两节点是否连通(BFS)。辅助验证。"""
    if not (node_exists(conn, frm) and node_exists(conn, to)):
        return {"connected": False, "reason": "节点不存在"}
    if frm == to:
        return {"connected": True, "path": [frm]}
    visited = {frm}
    frontier = {frm}
    while frontier:
        nxt = set()
        for node in frontier:
            for r in conn.execute(
                "SELECT object FROM edges WHERE subject=? "
                "UNION SELECT subject FROM edges WHERE object=?", (node, node)
            ):
                o = r["object"]
                if o == to:
                    return {"connected": True, "from": frm, "to": to}
                if o not in visited:
                    visited.add(o)
                    nxt.add(o)
        frontier = nxt
    return {"connected": False, "from": frm, "to": to}


def node_exists(conn, path):
    return conn.execute("SELECT 1 FROM nodes WHERE path=?", (path,)).fetchone() is not None


def get_node_status(conn, path):
    r = conn.execute("SELECT status FROM nodes WHERE path=?", (path,)).fetchone()
    return r["status"] if r else "unknown"


def fmt_text(result, cmd):
    """人类可读文本输出。"""
    if "error" in result:
        return f"错误: {result['error']}"
    if cmd == "node":
        n = result
        lines = [f"节点: {n['title']} ({n['type']})", f"  path: {n['path']}",
                 f"  status: {n['status']} | source_type: {n['source_type']} | date: {n['date']}",
                 f"  has_raw_source: {n['has_raw_source']}"]
        if n.get("aliases"): lines.append(f"  aliases: {', '.join(n['aliases'])}")
        if n.get("description"): lines.append(f"  description: {n['description']}")
        for gloss in n.get("glosses", []):
            primary = " [primary]" if gloss.get("is_primary") else ""
            lines.append(
                f"  gloss{primary}: {gloss['description']} "
                f"(来源: {gloss['source']}; page: {gloss['origin_page']})"
            )
        for review in n.get("description_reviews", []):
            lines.append(
                f"  description_review: {review['status']} | {review['reason']} "
                f"(来源: {review['source']}; page: {review['origin_page']})"
            )
        return "\n".join(lines)
    if cmd == "neighbors":
        lines = [f"关联召回(BFS depth={result['depth']}) 起点: {result['start']}",
                 f"命中节点 {len(result['nodes'])} 个,边 {len(result['edges'])} 条:"]
        if result.get("truncated"):
            lines.append("部分结果（" + ", ".join(result["truncation_reasons"]) + "）；未返回不代表不存在，请定位具体节点或显式提高预算。")
        if len(result["edges"]) > 20:
            lines.append("文本当前展示前 20 条边；完整受限结果请使用 --json。")
        for e in result["edges"][:20]:
            sr = " [SR]" if e["is_sr"] else ""
            cf = e["confidence"] or "-"
            lines.append(f"  {e['subject']} --{e['predicate']}--> {e['object']} "
                         f"[{cf}]{sr} (来源: {e['source'] or '-'})")
        return "\n".join(lines)
    if cmd == "relations":
        lines = [f"节点 {result['node']} 的关系边({len(result['edges'])} 条):"]
        for e in result["edges"]:
            sr = " [SR]" if e["is_sr"] else ""
            lines.append(f"  {e['subject']} --{e['predicate']}--> {e['object']} "
                         f"[{e['confidence'] or '-'}]{sr} (来源: {e['source']})")
        return "\n".join(lines)
    if cmd == "hub_of":
        if not result["hubs"]:
            return f"页面 {result['page']} 不属于任何 Hub"
        lines = [f"页面 {result['page']} 属于 {len(result['hubs'])} 个 Hub:"]
        for h in result["hubs"]:
            lines.append(
                f"  - {h['title']} ({h['predicate']}; {h['path']}, status={h['status']})"
            )
        return "\n".join(lines)
    if cmd == "search":
        count = result["count"]
        summary = f"返回前 {count} 个节点（仍有更多匹配）" if result.get("truncated") else f"命中 {count} 个节点"
        lines = [f"搜索 '{result['keyword']}': {summary}"]
        for n in result["nodes"][:20]:
            lines.append(f"  - {n['title']} [{n['type']}] {n['path']} ({n['status']})")
        if count > 20:
            lines.append(f"  当前展示前 20 个；--json 可查看本次返回的全部 {count} 个节点。")
        return "\n".join(lines)
    if cmd == "path_exists":
        return f"{result['from']} → {result['to']}: {'连通' if result['connected'] else '不连通'}"
    if cmd == "temporal":
        lines = [f"时态事实 at {result['at']}: {result['count']} 条"]
        for f in result["facts"]:
            sr = " [SR]" if f.get("is_sr") else ""
            lines.append(
                f"  {f['subject']} --{f['predicate']}--> {f['object']}"
                f" [{f.get('valid_from') or '-'} ~ {f.get('valid_until') or '∞'}]{sr}"
                f" (来源: {f.get('source') or '-'})"
            )
        return "\n".join(lines)
    return json.dumps(result, ensure_ascii=False, indent=2)


def main():
    ap = argparse.ArgumentParser(description="图查询接口")
    ap.add_argument("cmd", choices=["node", "neighbors", "relations", "hub_of", "search", "path_exists", "temporal"])
    ap.add_argument("args", nargs="*", help="命令参数")
    ap.add_argument("--depth", type=int, default=2)
    for name, default in NEIGHBOR_DEFAULTS.items():
        ap.add_argument("--" + name.replace("_", "-"), type=int, default=default,
                        help=f"neighbors 预算，默认 {default}，硬上限 {NEIGHBOR_CEILINGS[name]}")
    ap.add_argument("--predicate", default=None)
    ap.add_argument("--subject", default=None, help="temporal 命令专用：过滤 subject")
    ap.add_argument("--object", default=None, help="temporal 命令专用：过滤 object")
    ap.add_argument("--at", default=None, help="temporal 命令专用：YYYY-MM-DD")
    ap.add_argument("--top-k", type=int, default=None)
    ap.add_argument("--similar-topk", type=int, default=5,
                    help="每节点相似边上限(动态K;0=排除,-1=全部,默认5)")
    ap.add_argument("--include-hub", action="store_true")
    ap.add_argument("--include-deprecated", action="store_true", help="search 显式历史查询时包含废弃页；导航遍历始终保留历史路径")
    ap.add_argument("--profile", default="", choices=["", "fact", "relation", "explanation", "exploration", "lineage"],
                    help="按查询意图选择谓词族 traversal profile")
    ap.add_argument("--families", default="",
                    help="逗号分隔谓词族，优先于 --profile")
    ap.add_argument("--granularity", default=None,
                   choices=["keyword", "proposition"],
                   help="按颗粒度过滤(search 命令专用):keyword=导航聚合,proposition=精确推理")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--db", default=str(GRAPH_DB))
    args = ap.parse_args()
    min_args = {"node": 1, "neighbors": 1, "relations": 1, "hub_of": 1, "search": 1, "path_exists": 2, "temporal": 0}
    if len(args.args) < min_args[args.cmd]:
        ap.error(f"{args.cmd} 需要至少 {min_args[args.cmd]} 个位置参数")

    conn = connect(args.db)
    result = None
    if args.cmd == "node":
        result = node_info(conn, args.args[0])
        if result is None:
            result = {"error": f"节点不存在: {args.args[0]}"}
    elif args.cmd == "neighbors":
        result = neighbors_bfs(conn, args.args[0], args.depth, args.top_k, args.include_hub,
                               args.similar_topk, args.profile, args.families,
                               args.max_nodes, args.max_edges, args.max_scanned_rows)
    elif args.cmd == "relations":
        result = relations(conn, args.args[0], args.predicate, args.top_k,
                           args.profile, args.families)
    elif args.cmd == "hub_of":
        result = hub_of(conn, args.args[0])
    elif args.cmd == "search":
        result = search_nodes(conn, args.args[0], args.granularity, args.top_k, args.include_deprecated)
    elif args.cmd == "path_exists":
        result = path_exists(conn, args.args[0], args.args[1])
    elif args.cmd == "temporal":
        if not args.at:
            print("错误: temporal 必须提供 --at YYYY-MM-DD", file=sys.stderr)
            return 2
        result = temporal_at(conn, args.at, args.subject, args.object, args.predicate, args.top_k)

    conn.close()
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        if isinstance(result, dict) and "error" in result:
            print(f"错误: {result['error']}", file=sys.stderr); sys.exit(1)
        print(fmt_text(result, args.cmd))


if __name__ == "__main__":
    sys.exit(main())
