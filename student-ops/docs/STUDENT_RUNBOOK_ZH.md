# Case 005/007/009 同学运行手册

## 1. 先让 AI 读完任务

把以下内容一起交给你的 AI：

- 根目录 `README.md`；
- 你自己的 `assignments/CASE_XXX.md`；
- `docs/AI_ENVIRONMENT_CONTRACT_ZH.md`；
- `docs/ON_DEMAND_FETCH_ZH.md`；
- 本手册。

先只做公开代码的环境准备并回传 `environment-report.json`。在负责人发出正式获取清单、
补齐私有材料并明确说“可以运行”以前，不配置 API Key、不接收案例数据、不调用外部模型。

每个 Case 都是完整矩阵：

```text
6 个系统 × 2 个输入视图 × 2 个榜单 × 1 个种子 = 24 个单元
```

## 2. Windows 统一入口

所有命令都在 WSL2 Ubuntu 终端内执行。不要在 PowerShell 中调用 Windows Python 来跑
benchmark。项目路径建议放在 WSL 自己的 Linux 文件系统中，不要放在 OneDrive 或通过
`/mnt/c` 运行大量容器文件。

```bash
git clone https://github.com/yuzhou4t/workflow.git
cd workflow/student-ops
python3 scripts/case_operator.py status --case <005或007或009>
```

`status` 只读取公开任务策略，不会调用模型。

## 3. 正式 release 与工作区组装

正式工作区允许混合组装：公开仓库由 AI 按负责人清单中的 URL 和完整 commit 获取；公网
无法取得的修复仓库、案例视图、授权材料由负责人私发。不能自行搜索替代仓库或改用
`main/latest`。完整规则见 `docs/ON_DEMAND_FETCH_ZH.md`。

保持清单中的相对路径不变。典型结构如下，实际以正式 `release-package.json` 和获取清单
为准：

```text
frozen-workspace/
├── Agent Laboratory/
├── benchmark-baselines/
│   ├── DeepScientist/
│   ├── data-to-paper/
│   ├── hypoweaver-runtime/
│   └── six-system-comparison/
├── benchmark-cases-v3-pilot/
├── benchmark-results-v3-pilot/
└── workflow/
    └── student-ops/
```

组装完成后不要修改或移动冻结工作区文件，也不要再执行 `git pull` 或更新依赖。你的 AI
应在外部目录搭建容器和临时层，并验证：

- 每个公开仓库的 origin URL、完整 commit 和干净工作树；
- 每个私发压缩包的 SHA256；
- harness 与上游仓库 commit；
- suite、protocol、案例视图和补充资产哈希；
- 包内 release lock 与本机 capability/readiness evidence；
- 授权回执绑定的案例、供应商、endpoint、模型和协议。

RC10 及更早版本会被操作器拒绝。

## 4. API 配置

只使用冻结 suite 指定的供应商、endpoint 和模型。API Key 必须由电脑主人通过隐藏输入
填写：

```bash
python3 scripts/case_operator.py configure-api \
  --case <005或007或009> \
  --workspace /absolute/path/to/frozen-workspace \
  --release /absolute/path/to/release-package.json
```

脚本把配置写到 suite 指定的位置并限制权限，不会回显 Key。禁止把 Key 写进 shell 命令、
`.env`、聊天、Issue、日志或截图。

## 5. 离线预检

```bash
python3 scripts/case_operator.py preflight \
  --case <005或007或009> \
  --workspace /absolute/path/to/frozen-workspace \
  --release /absolute/path/to/release-package.json \
  --report /absolute/path/to/private-preflight.json
```

预检不会调用外部模型，主要核验：

- release 不早于当前任务允许的最低版本；
- Case 角色与 24 单元矩阵；
- Harness 精确 commit 和干净工作树；
- suite、protocol、Python、案例路径和本地合同测试；
- API 配置权限，但不输出 Key；
- 包内 release lock、本机 readiness evidence 和哈希绑定授权回执；
- 12 个 native 与 12 个 common 单元。

只有同时出现以下结果才可以继续：

```text
preflight_passed: true
external_execution_ready: true
```

否则把 report 和错误日志私下发给负责人，不要绕过检查。

## 6. 执行与停止规则

电脑主人确认开始付费运行后执行：

```bash
python3 scripts/case_operator.py run \
  --case <005或007或009> \
  --workspace /absolute/path/to/frozen-workspace \
  --release /absolute/path/to/release-package.json
```

运行器按冻结计划执行 native discovery、native aligned、common discovery 和 common
aligned。普通工作流失败会密封失败包并继续；完整性错误、release 漂移、缺失终态产物、
未裁决基础设施异常或不完整旧目录会停止整个批次。

禁止：

- 修改提示词、预算、系统代码、模型或案例后继续同一批次；
- 删除失败目录后重跑；
- 看到结果后换种子、换方法或只补表现不好的系统；
- 混合 discovery/aligned 或 native/common；
- 复制 Case 009 的任何旧结果到新 release；
- 把运行结果提交到 GitHub。

## 7. 结构化结果摘要

AI 在不读取隐藏参考的前提下生成 `case-result-summary.json`：

```json
{
  "schema_version": "sixbench-case-result-summary-v1",
  "case_number": "005",
  "release_id": "<exact release id>",
  "machine_report_sha256": "<sha256>",
  "preflight": {
    "preflight_passed": true,
    "external_execution_ready": true
  },
  "matrix": {
    "expected_cells": 24,
    "observed_cells": 24,
    "duplicate_cell_ids": [],
    "missing_cell_ids": []
  },
  "systems": [
    {
      "system_id": "hypoweaver",
      "cells_expected": 4,
      "cells_completed": 4,
      "cells_failed": 0,
      "failure_reason_codes": [],
      "elapsed_seconds": 0,
      "provider_attempts": 0
    }
  ],
  "integrity_incidents": [],
  "infrastructure_incidents": [],
  "scientific_contract_incidents": [],
  "operator_observations": [],
  "recommended_next_action": "ready_for_central_evaluation"
}
```

`systems` 必须包含六个系统。该摘要只报告执行和完整性事实，不计算隐藏评分、不宣布排名。

## 8. 私下回传

至少回传：

- `environment-report.json` 及其证据日志；
- private preflight report；
- `student-orchestration/progress.jsonl`；
- 每个单元的日志、`cell_manifest.json`、原生输出或 common result/receipt；
- `normalized_result.json`、`evidence_manifest.json`；
- `case-result-summary.json`；
- 你的异常判断和建议。

负责人集中处理基础设施裁决、独立复算、V 硬门、Q 盲评和最终分榜。不要自行索要隐藏参考。
