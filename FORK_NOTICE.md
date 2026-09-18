# 关于这个仓库 / About this repository

这是 **Ran-ASKS 的一个修改版本**，唯一目的是让它能在 **Windows** 上运行。
上游项目的科研方法、架构与全部功能均归原作者所有。

This is a **modified distribution of Ran-ASKS**, made for one purpose: to let it
run on **Windows**. The research method, architecture, and all functionality
belong to the original authors.

## 上游项目 / Upstream

- **原仓库 / Original repository:** <https://github.com/ranshiju/Ran-ASKS>
- **论文 / Paper:** Shi-Ju Ran, Kun Zhang, Xi Wu, Liu-Si Yang, and Wen-Jun Li,
  “LLMs Interpret, Embeddings Organize, Graphs Emerge: Agent-Driven Compilation
  of Scientific Knowledge,” [arXiv:2608.29612](https://arxiv.org/abs/2608.29612) (2026).
- **原作者联系方式 / Original contact:** [sjran@cnu.edu.cn](mailto:sjran@cnu.edu.cn)
- **分叉基线 / Fork baseline:** upstream `main`, v0.6.0

引用本方法、软件或论文产物时，请引用**上游**论文与仓库，而不是这个移植版本。
When citing the method, software, or paper artifacts, cite the **upstream**
paper and repository, not this port.

## 许可 / License

原始许可 **完整保留且未作修改**：
The original license is **retained unchanged**:

> PolyForm Noncommercial License 1.0.0 — Required Notice: Copyright (c) 2026 Shi-Ju Ran.

本移植版本同样依据该许可分发，因此**仅限非商业用途**（包括教育机构与公共研究
组织）。商业使用需要 Shi-Ju Ran 另行书面授权 —— 这个仓库无权授予任何商业许可。

This port is distributed under the same license and is therefore
**noncommercial only**. Commercial use requires a separate written license from
Shi-Ju Ran; this repository grants no commercial rights whatsoever.

冻结的论文产物（`paper-artifacts/`）及其 `CC BY-NC 4.0` / `CC BY 4.0` 数据许可
同样原样保留，**字节未变**，校验和可独立验证。
The frozen paper artifacts under `paper-artifacts/` and their `CC BY-NC 4.0` /
`CC BY 4.0` data licenses are likewise retained **byte-for-byte**; their
checksums verify independently.

## 这个分叉改了什么 / What this fork changes

只改跨平台可移植性，**不改科研方法、数据契约或任何行为语义**。
Portability only — no change to the method, the data contracts, or any
behavioural semantics.

| 领域 / Area | 改动 / Change |
| --- | --- |
| 文件锁 | `fcntl.flock` → Windows 用 `msvcrt.locking`，语义一致 |
| 换行符 | 所有文本写入固定为 LF，使内容哈希跨平台一致 |
| 仓库相对路径 | 统一以 POSIX 斜杠写出（`as_posix()`），不再随平台变反斜杠 |
| 子进程 | 用 `sys.executable` 取代硬编码 `python3`；强制 UTF-8 编解码 |
| macOS 专用命令 | `textutil`/`trash`/`open`/`find`/`curl` 均有跨平台等价实现 |
| SQLite 句柄 | 显式关闭，使 Windows 能替换与删除数据库文件 |
| `signal.SIGALRM` | Windows 上按耗时判定超时（POSIX 分支行为不变） |
| 目录 `fsync` | Windows 上跳过（`os.replace` 本身原子） |
| 新增 | `requirements.txt`、`docs/WINDOWS.md`、`.scripts/read_section.py`、`.scripts/platform_compat.py` |

完整的平台行为对照表见 [`docs/WINDOWS.md`](docs/WINDOWS.md)。
See [`docs/WINDOWS.md`](docs/WINDOWS.md) for the full platform behaviour table.

## 测试状态 / Test status

在 Windows 11 + Python 3.14 上，66 个测试文件中 **63 个通过**。
另外 3 个依赖未随公开仓库发布的私有内容（`.gitignore` 排除了 `projects/**`
与 `memory/**`），在 macOS 上全新克隆同样失败 —— 与平台无关：

On Windows 11 with Python 3.14, **63 of 66** test files pass. The other three
depend on private content that the public repository does not ship, and fail
the same way on a fresh macOS clone:

- `.scripts/test_playbook_dispatch.py` — 需要 `memory/playbooks/index.md`
- `.scripts/test_workspace_state.py` — 需要 `projects/_templates/research/`
- `paper-artifacts/v0.2.1/physh/frozen-source/test_run.py` — 需要 `projects/` 目录存在

两个冻结论文产物的校验和均通过验证。
Both frozen paper artifacts verify their checksums.
