# HypoWeaver-Qwen 代码工作流

这是一套代码原生、可停止、可恢复的社会科学假设验证链路。Dify 导出文件只保留为设计参考；正式运行时由 Python 状态机、严格 JSON Schema、SQLite Run 快照和服务端人工闸门共同控制。

当前第一版优先验证一条可信的核心闭环：

```text
标准案例包
→ 规范化与确定性校验
→ H1 研究边界确认
→ 假设拆解 + 数据画像（并行）
→ 方法路由
→ 七类方法设计器（互斥）
→ 四类 Critic（并行）与最多两轮修复
→ H2 冻结 FormalResearchContract
→ Fixture / Python Research Engine（互斥）
→ EvidenceAssessment + ScientificAudit
→ ClaimLedger
→ H3 逐条结论授权
→ 受约束写作与确定性一致性审计
→ 封存成果包
```

## 已实现

- 代码维护的版本化工作流定义，前端不再解析 Dify YAML；
- `CaseSubmission → ResearchPackage → AnalysisPlan → FormalResearchContract → ResearchRun → ClaimLedger → ManuscriptPackage` 的 Pydantic 严格 Schema；
- 真正会暂停的 H1/H2/H3 服务端状态机；
- H2 计划哈希冻结、乐观版本控制和幂等审批键；
- SQLite 持久化 Run、Step Attempt、事件、决策和 Artifact，刷新页面可恢复；
- 节点级 Prompt 模板/本次渲染、实际输入、实际输出和日志；
- Fixture 与外部 Python 执行器接口；
- `execution_status` 与 `scientific_status` 分开保存和展示；
- Fixture 安全边界：不生成任何样本量、系数、p 值、显著性或诊断结果，只允许生成研究计划；
- App A 输入 Schema 拒绝原论文结果和隐藏参考字段；
- 独立 App B 盲测服务：独立数据库、封存哈希校验、六维诊断和代码计算总分；
- 可运行的 React 研究 Run 控制台与两个预设案例。

## 本地启动

要求 Python 3.11、Node.js 20.19+。

后端：

```bash
PYTHONPATH=backend/src python3.11 -m uvicorn hypoweaver.api:app --reload --port 8000
```

前端：

```bash
npm install
npm run dev -- --port 5174
```

前端开发服务器会把 `/api` 代理到 `http://127.0.0.1:8000`。也可以设置 `VITE_API_TARGET` 指向其他后端地址。

## 验证

```bash
PYTHONPATH=backend/src python3.11 -m unittest discover -s backend/tests -v
npm test
npm run build
```

## 两种运行模式

### Fixture 流程演示

Fixture 用于验证状态机、Prompt、Schema、闸门、事件和前端。它会完整走到 H3，但返回：

```text
execution_status = fixture_only
scientific_status = not_evaluated
Claim.evidence_status = not_tested
Claim.allowed_strength = prohibited
```

H3 只能把每条 Claim 标为“拒绝”或“暂缓”，随后生成 `research_plan_only`。Fixture 不是实证结果。

### 真实研究执行

设置环境变量后，研究模式可调用百炼 Qwen 和独立 Python Research Engine：

```bash
export DASHSCOPE_API_KEY=...
export QWEN_MODEL=qwen-plus
export RESEARCH_ENGINE_URL=http://127.0.0.1:9000
export RESEARCH_ENGINE_TOKEN=...
export HYPOWEAVER_API_TOKEN=...
export HYPOWEAVER_BLIND_API_TOKEN=...
export HYPOWEAVER_SEAL_SECRET=...
```

未设置 API Token 时，两个服务的写接口仅允许 loopback 调用。生产或局域网部署必须设置 Token；浏览器控制台对应设置 `VITE_HYPOWEAVER_API_TOKEN`。封存默认使用本机 `backend/var/seal.key`，多实例部署必须通过 `HYPOWEAVER_SEAL_SECRET` 注入同一密钥。

外部执行器接收冻结的 `FormalResearchContract`，并必须返回符合 `ResearchRun` Schema 的 JSON。当前仓库只实现稳定的编排和执行器适配边界，不在 Web 进程内运行任意模型生成代码。

## HTTP API

```text
GET  /api/v1/health
GET  /api/v1/definitions/app-a
POST /api/v1/runs
GET  /api/v1/runs
GET  /api/v1/runs/{run_id}
POST /api/v1/runs/{run_id}/advance
POST /api/v1/runs/{run_id}/gates/{H1|H2|H3}
POST /api/v1/runs/{run_id}/revisions
GET  /api/v1/runs/{run_id}/artifacts/{artifact_key}
```

H1/H2 被退回后，Run 会进入 `blocked`，必须通过 `revisions` 接口提交新版 `CaseSubmission` 或递增版本的 `AnalysisPlan`；系统不会自动越过退回意见。

## 安全与盲测边界

App A 不读取原论文 PDF、已发表结果、回归表或 `02_hidden_reference`。严格输入 Schema 会在持久化之前拒绝额外隐藏字段。

Schema 不能识别用户故意粘贴进普通自由文本字段的隐藏答案；正式 Benchmark 必须由受控案例打包服务创建 App A 输入，并通过目录/对象存储 ACL 保证 App A 身份根本无法读取 `02_hidden_reference`。这属于部署权限边界，不能用 Prompt 或关键词过滤代替。

盲测 App B 已实现为独立进程与独立 SQLite 数据库。它只能在主 Run 封存后读取 `sealed_output + AnalysisPlan + ResearchRun + ClaimLedger + HiddenReference`，先验证 HMAC 封存签名和各 Artifact 哈希，再执行六维评估；LLM 只能建议分项分数，总分由代码按适用权重计算。App B 没有任何回写 App A 的接口。

```bash
PYTHONPATH=backend/src \
HYPOWEAVER_BLIND_DB_PATH=backend/var/blind/hypoweaver_blind.db \
python3.11 -m uvicorn hypoweaver.blind_api:app --port 8002
```

`05_AppB_BlindEvaluator.yml` 同样只是历史设计参考，不参与运行。

## 从“公文”工作流借鉴了什么

参考公共目录中 `公文/renhang_smartreport` 的代码链路，本项目采用了阶段进度、节点级输入输出与调试产物、并行后汇合、确定性校验和任务状态查询。同时做了三项关键加强：

- 用 SQLite Run 快照替代协程内可变上下文；
- 用服务端 `waiting_human` 状态替代前端取消请求式“暂停”；
- 页面刷新和状态读取使用持久化短请求，不依赖一条 SSE 连接保存运行状态。

## 目录

```text
backend/src/hypoweaver/
  api.py              FastAPI 接口
  blind_api.py        独立 App B FastAPI 入口
  blind_engine.py     封存校验、六维评估与代码评分
  blind_models.py     盲测输入输出 Schema
  blind_repository.py App B 独立 SQLite 存储
  definition.py       代码工作流定义
  engine.py           状态机与闸门
  models.py           严格领域 Schema
  prompts.py          版本化 Prompt 注册表
  adapters.py         Fixture、Qwen 与 Python 执行器适配
  repository.py       SQLite Run 快照与乐观锁
backend/tests/         状态机与安全边界测试
src/                   React 运行控制台
public/workflows/      Dify 历史设计参考，不参与运行
```

同级 `../Agent Laboratory` 是外部 Benchmark 基线，必须与本项目保持独立，不能向其中加入 HypoWeaver 的 Critic、冻结或 ClaimLedger 逻辑。
