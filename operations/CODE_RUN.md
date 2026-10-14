# 科研代码固定配方运行与证据契约

## 入口与控制权

preview 功能 `science.code_run`，能力 `code-run`，入口 `wg.py code-run`。
宿主显式选择计算后，shared 程序执行固定配方；来源文本、目录声明和 query 都不能触发运行。
首版只支持 `tt-gradient-check-v1`，不是任意脚本执行器、通用科学验证器或安全沙箱。
不调用 API/DSH/模型/网络，不支持 private、shell、自定义代码路径或远程执行。

```bash
python3 .scripts/route.py --capability code-run --capability-profile general
python3 .scripts/wg.py code-run tt-gradient-check --project tensorizing-neural-networks --seed 41
python3 .scripts/wg.py code-run inspect --project tensorizing-neural-networks --run-id <run-id>
```

## Preflight 与预算

要求现有公共 research 项目、真实 outputs 目录和明确的 local policy/backend。
TT 前向和梯度源码须与工作目录的已登记哈希一致；preflight 不刷新绑定。
固定输入：3 个核，形状 `(1,2,3,2)`、`(2,3,2,2)`、`(2,2,2,1)`，输入/输出维度均为 12。
44 个核参数、12 个输入和 12 个 bias 全部做中心有限差分，另检查 12 个前向输出。
seed 为 uint32；timeout 为 1–120 秒，默认 60 秒；使用少量单线程 CPU 数值操作。
输入形状与源码快照在 worker 启动前检查，输入哈希在执行前后核对。
子进程超时是资源控制，不是恶意代码隔离；只执行已明确选择的受控本地配方。

## 数学检查的范围

目标函数是诊断用 `0.5 * sum((W @ x + b - target)^2)`，不是论文分类实验的损失函数。
梯度实现用前向收缩状态的反向伴随；独立参考矩阵按元素核乘积构造，有限差分不调用同一前向实现。
epsilon=1e-6，atol=1e-7，rtol=1e-5；逐元素检查
`abs(actual-reference) <= atol + rtol*abs(reference)`，不以平均误差掩盖局部错误。
局部通过支持这一组输入、代码版本、依赖和阈值下的计算一致性，不证明普遍科学正确性或 MNIST 复现。

## 输出与失败

只在 `projects/<project>/outputs/code-runs/<new-run-id>/` 创建新目录，不覆盖。
保存 spec.json、inputs.npz、源码与运行器 snapshots、stdout/stderr、report.md；
执行完成时再保存 results.npz、metrics.json。metadata.json 使用 `scientific-run-v1`，记录：

- 实际运行身份、起止时间、命令、配方、参数、Python/NumPy 版本。
- 运行前输入哈希、源码哈希与快照、全部列出的产物哈希。
- 单项指标、实际通过/失败状态和错误；无实际执行则不伪造验证结果。
- `scientific_status: local_check_not_paper_reproduction`。

passed/failed 表示配方完成后的检查结果；error/timeout 表示未完成，保留诊断证据，CLI 返回非零。
preflight 失败不启动 worker；执行失败不改 Raw、Wiki、graph.db 或代码元图。
源码快照用于追溯，不是新事实源；正式知识发布另走受管摄入。

## 只读查询与失效

`inspect` 只核对目录身份、输入/spec、源码快照、产物与指标一致性，不导入计算模块或重新运行。
单文件上限 16 MiB，总产物上限 64 MiB；回执 JSON 上限 128 KiB；拒绝路径穿越与软链接。
当前源码或 Python/NumPy 版本改变后报告 stale；旧快照仍可保留，但不能当作当前版本的验证。
哈希完整性不是独立的计算或科学认证，运行结果保留其本地证据身份。

宿主明确审查后可在工作元图登记 kind=run、metadata.json 的 SHA-256 和固定 JSON pointer。
`code-library read` 校验底层产物和当前版本，再返回 local_run_record；来源列表仍不把它冒充 Raw。
查询不生成运行目录、不更新哈希、不执行代码，也不会自动登记下一次运行。

## 验证

`test_code_run.py` 覆盖实际执行、固定输入重现、失败/超时记录、产物篡改、源码失配、路径隔离、
只读 query、证据登记与检索。数值核心另由 `computing/code-library/tests/test_tensor_train.py` 多形状检查。
