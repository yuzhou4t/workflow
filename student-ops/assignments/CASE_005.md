# Case 005 任务书：农业绿色金融 Validation

## 你负责的完整问题

你不是只负责“按按钮”。你是 Case 005 的完整执行负责人：让同一台机器上的六套系统在完全
相同的案例、模型、预算和冻结协议下完成对照，识别执行异常，并交回可审计结果。

```text
6 系统 × 2 视图 × 2 榜单 × 1 种子 = 24 单元
```

六个系统是 HypoWeaver、Agent Laboratory、Data-to-Paper、Direct Qwen、
Qwen Code Agent Writer 和 DeepScientist。两个视图和两个榜单必须分开，不能混成一个结果。

## 第一阶段：现在就能做

把本任务书、根目录 `README.md` 和
`docs/AI_ENVIRONMENT_CONTRACT_ZH.md`、`docs/ON_DEMAND_FETCH_ZH.md` 一起交给你的 AI，
让它：

1. 在 Windows 的 WSL2 中检查并准备依赖；
2. 在冻结工作区之外实现统一隔离；
3. 运行离线检查；
4. 生成 `environment-report.json` 给负责人。

这一阶段不需要案例数据和 API Key，也不能调用外部模型。

## 第二阶段：拿到正式 release 材料后

只有以下条件全部满足才能运行：

- 新 release 不早于任务书要求的最低版本，且负责人明确说“可以运行”；
- 公开仓库按清单中的 URL 和完整 commit 获取，私发目录哈希正确；
- 数据控制者确认 Case 005 双视图可发送给冻结供应商；
- 包内 release lock、案例哈希、上游提交和本机 readiness 检查全部通过；
- authorization receipt 包含 `case_005_agri_green_finance`；
- `release-package.json` 中本 Case 的 `execution_enabled=true`；
- `preflight` 同时返回 `preflight_passed=true` 和
  `external_execution_ready=true`。

依次执行 `status`、`configure-api`、`preflight`、`run`，命令见根目录 README。

## 你的判断责任

- 判断失败来自环境、基础设施、系统工作流还是科学合同，不能把所有错误都写成“模型失败”；
- 发现疑似代码问题时保留原产物，写出复现条件和建议修复，不直接改冻结源码继续跑；
- 检查 24 个 cell 是否无缺失、无重复、身份字段一致；
- 汇总每个系统在两个视图和两个榜单上的执行状态、失败码、耗时和调用量；
- 不读取隐藏参考，不自行公布分数或排名。

## 回传

通过负责人指定的私有渠道回传：

- 完整 Case 结果目录；
- `environment-report.json`；
- preflight report；
- `case-result-summary.json`；
- 一段不超过 500 字的异常判断和后续建议。

结构化摘要格式见 `docs/STUDENT_RUNBOOK_ZH.md`。
