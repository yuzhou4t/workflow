# Case 002 v2 开发套件 r4 审计

## 结论

套件：`case002-v2-dev-20260719-r4`

分类：`development_calibration_only`。Case 002 是一个已经用于开发和调试的独立案例，两个输入视图和四个系统单元不会把独立案例数从 1 增加到 4。本轮不能支持任何一套系统具有更强总体科研能力的结论。

r4 冻结产物保持不变。运行后审计发现的报告层修复只进入后续源码和回归测试，不回填或重写 r4 产物。

## 四格结果

| 输入视图 | Agent Laboratory | HypoWeaver | 配对资格 |
| --- | --- | --- | --- |
| `discovery_blind` | 实验阶段模型请求连续三次 `RemoteDisconnected`；只有待执行代码，无实验结果或论文 | 完成；`execution_status=succeeded`，`scientific_status=limited` | 否：配对中存在供应商传输故障 |
| `reproduction_aligned` | 实验阶段模型请求连续三次 `RemoteDisconnected`；只有待执行代码，无实验结果或论文 | 完成；`execution_status=succeeded`，`scientific_status=limited` | 否：配对中存在供应商传输故障 |

汇总结果为：2 个 `none`、2 个 `provider_transport`、0 个 `benchmark_infrastructure`、0 个 `system_capability`；可比较完成率视图为 0，可比较产物质量视图为 0。

Agent Laboratory 的本地 `datasets.load_dataset` 兼容桥已在预检中真实读取冻结 CSV，校验 249,504 行、59 列和主数据 SHA256，且没有网络访问。因此 r3 的环境错配已修复；r4 的两个 Agent 终止点是外部供应商传输故障，不是数据桥或科研代码失败。每个终止请求的三次 attempt 都保持相同 `logical_call_id` 和 `input_sha256`。

## HypoWeaver 的冻结估计与敏感性

| 指标 | Discovery | Aligned |
| --- | ---: | ---: |
| 基准 `policy_exposure` | -0.4553（SE 0.0329） | -0.4772（SE 0.0313） |
| 固定政策前分组 | -0.5355 | -0.5343 |
| 稳定企业样本 | -0.6252 | -0.6233 |
| 替代结果 `polint2` | -1.1951 | -1.2537 |
| joint pretrend p | 0.0327 | 1.729×10^-7 |
| clean fake-time p | 0.000214 | 0.000112 |

固定分组、稳定企业、实体聚类和替代结果均保持负向，说明冻结规格下的负向条件关联对这些口径具有一定稳定性；它们不能挽救因果识别。

固定政策前分组只使用 155,909/249,504 行，并删除 45,261 个没有政策前组别的实体。该检查同时改变分组定义和样本构成，不能称为“同一样本稳健”。置换检验完成 199 次，经验 p=0.005 是 `(0+1)/(199+1)` 的最小 Monte Carlo 分辨率，而且对应固定分组样本，不是基准时变分组估计。

## P0 修复的真实验收

两种视图的 fake-time 都只使用 1998–2006 年的 93,405 行，排除真实政策年及以后的 156,099 行，`true_policy_contamination_rows=0`，且 pseudo-pre/pseudo-post 的 treated/control 支持完整。显著 placebo 因此不再是旧样本污染 bug，而是识别红旗。

Aligned 完整生成 `[1998, 1999, 2000, 2001]` 的 `event_remote_pre`，无缺失或共线；系数 0.3240、p=3.60×10^-8，并与 2002–2005 年项共同进入联合预趋势检验。`event_2005` 的 p=0.1127，单期不显著，但联合检验强烈拒绝全部政策前项为零。

事件项冻结为 `binary_group_year_contrast`。`event_2007` 的回归量权重为 1，而 aligned 基准政策年权重为 0.42；两类系数单位不同，不得直接比较大小。

## 复算边界

Discovery 的 94 项和 Aligned 的 124 项复算指标全部匹配，最大绝对差分别为 2.12×10^-13 和 2.41×10^-10。

该复算只具有 `independence_scope=estimator_only`：估计器和协方差实现独立，但政策分析表准备、事件研究和安慰剂变量构造与主流程共享；support 与 permutation 检查也不在覆盖范围内。它只能支持“估计器与协方差实现复算一致”，不能称为端到端独立复现。

## Claim Gate 与科学结论

两个视图的代码化证据均把事件研究和 fake-time 记为 opposing，并把其他已完成的方向一致检查及受限复算记为 supporting。确定性上限均为 `max_allowed_strength=mixed`。

Discovery 候选 Claim 自评为 `associational`，经 Gate 收紧为 `allowed_strength=mixed`，最终只准入“证据混合、只能报告受限统计关联、不能支持因果解释”。Aligned 候选 Claim 自评为 `insufficient`，比代码上限更保守；H3 不向上强化模型自评，因此拒绝主张并生成识别失败报告。模型是否过度或适度保守属于待评分的科研校准能力，报告必须同时展示 `allowed_strength`、`max_allowed_strength` 和 H3 决策，不能把差异误写成 Gate 结果不同。

本案例最严谨的科学表述是：

> 冻结面板规格下观察到较稳定的负向条件关联；但显著的政策前动态和干净 fake-time 效应破坏了因果识别，因此不能把该关联归因于绿色信贷政策。

## r4 后审计发现并修复的问题

1. Aligned 的模型 ScientificAudit 写成“政策前各期系数显著为正”，但 `event_2005` 不显著。旧失败报告直接复制了该自由文本。后续失败报告只读取类型化的 EvidenceRegistry、ClaimGateReport、ClaimLedger 和 ReproductionAudit；ScientificAudit 自由文本保留为独立工件但不进入确定性报告。
2. Evidence Registry 的复算理由原先只写 “Independent implementation matched”。后续理由强制写出 `estimator_only`、共享组件和“不是端到端复现”。
3. 固定政策前分组原先只按符号一致记为 supporting。后续必须同时存在行数和实体流失诊断；缺少这些字段时状态为 `incomplete`，存在时理由明确披露样本变化。
4. Discovery 的 `remote_pre_complete=true` 是空请求真值。后续新增 `remote_pre_requested` 和 `remote_pre_status`；未请求时为 `false/not_applicable`，不能解读成已经执行远期提前项。
5. Agent Laboratory 原始技术失败包把未评估的科学状态写成 `invalid`。后续原始包与汇总层统一为 `not_evaluated`。
6. H3 生成文本原先可能用较松的 `max_allowed_strength` 覆盖较严的 `allowed_strength`。后续文本只按已经收紧的实际强度生成，并在拒绝原因中透明展示二者。

上述修复通过 HypoWeaver 后端 499 项全量测试和 Agent Laboratory 25 项全量测试。另用 r4 Aligned 的真实 RunState 做只读重编译预演，确认错误 ScientificAudit 文本不再进入报告，复算范围、共享组件和 H3 强度字段均被写入；r4 冻结文件未被修改。

## 允许写入总报告的比较结论

> 在一个已见 Case 002 开发案例上，HypoWeaver 完成了两个输入视图，并能把相互冲突的统计证据降级为受限关联或识别失败；Agent Laboratory 的两个单元都因供应商传输故障被排除。由于两个视图均无有效系统配对，本轮不能比较两套系统的科研产物质量、完成能力或总体科研能力。
