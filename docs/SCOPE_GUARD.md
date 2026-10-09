# scope-guard — 对话范围控制

`dsh/guards/scope_guard.py`

把一次对话能触及的知识限制在一个声明好的范围内，由工具管道强制。

## 为什么不是提示词

提示词「只回答第一章的内容」是一句请求。模型可能被学生绕过，也可能自己
越界检索出第三章的页面再据此作答。范围一旦写进 guard，越界的那次
`read_raw` / `read_section` 在 `tools/pre-execute` 就被否决，与模型当时
怎么想无关。这是 DSH 能力接缝的常规用法：约束放在接缝上，不放在被约束者
手里。

提示词仍然有用，但只负责**解释**边界（`ScopeGuard.describe()` 可直接拼进
系统提示），省掉模型无谓的越界尝试；**执行**始终在管道。

## 范围怎么定义

不列关键词，由图自己长出来：

```json
{
  "name": "数学物理方法 第一章 复变函数基础",
  "graph": "cross-domain/graph.db",
  "roots": ["teaching/wiki/topics/modulus-argument", "..."],
  "follow": ["前置"],
  "attach": ["来源", "涉及"],
  "depth": 4,
  "deny_message": "这超出了第一章的范围，我们先把第一章过完。"
}
```

| 字段 | 作用 |
| --- | --- |
| `roots` | 范围点名的节点。可写 node path，也可写标题或别名 |
| `follow` | 沿这些谓词做**传递闭包** |
| `attach` | 对范围内每个节点再取**一跳**的这些谓词的宾语 |
| `depth` | `follow` 闭包的最大层数 |
| `extra` | 额外显式放行的 node path |

`follow: ["前置"]` 是这套设计的要点：**前置知识天然在范围内**。学生答不出
A 时正要回溯到 A 的前置 B，范围若不含前置，追问会被范围自己挡住。
`attach: ["来源", "涉及"]` 让范围内 topic 的可引用 raw 与关键词实体一并
放行，但不传递 —— 来源的来源不该连带进来。

展开结果是一个**可审计**的节点集合：每个节点都记着它为什么在里面。

```
本次对话范围：数学物理方法 第一章 复变函数基础
  主知识点 7 个，连带前置 4 个，范围内节点共 45 个。
```

```python
res.explain("teaching/wiki/topics/complex-plane")
# '前置 of teaching/wiki/topics/modulus-argument'
```

### roots 解析不到 = 契约违约

`roots` 里有节点在图中找不到，`resolve()` 抛 `ScopeError`，不跳过。
静默缩小范围等于静默改变考查内容 —— 老师以为在考 7 个知识点，实际只考了 6 个，
而且没有任何地方会说出来。

## 三个执行点

| 挂载点 | 作用于 | 行为 |
| --- | --- | --- |
| `on_pre_execute` | `read_section` `read_raw` `graph_neighbors` `graph_relations` `graph_hub_of` `hub_inspect` | 目标出界 → `deny`，工具正文根本不执行 |
| `on_post_execute` | `graph_search` `semantic_search` `wiki_recall` `admin_recall` `node_resolve` | 结果逐行过滤 → `replace` |
| `classify(text)` | 学生消息 | 三态判定，不调模型 |

检索类**过滤**而不**否决**：学生一问宽泛问题，检索本来就会同时命中范围内外；
否决会让他什么都查不到。全部被滤掉时返回一句可读的「范围内无匹配」，
而不是一个空串。

`classify()` 只查图的别名表与节点标题，是确定性的、零成本的：

| 结果 | 含义 |
| --- | --- |
| `in` | 命中范围内术语 |
| `out` | 只命中范围外术语 |
| `unknown` | 什么都没命中 —— 放行，但如实标出 |

`unknown` 单独成一态，而不是并进 `in`：「老师我不会」和「帮我写首诗」都落在
这里，调用方要自己决定把话头怎么引回去。把 `unknown` 混进 `in` 会让这层
判定看起来比它实际上强。真正的硬约束在 `pre_execute`，`classify` 只是一层
早期提示。

## 挂载

```python
from dsh.agent_loop import AgentLoop

loop = AgentLoop(mode="api", scope="path/to/unit.json")
```

`scope` 接受 `ScopeContract`、`ScopeGuard`、`dict` 或 json 文件路径。
**不传 = 不限范围，行为与以前完全一致**；`AgentLoop` 只在传了 `scope` 时
才挂这两个 hook。

手工挂载：

```python
guard = ScopeGuard(ScopeContract.load("unit.json"))
registry.on_pre_execute(guard.on_pre_execute)
registry.on_post_execute(guard.on_post_execute)
```

## 审计

`guard.report()` 给出这次对话的范围执行情况 —— 范围契约本身、范围内节点数、
每一次被否决的读取、每一次被过滤的检索。被否决的调用同样进 session log
（`tool/result` 事件带 `denied` 字段），所以「学生问过什么、什么被挡了」
是可以事后核实的，符合 DSH 的 *model-visible means logged* 不变量。

## 测试

```
python dsh/test_scope_guard.py      # 26 个用例
```

全部用临时合成图，不依赖任何具体领域数据 —— 在空仓库上也能跑。

## 命令行

```bash
python -m dsh.guards.scope_guard unit.json
python -m dsh.guards.scope_guard unit.json --list-nodes
python -m dsh.guards.scope_guard unit.json --ask "留数定理怎么用"
```

契约不成立（例如 roots 解析不到）时退出码 2 并打印原因。
样板见 `templates/scope/unit.example.json`。
