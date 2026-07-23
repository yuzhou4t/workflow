# Case 005/007 四人测试交接

本目录是公开的操作入口，目标是让四位同学从
`https://github.com/yuzhou4t/workflow` 拉取统一说明和工具，然后在获准的本机冻结工作区中
完成 Case 005 或 Case 007 的六系统测试。

公开仓库不包含案例数据、API Key、隐藏参考、机器专属 release lock、正式授权回执或结果。
这些内容不能通过把仓库设为 public 自动获得授权。

## 最终发给同学的压缩包

负责人准备的正式私有压缩包会在根目录直接包含：

```text
Case005-HypoWeaver/
├── AGENTS.md
├── ASSIGNMENT.json
├── README_FOR_STUDENT.md
├── START_HERE_FOR_AI.md
├── SETUP.command
├── START.command
├── CHECK_WINDOWS.cmd
├── SETUP_WINDOWS.cmd
├── START_WINDOWS.cmd
├── release-package.json
├── tools/student_handoff.py
├── workflow/student-benchmark/...
├── 冻结的系统、Harness 和 Case 文件
└── RETURN/
```

同学不需要自己拼路径或输入 Case 参数。解压后可以把整个目录交给 AI，让 AI 先读
`START_HERE_FOR_AI.md`；也可以直接双击 `START.command`。AI 会通过
`ASSIGNMENT.json` 识别本人唯一分工，并先报告当前机器上的真实绝对路径、负责的系统和应有单元数。

完成后，工具从真实 `cell_manifest.json`、`normalized_result.json` 和
`evidence_manifest.json` 生成：

- `RETURN/RESULT_SUMMARY.md`：逐单元结果表；
- `RETURN/RESULT_SUMMARY.json`：符合固定 Schema 的结构化结果；
- `RETURN/RETURN_MANIFEST.json`：回传文件和 SHA-256；
- `RETURN/RETURN_POINTER.json`：AI 可以原样返回的结构化回执；
- `RETURN/*-return.zip`：通过私有渠道回传的结果包。

报告会把“24/4/20 个产物是否收齐”和“科学工作流运行成功还是失败”分开。失败是允许保留的测试
结果，缺失、跨分组单元或身份不匹配才属于交付完整性问题。生成逻辑和模板在
[`package-template`](package-template)，负责人把它装入冻结工作区的工具是
[`scripts/install_ai_handoff.py`](scripts/install_ai_handoff.py)。

## 四人分工：每个案例拆成 1 + 5 个系统

| 小组 | 任务 | 工作 |
|---|---|---|
| Case 005 | [HypoWeaver 执行](assignments/CASE_005_HYPOWEAVER.md) | 只运行 HypoWeaver 的 4 个单元 |
| Case 005 | [五基线执行](assignments/CASE_005_BASELINES.md) | 只运行另外五个系统的 20 个单元 |
| Case 007 | [HypoWeaver 执行](assignments/CASE_007_HYPOWEAVER.md) | 只运行 HypoWeaver 的 4 个空间案例单元 |
| Case 007 | [五基线执行](assignments/CASE_007_BASELINES.md) | 只运行另外五个系统的 20 个空间案例单元 |

每个案例的工作量为：

```text
HypoWeaver 同学：1 个系统 × 2 个输入视图 × 2 个榜单 × 1 个种子 = 4 个单元
五基线同学：5 个系统 × 2 个输入视图 × 2 个榜单 × 1 个种子 = 20 个单元
合并后：6 个系统，共 24 个单元
```

两位同学使用同一个冻结 Case、协议、模型、预算和种子，但各自只运行分配到的系统。双方完成并
封存结果前，不交换中间输出，不依据对方结果修改代码、提示词或重跑策略。五基线任务包含 20 个
单元，明显重于 HypoWeaver 的 4 个单元，排期和 API 预算应据此安排。两台执行环境应尽量使用
相同的系统架构和资源规格；若不同，运行耗时只能单独报告，不能直接解释为系统能力差异。

## 从公开仓库开始

```bash
git clone https://github.com/yuzhou4t/workflow.git
cd workflow/student-benchmark
python3 -m unittest discover -s tests -v
python3 scripts/case_operator.py status --case 005 --assignment hypoweaver
python3 scripts/case_operator.py status --case 005 --assignment baselines
```

到这里都不需要 API Key 或案例数据。

随后，负责人通过私有渠道向四位执行同学分别提供：

1. 已经包含本人 `ASSIGNMENT.json` 和 AI 入口的冻结工作区压缩包；
2. 只启用本人 Case 和分组的 `release-package.json`；
3. 本地自动生成并校验的 release lock；
4. 覆盖 005/007、固定内容和固定模型服务的一次性项目授权回执。

授权回执确认的是“哪些 Case 内容可以发给哪个模型服务”，不是逐个批准某位同学工作。只要
Case 字节、协议、provider、endpoint 和 model 不变，同一项目授权可以供四个已分配任务使用。
当前 RC9 的 release lock 会记录本机路径和 Python 环境，因此每个工作区仍需自动生成自己的
技术校验文件，但不需要重新进行人工授权。

Windows 10/11 x64 使用单独的 WSL2 + Docker 入口：先双击 `CHECK_WINDOWS.cmd`。它不读取真实
Case、不使用 API Key，也不调用外部模型，只验收容器的文件与网络隔离。通过后再双击
`START_WINDOWS.cmd` 进入同一菜单。Windows 原生 Python 不属于获准执行路径。
当前仍是负责人验收版，详细顺序和正式分发前门槛见
[`docs/WINDOWS_PILOT_ZH.md`](docs/WINDOWS_PILOT_ZH.md)。

四项材料的含义和生成顺序见
[`docs/MATERIALS_EXPLAINED_ZH.md`](docs/MATERIALS_EXPLAINED_ZH.md)，负责人操作见
[`docs/COORDINATOR_GUIDE_ZH.md`](docs/COORDINATOR_GUIDE_ZH.md)。

## 执行命令

正式压缩包中不需要手工填写以下路径和参数，直接运行：

```bash
python3 tools/student_handoff.py explain
python3 tools/student_handoff.py setup --yes
python3 tools/student_handoff.py configure-api
python3 tools/student_handoff.py preflight
python3 tools/student_handoff.py run --yes
python3 tools/student_handoff.py bundle
```

也可以双击 `START.command` 使用菜单。以下底层命令只用于负责人排查；以 Case 005 的
HypoWeaver 分组为例，五基线同学把 `hypoweaver` 替换为 `baselines`，Case 007 把编号替换为
`007`。

安全写入 API 配置，Key 通过隐藏输入读取：

```bash
python3 scripts/case_operator.py configure-api \
  --case 005 \
  --assignment hypoweaver \
  --workspace /absolute/path/to/frozen-workspace \
  --release /absolute/path/to/release-package.json
```

先运行不会调用外部模型的预检：

```bash
python3 scripts/case_operator.py preflight \
  --case 005 \
  --assignment hypoweaver \
  --workspace /absolute/path/to/frozen-workspace \
  --release /absolute/path/to/release-package.json \
  --report /absolute/path/to/private-preflight-report.json
```

只有报告同时出现以下状态才允许继续：

```text
preflight_passed: true
external_execution_ready: true
```

正式运行：

```bash
python3 scripts/case_operator.py run \
  --case 005 \
  --assignment hypoweaver \
  --workspace /absolute/path/to/frozen-workspace \
  --release /absolute/path/to/release-package.json
```

详细停止条件和回传内容见
[`docs/STUDENT_RUNBOOK_ZH.md`](docs/STUDENT_RUNBOOK_ZH.md)。

## 当前公开状态

仓库中的 [`config/release-package.example.json`](config/release-package.example.json)
故意将 005/007 的 `execution_enabled` 都设为 `false`，并把 `enabled_assignments` 设为空。
这是安全模板，不是正式运行包。

只有数据使用范围确认、版本冻结和本地预检全部通过后，负责人才能在私有正式副本中把对应 Case
改为 `true`，并只启用 `hypoweaver` 或 `baselines` 中本人负责的一项。不得在 GitHub 提交这个
正式副本。
