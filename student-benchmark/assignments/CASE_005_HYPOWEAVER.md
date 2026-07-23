# Case 005：HypoWeaver 执行

目标：只运行农业绿色金融 Case 中 HypoWeaver 的 4 个 Validation 单元：

```text
1 个系统 × 2 个输入视图 × 2 个榜单 × 1 个种子 = 4 个单元
```

开始前确认：

- `release-package.json` 只为 005 设置 `execution_enabled=true`，且
  `enabled_assignments=["hypoweaver"]`；
- 离线预检显示 `external_execution_ready=true`；
- `plan` 只包含 `hypoweaver` 的 4 个单元；
- 不读取五基线同学的中间结果。

所有命令都使用 `--case 005 --assignment hypoweaver`。任何失败都保留原目录和日志，不得删除后
重跑。完成后先封存并私下回传结果，不与五基线同学交换输出；由项目负责人在两组都提交后合并。
