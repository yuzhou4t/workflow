# Case 007：HypoWeaver 执行

目标：只运行数字绿色金融韧性 Case 中 HypoWeaver 的 4 个 Validation 单元。

`release-package.json` 必须只为 007 启用 `enabled_assignments=["hypoweaver"]`，所有命令都使用
`--case 007 --assignment hypoweaver`。运行前确认 `plan` 只包含 HypoWeaver 的 4 个单元。

同时确认主数据、空间权重、行列映射和补充资产进入最终哈希绑定。空间矩阵、映射、效应分解合同
或 common execution receipt 任一不一致时立即停止，不把它当作科学失败。完成后先封存并私下
回传，不读取五基线同学的中间结果。
