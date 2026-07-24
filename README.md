# HypoWeaver-Qwen 代码工作流

这是一套代码原生、可停止、可恢复的社会科学假设验证链路。Dify 导出文件只保留为设计参考；正式运行时由 Python 状态机、严格 JSON Schema、SQLite Run 快照和服务端人工闸门共同控制。

新同学接手时，请先阅读 [`README_NEXT_STEPS.md`](README_NEXT_STEPS.md)。其中写明当前架构、真实案例暴露的不足、下一阶段任务拆分、代码入口与逐项验收标准。

当前第一版优先验证一条可信的核心闭环：

```text
标准案例包
→ 规范化与确定性校验
→ H1 研究边界确认
→ 假设拆解 + 数据画像（并行）
→ 方法路由
→ 三个不同目标的候选设计
→ 每个候选的无结果 Probe
→ 四类隔离 Reviewer（并行）
→ H2 人工选择候选并冻结 FormalResearchContract
→ Fixture / Python Research Engine（互斥）
→ 独立再次执行与数值复现审计
→ EvidenceAssessment + ScientificAudit
→ ClaimLedger
→ H3 逐条结论授权
→ 8 节受约束写作与确定性一致性审计
→ H4 人工审稿与定点退回
→ 封存成果包
```

## 已实现

- 代码维护的版本化工作流定义，前端不再解析 Dify YAML；
- `CaseSubmission → ResearchPackage → AnalysisPlan → FormalResearchContract → ResearchRun → ClaimLedger → ManuscriptPackage` 的 Pydantic 严格 Schema；
- 真正会暂停的 H1/H2/H3/H4 服务端状态机；
- 三个候选研究设计、无结果 Probe、四类隔离 Reviewer 与人工候选选择；
- H2 计划哈希冻结、乐观版本控制和幂等审批键；
- 冻结合同的主执行、独立复算与数值容差复现审计；
- SQLite 持久化 Run、Step Attempt、事件、决策和 Artifact，刷新页面可恢复；
- 节点级 Prompt 模板/本次渲染、实际输入、实际输出和日志；
- Fixture 与外部 Python 执行器接口；
- `execution_status` 与 `scientific_status` 分开保存和展示；
- Fixture 安全边界：不生成任何样本量、系数、p 值、显著性或诊断结果，只允许生成研究计划；
- App A 输入 Schema 拒绝原论文结果和隐藏参考字段；
- 独立 App B 盲测服务：独立数据库、封存哈希校验、六维诊断和代码计算总分；
- 面向研究者的 React 任务控制台：详细研究输入、开始前预检、纵向执行过程、嵌入式 H1/H2/H3/H4 和成果状态；
- 案例文件夹一键导入：选择案例根目录后自动识别主 CSV、隔离论文与代码，再登记数据并直接启动到 H1；
- 黑白极简 Research Bench：默认展示 HypoWeaver 纵向链路，可展开为与 Agent Laboratory 并排的双流程对照；
- Agent Laboratory 独立基线启动器：复用同一 Dataset ID 与文件哈希，通过同级仓库的适配器异步运行并回传阶段状态；
- 页面级运行配置入口，支持脱敏状态、私有保存与 Qwen/Research Engine 连接测试。

## Windows 11 从零安装与本地启动

同学的默认环境是 **Windows 11 + WSL2 + Ubuntu**。Git、Python、Node.js、测试和启动命令都在 Ubuntu 终端中执行，不要混用 Windows Python 与 WSL Python，也不要把项目放在 OneDrive 或 `/mnt/c` 下。

项目要求：

- Python 3.11 或 3.12；
- Node.js 20.19+；
- Git；
- 仅运行本工作流时不需要 Docker。执行 SixBench 六系统隔离测试时，另按 [`sixbench-student-ops`](https://github.com/yuzhou4t/sixbench-student-ops) 的任务书安装 Docker Desktop。

### 第 1 步：安装 WSL2 和 Ubuntu

在 Windows 中右键 PowerShell，选择“以管理员身份运行”：

```powershell
wsl --install
wsl --update
wsl --set-default-version 2
wsl -l -v
```

首次安装后按系统提示重启。如果没有自动安装 Ubuntu，先查看可用名称：

```powershell
wsl --list --online
```

如果列表中存在 `Ubuntu-24.04`，推荐安装它：

```powershell
wsl --install -d Ubuntu-24.04
```

安装完成后打开 Ubuntu，按提示创建 Linux 用户名和密码。`wsl -l -v` 中该 Ubuntu 的 `VERSION` 必须是 `2`。官方说明见 [Microsoft WSL 安装文档](https://learn.microsoft.com/windows/wsl/install)。

### 第 2 步：在 Ubuntu 中安装基础工具

以下命令全部在 Ubuntu 终端执行：

```bash
sudo apt update
sudo apt install -y git curl ca-certificates build-essential python3 python3-venv python3-pip

python3 --version
git --version
```

`python3 --version` 必须显示 3.11 或 3.12。如果不是这两个版本，先停止，让本机 AI 根据实际 Ubuntu 版本安装受支持的 Python，不要跳过版本检查。

使用 nvm 安装 Node.js 20：

```bash
curl -o- https://raw.githubusercontent.com/nvm-sh/nvm/v0.40.4/install.sh | bash
source ~/.bashrc
nvm install 20
nvm alias default 20

node --version
npm --version
```

`node --version` 必须不低于 `v20.19.0`。nvm 的安装与验证方式见 [nvm 官方仓库](https://github.com/nvm-sh/nvm)。

### 第 3 步：克隆项目并安装依赖

项目放在 WSL 自己的 Linux 文件系统：

```bash
mkdir -p ~/work
cd ~/work
git clone https://github.com/yuzhou4t/workflow.git
cd workflow

python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r backend/requirements.txt
npm ci
```

以后每次新开 Ubuntu 终端，都先进入项目并激活虚拟环境：

```bash
cd ~/work/workflow
source .venv/bin/activate
```

### 第 4 步：先做离线验证

这一步不需要 API Key，也不需要案例数据：

```bash
cd ~/work/workflow
source .venv/bin/activate
export PYTHONPATH=backend/src

python -m unittest discover -s backend/tests -v
npm test
npm run build
```

三项都通过后再启动服务。若失败，把完整错误交给本机 AI 排查，不要通过删除测试或修改科学门禁来“修绿”。

### 第 5 步：在三个 Ubuntu 终端启动服务

终端一，启动主后端：

```bash
cd ~/work/workflow
source .venv/bin/activate
export PYTHONPATH=backend/src
python -m uvicorn hypoweaver.api:app --port 8000
```

终端二，启动研究执行器：

```bash
cd ~/work/workflow
source .venv/bin/activate
export PYTHONPATH=backend/src
python -m uvicorn hypoweaver.research_api:app --port 9000
```

终端三，启动前端：

```bash
cd ~/work/workflow
source ~/.bashrc
npm run dev -- --port 5174
```

Windows 浏览器直接访问 `http://127.0.0.1:5174`。前端开发服务器会把 `/api` 代理到 `http://127.0.0.1:8000`；也可以设置 `VITE_API_TARGET` 指向其他后端地址。

页面入口：

```text
http://127.0.0.1:5174/#new       详细研究输入与开始前检查
http://127.0.0.1:5174/#runs      运行过程、人工审核与结果
http://127.0.0.1:5174/#settings  API Key、模型和执行器配置
```

健康检查：

```bash
curl -s http://127.0.0.1:8000/api/v1/health
curl -s http://127.0.0.1:9000/v1/health
```

两个地址都应返回健康状态。端口被占用时先查明占用进程，不要随意结束未知服务。

### 可以直接交给本机 AI 的安装提示词

```text
你负责在这台 Windows 11 电脑上部署 HypoWeaver workflow。

请严格按照本 README 的“Windows 11 从零安装与本地启动”执行：
1. 先检查 WSL2、Ubuntu、Python、Node.js、npm、Git 和可用磁盘；
2. 管理员权限、启用 WSL、安装系统组件和重启前必须让我确认；
3. 所有项目命令在 WSL2 Ubuntu 中执行，项目放在 ~/work，不放 OneDrive 或 /mnt/c；
4. Python 必须为 3.11 或 3.12，Node.js 必须不低于 20.19；
5. 先创建 .venv、安装依赖并运行后端测试、前端测试和生产构建；
6. 离线验证全部通过后，向我汇报版本、命令结果和仍存在的 blocker；
7. 在我提供 API Key 前，不测试外部模型、不搜索案例数据、不修改科学门禁；
8. 需要 API Key 时让我本人在 #settings 页面输入，不要要求我把 Key 发到聊天、命令或文件中。

遇到错误时保留完整日志，说明原因和最小修复方案；不要静默更换 Python、模型、代码分支或测试标准。
```

<details>
<summary>已有 macOS / Linux 环境的简短安装方式</summary>

已有 Python 3.11/3.12、Node.js 20.19+ 和 Git 时，可以使用：

```bash
git clone https://github.com/yuzhou4t/workflow.git
cd workflow
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r backend/requirements.txt
npm ci
```

后续验证和启动命令与上面的 WSL2 命令相同。

</details>

## 首次配置与测试范围

打开 `http://127.0.0.1:5174/#settings`，按顺序填写并测试：

1. Qwen API Key：只在本机页面填写，不要写进源码、README、Issue 或提交记录；
2. 模型 ID：当前完整链路按 `qwen3.7-plus` 测试，模型名区分大小写；
3. Qwen Base URL：`https://dashscope.aliyuncs.com/compatible-mode/v1`；
4. Research Engine URL：本地执行器使用 `http://127.0.0.1:9000`；
5. Research Engine Token：本机未设置 `RESEARCH_ENGINE_TOKEN` 时保持为空；如果设置了，两端必须使用同一个值；
6. 保存后分别点击 Qwen 和 Research Engine 连接测试，两项都成功后再启动真实研究。

四类 Reviewer 和写作质量失败后的升级会调用 `qwen3.7-max`，因此完整真实流程还要求同一个百炼账号有该模型权限。没有 API Key 或真实数据时，仍可运行全部单元测试、生产构建和 Fixture 流程；Fixture 不会生成统计结果。

公开仓库不包含原始案例 CSV、论文、作者代码或隐藏参考。真实盲测需要另行取得符合规范的案例文件夹；最少包含一份主 CSV，建议同时提供不含论文结果的 `case_profile.json` 和中立变量字典。Agent Laboratory 只用于可选对照；未在同级目录安装该仓库时，不影响 HypoWeaver 本身、Fixture、单元测试或本地执行器运行。

## 一键导入案例包

1. 在 `#settings` 配置并测试 Qwen；真实研究还要配置 Python Research Engine。
2. 回到 `#new`，点击“选择案例文件夹并启动”，在系统文件选择器中选择案例根目录，例如 `案例1`，而不是目录中的某个文件。
3. 前端会在文件夹中自动选择主分析 CSV，并只上传这一份数据。后端会计算 SHA256/行列数/年份范围、登记 Dataset ID，并直接创建 Run 到 H1。
4. 在 H1 确认系统根据表头推断的研究问题、变量角色和样本边界后，再继续方法设计。

案例规范化、H1 前校验和方法家族路由全部由确定性代码完成；因此短暂的模型网络异常不会阻止案例先进入 H1，路由也不会因模型给出自相矛盾的状态而失败。千问从 H1 批准后的假设拆解阶段开始调用，结构化 JSON 请求会显式关闭思考模式，连接中断或超时时会记录明确错误，不再返回无说明的 HTTP 500。系统随后从直接基准、识别优先和测量优先三个目标生成候选设计；每个候选先接受不读取统计结果的 Probe，再由测量、因果识别、统计推断和可复现性四类隔离 Reviewer 并行审查，最后交给 H2 人工选择和冻结。运行记录页支持删除单条历史 Run；删除不会移除已上传的案例数据文件。

需要做流程对照时，点击首页“展开基线”。HypoWeaver 与 Agent Laboratory 各有独立启动按钮；二者复用同一份已登记 CSV 和千问配置，但分别运行、分别记录状态。Agent Laboratory 会执行模型生成的 Python 代码，因此启动前页面会要求一次明确确认。其输出只作为外部基线，默认 `scientific_status=not_assessed`，不会自动继承 HypoWeaver 的 Critic、H2 冻结或 ClaimLedger。

文件夹选择器会读取目录清单以寻找主 CSV，但只向后端上传选中的主 CSV。PDF、Word、Stata/R/Python 脚本，以及位于 `原始论文` 或 `02_hidden_reference` 等目录中的隐藏参考材料都不会上传；页面只显示隔离数量，不显示其文件名、路径或内容。保留的本地目录导入 API 也执行同样的隔离规则。私有数据注册表位于：

```text
backend/var/datasets.json
```

它仅供本机后端/执行器按 Dataset ID 解析，权限为 `0600`，不会进入 Git。上传文件存放在同样被 Git 忽略的 `backend/var/uploads/`；生产部署可将该存储层替换为对象存储。

## 验证

```bash
source .venv/bin/activate
export PYTHONPATH=backend/src
python -m unittest discover -s backend/tests -v
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

研究模式可通过页面对应的运行配置 API，或通过环境变量，连接百炼 Qwen 和独立 Python Research Engine。页面保存的配置写入：

```text
backend/var/runtime-config.json
```

文件使用 `0600` 权限，只允许当前系统用户读取。API 响应只返回“是否已配置”和配置来源，从不返回 Qwen API Key 或 Research Engine Token 的明文。

也可以通过环境变量配置：

```bash
export DASHSCOPE_API_KEY=...
export QWEN_MODEL=qwen3.7-plus
export RESEARCH_ENGINE_URL=http://127.0.0.1:9000
export RESEARCH_ENGINE_TOKEN=...
export HYPOWEAVER_API_TOKEN=...
export HYPOWEAVER_BLIND_API_TOKEN=...
export HYPOWEAVER_SEAL_SECRET=...
```

环境变量的优先级始终高于 `runtime-config.json`。因此，如果页面显示某个字段的来源为 `environment`，页面保存的新值不会覆盖当前进程中的环境变量。`HYPOWEAVER_RUNTIME_CONFIG_PATH` 可以修改配置文件位置，默认值为 `backend/var/runtime-config.json`。

配置状态读取接口只返回脱敏信息；配置写入与连接测试受本机/API Token 保护：

```text
GET  /api/v1/runtime-config
PUT  /api/v1/runtime-config
POST /api/v1/runtime-config/tests
```

`PUT` 可更新 `qwen_api_key`、`qwen_model`、`qwen_base_url`、`research_engine_url` 和 `research_engine_token`。密钥留空表示“不修改”；清除已保存密钥需显式提交 `clear_qwen_api_key=true` 或 `clear_research_engine_token=true`。

连接测试请求示例：

```json
{"target": "qwen"}
```

或：

```json
{"target": "research_engine"}
```

Qwen 测试会发起一次 `max_tokens=1` 的最小模型调用；Research Engine 测试约定执行器提供 `GET /v1/health`。

未设置 API Token 时，两个服务的写接口仅允许 loopback 调用。生产或局域网部署必须设置 `HYPOWEAVER_API_TOKEN`，然后在 `#settings` 页的“工作流访问令牌”中输入同一值。该令牌只保存在当前标签页的 `sessionStorage`，关闭标签页即清除；它不会通过 `VITE_*` 变量写入前端构建产物。封存默认使用本机 `backend/var/seal.key`，多实例部署必须通过 `HYPOWEAVER_SEAL_SECRET` 注入同一密钥。

外部执行器接收冻结的 `FormalResearchContract`，并必须返回符合 `ResearchRun` Schema 的 JSON。主 Web 进程不运行任意模型生成代码，计量执行被隔离在独立服务中。

本仓库同时提供一个独立、受限的本地执行器。它不执行模型生成的任意代码，只按冻结合同运行已经实现的估计器；当前支持 `panel_association`、`mechanism_boundary` 的双向固定效应基准模型，以及具有固定权重资产、双向固定效应和直接/间接/总效应分解的空间杜宾基准模型。其他方法会明确返回未支持：

```bash
source .venv/bin/activate
export PYTHONPATH=backend/src
python -m uvicorn hypoweaver.research_api:app --port 9000
```

执行器读取私有 Dataset Registry，通过 Dataset ID 解析已上传 CSV，并在估计前复核文件 SHA256。企业面板合同会按 H2 冻结参数逐项运行当前已支持的诊断、稳健性、证伪、机制和异质性步骤；不支持或预算内未完成的步骤会明确标记，不会静默替换模型。空间执行器当前只支持冻结的空间杜宾基准模型。`scientific_status` 由实际完成情况和识别边界决定，不能由代码运行成功自动升级。

## HTTP API

```text
GET  /api/v1/health
GET  /api/v1/definitions/app-a
GET  /api/v1/runtime-config
PUT  /api/v1/runtime-config
POST /api/v1/runtime-config/tests
POST /api/v1/case-imports/local
POST /api/v1/case-imports/upload?filename=main_data.csv
POST /api/v1/baselines/agent-laboratory/runs
GET  /api/v1/baselines/agent-laboratory/runs/{run_id}
POST /api/v1/runs
GET  /api/v1/runs
GET  /api/v1/runs/{run_id}
POST /api/v1/runs/{run_id}/advance
POST /api/v1/runs/{run_id}/gates/{H1|H2|H3|H4}
POST /api/v1/runs/{run_id}/revisions
POST /api/v1/runs/{run_id}/writing/retry
GET  /api/v1/runs/{run_id}/artifacts/{artifact_key}
```

H1/H2 被退回后，Run 会进入 `blocked`，必须通过 `revisions` 接口提交新版 `CaseSubmission` 或递增版本的 `AnalysisPlan`；系统不会自动越过退回意见。

## 安全与盲测边界

App A 不读取原论文 PDF、已发表结果、回归表或 `02_hidden_reference`。严格输入 Schema 会在持久化之前拒绝额外隐藏字段。

Schema 不能识别用户故意粘贴进普通自由文本字段的隐藏答案；正式 Benchmark 必须由受控案例打包服务创建 App A 输入，并通过目录/对象存储 ACL 保证 App A 身份根本无法读取 `02_hidden_reference`。这属于部署权限边界，不能用 Prompt 或关键词过滤代替。

盲测 App B 已实现为独立进程与独立 SQLite 数据库。它只能在主 Run 封存后读取 `sealed_output + AnalysisPlan + ResearchRun + ClaimLedger + HiddenReference`，先验证 HMAC 封存签名和各 Artifact 哈希，再执行六维评估；LLM 只能建议分项分数，总分由代码按适用权重计算。App B 没有任何回写 App A 的接口。

```bash
source .venv/bin/activate
export PYTHONPATH=backend/src
export HYPOWEAVER_BLIND_DB_PATH=backend/var/blind/hypoweaver_blind.db
python -m uvicorn hypoweaver.blind_api:app --port 8002
```

`05_AppB_BlindEvaluator.yml` 同样只是历史设计参考，不参与运行。

## 目录

```text
backend/src/hypoweaver/
  api.py              FastAPI 接口
  benchmark_runner.py Agent Laboratory 独立基线启动与状态适配
  research_api.py     独立 Python Research Engine 接口
  research_engine.py  受限面板计量执行器
  case_import.py      本地案例安全扫描与数据资产登记
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

Group 2 的职责边界、与《框架设计》的逐项映射、智能体竞争与 Reviewer 审计设计，以及下一轮 Benchmark 计划见 [`docs/GROUP2_WORKFLOW_ARCHITECTURE.md`](docs/GROUP2_WORKFLOW_ARCHITECTURE.md)。
