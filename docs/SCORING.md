# conversation-scoring — 对话打分与可核实记录

`dsh/scoring.py`

给学生与系统的一次对话打分，并留下老师可以逐条核实的记录。

与 [scope-guard](SCOPE_GUARD.md) **相互独立**：范围控制管「能聊什么」，
这里管「聊得怎么样」。两者不互相 import，可以单独使用。

## 三个设计决定

### 1. 逐条要点判，不整体判对错

整体判分实测高报 25%（云端 judge 20%，本地 25%；两者都没有漏判，
只会把不对的判成对）。逐条判还顺带可解释 —— 能告诉学生
「你讲到了 1、3，漏了 2、4」，而不是丢给他一个分数。

也不是关键词匹配：

| 学生的回答 | 关键词匹配 | 逐条判 |
| --- | --- | --- |
| 「两点镜像于横轴」 | 漏判（关键词全不中） | ✓ 关于实轴对称 |
| 「z 和 z\* 不是关于实轴对称的」 | 误判为对（关键词全中） | ✗ |

### 2. 判定是三态，不是两态

```
covered     讲到了
missed      没讲到
ungradable  判不了
```

ASKS 本身会审查答案：`answer_judge.py` 的生成规则是「只使用检索内容中的
信息回答，不使用自身知识补充」「仅当检索内容完全不包含相关信息时，才回答
证据不足」。所以系统有可能**根本没给出参考**。这时候学生不该被扣分 ——
那是知识库没覆盖，不是他不会。

**ungradable 不进分母。** 把它并进 `missed`，等于拿知识库的洞去扣学生的分。

判定器自己抛异常（模型没响应、超时）同样记 `ungradable`，不记 `missed`：
判不了和答错是两件事。

分母为 0 时 `ScoreCard.gradable` 为 `False`，成绩单直接写「无法判分」，
而不是谎称考了 0 分。

### 3. 要点必须可追溯到图里的节点

`prerequisites` 在 ASKS 里是受验证的图契约而不是展示字段；要点同理。
每条要点记着它出自哪个节点，`validate()` 核对那些节点真的存在。

```json
{
  "teaching/wiki/topics/complex-conjugate": {
    "title": "共轭复数",
    "reviewed_by": "任课老师",
    "points": [
      {"text": "共轭复数在复平面上关于实轴对称。", "weight": 1.0,
       "source": "teaching/wiki/topics/complex-conjugate"}
    ]
  }
}
```

要点是教学判断，最终该由任课老师审定；但「出自哪里」必须是机械可查的。
`reviewed_by` 为空时 `validate()` 会报出来 —— 模型起草的草稿不能直接拿去考人。

## 判定器是注入的

```python
from dsh.scoring import load_rubrics, grade, Transcript

rubrics = load_rubrics("rubrics.json")
r = rubrics["teaching/wiki/topics/complex-conjugate"]

def judge(point: str, answer: str) -> bool:
    ...                      # 用什么模型、甚至用不用模型，由调用方决定

t = Transcript(student="张三", scope="第一章").open("transcripts/zhangsan.jsonl")
t.append(grade(r, student_answer, judge, question="共轭复数有哪些性质？"))
print(t.report())
```

ASKS 不替使用者决定判定器。`grade()` 只负责：逐条调、三态归并、
异常归到 `ungradable`、按权重算分。

`points=` 只传本次抽中的要点时，就只按这些算分 —— 随机抽要点可以防背答案，
而且更公平：学生不会因为没提到「没问的东西」而失分。

## 记录

一次对话一个 JSONL 文件，**append-only，每条立刻落盘**：

```
{"type":"header","session_id":"…","student":"张三","scope":"第一章",…}
{"type":"exchange","topic":"…","question":"…","answer":"…","judgements":[…]}
```

用 JSONL 而不是 json，是为了中途崩了只丢最后一条。读回时半截的行会跳过，
但**行号记在 `Transcript.damaged` 里并打进成绩单** —— 这是要给人核实的记录，
静默跳过等于悄悄少了一题。

`is_probe=True` 的追问不计分：追问是用来定位欠缺的，不是再扣一次分。
`withheld=True` 记下系统自己因证据不足没给参考答案的次数。

## 命令行

```bash
python -m dsh.scoring rubrics.json                  # 看规模
python -m dsh.scoring rubrics.json --validate       # 对着图校验
python -m dsh.scoring transcripts/zhangsan.jsonl --report
```

`--validate` 有问题时退出码 1，契约本身不成立时退出码 2。

## 测试

```
python dsh/test_scoring.py      # 21 个用例
```

不依赖任何课程数据，也不调模型 —— 判定器在测试里是假的。
