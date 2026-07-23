# Case 007 独立审计负责人

目标：不重复调用模型，独立确认 Case 007 的 24 单元终态和空间资产完整性。

在 Case 005 通用检查之外，逐项核对：

- 权重矩阵形状、行列标识与省份映射；
- 空间资产 SHA256 是否进入 cell manifest；
- common execution result/receipt 是否绑定相同合同和资产；
- 直接、间接、总效应的交付字段没有被静默替换；
- 失败记录没有被删除或选择性重跑。

输出 `complete`、`incomplete` 或 `needs_coordinator_adjudication`，不自行评分或解锁重跑。
