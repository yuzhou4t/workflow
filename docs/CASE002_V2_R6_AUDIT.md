# Case 002 v2 开发套件 r6 审计

## 结论

套件 `case002-v2-dev-20260719-r6` 已完成并封存，分类保持为
`development_calibration_only`。Case 002 是一个已参与开发的独立案例；两个输入
视图和四个系统单元仍只构成 1 个独立研究任务，不能用于总体科研能力排名。

HypoWeaver 完成两个视图，执行状态均为 `succeeded`，科学状态均为 `limited`。
Agent Laboratory 两个视图都在实验阶段因终端供应商传输故障停止，科学状态均为
`not_evaluated`。两个视图都没有有效系统配对，因此本轮不能比较两套系统的科研
产物质量、完成能力或总体科研能力。

r6 的冻结源码与协议哈希为：

- protocol：`b7b986f1e65d0fccfab2b3110be35a3ec506293eb0bdbc605a098c8d57af88c5`
- HypoWeaver：`5ea7d4276524de2517a45dbefc4ef82cc5e5be48af5ef3b9719594f562c8e02d`
- Agent Laboratory：`9dc4790d53202e4bc2080d86bafb5575bdb7aa8f5f156d86af1f6f1ca2e5b4a5`
- benchmark harness：`2988f07babb587901053fb69f3f116976a77542cd63074c260365d7fb343eae2`

运行后修复不回填 r6 产物，也不冒充已在 r6 中获得端到端验证。

## 四格结果与预算

| 输入视图 | 系统 | 终态 | 逻辑调用 / 供应商请求 | 活跃单元耗时 |
| --- | --- | --- | ---: | ---: |
| `discovery_blind` | Agent Laboratory | `provider_transport`；`not_evaluated` | 17 / 19 | 447.16 秒 |
| `discovery_blind` | HypoWeaver | 完成；`limited` | 7 / 7 | 282.92 秒 |
| `reproduction_aligned` | HypoWeaver | 完成；`limited` | 9 / 13 | 1,984.75 秒 |
| `reproduction_aligned` | Agent Laboratory | `provider_transport`；`not_evaluated` | 12 / 19 | 706.83 秒 |

四格均未超过每系统每视图 40 次供应商请求上限；HypoWeaver 的逻辑调用也未超过
20 次。隐藏参考访问均被拒绝。

Agent discovery 的最后一个逻辑请求使用相同输入哈希连续三次
`RemoteDisconnected`。Agent aligned 的最后一个逻辑请求也在电脑恢复后连续三次
`RemoteDisconnected`；因此两格的终端传输分类都有独立 receipt 证据，不是根据
自由文本猜测。

aligned Agent 单元开始约两分钟后电脑合盖休眠，恢复后继续执行。Python 冻结超时
按活跃单调时钟计，因此 706.83 秒没有超过 2,700 秒单格预算；但上游按墙钟记录的
`plan formulation` 时长被休眠膨胀到 5,374 秒，不能用于阶段效率比较。该干扰影响
阶段计时审计，但不改变最终终端请求是在恢复后连续三次断开的事实。

## HypoWeaver 的统计结果

| 指标 | Discovery | Aligned |
| --- | ---: | ---: |
| 基准 `policy_exposure` | -0.4553（SE 0.0329） | -0.4772（SE 0.0313） |
| 固定政策前分组 | -0.5355 | -0.5343 |
| 稳定企业样本 | -0.6252 | -0.6233 |
| 替代结果 `polint2` | -1.1951 | -1.2537 |
| joint pretrend p | 0.0327 | 1.729×10^-7 |
| clean fake-time p | 0.000214 | 0.000112 |

两个视图都观察到方向稳定的负向条件关联，但联合政策前趋势和干净伪政策时点均
失败。因此最严谨的案例结论是：

> 冻结面板规格下观察到较稳定的负向条件关联；显著的政策前动态和干净伪时点
> 效应破坏了因果识别，不能把该关联归因于绿色信贷政策。

Aligned 的远期政策前项明确为 `requested=true/status=complete`，聚合 1998--2001，
系数为 0.3240（p=3.60×10^-8），并与 2002--2005 项共同进入联合检验。2010 没有
观察且没有插补；事件项是相对 2006 的年份对比，不依赖线性插值或平滑。

固定政策前分组只使用 155,909/249,504 行，并因没有政策前组别删除 45,261 个
实体，所以不是同样本稳健性。clean fake-time 只用 1998--2006 的 93,405 行，排除
真实政策期及以后 156,099 行，污染行数为 0。置换检验对应固定分组样本而不是基准
时变分组估计；完成 199/199 次，经验 p=0.005 是有限重复下的最小分辨率，其随机化
式解释还依赖企业分配单元在冻结设计下可交换。

## Claim Gate 与复算

两个视图的代码化证据都将事件研究和 fake-time 记为 opposing，并把确定性上限
收紧为 `max_allowed_strength=mixed`。

Discovery 的模型候选自评为 `insufficient`。旧英文词法规则又把“更可能反映预存
趋势而不是政策因果效应”的否定句误判为因果断言；即使去掉该误判，实际强度仍为
`insufficient`，所以 H3 仍必须拒绝主张并生成识别失败报告。Aligned 候选的
`evidence_status=mixed`、`allowed_strength=associational`；Claim Gate 将
`max_allowed_strength` 收紧为 `mixed`，并把最终 `allowed_strength` 降级为 `mixed`
后准入，随后生成完整论文。最终文字明确只能报告受限关联，不能
支持因果解释。两种生成形式不同，但科学上限一致。

Discovery 的 94 项和 Aligned 的 124 项复算指标全部匹配，最大绝对差分别为
2.12×10^-13 和 2.41×10^-10。复算范围均为 `estimator_only`：估计器与协方差路径
独立，但分析表准备以及事件/安慰剂回归量构造共享，不能称为端到端独立复现。

## r6 暴露的后续修复项

以下问题在 r6 封存后修复或进入回归，不回写 r6：

1. Claim Gate 增加窄范围英文否定因果豁免，并补上高置信英文正向因果漏检；用 r6
   Discovery 原始候选重放后，错误措辞理由消失，只保留前趋势和 fake-time 反对
   证据，科学结论没有被抬高。
2. benchmark manifest 明确披露比较对象是 `system_package_capability`。Agent 全部
   使用 `qwen3.7-plus`；HypoWeaver 默认使用 `qwen3.7-plus`，Reviewer、Scientific
   Audit、Design Retry 和 Writer Escalation 使用 `qwen3.7-max`。因此不能再描述成
   “同一底模下纯工作流对照”。
3. `provider_transport` 只接受明确的传输耗尽类别或最终同一逻辑请求的三条同参
   transport receipts。HTTP 400 和响应合同错误不得按基础设施故障排除。
4. Agent 失败包需要保留 `diagnostic_only` 的阶段性计划、数据代码和实际文件哈希，
   补全失败阶段活跃计时与本地 datasets bridge 调用记录，同时保持科学状态为
   `not_evaluated`。
5. 完整论文必须用代码拥有的 required statements 披露固定分组换样本、clean
   fake-time 样本、置换目标和交换性边界、remote-pre 范围以及政策年事件项与基准
   系数不可直接比较。
6. ScientificAudit 自由文本只保留为独立第二意见工件，不得进入 Writer 输入或
   ManuscriptPackage 元数据。r6 audit 曾错误声称 2010 缺失引入线性/平滑假设；
   该句虽未进入正文，却原样进入了封存包 `unresolved_issues`，需要彻底隔离。

上述 post-r6 修复通过 HypoWeaver 后端 511 项全量测试和 Agent Laboratory
适配层 30 项全量测试；Agent 测试包含真实 macOS `sandbox-exec`。四个冻结上游
文件哈希保持不变。用 r6 Discovery 原始结构化工件重放 Claim Gate，得到
`downgrade_required / max=mixed / allowed=insufficient`，且 Gate reasons 只剩前趋势
和 fake-time 两项。该重放是修复的回归证据，不改变 r6 的冻结哈希或原始产物。

## 允许写入总报告的结论

> 在已见 Case 002 开发案例上，HypoWeaver 在两个输入视图中完成了冻结统计执行、
> 证伪测试、受限复算和主张校准；两次均拒绝将稳定负向关联升级为因果结论。
> Agent Laboratory 两个单元都在形成实验结果和论文前因供应商传输故障终止。
> 由于没有任何有效系统配对，本轮不能据此判断哪套系统科研能力更强。
