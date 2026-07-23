# Case 005：五个对照系统执行

目标：只运行农业绿色金融 Case 中以下五个对照系统的 20 个 Validation 单元：

- Agent Laboratory
- data-to-paper
- Direct Qwen
- Qwen Code Agent + Writer
- DeepScientist

```text
5 个系统 × 2 个输入视图 × 2 个榜单 × 1 个种子 = 20 个单元
```

`release-package.json` 必须只为 005 启用 `enabled_assignments=["baselines"]`，所有命令都使用
`--case 005 --assignment baselines`。运行前确认 `plan` 只包含上述五个系统的 20 个单元。

任何失败都保留终态证据，不删除、不选择性重跑。完成后先封存并私下回传结果，不查看
HypoWeaver 同学的中间输出；由项目负责人在两组都提交后合并对照。
