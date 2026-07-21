# Group 2 假设验证、复现与写作工作流

## 1. 本项目的职责边界

当前工程只实现 Group 2 Task 3：把安全可见的“研究假设 + 客观数据说明 + 数据资产”转化为可审计的研究设计、真实执行结果、逐条授权结论和论文初稿。

本轮不承担以下工作：

- Group 1 的政策、论文、数据库和 ESG 报告采集；
- Group 1 的知识图谱、Text-RAG / Graph-RAG 与研究空白发现；
- Group 2 Task 1 的大规模论文元数据生产；
- Group 2 Task 2 的完整模型库、方法库建设；
- Group 2 Task 4 的正式科研绘图库建设。

这些模块以后通过标准对象接入，但不应为了展示“多智能体”而混入当前验证链路。

## 2. 当前代码链路

```text
安全案例包
  ↓
确定性校验与数据画像
  ↓
H1：确认研究问题、假设、变量定义、数据与设计边界
  ↓
三个独立候选设计器
  ├─ 直接基准策略
  ├─ 识别优先策略
  └─ 测量优先策略
  ↓
每个候选的低成本 Probe
  ├─ 数据字段与面板主键
  ├─ 测量口径
  ├─ 识别条件
  ├─ 空间权重与估计目标
  └─ 执行器能力
  ↓
四个隔离 Reviewer 并行审查
  ├─ Measurement Reviewer
  ├─ Causal Reviewer
  ├─ Statistical Reviewer
  └─ Reproducibility Reviewer
  ↓
Reviewer Arena 汇合：淘汰硬失败，保留多个可行候选
  ↓
H2：人工选择一个候选并冻结 FormalResearchContract
  ↓
从结构化科学威胁编译已注册方法的 Test DAG
  ↓
真实执行器运行冻结模型
  ↓
独立再次执行 + 数值容差复现审计
  ↓
代码拥有的 Evidence Registry
  ↓
确定性 Claim Gate 编译 ClaimLedger
  ↓
H3：逐条批准、降级、暂缓或拒绝结论
  ↓
Manuscript IR 绑定 Claim、Execution 与受保护事实
  ↓
8 个通用论文章节分节写作 + 确定性全文审计
  ↓
H4：人工批准、退回指定章节或终止
  ↓
HMAC 防篡改封存
```

工作流始终把 `execution_status` 与 `scientific_status` 分开。一段代码成功运行，不代表研究设计有效；独立复现匹配，也不代表因果识别成立。

## 3. 与《框架设计》的对应关系

| 《框架设计》目标态 | 当前 Group 2 第一版 | 说明 |
|---|---|---|
| G1 ResearchBrief | H1 研究边界确认 | 已实现服务端暂停、版本校验与决策记录 |
| Evidence–Idea Loop | 三个候选设计器 | 只处理已有假设的研究设计，不承担文献和政策 Scout |
| 多方案竞争与质疑 | Candidate Set + 4 个隔离 Reviewer | 不用总分或多数投票决定科学真值，硬失败直接淘汰 |
| G2 候选选择 | 合并进 H2 | 当前案例已有数据，先完成无结果 Probe，再由人选择候选 |
| Probe Tree / G3 | ProbeReport + H2 合同冻结 + 企业面板/政策 DID Test DAG | 已实现低成本结构预检、稳定威胁/Claim 绑定和必做检查终态；尚未实现其他方法注册表和开放式多层预算树 |
| Formal Research Loop | 冻结合同、真实执行、独立复现 | 已实现主运行与第二次独立运行的确定性比较 |
| G4 Claim 审核 | Evidence Registry + 确定性 Claim Gate + H3 | 代码先给出准入状态与最大措辞，人类只能批准、降级、暂缓或拒绝，不能越过上限 |
| Writing–Review Loop | Manuscript IR + 分节 Writer + H4 | 统计事实由代码按来源指针注入，H4 支持定点退回；未提供文献时禁止虚构引用 |
| G5 发布决定 | H4 封存决定 | 当前第一版将最终人工发布关口命名为 H4 |
| Versioned Memory | SQLite Run / Step / Event / Decision / Artifact | 已实现单 Run 可恢复审计历史；跨任务经验库尚未启用 |

当前的“智能体博弈”是有边界的科学分工，不是开放式辩论：

1. 候选生成角色从不同优化目标独立提出方案；
2. Probe 只检查数据、测量、识别和执行条件，不读取结果显著性；
3. Reviewer 使用隔离上下文逐个攻击候选；
4. Arena 只做硬约束汇合，不让 Agent 自己给自己颁奖；
5. 最终方案由人类在 H2 选择并冻结。

这实现了《框架设计》中“扩大探索空间、降低自我认证风险、保留多个可行方案”的核心思想。尚未实现的是动态 19 角色池、Scout 证据图和完整多分支 Probe Tree；这些应在前三个案例验证稳定后再扩展。

用于六系统 common-executor 对照时，同一生产状态机会在 `waiting_human/H2`、冻结合同和任何统计结果产生之前停止。恢复入口只接受与原 `AnalysisRequest` 精确哈希绑定的密封公共执行结果，跳过原生估计器后继续 Evidence Registry、Claim Gate 与 H3；重新规划、换结果或直接给 Writer 看结果都不属于该接口。

## 4. 怎样自然贴近原文方法

盲测不应把“使用 SDM / DID / IV”直接告诉模型，也不应只给裸 CSV。建议为每个案例提供中性的 `DesignEnvelope`：

```json
{
  "benchmark_track": "reproduction_aligned",
  "research_goal": "associational",
  "target_estimands": [
    "本地直接关联",
    "跨地区间接关联",
    "直接与间接关联合计的总关联"
  ],
  "design_constraints": [
    "区分结果变量空间反馈与解释变量跨地区关联",
    "冻结权重、变量转换、固定效应和推断策略"
  ],
  "required_diagnostics": [
    "空间标识与权重矩阵对齐",
    "报告空间参数与效应分解",
    "披露只能支持关联解释"
  ],
  "allowed_claim_strength": "associational"
}
```

它告诉系统“要估计什么、必须区分什么、什么结果才可比较”，但不告诉系统原论文使用的方法名称、模型方程、系数方向、显著性或结论。Method Router 和候选设计器仍需根据估计目标、数据结构、权重资产和识别边界自然选择方法。

正式 Benchmark 应同时保留两条轨道：

- `discovery_blind`：只提供研究问题、假设、变量字典和数据，评估系统能否自主选择合理方法；
- `reproduction_aligned`：额外提供中性估计目标和设计约束，评估系统能否忠实实现与原研究可比较的研究问题。

两条轨道回答不同问题，不能混成一个分数。前者评价研究自主性，后者评价方法实现与复现忠实度。

## 5. 当前真实案例验收

案例 `case_001_green_finance_spatial_method_aligned` 使用安全可见的 30 省、2014—2023 年平衡空间面板和独立重建的空间权重矩阵。原论文、代码、回归表、结果方向和显著性未进入主流程。

在不知道方法名称和隐藏结果的条件下，三个候选均识别出需要同时处理结果变量空间反馈、解释变量空间滞后和直接/间接/总效应分解；H2 人工选择直接基准候选后冻结双向固定效应 SDM。主执行与独立第二次执行在 `1e-8` 容差内一致。

真实执行结果为：

- 300 个省份—年份观测，30 个空间单元，10 年；
- zGF 直接效应约 `-0.0007`，`p=0.850`；
- 间接效应约 `-0.0231`，`p=0.574`；
- 总效应约 `-0.0238`，`p=0.576`；
- `rho≈-0.99`，位于稳定区间边界附近；
- 执行成功、独立复现匹配，但科学状态为 `limited`。

H3 将三条结论全部降级为“未发现达到常用统计显著性阈值、证据不足、不等于零效应证明”的关联表述。Writer 最终生成 8 章约 1 万字初稿；H4 多轮只退回有问题的章节，最终稿未包含任何 H3 未授权控制变量系数，并已完成防篡改封存。

这个案例只能视为“方法选择与流程约束压力测试”，不能视为原论文的数值复现。隐藏参考揭示后确认：原文使用 31 省样本和作者的试点区距离矩阵，而安全案例只有 30 省，并使用公开坐标按披露公式独立重建的全省距离矩阵；部分控制变量也只能近似映射。只有取得原始分析样本、固定省份顺序和作者实际权重矩阵后，才应计算系数复现误差。

## 6. 六系统 Benchmark v3 的当前边界

正式协议由同级 [`six-system-comparison`](../../benchmark-baselines/six-system-comparison/README.md) 中立 harness 管理，不再由本仓库内的双流程演示或 App B 单独定义。当前比较 HypoWeaver、Agent Laboratory、data-to-paper、Direct Qwen、Qwen Code-Agent + fixed Writer 和 DeepScientist。

它保留两块互不混合的能力板：

- `native_system_package`：比较六套系统按各自原生流程交付完整科研包的能力；
- `common_executor_reasoning_control`：六套流程先提交无结果分析请求，再使用同一 benchmark 执行器，比较方法选择、诊断和主张校准。

每块能力板又分别报告两个输入视图：`discovery_blind` 是自主选方法的主视图，`reproduction_aligned` 是给定冻结方法规格的复现诊断视图；二者不平均。两个能力板当前均显示 `12/12`，只说明结构化前后阶段或声明接口已经接通，不说明系统通过科学硬门，也不证明案例级科研能力相同。HypoWeaver 原生流程不能完整交付 Case 010 的 CR/AR 两个结果变量仍是明确能力缺口，不能用接口齐全代替这项能力证据。

当前 formal 明确关闭：Case 004 的科学冻结失败；Case 010 虽通过本地科学复算，但第三方数据处理权和哈希绑定外发授权未解决，而且正式协议要求 Case 004/010 成对通过，不能退化为 Case 010 单案例正式榜。因此目前没有 144 单元 formal 结果，也没有可发布的正式排名。Case 005/007/009 仍是 validation；在最终哈希绑定授权前也没有外发运行。即使未来两个准留出案例完成，结论也只能表述为“初步跨方法证据”，不能据此宣称通用 AI Scientist 能力已获证明。

后续消融仍应围绕隔离 Reviewer、Probe、H2 冻结、独立复算、Claim Gate 与 Manuscript IR 展开，用故障召回、证据可追溯、主张越界和资源成本解释差异，而不是把多 Agent 数量本身当作研究贡献。
