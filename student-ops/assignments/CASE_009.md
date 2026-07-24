# Case 009 任务书：绿色金融改革创新试验区与空气质量

## 当前状态：修复已完成，先认领和配环境，暂时不要运行

Case 009 的协议边界、discovery 控制变量合同和适配器修复已经完成，本地回归也已通过。
但旧运行仍不能继续使用：负责人还需要把修复后的多个仓库提交统一冻结成一个新的不可变
release，再从头运行完整矩阵。RC10 及更早版本都不能用于本任务。

当前准确状态是：

```text
代码修复完成
→ 新 release 尚未生成
→ 修复后 24 单元尚未运行
```

你的第一阶段工作可以现在开始，但在负责人提供正式获取清单、补齐私有材料并明确解锁前，
不得上传案例内容、配置 API Key 或调用模型。

## 你负责的完整问题

你是 Case 009 的完整重跑负责人。在修复后的同一环境中运行：

```text
6 系统 × 2 视图 × 2 榜单 × 1 种子 = 24 单元
```

六个系统是 HypoWeaver、Agent Laboratory、Data-to-Paper、Direct Qwen、
Qwen Code Agent Writer 和 DeepScientist。旧结果、旧失败目录和旧 partial run 都不得
复制进新矩阵。

## 第一阶段：现在就能做

把本任务书、根目录 `README.md` 和
`docs/AI_ENVIRONMENT_CONTRACT_ZH.md`、`docs/ON_DEMAND_FETCH_ZH.md` 一起交给你的 AI，
让它：

1. 在 Windows 的 WSL2 中准备依赖和统一隔离；
2. 只用公开代码完成本机离线能力检查；
3. 生成 `environment-report.json`；
4. 明确写出自己在等待“post-repair frozen release”，然后停止。

## 第二阶段：新 release 材料到达后

只有以下条件全部满足才能运行：

- release 至少为 RC11，且确实包含本轮修复后的干净 Harness 与三个适配器/runtime 提交；
- 公开仓库按清单中的 URL 和完整 commit 获取，私发目录哈希正确；
- Case 009 双视图、协议、控制变量合同和适配器测试全部通过；
- 包内 release lock、全部输入哈希和本机 readiness 检查全部通过；
- 新的 authorization receipt 明确包含 `case_009_gfri_air_quality`；
- `release-package.json` 中本 Case 的 `execution_enabled=true`；
- `preflight` 同时返回 `preflight_passed=true` 和
  `external_execution_ready=true`。

如果负责人最终采用的正式编号晚于 RC11，以发放包中的精确 release id 为准。禁止为了满足
最低编号而自行改 JSON。

“代码修复完成”不能作为运行解锁口令。只有正式包通过 preflight，且负责人另行明确发送
“可以运行”，才可以开始付费模型调用。

## 你的判断责任

- 确认这是全新矩阵，不是从旧目录恢复；
- 重点核对 treatment start、pre-trend、discovery 控制选择、aligned 合同和 common
  execution receipt；
- 区分环境/基础设施失败、系统工作流失败和科学合同失败；
- 发现修复不完整时立即停止，保留原现场并提交最小复现；
- 检查 24 个 cell 无缺失、无重复，并汇总执行状态、失败码、耗时和调用量；
- 不读取隐藏参考，不自行公布分数或排名。

## 回传

通过负责人指定的私有渠道回传：

- 完整 Case 结果目录；
- `environment-report.json`；
- preflight report；
- `case-result-summary.json`；
- 一段不超过 500 字的异常判断和后续建议。

结构化摘要格式见 `docs/STUDENT_RUNBOOK_ZH.md`。
