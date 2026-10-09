#!/usr/bin/env python3
"""conversation-scoring 回归测试。

不依赖任何具体课程数据，也不调模型 —— judge_fn 是注入的，测试里用假的。

验证点：
- 要点契约：空要点集 / 空要点 / 非正权重 一律报错
- 校验：topic 与来源节点必须在图里；未审定要给出提示
- 判分：逐条判、只按抽中的要点算、判定器抛异常记 ungradable 而非 missed
- **ungradable 不进分母**（知识库没覆盖不该扣学生分）
- 追问不计分
- 记录：append-only 落盘、读回一致、崩在半路也只丢最后一条
- 成绩单：分母为 0 时 gradable=False，而不是谎称考了 0 分
"""
import json
import sqlite3
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from dsh.scoring import (
    COVERED, MISSED, UNGRADABLE, Exchange, PointJudgement, Rubric, RubricError,
    RubricPoint, Transcript, grade, load_rubrics, main, validate,
)

RUB = {
    "wiki/topics/a": {
        "title": "共轭复数",
        "reviewed_by": "任课老师",
        "points": [
            {"text": "虚部取反", "weight": 1.0, "source": "wiki/topics/a"},
            {"text": "关于实轴对称", "weight": 2.0, "source": "wiki/topics/a"},
            {"text": "与原数之积等于模的平方", "weight": 1.0, "source": "wiki/topics/a"},
        ],
    }
}


def make_graph() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE nodes (path TEXT PRIMARY KEY, title TEXT)")
    conn.executemany("INSERT INTO nodes VALUES (?,?)",
                     [("wiki/topics/a", "共轭复数")])
    return conn


def rubric() -> Rubric:
    return Rubric.from_dict("wiki/topics/a", RUB["wiki/topics/a"])


def judge_contains(point: str, answer: str) -> bool:
    """假判定器：要点里的字都在回答里就算讲到。只为测流程，不是真判法。"""
    return all(ch in answer for ch in point[:3])


# ============ 要点契约 ============

def test_empty_rubric_is_rejected():
    for bad in ({"points": []}, []):
        try:
            Rubric.from_dict("t", bad)
        except RubricError:
            continue
        raise AssertionError("空要点集等于白送满分，必须拒绝")


def test_blank_text_and_nonpositive_weight_rejected():
    for bad in ({"points": [{"text": "  "}]},
                {"points": [{"text": "x", "weight": 0}]},
                {"points": [{"text": "x", "weight": -1}]}):
        try:
            Rubric.from_dict("t", bad)
        except RubricError:
            continue
        raise AssertionError(f"应拒绝: {bad}")


def test_plain_string_points_are_accepted():
    r = Rubric.from_dict("t", ["要点一", "要点二"])
    assert [p.text for p in r.points] == ["要点一", "要点二"]
    assert r.total_weight == 2.0


def test_rubric_file_roundtrip():
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "r.json"
        p.write_text(json.dumps(RUB, ensure_ascii=False), encoding="utf-8")
        loaded = load_rubrics(p)
        assert loaded["wiki/topics/a"].total_weight == 4.0


# ============ 校验 ============

def test_validate_passes_a_well_formed_rubric():
    assert validate({"wiki/topics/a": rubric()}, make_graph()) == []


def test_validate_flags_missing_topic_and_source():
    r = Rubric.from_dict("wiki/topics/缺", {
        "reviewed_by": "老师",
        "points": [{"text": "x", "source": "wiki/topics/也缺"}]})
    problems = validate({"wiki/topics/缺": r}, make_graph())
    assert len(problems) == 2
    assert any("知识点不在图里" in p for p in problems)
    assert any("来源" in p for p in problems)


def test_validate_flags_an_unreviewed_draft():
    r = Rubric.from_dict("wiki/topics/a", {"points": ["x"]})
    problems = validate({"wiki/topics/a": r}, make_graph())
    assert any("审定人" in p for p in problems)


# ============ 判分 ============

def test_grade_judges_each_point_separately():
    ex = grade(rubric(), "虚部取反，并且关于实轴对称", judge_contains)
    verdicts = [j.verdict for j in ex.judgements]
    assert verdicts == [COVERED, COVERED, MISSED]
    assert ex.earned == 3.0 and ex.possible == 4.0


def test_grade_only_counts_the_points_that_were_asked():
    """只抽了一条要点来考，就只按这一条算分。"""
    r = rubric()
    ex = grade(r, "虚部取反", judge_contains, points=(r.points[0],))
    assert ex.earned == 1.0 and ex.possible == 1.0


def test_judge_failure_is_ungradable_not_missed():
    """判不了和答错是两件事 —— 判定器崩了不该算学生答错。"""
    def boom(point, answer):
        raise RuntimeError("模型没响应")
    ex = grade(rubric(), "随便答的", boom)
    assert all(j.verdict == UNGRADABLE for j in ex.judgements)
    assert ex.possible == 0.0, "ungradable 不该进分母"
    assert "模型没响应" in ex.judgements[0].note


def test_ungradable_is_excluded_from_the_denominator():
    """知识库没覆盖的要点，不由学生承担。"""
    def partial(point, answer):
        return UNGRADABLE if "模的平方" in point else (point[:2] in answer)
    ex = grade(rubric(), "虚部取反", partial)
    assert ex.earned == 1.0
    assert ex.possible == 3.0, "4 分里那 1 分判不了，分母应是 3 不是 4"
    assert ex.ungradable == 1.0


def test_judge_may_return_a_verdict_string_directly():
    ex = grade(rubric(), "x", lambda p, a: COVERED)
    assert ex.earned == 4.0


def test_bad_verdict_string_is_rejected():
    try:
        PointJudgement(RubricPoint("x"), "大概对吧")
    except RubricError:
        return
    raise AssertionError("判定只能是三态之一")


# ============ 记录 ============

def test_transcript_is_appended_to_disk_as_it_goes():
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "t.jsonl"
        t = Transcript(student="张三", scope="第一章").open(p)
        t.append(grade(rubric(), "虚部取反", judge_contains, question="问题一"))
        after_one = p.read_text(encoding="utf-8").count("\n")
        t.append(grade(rubric(), "关于实轴对称", judge_contains, question="问题二"))
        assert after_one == 2, "表头 + 第一条就该已经落盘，而不是等结束才写"
        assert p.read_text(encoding="utf-8").count("\n") == 3


def test_transcript_roundtrip_preserves_everything():
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "t.jsonl"
        t = Transcript(student="张三", scope="第一章").open(p)
        t.append(grade(rubric(), "虚部取反", judge_contains, question="Q1"))
        t.append(grade(rubric(), "不知道", judge_contains, question="Q2",
                       is_probe=True, withheld=True))
        back = Transcript.load(p)
        assert back.student == "张三" and back.scope == "第一章"
        assert back.session_id == t.session_id
        assert len(back.exchanges) == 2
        assert back.exchanges[1].is_probe and back.exchanges[1].withheld
        assert back.scorecard().to_dict() == t.scorecard().to_dict()


def test_a_truncated_transcript_still_loads():
    """中途崩了只丢最后一条，前面答完的部分必须还在 —— 这是用 JSONL 的理由。"""
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "t.jsonl"
        t = Transcript().open(p)
        t.append(grade(rubric(), "虚部取反", judge_contains, question="Q1"))
        t.append(grade(rubric(), "关于实轴对称", judge_contains, question="Q2"))
        text = p.read_text(encoding="utf-8")
        p.write_text(text[:-12], encoding="utf-8")       # 砍掉最后一行的尾巴
        back = Transcript.load(p)
        assert len(back.exchanges) == 1 and back.exchanges[0].question == "Q1"
        assert back.damaged == [3], "丢了哪一行必须说出来，不能静默跳过"
        assert "读不出来" in back.report()


# ============ 成绩单 ============

def test_probes_do_not_count_toward_the_score():
    t = Transcript()
    t.append(grade(rubric(), "虚部取反", judge_contains))
    t.append(grade(rubric(), "全不会", judge_contains, is_probe=True))
    card = t.scorecard()
    assert card.possible == 4.0, "追问是用来定位欠缺的，不是再扣一次分"
    assert card.probes == 1


def test_scorecard_marks_weak_topics():
    t = Transcript()
    t.append(grade(rubric(), "虚部取反", judge_contains))     # 1/4 < 50%
    card = t.scorecard()
    assert card.weak_topics == ["wiki/topics/a"]
    assert card.ratio == 0.25


def test_scorecard_says_so_when_nothing_was_gradable():
    """分母为 0 时不能谎称考了 0 分。"""
    t = Transcript()
    t.append(grade(rubric(), "x", lambda p, a: UNGRADABLE))
    card = t.scorecard()
    assert not card.gradable and card.ratio == 0.0 and card.ungradable == 4.0
    assert "无法判分" in t.report()


def test_report_is_readable_and_explains_each_point():
    t = Transcript(student="张三", scope="第一章")
    t.append(grade(rubric(), "虚部取反", judge_contains, question="共轭复数有什么性质？"))
    r = t.report()
    assert "共轭复数有什么性质？" in r
    assert "✓ 虚部取反" in r and "✗ 关于实轴对称" in r
    assert "总分 1 / 4" in r


# ============ 命令行 ============

def test_cli_validates_and_reports():
    with tempfile.TemporaryDirectory() as d:
        rp = Path(d) / "r.json"
        rp.write_text(json.dumps(RUB, ensure_ascii=False), encoding="utf-8")
        assert main([str(rp)]) == 0                      # 只看规模，不校验
        bad = Path(d) / "bad.json"
        bad.write_text(json.dumps({"t": {"points": []}}), encoding="utf-8")
        assert main([str(bad)]) == 2

        tp = Path(d) / "t.jsonl"
        t = Transcript().open(tp)
        t.append(grade(rubric(), "虚部取反", judge_contains, question="Q"))
        assert main([str(tp), "--report"]) == 0


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    passed = 0
    for t in tests:
        t()
        passed += 1
        print(f"  {t.__name__}: PASS")
    print(f"conversation-scoring regression: {passed}/{len(tests)} PASS")
