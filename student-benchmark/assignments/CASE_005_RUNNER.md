# Case 005 执行负责人

目标：在唯一获准机器上完成农业绿色金融 Case 的 24 单元 Validation。

开始前必须确认：

- 数据控制方确认、release lock 和 hash-bound receipt 均覆盖 Case 005；
- `release-package.json` 只为 005 设置 `execution_enabled=true`；
- 离线预检显示 `external_execution_ready=true`；
- 私有结果目录为空，不存在旧的部分批次。

执行时按根 README 的 `configure-api → preflight → run` 顺序操作。任何失败都保留原目录和日志，
不得删除后重跑。完成后通过私有渠道把整个 Case 结果目录交给审计负责人和项目负责人。
