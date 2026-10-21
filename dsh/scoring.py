"""scoring — 给学生与系统的一次对话打分，并留下可人工核实的记录。

与 scope-guard 相互独立：范围控制管「能聊什么」，这里管「聊得怎么样」。
两者不互相依赖，可以单独使用。

**三个设计决定，都是被实测逼出来的：**

1. **逐条要点判，不整体判对错。**
   整体判分实测高报 25%（云端 judge 20%，本地 25%，两者都没有漏判）。
   逐条判还顺带可解释：能告诉学生「你讲到了 1、3，漏了 2、4」。

2. **判定是三态：covered / missed / ungradable。**
   ASKS 本身会审查答案 —— `answer_judge.py` 的生成规则是「只使用检索内容，
   不使用自身知识补充」「检索内容完全不含相关信息时才回答证据不足」。
   所以系统有可能**根本没给出参考答案**。这时候学生不该被扣分：
   那是知识库没覆盖，不是他不会。**ungradable 不进分母。**
   把它并进 missed，等于拿知识库的洞去扣学生的分。

3. **要点必须可追溯到图里的节点。**
   `prerequisites` 在 ASKS 里是受验证的图契约而不是展示字段；要点同理。
   每条要点记着它出自哪个节点，`validate()` 会核对那些节点真的存在。
   要点是教学判断，最终该由任课老师审定，但「出自哪里」必须是机械可查的。

记录是 append-only 的 JSONL，一次对话一个文件：老师要能事后逐条核实
系统问了什么、学生答了什么、每一条要点判成了什么、为什么。

    python -m dsh.scoring rubrics.json --validate
    python -m dsh.scoring transcript.jsonl --report
"""
from __future__ import annotations

import json
import sqlite3
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

REPO = Path(__file__).resolve().parent.parent

COVERED = "covered"
MISSED = "missed"
UNGRADABLE = "ungradable"
VERDICTS = (COVERED, MISSED, UNGRADABLE)


class RubricError(Exception):
    """要点契约不成立 —— 空要点、权重非正、来源节点不在图里等。"""


@dataclass(frozen=True)
class RubricPoint:
    """一条要点。

    text    一句可判断「说到没说到」的具体陈述
    weight  配分权重，正数
    source  这条要点出自哪个节点（node path）—— 必须可追溯
    """
    text: str
    weight: float = 1.0
    source: str = ""

    @classmethod
    def from_any(cls, data) -> "RubricPoint":
        if isinstance(data, str):
            return cls(text=data)
        if isinstance(data, dict):
            return cls(text=data.get("text", ""),
                       weight=float(data.get("weight", 1.0)),
                       source=data.get("source", ""))
        raise RubricError(f"要点既不是字符串也不是对象: {data!r}")

    def to_dict(self) -> dict:
        return {"text": self.text, "weight": self.weight, "source": self.source}


@dataclass(frozen=True)
class Rubric:
    """一个知识点的要点集合。"""
    topic: str                       # 图里的节点 path
    points: tuple[RubricPoint, ...]
    title: str = ""
    reviewed_by: str = ""            # 审定人；空 = 还是模型草稿

    @property
    def total_weight(self) -> float:
        return sum(p.weight for p in self.points)

    @classmethod
    def from_dict(cls, topic: str, data) -> "Rubric":
        if isinstance(data, list):
            data = {"points": data}
        pts = tuple(RubricPoint.from_any(x) for x in data.get("points", ()))
        if not pts:
            raise RubricError(f"{topic} 没有要点 —— 不接受空要点集（等于白送满分）")
        for p in pts:
            if not p.text.strip():
                raise RubricError(f"{topic} 有一条空要点")
            if p.weight <= 0:
                raise RubricError(f"{topic} 的要点「{p.text[:16]}」权重非正: {p.weight}")
        return cls(topic=topic, points=pts, title=data.get("title", ""),
                   reviewed_by=data.get("reviewed_by", ""))

    def to_dict(self) -> dict:
        return {"title": self.title, "reviewed_by": self.reviewed_by,
                "points": [p.to_dict() for p in self.points]}


def load_rubrics(path: str | Path) -> dict[str, Rubric]:
    """从 json 读要点表：{topic: [要点...]} 或 {topic: {title, points:[...]}}。"""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, dict) or not data:
        raise RubricError("要点文件应是 {知识点: 要点列表} 且非空")
    return {t: Rubric.from_dict(t, v) for t, v in data.items()}


def validate(rubrics: dict[str, Rubric], conn: sqlite3.Connection) -> list[str]:
    """核对 topic 与每条要点的 source 都在图里。返回问题清单，空=全部通过。

    不抛异常而是返回清单：起草阶段要能一次看完所有问题再去改。
    """
    problems = []
    for topic, r in rubrics.items():
        if not conn.execute("SELECT 1 FROM nodes WHERE path=?", (topic,)).fetchone():
            problems.append(f"{topic}: 知识点不在图里")
        for p in r.points:
            if p.source and not conn.execute(
                    "SELECT 1 FROM nodes WHERE path=?", (p.source,)).fetchone():
                problems.append(f"{topic}: 要点「{p.text[:20]}」的来源 {p.source} 不在图里")
        if not r.reviewed_by:
            problems.append(f"{topic}: 尚无审定人 —— 要点是教学判断，应由任课老师审定后再用")
    return problems


@dataclass
class PointJudgement:
    """一条要点的判定结果。"""
    point: RubricPoint
    verdict: str                     # covered | missed | ungradable
    note: str = ""

    def __post_init__(self):
        if self.verdict not in VERDICTS:
            raise RubricError(f"判定只能是 {VERDICTS}，收到 {self.verdict!r}")

    def to_dict(self) -> dict:
        return {"point": self.point.to_dict(), "verdict": self.verdict, "note": self.note}


@dataclass
class Exchange:
    """一问一答，连同逐条要点的判定。"""
    topic: str
    question: str
    answer: str
    judgements: list[PointJudgement] = field(default_factory=list)
    is_probe: bool = False           # 追问：用于定位欠缺，不计分
    withheld: bool = False           # 系统自己因证据不足没给参考答案
    asked_at: str = field(default_factory=lambda: time.strftime("%Y-%m-%dT%H:%M:%S"))

    @property
    def earned(self) -> float:
        return sum(j.point.weight for j in self.judgements if j.verdict == COVERED)

    @property
    def possible(self) -> float:
        """ungradable 不进分母 —— 知识库没覆盖不该由学生承担。"""
        return sum(j.point.weight for j in self.judgements if j.verdict != UNGRADABLE)

    @property
    def ungradable(self) -> float:
        return sum(j.point.weight for j in self.judgements if j.verdict == UNGRADABLE)

    @property
    def missed_points(self) -> list[RubricPoint]:
        return [j.point for j in self.judgements if j.verdict == MISSED]

    def to_dict(self) -> dict:
        return {"topic": self.topic, "question": self.question, "answer": self.answer,
                "judgements": [j.to_dict() for j in self.judgements],
                "is_probe": self.is_probe, "withheld": self.withheld,
                "asked_at": self.asked_at,
                "earned": round(self.earned, 3), "possible": round(self.possible, 3)}

    @classmethod
    def from_dict(cls, d: dict) -> "Exchange":
        return cls(topic=d["topic"], question=d.get("question", ""),
                   answer=d.get("answer", ""),
                   judgements=[PointJudgement(RubricPoint.from_any(j["point"]),
                                              j["verdict"], j.get("note", ""))
                               for j in d.get("judgements", [])],
                   is_probe=d.get("is_probe", False),
                   withheld=d.get("withheld", False),
                   asked_at=d.get("asked_at", ""))


def grade(rubric: Rubric, answer: str,
          judge_fn: Callable[[str, str], bool | str],
          question: str = "", points: tuple[RubricPoint, ...] | None = None,
          **kw) -> Exchange:
    """逐条判一个回答。

    judge_fn(要点文本, 学生回答) 返回 True/False，或直接返回三态之一。
    判定器由调用方注入 —— ASKS 不替使用者决定用哪个模型、甚至用不用模型。
    judge_fn 抛异常时该条记 ungradable 并把原因写进 note，而不是记 missed：
    判不了和答错是两件事。

    points 只传本次抽中的要点时，就只按这些算分。
    """
    chosen = points if points is not None else rubric.points
    js = []
    for p in chosen:
        try:
            v = judge_fn(p.text, answer)
        except Exception as e:                      # noqa: BLE001 - 判定器什么都可能抛
            js.append(PointJudgement(p, UNGRADABLE, f"判定失败: {type(e).__name__}: {e}"[:200]))
            continue
        if isinstance(v, str):
            js.append(PointJudgement(p, v))
        else:
            js.append(PointJudgement(p, COVERED if v else MISSED))
    return Exchange(topic=rubric.topic, question=question, answer=answer,
                    judgements=js, **kw)


@dataclass
class ScoreCard:
    """一次对话的成绩。"""
    earned: float = 0.0
    possible: float = 0.0
    ungradable: float = 0.0
    by_topic: dict[str, tuple[float, float]] = field(default_factory=dict)
    weak_topics: list[str] = field(default_factory=list)
    probes: int = 0
    withheld: int = 0

    @property
    def ratio(self) -> float:
        """可判部分的得分率。分母为 0 时返回 0 并由 gradable 标明。"""
        return round(self.earned / self.possible, 4) if self.possible else 0.0

    @property
    def gradable(self) -> bool:
        """这次对话到底判没判成 —— 分母为 0 时 ratio=0 不代表考了 0 分。"""
        return self.possible > 0

    def to_dict(self) -> dict:
        return {"earned": round(self.earned, 3), "possible": round(self.possible, 3),
                "ratio": self.ratio, "gradable": self.gradable,
                "ungradable": round(self.ungradable, 3),
                "by_topic": {k: [round(a, 3), round(b, 3)] for k, (a, b) in self.by_topic.items()},
                "weak_topics": list(self.weak_topics),
                "probes": self.probes, "withheld": self.withheld}


@dataclass
class Transcript:
    """一次对话的 append-only 记录。

    老师要能事后逐条核实：系统问了什么、学生答了什么、每条要点判成什么。
    所以写 JSONL 而不是 json —— 中途崩了也不会丢掉已经答完的部分。
    """
    session_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    student: str = ""
    scope: str = ""
    started_at: str = field(default_factory=lambda: time.strftime("%Y-%m-%dT%H:%M:%S"))
    exchanges: list[Exchange] = field(default_factory=list)
    path: Path | None = None
    damaged: list[int] = field(default_factory=list)   # 读不出来的行号

    def open(self, path: str | Path) -> "Transcript":
        """绑定落盘文件并写入表头。之后每条 append 立刻落盘。"""
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("w", encoding="utf-8", newline="\n") as f:
            f.write(json.dumps({"type": "header", "session_id": self.session_id,
                                "student": self.student, "scope": self.scope,
                                "started_at": self.started_at}, ensure_ascii=False) + "\n")
        return self

    def append(self, ex: Exchange) -> Exchange:
        self.exchanges.append(ex)
        if self.path is not None:
            with self.path.open("a", encoding="utf-8", newline="\n") as f:
                f.write(json.dumps({"type": "exchange", **ex.to_dict()},
                                   ensure_ascii=False) + "\n")
        return ex

    @classmethod
    def load(cls, path: str | Path) -> "Transcript":
        """读回记录。

        半截的行跳过但**记在 damaged 里**：用 JSONL 就是为了崩在半路也只丢
        最后一条，可丢了哪一条必须说出来 —— 这是要给人核实的记录，
        静默跳过等于悄悄少了一题。
        """
        t = cls(exchanges=[])
        t.path = Path(path)
        for lineno, line in enumerate(
                t.path.read_text(encoding="utf-8").splitlines(), 1):
            if not line.strip():
                continue
            try:
                d = json.loads(line)
            except ValueError:
                t.damaged.append(lineno)
                continue
            if d.get("type") == "header":
                t.session_id = d.get("session_id", t.session_id)
                t.student = d.get("student", "")
                t.scope = d.get("scope", "")
                t.started_at = d.get("started_at", "")
            elif d.get("type") == "exchange":
                t.exchanges.append(Exchange.from_dict(d))
        return t

    def scorecard(self) -> ScoreCard:
        """追问不计分 —— 它是用来定位欠缺的，不是用来再扣一次分的。"""
        card = ScoreCard()
        for ex in self.exchanges:
            if ex.withheld:
                card.withheld += 1
            if ex.is_probe:
                card.probes += 1
                continue
            card.earned += ex.earned
            card.possible += ex.possible
            card.ungradable += ex.ungradable
            a, b = card.by_topic.get(ex.topic, (0.0, 0.0))
            card.by_topic[ex.topic] = (a + ex.earned, b + ex.possible)
        card.weak_topics = sorted(
            t for t, (a, b) in card.by_topic.items() if b > 0 and a / b < 0.5)
        return card

    def report(self) -> str:
        """给人看的成绩单。"""
        card = self.scorecard()
        out = [f"对话记录 {self.session_id}" + (f"  学生：{self.student}" if self.student else ""),
               f"  范围：{self.scope}" if self.scope else "",
               f"  开始：{self.started_at}", ""]
        for i, ex in enumerate(self.exchanges, 1):
            tag = "（追问，不计分）" if ex.is_probe else ""
            out.append(f"{i}. [{ex.topic}] {ex.question}{tag}")
            out.append(f"   答：{ex.answer[:120]}")
            for j in ex.judgements:
                mark = {COVERED: "✓", MISSED: "✗", UNGRADABLE: "—"}[j.verdict]
                note = f"  （{j.note}）" if j.note else ""
                out.append(f"     {mark} {j.point.text[:44]}{note}")
            if not ex.is_probe:
                out.append(f"     本题 {ex.earned:g} / {ex.possible:g}")
            out.append("")
        if card.gradable:
            out.append(f"总分 {card.earned:g} / {card.possible:g}  得分率 {card.ratio:.0%}")
        else:
            out.append("无法判分：本次没有任何可判的要点（见下）")
        if card.ungradable:
            out.append(f"另有 {card.ungradable:g} 分的要点判不了（知识库没覆盖），"
                       f"**未计入分母**，不由学生承担")
        if card.withheld:
            out.append(f"其中 {card.withheld} 次系统因证据不足未给出参考答案")
        if card.weak_topics:
            out.append("薄弱知识点：" + "、".join(card.weak_topics))
        if card.probes:
            out.append(f"追问 {card.probes} 次（不计分）")
        if self.damaged:
            out.append(f"⚠ 记录第 {self.damaged} 行读不出来，这些问答已丢失，"
                       f"成绩单不含它们")
        return "\n".join(x for x in out if x is not None)


def main(argv: list[str] | None = None) -> int:
    import argparse

    ap = argparse.ArgumentParser(description="要点校验 / 对话记录成绩单")
    ap.add_argument("file", help="要点 json，或对话记录 jsonl")
    ap.add_argument("--validate", action="store_true", help="校验要点表（需 --graph）")
    ap.add_argument("--graph", default="cross-domain/graph.db")
    ap.add_argument("--report", action="store_true", help="打印对话记录成绩单")
    args = ap.parse_args(argv)

    if args.report:
        print(Transcript.load(args.file).report())
        return 0

    try:
        rubrics = load_rubrics(args.file)
    except RubricError as e:
        print(f"[要点契约不成立] {e}")
        return 2
    print(f"{len(rubrics)} 个知识点，"
          f"{sum(len(r.points) for r in rubrics.values())} 条要点")
    if not args.validate:
        return 0

    db = Path(args.graph)
    if not db.is_absolute():
        db = REPO / args.graph
    if not db.exists():
        print(f"[graph.db 不存在] {db}")
        return 2
    conn = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True)
    try:
        problems = validate(rubrics, conn)
    finally:
        conn.close()
    if not problems:
        print("校验通过")
        return 0
    print(f"{len(problems)} 个问题：")
    for p in problems:
        print(f"  - {p}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
