# HypoWeaver vs. Agent Laboratory：system-comparison-v2 预注册说明

## 1. 协议边界

`system-comparison-v2` 是独立于 `enterprise-panel-v1` 的两系统比较协议。旧协议、旧的 46-call 预算、旧 Case 002 结果和一次性锁均保持原样，不能把历史运行重新标成 v2。

本轮估计对象是“冻结系统包在给定输入上的科研任务能力”，机器字段固定为 `comparison_estimand=system_package_capability`，不是 HypoWeaver 某个单独模块的因果效果。模型控制固定为 `model_control=native_role_routing_disclosed_not_equalized`：允许两套原生系统按各自角色路由模型，但必须逐项披露，不能把结果解释成纯工作流因果效应。

Case002 v2 的模型路由冻结如下：

| 系统 | 默认模型 | 角色覆盖 |
|---|---|---|
| Agent Laboratory | `qwen3.7-plus` | 全部角色均为 `qwen3.7-plus` |
| HypoWeaver | `qwen3.7-plus` | `reviewer`、`scientific_audit`、`design_retry`、`writer_escalation` 使用 `qwen3.7-max` |

公开 runtime envelope、preflight 与 suite manifest 必须同时写入 `default_model`、`comparison_estimand`、`model_control` 和 `system_model_routing`，并把它们纳入 configuration/protocol SHA256。旧读取方使用的 `model` 字段继续保留为 `default_model` 的兼容别名。若两个系统实际使用的模型路由不同，只能比较完整系统包；只有在所有模型调用都冻结为同一模型时，才可进一步讨论工作流差异。

系统均只读取语义等价的可见输入和同一数据资产。因为 HypoWeaver 接收结构化 JSON、Agent Laboratory 接收薄适配器渲染的文件，协议同时冻结：

- `semantic_input_sha256`：共同语义合同；
- `system_visible_input_sha256`：两个系统各自真正读取的渲染版本；
- `data_sha256`：共同数据资产；
- `hidden_reference_sha256`：只在两系统输出封存后进入评测。

## 2. 固定案例分组

协议冻结前必须把所有案例分入以下三组，之后不得根据结果移动案例：

- `dev`：允许调试和重复运行，不进入任何科研能力主分数。Case 002 的 discovery-blind 与 reproduction-aligned 两个视图均属于这一组。
- `validation`：只用于在正式批次前校验输入转换、适配器和评分器。结果可以报告，但不进入主分数；一旦用于校准，就不能再改称 holdout。
- `quasi_holdout`：本地候选案例并非真正私有，因此只称“准留出”。必须使用 `discovery_blind` 视图，一次性运行，并全部进入预注册主分数。

每个准留出案例的系统先后顺序在协议中冻结；当准留出案例不少于两个时，两种顺序都必须出现，以降低供应商时段和机器状态造成的顺序偏差。所有准留出系统输出应先封存，再打开任何隐藏参考或汇总中间得分。

## 3. 预算和技术重试

两系统获得相同的外部调用包络：

- 每系统最多 `40` 次真实 provider attempt；成功和失败 attempt 都计费、计预算并保存 receipt；
- 每个逻辑请求最多 `3` 次 provider attempt；
- HypoWeaver 另外最多 `20` 个不同 `logical_call_id`；Agent Laboratory 不设置跨架构的逻辑调用数上限，只冻结其原生调度器；
- Case002 r3 固定为“给定输入后的方法选择→实验→写作”赛道；文献收集不是硬门，Agent Laboratory 的 `num_papers_lit_review=0`，适配器在进入上游调度前直接标记该 subtask 完成，不消耗模型或文献工具调用；
- Agent Laboratory 其余调度固定为 `max_steps=5`、`mlesolver_max_steps=1`、`papersolver_max_steps=0`；
- 两系统均单独报告 input/output token、模型等待时间、总墙钟时间和失败类型。

技术重试只允许 DNS、TLS、连接/读取超时、代理、连接重置、HTTP 429 和 HTTP 5xx。重试必须复用同一 `logical_call_id`、同一请求 SHA256、同一模型和同一参数；不得借重试改 prompt、上下文、数据或研究方案。Schema repair 和内容 repair 必须另行标识，不能伪装成网络重试。

`provider_transport` 排除只能由明确的 `model_transport_exhausted`，或最终同一 `logical_call_id` 的冻结传输 receipts 证明。receipt 路径必须到达第 3 次尝试，保持同一 provider、模型和请求 SHA256，且三次均为明确传输错误；不能凭笼统的 `model_technical_failure`、自由文本或“最后一条 receipt 失败”排除。HTTP 400、响应 Schema/内容合同失败（如 `response_contract`）及其他非传输错误属于系统能力失败。Case002 r6 的旧 `model_technical_failure` 只作只读兼容：其最后一个 logical call 有 3 条同参 `RemoteDisconnected` receipts，因此仍可确定为供应商传输耗尽；不得由此恢复对其他笼统旧错误的宽松分类。

v2 的调用数由 receipt 确定性派生：

```text
provider_attempts = receipt 数量
logical_calls = distinct(logical_call_id)
technical_retry_attempts = attempt_type=transport_retry
```

自报的 `llm_calls` 只能与 receipt 数量核对，不能作为真值。重试序号必须从 1 连续递增，且不超过 3；发生技术重试却无法证明请求 SHA256 相同的运行，不满足 v2 预算合规性。

准留出案例不允许在一次 Run 终止后重新开新 Run。Run 内的同参技术重试是预注册流程的一部分。供应商传输或 benchmark 基础设施故障应标为 `excluded_infrastructure_failure`，不记作科学能力 0 分；模型不能规划、代码生成失败、超过冻结的 cell 端到端时限、其他研究预算耗尽或错误结论等系统自身失败属于 `system_capability`，仍进入科研能力评分。

墙钟预算分三层记录：模型账本只报告 provider 请求延迟累计值；冻结合同的 `max_wall_time_seconds=1800` 是每个统计实现阶段的时限；benchmark 的 `max_end_to_end_wall_time_seconds=2700` 是从系统进程启动到终止的整格时限。`max_executions=12` 表示每个统计实现的冻结 DAG step 槽位，不等于物理拟合次数，也不等于 LLM 调用数；置换重复次数必须另行报告。

Case002 v2 运行前必须先固定 `SUITE_ID` 和 `OUTPUT_ROOT`，并使 Research Engine 绑定该 suite 唯一的 registry：

```bash
HYPOWEAVER_DATASET_REGISTRY_PATH="$OUTPUT_ROOT/suites/$SUITE_ID/hypoweaver-datasets.json" PYTHONPATH=backend/src python3.11 -m uvicorn hypoweaver.research_api:app --host 127.0.0.1 --port 9000
PYTHONPATH=backend/src python3.11 -m hypoweaver.case002_v2_dev_runner --output-root "$OUTPUT_ROOT" --suite-id "$SUITE_ID" --preflight-only
```

`/v1/health` 必须同时匹配本地完整 runtime identity 与上述 registry 路径的 SHA256；只记录哈希，不记录 registry 路径或 Research Engine token。预检与正式运行必须复用同一 `SUITE_ID`。

## 4. 科研能力评分

每个可评分案例、每个系统按 100 分量表记录六个维度：

| 维度 | 分值 |
|---|---:|
| 方法选择与研究设计 | 20 |
| 执行正确性与可复现性 | 20 |
| 识别与诊断 | 20 |
| 稳健性、证伪与敏感性分析 | 15 |
| Claim 校准与证据可追溯性 | 15 |
| 报告质量与失败披露 | 10 |

评测必须保存每个维度的证据位置和诊断，不以“是否复现作者显著结论”代替科学正确性。隐藏论文和作者结果只是核验材料，不是绝对真值。

五次模型盲评用于估计同一案例评分的不稳定性，报告中位数和四分位距；它们不是五个独立科研样本。统计单位始终是独立案例。主汇总只使用 `quasi_holdout`，按案例做配对差值；`dev` 和 `validation` 分开展示。案例数较少时只称“多案例试点评测”，不得据此宣称普适优越性。

除科研分数外，必须并列报告：

- 工作流完成率；
- 科学/系统能力失败率；
- 基础设施排除率；
- provider attempts、logical calls、token、耗时；
- 质量—成本 Pareto 关系。

不得把上述资源指标事后合成一个有利于任一系统的新总分。

## 5. 当前实现状态和正式运行门槛

代码入口 `hypoweaver.system_comparison_v2` 已提供：冻结协议与运行配置、三组案例约束、40/20 预算模型、Case002 r3 的 5/0/0 Agent Laboratory 调度（`max_steps / required literature papers / paper refinement steps`，MLE 步数仍为 1）、receipt 派生的资源输出，以及六维评分 Schema。

这一步尚未修改 `enterprise-panel-v1` 的正式 runner，也尚未把 HypoWeaver 当前运行时的 `ModelCallBudget` 从“provider attempt 上限 20”拆成“provider attempt 上限 40 + logical call 上限 20”。因此，当前历史 Run 可以被 v2 Schema审计，但不能因为“实际调用少于 40”就声称已经获得并验证了相同的 40-attempt 外部包络。

正式 v2 付费运行前还必须完成并测试：

1. v2 runner 在 preflight 中验证两系统外部上限均为 40，HypoWeaver 不同逻辑 ID 不超过 20；
2. runner 传入并核验 Case002 r3 的 5/0/0 调度及 `given_input_method_experiment_write` 赛道，不沿用 v1 的 `max_steps=3`；
3. 两系统及所有准留出案例输出全部封存后，评测器才读取隐藏参考；
4. 预先冻结具体案例清单、系统顺序、源码/配置哈希和评分 rubric。

Agent Laboratory v2 适配器已经在每条成功或失败 receipt 中记录同一请求的
`input_sha256`、连续 `attempt_index` 和 `attempt_type`；该项仍须由正式 runner
在运行前后校验，不能只信任适配器自报。

在以上门槛全部通过之前，v2 状态应标记为 `protocol_ready_runtime_pending`，不能启动正式准留出比较。
