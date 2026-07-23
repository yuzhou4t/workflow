# Case 007：五个对照系统执行

目标：只运行数字绿色金融韧性 Case 中五个对照系统的 20 个 Validation 单元：

- Agent Laboratory
- data-to-paper
- Direct Qwen
- Qwen Code Agent + Writer
- DeepScientist

`release-package.json` 必须只为 007 启用 `enabled_assignments=["baselines"]`，所有命令都使用
`--case 007 --assignment baselines`。运行前确认 `plan` 只包含上述五系统的 20 个单元。

权重矩阵、行列映射、空间资产哈希或 common result/receipt 任一不一致时立即停止。保留所有失败
记录，不删除、不选择性重跑。完成后先封存并私下回传，不查看 HypoWeaver 同学的中间输出。
