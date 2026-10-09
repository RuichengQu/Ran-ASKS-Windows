"""scope-guard — 对话范围控制 guard。

守卫目标：把一次对话限制在课程某个单元的知识范围内。

**为什么做成 guard 而不是提示词**：
    提示词「只回答第一章的内容」是一句请求，模型可以被绕过、也可能自己越界；
    范围一旦写进 guard，越界的那次 read/search 在工具管道里就被否决或过滤，
    与模型当时怎么想无关。这是 DSH「能力接缝」的用法：
    约束放在接缝上，不放在被约束者手里。

**范围怎么定义** —— 不列关键词，而是由图自己长出来：
    roots           课程单元点名的那几个 topic
    follow  (传递)  沿 `前置` 闭包 —— 前置知识天然在范围内，
                    因为学生答不出 A 时正要往 B 回溯；范围若不含前置，追问会被自己挡住
    attach  (一跳)  每个范围内节点的 `来源`（可引用的 raw）与 `涉及`（关键词实体）

    所以「范围」是一个可审计的节点集合：每个节点都带着「它为什么在范围里」。
    roots 里有节点在图中不存在 → 视为契约违约而非警告：
    静默缩小范围等于静默改变了考查内容。

三个执行点：
    on_pre_execute   点名读取类工具（read_section/read_raw/graph_neighbors…）
                     目标出界 → deny
    on_post_execute  检索类工具（graph_search/semantic_search/wiki_recall…）
                     结果里出界的行过滤掉 → replace（检索本身不该被否决，
                     否则学生一问宽泛问题就什么都查不到）
    classify         学生这句话是否在范围内（只查图的别名表，不调模型）

用法：
    scope = ScopeContract.load("path/to/unit.json")
    guard = ScopeGuard(scope)
    registry.on_pre_execute(guard.on_pre_execute)
    registry.on_post_execute(guard.on_post_execute)
"""
from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

from dsh.harness import PostToolDecision, PreToolDecision, ToolExecution, ToolExecutionResult

REPO = Path(__file__).resolve().parent.parent.parent

DEFAULT_GRAPH = "cross-domain/graph.db"
DEFAULT_DENY = "这个问题超出了本次的考查范围，我们先把本单元的内容过完。"

# 点名读取类工具 → 它用哪个参数指定目标节点
TARGET_ARG = {
    "read_section": "page",
    "read_raw": "locator",
    "graph_neighbors": "node",
    "graph_relations": "node",
    "graph_hub_of": "page",
    "hub_inspect": "hub",
}

# 检索类工具：结果过滤而非否决
SEARCH_TOOLS = {
    "graph_search", "semantic_search", "wiki_recall", "admin_recall", "node_resolve",
}

# JSON 信封里装结果列表的字段名（count 要跟着它走）
LIST_KEYS = ("nodes", "candidates", "results", "items", "hits")

# 一条结果里表示节点身份的键
PATH_KEYS = ("path", "node_path", "node_id", "page", "hub")


def _entry_path(item) -> str | None:
    """一条检索结果指向哪个节点；不是「一条结果」则返回 None。"""
    if not isinstance(item, dict):
        return None
    for k in PATH_KEYS:
        v = item.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip().strip("/")
    return None


class ScopeError(Exception):
    """范围契约本身不成立 —— roots 在图里找不到、图文件不存在等。"""


@dataclass(frozen=True)
class ScopeContract:
    """一次对话允许触及的知识范围，声明式。

    name         人类可读的名字，进日志用（如「数学物理方法 第一章」）
    roots        根节点：node path，或标题/别名（会先解析成 path）
    follow       沿这些谓词做传递闭包
    attach       对范围内每个节点再取一跳的这些谓词的宾语
    depth        follow 闭包的最大层数
    extra        额外显式放行的 node path
    deny_message 越界时对学生说的话
    """
    name: str
    roots: tuple[str, ...]
    graph: str = DEFAULT_GRAPH
    follow: tuple[str, ...] = ("前置",)
    attach: tuple[str, ...] = ("来源", "涉及")
    depth: int = 4
    extra: tuple[str, ...] = ()
    deny_message: str = DEFAULT_DENY

    @classmethod
    def load(cls, path: str | Path) -> "ScopeContract":
        return cls.from_dict(json.loads(Path(path).read_text(encoding="utf-8")))

    @classmethod
    def from_dict(cls, data: dict) -> "ScopeContract":
        if not data.get("name"):
            raise ScopeError("范围契约缺少 name")
        if not data.get("roots"):
            raise ScopeError("范围契约缺少 roots —— 不接受空范围（空范围等于不设防）")
        return cls(
            name=data["name"],
            roots=tuple(data["roots"]),
            graph=data.get("graph", DEFAULT_GRAPH),
            follow=tuple(data.get("follow", ("前置",))),
            attach=tuple(data.get("attach", ("来源", "涉及"))),
            depth=int(data.get("depth", 4)),
            extra=tuple(data.get("extra", ())),
            deny_message=data.get("deny_message", DEFAULT_DENY),
        )

    def to_dict(self) -> dict:
        return {"name": self.name, "roots": list(self.roots), "graph": self.graph,
                "follow": list(self.follow), "attach": list(self.attach),
                "depth": self.depth, "extra": list(self.extra),
                "deny_message": self.deny_message}


@dataclass
class ScopeResolution:
    """展开后的范围。每个节点都记着它为什么在里面 —— 范围必须可审计。"""
    contract: ScopeContract
    reasons: dict[str, str] = field(default_factory=dict)
    aliases: dict[str, str] = field(default_factory=dict)   # 别名/标题 → node path

    @property
    def nodes(self) -> set[str]:
        return set(self.reasons)

    def __contains__(self, node: str) -> bool:
        return node in self.reasons

    def explain(self, node: str) -> str:
        return self.reasons.get(node, "")


def _connect(graph: str) -> sqlite3.Connection:
    p = Path(graph)
    if not p.is_absolute():
        p = REPO / graph
    if not p.exists():
        raise ScopeError(f"graph.db 不存在: {p}")
    return sqlite3.connect(f"file:{p.as_posix()}?mode=ro", uri=True)


def _resolve_root(conn: sqlite3.Connection, name: str) -> str | None:
    """root 可以写 path，也可以写标题或别名。"""
    if conn.execute("SELECT 1 FROM nodes WHERE path=?", (name,)).fetchone():
        return name
    row = conn.execute("SELECT path FROM nodes WHERE title=?", (name,)).fetchone()
    if row:
        return row[0]
    row = conn.execute("SELECT node_path FROM aliases WHERE alias=?", (name,)).fetchone()
    return row[0] if row else None


def resolve(contract: ScopeContract, conn: sqlite3.Connection | None = None) -> ScopeResolution:
    """把契约展开成节点集合。roots 解析不到 → 抛 ScopeError。"""
    own = conn is None
    conn = conn or _connect(contract.graph)
    try:
        res = ScopeResolution(contract=contract)
        missing, frontier = [], []
        for r in contract.roots:
            path = _resolve_root(conn, r)
            if path is None:
                missing.append(r)
                continue
            res.reasons.setdefault(path, "root")
            frontier.append(path)
        if missing:
            raise ScopeError(
                f"范围契约「{contract.name}」的 roots 在图中找不到: {missing}。"
                f"不自动跳过 —— 静默缩小范围会静默改变考查内容。")

        # 1. follow 谓词的传递闭包
        for _ in range(contract.depth):
            nxt = []
            for node in frontier:
                for pred in contract.follow:
                    for (obj,) in conn.execute(
                            "SELECT object FROM edges WHERE subject=? AND predicate=?",
                            (node, pred)):
                        if obj not in res.reasons:
                            res.reasons[obj] = f"{pred} of {node}"
                            nxt.append(obj)
            frontier = nxt
            if not frontier:
                break

        # 2. extra 在 attach 之前并入：显式放行一个页面，就该同时放行它的
        #    来源与关键词，否则模型能看见这一页却读不了它引的原件。
        #    但 extra 不参与 follow 闭包 —— 它的语义是「只放行这一个」。
        for e in contract.extra:
            res.reasons.setdefault(e, "extra")

        # 3. attach 谓词各取一跳（不传递：来源的来源不该连带进来）
        for node in list(res.reasons):
            for pred in contract.attach:
                for (obj,) in conn.execute(
                        "SELECT object FROM edges WHERE subject=? AND predicate=?",
                        (node, pred)):
                    res.reasons.setdefault(obj, f"{pred} of {node}")

        # 4. 别名表：供 classify 判断学生这句话说的是不是范围内的东西
        if res.reasons:
            qs = ",".join("?" * len(res.reasons))
            args = tuple(res.reasons)
            for alias, path in conn.execute(
                    f"SELECT alias, node_path FROM aliases WHERE node_path IN ({qs})", args):
                res.aliases[alias] = path
            for path, title in conn.execute(
                    f"SELECT path, title FROM nodes WHERE path IN ({qs}) AND title IS NOT NULL",
                    args):
                res.aliases[title] = path
        return res
    finally:
        if own:
            conn.close()


@dataclass
class ScopeVerdict:
    """学生这句话在不在范围内。

    三态，不是两态：
        in       命中了范围内术语
        out      只命中范围外术语
        unknown  什么术语都没命中 —— 可能是「老师我不会」，
                 也可能是「帮我写首诗」。放行（inside=True），但如实标出，
                 让调用方自己决定要不要把话头引回题目。
                 把 unknown 混进 in 会让这层判定看起来比实际更强。
    """
    inside: bool
    verdict: str = "in"                                # in | out | unknown
    hit: list[str] = field(default_factory=list)       # 命中的范围内词
    outside: list[str] = field(default_factory=list)   # 命中的范围外词
    reason: str = ""


def _locator_node(value: str) -> str:
    """read_raw 的 locator 形如 path#L10-L20 / path#标题，取前面的 path。"""
    return value.split("#", 1)[0].strip().strip("/")


class ScopeGuard:
    """对话范围控制 guard。"""

    def __init__(self, contract: ScopeContract, conn: sqlite3.Connection | None = None,
                 outside_vocab: dict[str, str] | None = None):
        self.contract = contract
        self.resolution = resolve(contract, conn)
        # 范围外词表：让 classify 能给出「你问的是第三章的留数」这种具体理由
        self._outside = (outside_vocab if outside_vocab is not None
                         else self._load_outside(conn))
        self.denied: list[dict] = []
        self.filtered: list[dict] = []

    def _load_outside(self, conn: sqlite3.Connection | None) -> dict[str, str]:
        own = conn is None
        try:
            conn = conn or _connect(self.contract.graph)
        except ScopeError:
            return {}
        try:
            inside = self.resolution.nodes
            vocab: dict[str, str] = {}
            # 别名表之外还要收节点标题：关键词实体（留数、柯西定理…）多数只有标题没有别名，
            # 只读 aliases 会让范围外词表几乎是空的。
            for sql in ("SELECT alias, node_path FROM aliases",
                        "SELECT title, path FROM nodes WHERE title IS NOT NULL AND title<>''"):
                for name, path in conn.execute(sql):
                    if name and path not in inside and name not in self.resolution.aliases:
                        vocab.setdefault(name, path)
            return vocab
        finally:
            if own:
                conn.close()

    # ---------- 管道挂载点 ----------

    def on_pre_execute(self, exec_ctx: ToolExecution) -> PreToolDecision | None:
        """点名读取类工具：目标出界就否决。"""
        arg = TARGET_ARG.get(exec_ctx.name)
        if not arg:
            return None
        raw = (exec_ctx.arguments or {}).get(arg, "")
        if not isinstance(raw, str) or not raw.strip():
            return None
        node = _locator_node(raw)
        if node in self.resolution:
            return None
        self.denied.append({"tool": exec_ctx.name, "target": node})
        return PreToolDecision(
            kind="deny",
            reason=(f"{node} 不在范围「{self.contract.name}」内。"
                    f"本次对话只能使用该范围内的知识；"
                    f"对学生应答：{self.contract.deny_message}"))

    def _filter_json(self, obj):
        """递归过滤 JSON 结构，返回 (过滤后的对象, 丢弃条数)。

        ASKS 的检索工具返回的是 JSON 信封（graph_search 的 nodes、
        wiki_recall / node_resolve 的 candidates），不是一行一条。
        按行过滤会把 JSON 切坏，模型拿到的就是一段解析不了的文本。
        """
        if isinstance(obj, list):
            kept, dropped = [], 0
            for item in obj:
                p = _entry_path(item)
                if p is not None:
                    if p in self.resolution:
                        kept.append(item)
                    else:
                        dropped += 1
                    continue
                sub, d = self._filter_json(item)
                dropped += d
                kept.append(sub)
            return kept, dropped
        if isinstance(obj, dict):
            out, dropped = {}, 0
            for k, v in obj.items():
                sub, d = self._filter_json(v)
                dropped += d
                out[k] = sub
            # count 字段跟着它描述的那个列表走，否则信封自相矛盾
            for key in LIST_KEYS:
                if key in out and isinstance(out[key], list) and isinstance(out.get("count"), int):
                    out["count"] = len(out[key])
                    break
            # node_resolve 这类把结果提到顶层的信封：顶层节点出界就不能留
            top = out.get("node_id") or out.get("path")
            if isinstance(top, str) and top and top not in self.resolution:
                for k in ("node_id", "path", "title", "description"):
                    if k in out:
                        out[k] = ""
                out["decision"] = "out_of_scope"
                dropped += 1
            return out, dropped
        return obj, 0

    def on_post_execute(self, exec_ctx: ToolExecution,
                        result: ToolExecutionResult) -> PostToolDecision | None:
        """检索类工具：过滤掉范围外的结果。JSON 信封按结构过滤，其余按行。"""
        if exec_ctx.name not in SEARCH_TOOLS or result.is_error:
            return None
        try:
            parsed = json.loads(result.content)
        except (ValueError, TypeError):
            parsed = None

        if parsed is not None:
            filtered, dropped = self._filter_json(parsed)
            if not dropped:
                return None
            if isinstance(filtered, dict):
                filtered["scope_guard"] = {
                    "scope": self.contract.name, "dropped": dropped,
                    "note": "已滤除范围外结果；本次对话只能使用该范围内的知识"}
            body = json.dumps(filtered, ensure_ascii=False, indent=2)
        else:
            kept, dropped = [], 0
            for line in result.content.splitlines():
                cand = [p.strip("/") for p in re.findall(
                    r"[A-Za-z0-9_\-/]*[A-Za-z0-9_\-]/[A-Za-z0-9_\-一-鿿/]+", line)]
                if cand and not any(c in self.resolution for c in cand):
                    dropped += 1
                    continue
                kept.append(line)
            if not dropped:
                return None
            body = "\n".join(kept)
            if not body.strip():
                body = f"[范围内无匹配] 范围「{self.contract.name}」内没有与此查询相关的内容。"
            body += f"\n[scope-guard] 已滤除 {dropped} 条范围外结果。"

        self.filtered.append({"tool": exec_ctx.name, "dropped": dropped})
        return PostToolDecision(
            kind="replace",
            replacement=ToolExecutionResult(content=body),
            feedback=f"scope-guard 滤除 {dropped} 条范围外结果")

    # ---------- 学生消息判定 ----------

    def classify(self, text: str) -> ScopeVerdict:
        """学生这句话在不在范围内。只查别名表，不调模型。

        判定偏宽松：只有当句子命中了范围外的具体术语、且没命中任何范围内术语，
        才判出界。宁可放过，也不要把学生一句正常的提问挡掉 ——
        真正的硬约束在 pre_execute，这里只是一层早期提示。
        """
        if not text or not text.strip():
            return ScopeVerdict(inside=True, verdict="unknown", reason="空消息")
        hit = sorted({a for a in self.resolution.aliases if len(a) >= 2 and a in text},
                     key=len, reverse=True)
        out = sorted({a for a in self._outside if len(a) >= 3 and a in text},
                     key=len, reverse=True)
        # 范围内的长词包住了范围外的短词时，不算出界
        out = [o for o in out if not any(o in h for h in hit)]
        if hit:
            return ScopeVerdict(inside=True, verdict="in", hit=hit[:5], outside=out[:5],
                                reason=f"命中范围内术语: {hit[:3]}")
        if out:
            return ScopeVerdict(inside=False, verdict="out", outside=out[:5],
                                reason=f"只命中范围外术语: {out[:3]}")
        return ScopeVerdict(inside=True, verdict="unknown",
                            reason="未命中任何术语：无法判断，按在范围内放行")

    # ---------- 审计 ----------

    def report(self) -> dict:
        """这次对话的范围执行情况，进 session log / 供老师核实。"""
        return {
            "scope": self.contract.name,
            "contract": self.contract.to_dict(),
            "node_count": len(self.resolution.nodes),
            "denied": list(self.denied),
            "filtered": list(self.filtered),
        }

    def describe(self) -> str:
        """范围的人类可读说明，可直接放进系统提示 —— guard 执行，提示解释。"""
        roots = [n for n, why in self.resolution.reasons.items() if why == "root"]
        pre = [n for n, why in self.resolution.reasons.items() if why.startswith("前置")]
        return (f"本次对话范围：{self.contract.name}\n"
                f"  主知识点 {len(roots)} 个，连带前置 {len(pre)} 个，"
                f"范围内节点共 {len(self.resolution.nodes)} 个。\n"
                f"  超出范围的内容不要回答，对学生说：{self.contract.deny_message}")


def main(argv: list[str] | None = None) -> int:
    """命令行：展开一个范围契约，或拿它判一句话。

        python -m dsh.guards.scope_guard unit.json
        python -m dsh.guards.scope_guard unit.json --ask "留数定理怎么用"
    """
    import argparse

    ap = argparse.ArgumentParser(description="展开并检查一个对话范围契约")
    ap.add_argument("contract", help="范围契约 json 路径")
    ap.add_argument("--ask", action="append", default=[],
                    help="拿这个范围判一句话，可重复")
    ap.add_argument("--list-nodes", action="store_true", help="列出范围内全部节点及纳入理由")
    args = ap.parse_args(argv)

    try:
        guard = ScopeGuard(ScopeContract.load(args.contract))
    except ScopeError as e:
        print(f"[范围契约不成立] {e}")
        return 2

    print(guard.describe())
    if args.list_nodes:
        for node, why in sorted(guard.resolution.reasons.items(), key=lambda kv: kv[1]):
            print(f"  {why:<48} {node}")
    for text in args.ask:
        v = guard.classify(text)
        mark = {"in": "  在范围内", "out": "✗ 出  界 ", "unknown": "?  判不了 "}[v.verdict]
        print(f"{mark}  {text}\n             {v.reason}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
