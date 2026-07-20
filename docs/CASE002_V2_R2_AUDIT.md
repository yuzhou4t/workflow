# Case002 v2 r2 四格开发集审计

## 结论

`case002-v2-dev-20260719-r2` 是一次基准设施有效的开发校准运行，但不构成完整的系统科研能力对比。只有 HypoWeaver 的 `discovery_blind` 格完整产生了可执行、可复算、可审计的科学终态；两个 Agent Laboratory 格都不可进入科研质量评分，HypoWeaver 的 `reproduction_aligned` 格则被 H2 规则误杀。

- 分类：`development_calibration_only`
- 独立案例数：1
- 协议 SHA256：`b2b2099b227eaf91e3fabd1a01ecba389e99150a62dd43b57fb43e98bffa1f0c`
- 隐藏参考：全部拒绝访问
- 原始总结：`benchmark-results-v2-dev/suites/case002-v2-dev-20260719-r2/suite_summary.json`

## 四格终态

| 输入视图 | 系统 | 原生终态 | 后验可比性 | 耗时 | 调用 |
|---|---|---|---|---:|---|
| discovery_blind | Agent Laboratory | `system_capability` | 排除：文献能力契约错配 | 58.47 s | 6 logical / 6 provider，无技术失败 |
| discovery_blind | HypoWeaver | `none` | 可审计开发格 | 279.42 s | 7 / 7，无技术失败 |
| reproduction_aligned | HypoWeaver | `system_capability` | 不可比：H2 规则误杀 | 228.24 s | 5 / 5，无技术失败 |
| reproduction_aligned | Agent Laboratory | `provider_transport` | 按协议排除 | 614.83 s | 23 logical / 26 provider，4 次技术失败 |

### Agent discovery 不是科研零分

Agent Laboratory 连续五轮只发出 `SUMMARY` 命令，没有进入 `FULL_TEXT` 或 `ADD_PAPER`，最终触发 `Max tries during phase: Literature Review`。但该视图的冻结证据只有政策原文，而运行契约强制要求收录 1 篇论文。因此它反映“封闭工作流适配”和“基准契约”的混合问题，不能进入科研能力主评分。

### Agent aligned 是供应商传输终止

文献、计划和数据准备共完成 22 个逻辑调用；第 23 个逻辑调用在实验阶段连续三次 `RemoteDisconnected`，三次请求哈希一致。它没有估计结果、结果解释或论文，必须排除。

## HypoWeaver discovery 的科学结果

系统自主路由至 `policy_causal`，完成基准 DID、固定政策前分组、稳定分组子样本、替代结果、事件研究、伪时点、置换和独立复算。

| 检验 | policy_exposure 系数 | SE | p | N |
|---|---:|---:|---:|---:|
| 当年动态分组基准 | -0.455327 | 0.032907 | 1.53e-43 | 249,504 |
| 政策前最后观测固定分组 | -0.535469 | 0.035974 | 4.14e-50 | 155,909 |
| 仅稳定分组企业 | -0.625226 | 0.042578 | 8.15e-49 | 236,605 |
| 替代结果 polint2 | -1.195081 | 0.114804 | 2.24e-25 | 249,504 |
| 2004 伪政策时点 | -0.281233 | 0.036931 | 2.63e-14 | 249,504 |

关键反证：

- 联合政策前趋势 `p=0.0327124`，冻结 alpha=0.05 下拒绝平行趋势。
- 2002、2003、2004 的提前项显著为正。
- 2004 伪时点效应高度显著。
- 固定政策前分组的 199 次 `idcode` 标签置换经验 `p=0.005`，但不能覆盖平行趋势和伪时点失败。
- 2,333 家企业在样本期切换高污染行业状态。

独立 NumPy 实现复算了 6 个估计步骤、94 个系数/SE 指标，差异列表为空，最大绝对差 `2.12e-13`。这证明计算实现一致，不证明识别有效。

Claim Gate 拒绝了唯一候选主张，没有任何最终获准主张。产物为 `identification_failure_report`，而不是把显著负系数写成政策因果效应。

## HypoWeaver aligned 的 H2 误杀

三个候选的 Probe 均为 `warn`、`executor_ready=true`，也没有 Reviewer `reject`。Reviewer 正确识别出 `indcode × areacode2 × year` 交互聚类有 65.4% 的单观测单元，但三个计划都已包含必做的 `check-policy-cluster-entity`。代码仍将所有 `policy_causal` 的 open critical 无条件当作硬阻断，导致实验未开始。

这应修正为：保留原交互聚类作复现基准，把实体聚类作共同主推断/必做敏感性；只有 `critical + human_required`、Probe 硬失败或 Reviewer `reject` 才在 H2 前阻断。其余风险应进入冻结 Test DAG 和 Claim Gate，而不是让系统停在 `not_evaluated`。

## r3 的预注册修正

1. 主赛道限定为“给定输入后选方法→跑实验→写报告”，取消 Agent Laboratory 必须收录 1 篇论文的硬门；文献搜索能力另设赛道。
2. `reproduction_aligned` 的所有策略必须保留冻结控制变量，策略只能改变检查优先级和报告规则。
3. LLM Reviewer 只能提供结构化建议；硬阻断由代码规则拥有。
4. 实体聚类结果必须进入 Claim Gate；与复现基准推断冲突时自动降级或拒绝因果表述。
5. r2 保持不变，r3 使用新协议哈希和新 suite id；两者都只属于 dev calibration。

## 不能宣称的结论

该套件只有 1 个独立案例，且 r2 没有完整的系统配对产物。因此不能宣称 HypoWeaver 总体科研能力优于 Agent Laboratory。当前最强的证据化表述是：

> HypoWeaver 在 Case002 的自主发现开发格中完整执行了政策面板检验，独立复算一致，并在系数高度显著时因预趋势和伪时点失败而拒绝因果主张。

## r3 后新增的元数据契约

r3 审计后，后续新 suite 使用以下兼容契约；已经生成的 r1–r3 文件保持原样，不回写：

- `provider_transport` 的 `scientific_status` 固定为 `not_evaluated`，不得把供应商故障写成科学无效。
- Agent Laboratory 没有跨架构逻辑调用上限，因此 `within_logical_call_budget=null`；共同硬上限仍是每系统、每视图 40 次 provider attempt。
- standalone normalized 输出携带 `suite_id`、`run_id`、`input_view`、`independent_case_id`、隐藏参考权限、cell 总耗时和 timeout 状态。
- 模型账本时间改名为 `model_provider_wall_time_seconds`，明确它是 provider 请求延迟累计值，不是 cell 端到端耗时；旧 `wall_time_seconds` 仍可作为输入解析。
- suite summary 使用 `non_infrastructure_terminal_cells`；旧 `scientifically_comparable_terminal_cells` 只保留为兼容别名。
- summary 按输入视图分别报告 completion pair 与 artifact-quality pair 的资格和排除原因，不能再用单格数量冒充配对样本数。
