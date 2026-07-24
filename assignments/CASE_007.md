# Case 007 任务书：数字绿色金融韧性 Validation

## 你负责的完整问题

你是 Case 007 的完整执行负责人。你要在同一台机器、同一冻结协议下比较六套系统，并重点
判断空间权重、SDM 规格、效应分解、诊断和主张校准是否被正确执行。

```text
6 系统 × 2 视图 × 2 榜单 × 1 种子 = 24 单元
```

六个系统是 HypoWeaver、Agent Laboratory、Data-to-Paper、Direct Qwen、
Qwen Code Agent Writer 和 DeepScientist。两个视图和两个榜单必须分开。

## 第一阶段：现在就能做

把本任务书、根目录 `README.md` 和
`docs/AI_ENVIRONMENT_CONTRACT_ZH.md`、`docs/ON_DEMAND_FETCH_ZH.md` 一起交给你的 AI，
让它：

1. 在 Windows 的 WSL2 中准备依赖；
2. 在冻结工作区之外实现统一隔离；
3. 做本机离线能力检查；
4. 生成 `environment-report.json` 给负责人。

这一阶段不需要案例数据和 API Key，也不能调用外部模型。

## 第二阶段：拿到正式 release 材料后

只有以下条件全部满足才能运行：

- 新 release 不早于任务书要求的最低版本，且负责人明确说“可以运行”；
- 公开仓库按清单中的 URL 和完整 commit 获取，私发目录哈希正确；
- 数据控制者确认主数据、空间权重和省份映射可发送给冻结供应商；
- 包内 release lock、全部补充资产哈希和本机 readiness 检查通过；
- authorization receipt 包含
  `case_007_digital_green_finance_resilience`；
- `release-package.json` 中本 Case 的 `execution_enabled=true`；
- `preflight` 同时返回 `preflight_passed=true` 和
  `external_execution_ready=true`。

依次执行 `status`、`configure-api`、`preflight`、`run`，命令见根目录 README。

## 你的判断责任

- 核对空间权重矩阵、行列映射和案例数据的身份关系；
- 判断失败来自环境、基础设施、系统工作流还是科学合同；
- 检查效应分解、诊断和 common execution receipt 是否齐全且哈希一致；
- 发现疑似 bug 时保存原现场，提交复现和修复建议，不修改冻结源码后续跑；
- 检查 24 个 cell 无缺失、无重复，并汇总执行状态、失败码、耗时和调用量；
- 不读取隐藏参考，不自行公布分数或排名。

空间权重矩阵、行列映射、效应分解合同或 common execution receipt 任一不一致时，停止整个
批次并联系负责人，不能把它记成某个系统的科学失败。

## 回传

通过负责人指定的私有渠道回传：

- 完整 Case 结果目录；
- `environment-report.json`；
- preflight report；
- `case-result-summary.json`；
- 一段不超过 500 字的异常判断和后续建议。

结构化摘要格式见 `docs/STUDENT_RUNBOOK_ZH.md`。
