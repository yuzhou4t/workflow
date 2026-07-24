# SixBench 四人任务交接

本仓库把后续工作拆成四份独立任务。三位同学各自完整负责一个 Case 的六系统对照，另一位
同学负责 HypoWeaver 前端 UI。这样每位同学都要理解、部署、判断和汇报自己的任务，而不是
只替负责人机械地跑一个系统。

本仓库只放公开的任务说明和安全工具，不包含 API Key、案例数据、隐藏参考、授权回执或
运行结果。

## 四人分工

| 同学 | 任务 | 完整责任 | 现在能做什么 |
|---|---|---|---|
| A | [Case 005](assignments/CASE_005.md) | 六系统 × 双视图 × 双榜单，共 24 单元 | 先让 AI 配环境；正式 release 解锁后运行 |
| B | [Case 007](assignments/CASE_007.md) | 六系统 × 双视图 × 双榜单，共 24 单元 | 先让 AI 配环境；正式 release 解锁后运行 |
| C | [Case 009](assignments/CASE_009.md) | 六系统 × 双视图 × 双榜单，共 24 单元 | 修复已完成；先配环境，等待正式 release |
| D | [前端 UI](assignments/FRONTEND_UI.md) | 把设计稿落实为可用界面并提交 PR | 拿到指定的干净基线提交后即可开始 |

让一个人完整负责同一个 Case，可以避免同一 Case 的六个系统被不同电脑、不同网络和不同
环境拆散，从而减少机器差异对系统对照的干扰。

## 我们给同学什么

现在先发：

1. 本仓库地址；
2. 对应的任务书链接；
3. [同学运行手册](docs/STUDENT_RUNBOOK_ZH.md)；
4. [AI 环境与隔离合同](docs/AI_ENVIRONMENT_CONTRACT_ZH.md)；
5. [按需获取公开代码与依赖](docs/ON_DEMAND_FETCH_ZH.md)；
6. 可直接复制的[四条分发消息](docs/DISTRIBUTION_MESSAGES_ZH.md)。

完成新版本冻结后再单独私发正式获取清单和私有材料：

- Case 同学：公开仓库的精确 URL/commit、公网无法取得的冻结仓库、正式
  `release-package.json`、本人负责的可见案例包和哈希绑定授权回执；
- UI 同学：`yuzhou4t/workflow` 的指定干净 base commit/branch，以及确认采用的设计参考。

案例数据、授权回执和运行结果都不能放进这个公开仓库。

## 同学让 AI 做什么

Case 005/007/009 的同学把自己的任务书和本仓库交给 AI，让 AI：

1. 在 Windows 上用 WSL2 准备环境；
2. 按获取清单拉取公开代码，缺失的私有目录由负责人补发；
3. 按统一合同建立隔离，不改冻结源码；
4. 生成 `environment-report.json`，先让同学回传负责人验收；
5. 正式工作区通过哈希核对后运行 `status → configure-api → preflight → run`；
6. 保存完整产物，并生成结构化 `case-result-summary.json`。

人只需要在安装 WSL/Docker、电脑重启、输入 API Key、确认付费运行及回传结果时介入。

UI 同学则让 AI 先核对指定 base commit，再实现界面、补测试、截图并提交独立 PR。

## Case 命令入口

以下命令都在 WSL2 的本仓库目录中执行。先查看任务状态，不会调用模型：

```bash
python3 scripts/case_operator.py status --case 005
```

拿到正式 release 并完成工作区组装后，由自己的 AI 替换 Case 编号和绝对路径：

```bash
python3 scripts/case_operator.py configure-api \
  --case 005 \
  --workspace /absolute/path/to/frozen-workspace \
  --release /absolute/path/to/release-package.json

python3 scripts/case_operator.py preflight \
  --case 005 \
  --workspace /absolute/path/to/frozen-workspace \
  --release /absolute/path/to/release-package.json \
  --report /absolute/path/to/private-preflight.json

python3 scripts/case_operator.py run \
  --case 005 \
  --workspace /absolute/path/to/frozen-workspace \
  --release /absolute/path/to/release-package.json
```

只有预检同时返回 `preflight_passed: true` 和 `external_execution_ready: true` 才能运行。

## 当前状态

- Case 009 的 Harness、适配器和评估边界修复已经完成，并已形成干净提交和本地回归记录。
- “修复完成”不等于“新实验完成”：新的 protocol、suite、release inventory、release
  lock、获取清单与授权回执仍待生成，修复后 24 单元尚未运行。
- 旧 RC10 及更早版本不能发给同学继续跑。
- 三个 Case 都要使用同一版新的不可变 release；现在只允许环境准备，不能配置 API Key
  或调用模型。
- 公开代码可以由同学的 AI 按清单获取；当前四个核心修复仓库尚不能全部从公网精确还原，
  缺失提交必须先发布或由负责人私发。
- UI 仓库当前需要先整理出一个干净的基线提交；不能把负责人电脑上的未提交工作树直接发给
  同学。

负责人只负责统一任务边界、正式冻结内容、数据授权和最终评估，不再替四位同学逐台搭建
环境。详细边界见 [负责人指南](docs/COORDINATOR_GUIDE_ZH.md)。
