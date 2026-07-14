# HypoWeaver-Qwen 工作流实验台

一个面向团队讨论和工程交接的只读工作流前端。它直接解析现有 Dify YAML，展示真实节点、分支与汇合关系，并允许逐节点查看 System/User Prompt、上游输入引用和目标输出 Schema。

当前版本是“流程定义浏览器”，不是统计执行器。它不会调用千问、运行回归或读取隐藏参考结果。

## 已实现

- 在 App A / App B 之间切换，保持主研究与隐藏盲测物理隔离；
- 用流程画布显示节点、连线、互斥路由、汇合节点和 H1–H3 人类闸门；
- 按阶段聚焦画布，并可从左侧检索节点或提示词；
- 检查每个节点的提示词、输入契约、输出字段和从 Prompt 提取的 JSON Schema；
- 标出当前 YAML 中会阻塞正式代码落地的问题；
- 支持桌面和移动端浏览。

## 本地运行

要求 Node.js 20.19+。

```bash
npm install
npm run dev
```

验证：

```bash
npm test
npm run build
```

## 数据源

前端启动时读取以下原始 Dify 导出，不维护第二份手抄节点表：

- `public/workflows/04_AppA_humaninput.yml`
- `public/workflows/05_AppB_BlindEvaluator.yml`

解析入口在 `src/domain/parseDifyWorkflow.ts`。阶段含义和静态落地检查在 `src/data/workflowConfig.ts`。替换 YAML 后应先运行测试；测试锁定了当前基线统计：

| 工作流 | 节点 | 连线 | LLM | Human Input |
|---|---:|---:|---:|---:|
| App A | 35 | 42 | 17 | 3 |
| App B | 5 | 5 | 1 | 0 |

## 真实流程边界

App A 当前 YAML 的主链路是：

```text
三份说明材料并行提取
→ ResearchPackage
→ H1
→ 假设拆解
→ 数据画像
→ 方法路由
→ 六类方法互斥选择并汇合
→ Critic 与单轮修订
→ H2
→ Fixture / HTTP Executor 二选一并汇合
→ 结果解释
→ 科学审查
→ ClaimLedger
→ H3
→ 写作与一致性审计
```

App B 只在主系统产物封存后读取隐藏参考材料：

```text
ResearchRun + ClaimLedger + AnalysisPlan
+ 原论文与参考摘要并行提取
→ 独立盲测评估
→ 评分输出
```

概念设计中曾讨论过的“四类 Critic 并行”“四类扩展检验并行”等结构，尚未出现在当前 YAML 中，因此本前端不会把它们画成已实现功能。

## 与 Agent Laboratory 的关系

本项目和同级目录 `../Agent Laboratory` 必须保持独立：

- 本项目是 HypoWeaver 主系统的流程定义与前端；
- Agent Laboratory 是外部 Benchmark 基线，刻意不包含 Critic、H2 冻结、ClaimLedger 和 H3 授权；
- 两者未来只通过标准 `01_model_input` 案例包和 `benchmark_output.json` 交接，不互相导入 Prompt 或工作流实现。

## 正式后端的下一步

当前最小前端完成后，后端应优先实现：

1. 可版本化的 `WorkflowDefinition` API，而不是让浏览器长期依赖 Dify 私有 YAML 结构；
2. 异步运行接口：`POST /api/runs`、`GET /api/runs/{run_id}` 和产物查询；
3. `CasePackage → AnalysisPlan → ResearchRun → ClaimLedger` 的严格 Schema 校验；
4. 会真正停止、退回或等待的 H1/H2/H3 状态机；
5. 在隔离容器中运行的 Python 计量执行器。

当前阻塞项可直接在页面右上角“落地检查”中查看。

## 目录

```text
public/workflows/            Dify 原始导出
src/components/              画布、阶段导航、节点检查器、落地检查
src/data/workflowConfig.ts   阶段映射与已确认的落地差异
src/domain/                  YAML 解析与前端契约类型
tests/                       真实 YAML 解析回归测试
```
