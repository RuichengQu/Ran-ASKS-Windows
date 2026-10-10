#!/usr/bin/env python3
"""scope-guard 回归测试。

用临时合成图，不依赖任何具体课程数据 —— 测试必须能在空仓库上跑。

验证点：
- 契约：缺 name/roots 直接报错；roots 解析不到 → ScopeError（不静默缩小范围）
- 展开：前置闭包是传递的；来源/涉及只走一跳；每个节点都带纳入理由
- pre-execute：点名读取出界被 deny，范围内放行，locator 的 #片段 不影响判定
- post-execute：检索结果逐行过滤；全被滤掉时给出可读提示；范围内结果不触发 replace
- classify：范围内术语命中、范围外术语命中、长词包短词不误判
- report/describe：可审计
"""
import json
import sqlite3
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from dsh.harness import (
    SessionLog, ToolDefinition, ToolExecution, ToolExecutionResult, ToolRegistry,
)
from dsh.guards.scope_guard import (
    ScopeContract, ScopeError, ScopeGuard, resolve,
)

# ---- 合成图 ----
# ch1: A ←前置- B ←前置- C   (C 的前置是 B，B 的前置是 A)
# ch3: Z 完全无关
NODES = [
    ("wiki/topics/a", "复数的定义"),
    ("wiki/topics/b", "复平面"),
    ("wiki/topics/c", "模与辐角"),
    ("wiki/topics/z", "留数定理"),
    ("raw/courses/ch1", "第一章讲义"),
    ("raw/courses/ch3", "第三章讲义"),
    ("kw/实部", "实部"),
    ("kw/留数", "留数"),
]
EDGES = [
    ("wiki/topics/c", "前置", "wiki/topics/b"),
    ("wiki/topics/b", "前置", "wiki/topics/a"),
    ("wiki/topics/c", "来源", "raw/courses/ch1"),
    ("wiki/topics/a", "涉及", "kw/实部"),
    ("wiki/topics/z", "来源", "raw/courses/ch3"),
    ("wiki/topics/z", "涉及", "kw/留数"),
]
ALIASES = [
    ("复数的定义", "wiki/topics/a"), ("实部", "kw/实部"),
    ("模与辐角", "wiki/topics/c"), ("复数的模", "wiki/topics/c"),
    ("留数", "kw/留数"), ("留数定理", "wiki/topics/z"),
]


def make_graph() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE nodes (path TEXT PRIMARY KEY, title TEXT)")
    conn.execute("CREATE TABLE edges (subject TEXT, predicate TEXT, object TEXT)")
    conn.execute("CREATE TABLE aliases (alias TEXT, node_path TEXT)")
    conn.executemany("INSERT INTO nodes VALUES (?,?)", NODES)
    conn.executemany("INSERT INTO edges VALUES (?,?,?)", EDGES)
    conn.executemany("INSERT INTO aliases VALUES (?,?)", ALIASES)
    return conn


def ch1_contract(**kw) -> ScopeContract:
    data = {"name": "测试课 第一章", "roots": ["wiki/topics/c"]}
    data.update(kw)
    return ScopeContract.from_dict(data)


def ch1_guard(conn) -> ScopeGuard:
    return ScopeGuard(ch1_contract(), conn=conn)


# ============ 契约 ============

def test_contract_requires_name_and_roots():
    for bad in ({"roots": ["x"]}, {"name": "x"}, {"name": "x", "roots": []}):
        try:
            ScopeContract.from_dict(bad)
        except ScopeError:
            continue
        raise AssertionError(f"应当拒绝: {bad}")


def test_contract_roundtrip():
    c = ch1_contract(depth=2, extra=["wiki/topics/z"])
    assert ScopeContract.from_dict(c.to_dict()) == c


def test_missing_root_is_an_error_not_a_warning():
    conn = make_graph()
    c = ch1_contract(roots=["wiki/topics/c", "wiki/topics/不存在"])
    try:
        resolve(c, conn)
    except ScopeError as e:
        assert "不存在" in str(e)
        return
    raise AssertionError("roots 缺失必须报错 —— 静默缩小范围会静默改变考查内容")


def test_root_can_be_a_title_or_alias():
    conn = make_graph()
    for name in ("模与辐角", "复数的模"):
        r = resolve(ch1_contract(roots=[name]), conn)
        assert "wiki/topics/c" in r, name


# ============ 展开 ============

def test_prerequisite_closure_is_transitive():
    r = resolve(ch1_contract(), make_graph())
    # c -前置-> b -前置-> a 全部进范围
    for n in ("wiki/topics/c", "wiki/topics/b", "wiki/topics/a"):
        assert n in r, n
    assert "wiki/topics/z" not in r


def test_depth_limits_the_closure():
    r = resolve(ch1_contract(depth=1), make_graph())
    assert "wiki/topics/b" in r
    assert "wiki/topics/a" not in r, "depth=1 不应走到第二层前置"


def test_attach_predicates_are_one_hop_and_recorded():
    r = resolve(ch1_contract(), make_graph())
    assert "raw/courses/ch1" in r          # c 的来源
    assert "kw/实部" in r                   # a 的涉及
    assert "raw/courses/ch3" not in r
    assert r.explain("wiki/topics/c") == "root"
    assert r.explain("wiki/topics/b").startswith("前置")
    assert r.explain("raw/courses/ch1").startswith("来源")


def test_extra_nodes_are_allowed_explicitly():
    r = resolve(ch1_contract(extra=["wiki/topics/z"]), make_graph())
    assert r.explain("wiki/topics/z") == "extra"


def test_extra_also_brings_its_own_sources_and_keywords():
    """显式放行一个页面，就该同时放行它引的原件 —— 否则模型看得见却读不了。"""
    r = resolve(ch1_contract(extra=["wiki/topics/z"]), make_graph())
    assert "raw/courses/ch3" in r, "extra 的来源应一并放行"
    assert "kw/留数" in r, "extra 的关键词应一并放行"


def test_extra_does_not_expand_through_follow():
    """extra 的语义是「只放行这一个」，不沿前置继续扩张。"""
    conn = make_graph()
    conn.execute("INSERT INTO edges VALUES ('wiki/topics/z','前置','wiki/topics/zz')")
    conn.execute("INSERT INTO nodes VALUES ('wiki/topics/zz','更早的东西')")
    r = resolve(ch1_contract(extra=["wiki/topics/z"]), conn)
    assert "wiki/topics/zz" not in r


# ============ pre-execute ============

def test_pre_execute_denies_out_of_scope_read():
    g = ch1_guard(make_graph())
    d = g.on_pre_execute(ToolExecution(name="read_section",
                                       arguments={"page": "wiki/topics/z"}))
    assert d is not None and d.kind == "deny"
    assert g.contract.deny_message in d.reason
    assert g.denied and g.denied[0]["target"] == "wiki/topics/z"


def test_pre_execute_allows_in_scope_and_prerequisites():
    g = ch1_guard(make_graph())
    for page in ("wiki/topics/c", "wiki/topics/b", "wiki/topics/a"):
        assert g.on_pre_execute(
            ToolExecution(name="read_section", arguments={"page": page})) is None, page
    assert not g.denied


def test_locator_fragment_does_not_affect_judgement():
    g = ch1_guard(make_graph())
    assert g.on_pre_execute(ToolExecution(
        name="read_raw", arguments={"locator": "raw/courses/ch1#L10-L20"})) is None
    d = g.on_pre_execute(ToolExecution(
        name="read_raw", arguments={"locator": "raw/courses/ch3#L1-L5"}))
    assert d is not None and d.kind == "deny"


def test_unguarded_tools_and_empty_args_pass_through():
    g = ch1_guard(make_graph())
    assert g.on_pre_execute(ToolExecution(name="remember", arguments={"x": "y"})) is None
    assert g.on_pre_execute(ToolExecution(name="read_section", arguments={})) is None


# ============ post-execute ============

def _search(content: str) -> ToolExecutionResult:
    return ToolExecutionResult(content=content)


def test_post_execute_filters_out_of_scope_hits():
    g = ch1_guard(make_graph())
    res = _search("wiki/topics/c 模与辐角\nwiki/topics/z 留数定理\nwiki/topics/b 复平面")
    d = g.on_post_execute(ToolExecution(name="graph_search", arguments={}), res)
    assert d is not None and d.kind == "replace"
    body = d.replacement.content
    assert "wiki/topics/c" in body and "wiki/topics/b" in body
    assert "wiki/topics/z" not in body
    assert g.filtered[0]["dropped"] == 1


def test_post_execute_keeps_in_scope_results_untouched():
    g = ch1_guard(make_graph())
    res = _search("wiki/topics/c 模与辐角\nwiki/topics/a 复数的定义")
    assert g.on_post_execute(ToolExecution(name="graph_search", arguments={}), res) is None


def test_post_execute_explains_when_everything_is_filtered():
    g = ch1_guard(make_graph())
    res = _search("wiki/topics/z 留数定理\nraw/courses/ch3 第三章讲义")
    d = g.on_post_execute(ToolExecution(name="semantic_search", arguments={}), res)
    assert "范围内无匹配" in d.replacement.content


# ASKS 的检索工具实际返回 JSON 信封，不是一行一条。下面三个用例用真实形状。

def test_post_execute_filters_graph_search_json_envelope():
    g = ch1_guard(make_graph())
    res = _search(json.dumps({"keyword": "复数", "count": 2, "nodes": [
        {"path": "wiki/topics/c", "title": "模与辐角", "type": "page"},
        {"path": "wiki/topics/z", "title": "留数定理", "type": "page"}]},
        ensure_ascii=False))
    d = g.on_post_execute(ToolExecution(name="graph_search", arguments={}), res)
    out = json.loads(d.replacement.content)          # 必须仍是合法 JSON
    assert [n["path"] for n in out["nodes"]] == ["wiki/topics/c"]
    assert out["count"] == 1, "count 必须跟着列表走，否则信封自相矛盾"
    assert out["scope_guard"]["dropped"] == 1


def test_post_execute_filters_wiki_recall_candidates():
    g = ch1_guard(make_graph())
    res = _search(json.dumps({"query": "复数", "count": 2, "candidates": [
        {"path": "wiki/topics/a", "title": "复数的定义", "score": 0.9},
        {"path": "wiki/topics/z", "title": "留数定理", "score": 0.8}]},
        ensure_ascii=False))
    d = g.on_post_execute(ToolExecution(name="wiki_recall", arguments={}), res)
    out = json.loads(d.replacement.content)
    assert [c["path"] for c in out["candidates"]] == ["wiki/topics/a"]
    assert out["count"] == 1


def test_post_execute_neutralizes_out_of_scope_node_resolve():
    """node_resolve 把结果提到顶层；顶层节点出界就不能原样留着。"""
    g = ch1_guard(make_graph())
    res = _search(json.dumps({"decision": "resolved", "node_id": "wiki/topics/z",
                              "title": "留数定理", "candidates": []}, ensure_ascii=False))
    d = g.on_post_execute(ToolExecution(name="node_resolve", arguments={}), res)
    out = json.loads(d.replacement.content)
    assert out["decision"] == "out_of_scope"
    assert out["node_id"] == "" and out["title"] == ""


def test_post_execute_keeps_in_scope_json_untouched():
    g = ch1_guard(make_graph())
    res = _search(json.dumps({"count": 1, "nodes": [{"path": "wiki/topics/a"}]},
                             ensure_ascii=False))
    assert g.on_post_execute(ToolExecution(name="graph_search", arguments={}), res) is None


def test_post_execute_ignores_non_search_tools_and_errors():
    g = ch1_guard(make_graph())
    res = _search("wiki/topics/z 留数定理")
    assert g.on_post_execute(ToolExecution(name="read_raw", arguments={}), res) is None
    err = ToolExecutionResult(content="wiki/topics/z", is_error=True)
    assert g.on_post_execute(ToolExecution(name="graph_search", arguments={}), err) is None


# ============ classify ============

def test_classify_in_scope_question():
    g = ch1_guard(make_graph())
    v = g.classify("复数的实部是什么意思？")
    assert v.inside and v.hit


def test_classify_out_of_scope_question():
    g = ch1_guard(make_graph())
    v = g.classify("留数定理怎么用？")
    assert not v.inside and "留数" in str(v.outside)


def test_classify_does_not_flag_a_long_in_scope_term():
    """「复数的模」在范围内，不能因为它含「模」之类的片段被判出界。"""
    g = ch1_guard(make_graph())
    assert g.classify("复数的模怎么算？").inside


def test_strict_classify_rejects_any_out_of_scope_term():
    """宽松用于判「学生说了什么」，从严用于校验「我们自己生成了什么」。

    实测：系统出的题同时含「复数」和「留数」，宽松判定会放行，
    学生就读到了没学过的词。
    """
    g = ch1_guard(make_graph())
    mixed = "复数的定义和留数定理有什么关系"
    assert g.classify(mixed).verdict == "in"
    v = g.classify(mixed, strict=True)
    assert v.verdict == "out" and not v.inside
    assert "留数" in str(v.outside)
    # 干净的句子两种模式都在范围内
    assert g.classify("复数的实部", strict=True).verdict == "in"


def test_classify_reports_unknown_separately():
    """什么术语都没命中 → unknown，不是 in。放行，但不假装判定成立。"""
    g = ch1_guard(make_graph())
    for text in ("老师好", "帮我写一首诗", ""):
        v = g.classify(text)
        assert v.inside and v.verdict == "unknown", text
    assert g.classify("复数的实部").verdict == "in"
    assert g.classify("留数定理").verdict == "out"


# ============ 审计 ============

def test_report_and_describe_are_auditable():
    g = ch1_guard(make_graph())
    g.on_pre_execute(ToolExecution(name="read_section", arguments={"page": "wiki/topics/z"}))
    rep = g.report()
    assert rep["scope"] == "测试课 第一章"
    assert rep["node_count"] == len(g.resolution.nodes)
    assert rep["denied"][0]["target"] == "wiki/topics/z"
    assert rep["contract"]["roots"] == ["wiki/topics/c"]
    assert "本次对话范围" in g.describe()


def test_contract_file_roundtrip():
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "unit.json"
        p.write_text(json.dumps(ch1_contract().to_dict(), ensure_ascii=False),
                     encoding="utf-8")
        assert ScopeContract.load(p) == ch1_contract()


# ============ 走完整管道（guard 的真正主张：模型绕不过去） ============

def _registry_with(guard):
    reg = ToolRegistry()
    reg.register(ToolDefinition(
        name="read_section", description="读一节",
        input_schema={"type": "object", "properties": {"page": {"type": "string"}}},
        execute_fn=lambda args: f"[正文] {args['page']} 的内容"))
    reg.register(ToolDefinition(
        name="graph_search", description="搜索",
        input_schema={"type": "object", "properties": {"term": {"type": "string"}}},
        execute_fn=lambda args: "wiki/topics/c 模与辐角\nwiki/topics/z 留数定理"))
    reg.on_pre_execute(guard.on_pre_execute)
    reg.on_post_execute(guard.on_post_execute)
    return reg


def test_pipeline_blocks_out_of_scope_read():
    g = ch1_guard(make_graph())
    reg, log = _registry_with(g), SessionLog()
    r = reg.execute(ToolExecution(name="read_section",
                                  arguments={"page": "wiki/topics/z"}), log)
    assert r.is_error and r.error_code == "DENIED"
    assert "[正文]" not in r.content, "工具正文根本不该被执行"
    # 被否决的调用同样留在 session log 里 —— 老师要能看见学生问过什么、被挡了什么
    assert any(e.type == "tool/result" and e.data.get("denied") for e in log.events())


def test_pipeline_allows_in_scope_read():
    g = ch1_guard(make_graph())
    r = _registry_with(g).execute(
        ToolExecution(name="read_section", arguments={"page": "wiki/topics/a"}), SessionLog())
    assert not r.is_error and "的内容" in r.content


def test_pipeline_filters_search_results():
    g = ch1_guard(make_graph())
    r = _registry_with(g).execute(
        ToolExecution(name="graph_search", arguments={"term": "复数"}), SessionLog())
    assert "wiki/topics/c" in r.content
    assert "wiki/topics/z" not in r.content


def test_cli_reports_a_broken_contract_instead_of_crashing():
    from dsh.guards import scope_guard as sg
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "bad.json"
        p.write_text(json.dumps({"name": "坏契约", "roots": ["不存在的节点"],
                                 "graph": str(Path(d) / "nope.db")}, ensure_ascii=False),
                     encoding="utf-8")
        assert sg.main([str(p)]) == 2


def test_example_template_is_valid_json_and_shape():
    """templates/scope/unit.example.json 是对外的样板，必须能被解析。"""
    tpl = REPO / "templates" / "scope" / "unit.example.json"
    data = json.loads(tpl.read_text(encoding="utf-8"))
    c = ScopeContract.from_dict(data)
    assert c.follow == ("前置",) and "来源" in c.attach


def test_agent_loop_without_scope_is_unchanged():
    """不传 scope 时不挂 guard —— 老行为必须一字不改。"""
    from dsh.agent_loop import AgentLoop
    assert AgentLoop(mode="api").scope_guard is None


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    passed = 0
    for t in tests:
        t()
        passed += 1
        print(f"  {t.__name__}: PASS")
    print(f"scope-guard regression: {passed}/{len(tests)} PASS")
